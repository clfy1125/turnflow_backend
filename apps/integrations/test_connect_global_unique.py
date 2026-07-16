"""전서비스 유일 연동(Feature 1) + 재연결 버그 2건(Feature 3) 테스트.

- find_conflicting_connection / mask_email 단위
- connect_callback 글로벌 중복 차단 · is_active 자동 복구
- connect_start reconnect_connection_id 게이트 우회
- audit_ig_duplicates 커맨드 스모크
"""

import json
import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import SubscriptionPlan, UserSubscription
from apps.integrations import oauth_callback_pages
from apps.integrations.models import IGAccountConnection, IGOAuthState
from apps.integrations.services import InstagramOAuthService
from apps.workspace.models import Membership, Workspace

User = get_user_model()


def _user(email_prefix="uniq", **kw):
    return User.objects.create_user(
        email=f"{email_prefix}-{uuid.uuid4().hex[:10]}@example.com",
        password="Pass1234!",
        **kw,
    )


def _ws(user):
    ws = Workspace.objects.create(name="uniq-ws", slug=f"uniq-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws, external_account_id=None, status=IGAccountConnection.Status.ACTIVE, is_active=True):
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=external_account_id or f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=status,
        is_active=is_active,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return conn


def _give_plan(user, plan_name):
    plan = SubscriptionPlan.objects.get(name=plan_name)
    sub, _ = UserSubscription.objects.get_or_create(user=user, defaults={"plan": plan})
    sub.plan = plan
    sub.status = "active"
    sub.current_period_end = timezone.now() + timedelta(days=20)
    sub.save()
    return sub


def _patch_oauth(monkeypatch, *, ig_id, username="dupuser"):
    """production 콜백 경로가 부르는 서비스 4종을 가짜로 대체."""
    monkeypatch.setattr(
        InstagramOAuthService,
        "exchange_code_for_token",
        classmethod(lambda cls, code, redirect: {"access_token": "sl", "user_id": ig_id}),
    )
    monkeypatch.setattr(
        InstagramOAuthService,
        "get_long_lived_token",
        classmethod(lambda cls, tok: {"access_token": "ll_token", "expires_in": 5184000}),
    )
    monkeypatch.setattr(
        InstagramOAuthService,
        "get_account_info",
        classmethod(lambda cls, tok: {"user_id": ig_id, "username": username}),
    )
    monkeypatch.setattr(
        InstagramOAuthService,
        "subscribe_to_webhooks",
        classmethod(lambda cls, ig_user_id, access_token, fields="comments,messages": {"ok": True}),
    )
    # 콜백이 큐잉하는 celery 태스크들은 .delay 를 no-op 으로 (브로커 불필요).
    from apps.insights import tasks as insights_tasks
    from apps.integrations import tasks as ig_tasks

    monkeypatch.setattr(insights_tasks.bootstrap_account, "delay", lambda *a, **k: None)
    monkeypatch.setattr(ig_tasks.sync_ig_profile_picture, "delay", lambda *a, **k: None)
    monkeypatch.setattr(ig_tasks.revive_failed_token_logs, "delay", lambda *a, **k: None)


def _do_callback(ws, code="realcode"):
    state = uuid.uuid4().hex
    IGOAuthState.objects.create(
        state=state, workspace=ws, expires_at=timezone.now() + timedelta(minutes=10)
    )
    client = APIClient()
    return client.get(
        "/api/v1/integrations/instagram/connect/callback/", {"code": code, "state": state}
    )


# ──────────────────────────────────────────────
# 단위: find_conflicting_connection
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestFindConflicting:
    def test_other_ws_active_blocks(self):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws_a = _ws(_user())
        ws_b = _ws(_user())
        _conn(ws_b, external_account_id=ig, status=IGAccountConnection.Status.ACTIVE)
        assert IGAccountConnection.find_conflicting_connection(ig, ws_a) is not None

    @pytest.mark.parametrize(
        "status",
        [
            IGAccountConnection.Status.EXPIRED,
            IGAccountConnection.Status.ERROR,
        ],
    )
    def test_other_ws_expired_error_block(self, status):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws_a = _ws(_user())
        ws_b = _ws(_user())
        _conn(ws_b, external_account_id=ig, status=status)
        assert IGAccountConnection.find_conflicting_connection(ig, ws_a) is not None

    def test_other_ws_soft_deactivated_blocks(self):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws_a = _ws(_user())
        ws_b = _ws(_user())
        _conn(ws_b, external_account_id=ig, is_active=False)  # status ACTIVE, is_active False
        assert IGAccountConnection.find_conflicting_connection(ig, ws_a) is not None

    def test_other_ws_revoked_does_not_block(self):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws_a = _ws(_user())
        ws_b = _ws(_user())
        _conn(ws_b, external_account_id=ig, status=IGAccountConnection.Status.REVOKED)
        assert IGAccountConnection.find_conflicting_connection(ig, ws_a) is None

    def test_same_ws_never_conflicts(self):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws = _ws(_user())
        _conn(ws, external_account_id=ig)
        assert IGAccountConnection.find_conflicting_connection(ig, ws) is None

    def test_same_owner_other_ws_still_blocks(self):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        owner = _user()
        ws_a = _ws(owner)
        ws_b = _ws(owner)  # 같은 owner, 다른 워크스페이스
        _conn(ws_b, external_account_id=ig)
        assert IGAccountConnection.find_conflicting_connection(ig, ws_a) is not None


# ──────────────────────────────────────────────
# 단위: mask_email
# ──────────────────────────────────────────────


class TestMaskEmail:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("sihyeon.kim@clfy.ai.kr", "si***@clfy.ai.kr"),
            ("ab@x.com", "a***@x.com"),  # 로컬 2자 → 1자만
            ("a@x.com", "a***@x.com"),  # 로컬 1자
            ("no-at-sign", "***"),
            ("", "***"),
        ],
    )
    def test_mask(self, raw, expected):
        assert oauth_callback_pages.mask_email(raw) == expected


# ──────────────────────────────────────────────
# 콜백 통합: 글로벌 중복 + is_active 복구
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestCallbackGlobalUnique:
    def test_blocked_when_connected_elsewhere(self, monkeypatch):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        other_owner = _user(email_prefix="owner")
        ws_other = _ws(other_owner)
        _conn(ws_other, external_account_id=ig, status=IGAccountConnection.Status.ACTIVE)

        ws_a = _ws(_user())
        _patch_oauth(monkeypatch, ig_id=ig)
        resp = _do_callback(ws_a)

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "ALREADY_CONNECTED_ELSEWHERE" in body
        # 마스킹된 이메일은 노출, 원본은 절대 노출 금지
        assert oauth_callback_pages.mask_email(other_owner.email) in body
        assert other_owner.email not in body
        # ws_a 에는 행이 생기지 않아야 함
        assert not IGAccountConnection.objects.filter(
            workspace=ws_a, external_account_id=ig
        ).exists()

    def test_revoked_elsewhere_allows_connect(self, monkeypatch):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws_other = _ws(_user())
        _conn(ws_other, external_account_id=ig, status=IGAccountConnection.Status.REVOKED)

        ws_a = _ws(_user())
        _patch_oauth(monkeypatch, ig_id=ig)
        resp = _do_callback(ws_a)

        assert resp.status_code == 200
        assert "INSTAGRAM_CONNECTED" in resp.content.decode()
        conn = IGAccountConnection.objects.get(workspace=ws_a, external_account_id=ig)
        assert conn.status == IGAccountConnection.Status.ACTIVE

    def test_same_ws_revive_reuses_row(self, monkeypatch):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws = _ws(_user())
        existing = _conn(ws, external_account_id=ig, status=IGAccountConnection.Status.EXPIRED)

        _patch_oauth(monkeypatch, ig_id=ig)
        resp = _do_callback(ws)

        assert resp.status_code == 200
        assert "INSTAGRAM_CONNECTED" in resp.content.decode()
        rows = IGAccountConnection.objects.filter(workspace=ws, external_account_id=ig)
        assert rows.count() == 1
        existing.refresh_from_db()
        assert existing.status == IGAccountConnection.Status.ACTIVE


@pytest.mark.django_db
class TestCallbackIsActiveRecovery:
    def test_revoked_reconnect_reactivates(self, monkeypatch):
        """disconnect(REVOKED, is_active=False) → 재연결 시 활성 복구 (버그 B)."""
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        ws = _ws(_user())
        conn = _conn(
            ws,
            external_account_id=ig,
            status=IGAccountConnection.Status.REVOKED,
            is_active=False,
        )
        _patch_oauth(monkeypatch, ig_id=ig)
        resp = _do_callback(ws)

        assert resp.status_code == 200
        conn.refresh_from_db()
        assert conn.status == IGAccountConnection.Status.ACTIVE
        assert conn.is_active is True

    def test_soft_inactive_reconnect_stays_off_when_no_slot(self, monkeypatch):
        """소프트 비활성(status ACTIVE, is_active False) + 슬롯 없음 → 활성 복구 안 함."""
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        owner = _user()  # free, 허용량 1
        ws = _ws(owner)
        # 슬롯을 채우는 다른 활성 계정 1개
        _conn(ws, status=IGAccountConnection.Status.ACTIVE, is_active=True)
        target = _conn(ws, external_account_id=ig, is_active=False)  # status ACTIVE

        _patch_oauth(monkeypatch, ig_id=ig)
        resp = _do_callback(ws)

        assert resp.status_code == 200
        target.refresh_from_db()
        assert target.is_active is False  # 허용량 초과라 억지로 켜지 않음

    def test_soft_inactive_reconnect_reactivates_when_slot_free(self, monkeypatch):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        owner = _user()  # free, 허용량 1
        ws = _ws(owner)
        target = _conn(ws, external_account_id=ig, is_active=False)  # 유일 계정, 비활성

        _patch_oauth(monkeypatch, ig_id=ig)
        resp = _do_callback(ws)

        assert resp.status_code == 200
        target.refresh_from_db()
        assert target.is_active is True


# ──────────────────────────────────────────────
# connect_start reconnect_connection_id
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestConnectStartReconnect:
    def test_reconnect_id_bypasses_limit(self):
        user = _user()
        ws = _ws(user)
        conn = _conn(ws)  # free 한도 1 소진

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(
            f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/",
            {"reconnect_connection_id": str(conn.id)},
            format="json",
        )
        assert resp.status_code == 200
        assert "authorization_url" in resp.data

    def test_without_param_allowed_when_has_live_connection(self):
        """파라미터가 없어도 살아있는 연동이 있으면 start 허용(재연동 UX).

        신규 계정 추가 의도라면 콜백에서 걸러진다
        (test_reconnect_start_then_new_account_rejected_at_callback 참고).
        """
        user = _user()
        ws = _ws(user)
        _conn(ws)  # 한도 소진이지만 살아있는 연동 보유

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/")
        assert resp.status_code == 200
        assert "authorization_url" in resp.data

    def test_reconnect_id_other_workspace_400(self):
        user = _user()
        ws = _ws(user)
        _conn(ws)
        other_conn = _conn(_ws(_user()))  # 남의 워크스페이스 연동

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(
            f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/",
            {"reconnect_connection_id": str(other_conn.id)},
            format="json",
        )
        assert resp.status_code == 400

    def test_reconnect_id_revoked_400(self):
        user = _user()
        ws = _ws(user)
        revoked = _conn(ws, status=IGAccountConnection.Status.REVOKED, is_active=False)

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(
            f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/",
            {"reconnect_connection_id": str(revoked.id)},
            format="json",
        )
        assert resp.status_code == 400

    def test_reconnect_start_then_new_account_rejected_at_callback(self, monkeypatch):
        """재연결로 start 를 풀어도 콜백에서 신규 계정이면 플랜 게이트가 거절."""
        user = _user()
        ws = _ws(user)
        conn = _conn(ws)  # 한도 소진, 이 계정을 재연결하려는 척

        client = APIClient()
        client.force_authenticate(user=user)
        start = client.post(
            f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/",
            {"reconnect_connection_id": str(conn.id)},
            format="json",
        )
        assert start.status_code == 200

        # 그런데 OAuth 에서 전혀 다른 신규 계정을 인증 → 콜백에서 거절되어야
        new_ig = f"ig_{uuid.uuid4().hex[:12]}"
        _patch_oauth(monkeypatch, ig_id=new_ig)
        resp = _do_callback(ws)
        assert resp.status_code == 200
        assert "PLAN_LIMIT_EXCEEDED" in resp.content.decode()
        assert not IGAccountConnection.objects.filter(
            workspace=ws, external_account_id=new_ig
        ).exists()


# ──────────────────────────────────────────────
# audit 커맨드 스모크
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestAuditCommand:
    # NOTE: 테스트 DB 는 공유·비청결(기존 실데이터 중복 존재) — 전역 카운트/메시지 대신
    #       내가 만든 특정 IG 계정이 리포트에 잡히는지/안 잡히는지로 델타 단언한다.
    def test_reports_cross_workspace_duplicate(self):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        _conn(_ws(_user()), external_account_id=ig)
        _conn(_ws(_user()), external_account_id=ig)  # 같은 IG, 다른 ws

        out = StringIO()
        call_command("audit_ig_duplicates", stdout=out)
        assert ig in out.getvalue()  # 내 중복 계정이 리포트됨

    def test_single_connection_not_flagged(self):
        ig = f"ig_{uuid.uuid4().hex[:12]}"
        _conn(_ws(_user()), external_account_id=ig)  # 단일 연동 — 충돌 아님

        out = StringIO()
        call_command("audit_ig_duplicates", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        flagged = {a["external_account_id"] for a in payload["accounts"]}
        assert ig not in flagged
