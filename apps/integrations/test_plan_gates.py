"""플랜 기능 게이팅 테스트 — DM 월한도 / 스팸필터 / IG 계정 수."""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.dm_limits import check_dm_quota, get_dm_monthly_limit
from apps.billing.models import SubscriptionPlan, UserSubscription
from apps.billing.subscription_utils import get_ig_account_allowance
from apps.integrations.models import (
    AutoDMCampaign,
    IGAccountConnection,
    SentDMLog,
    SpamCommentLog,
    SpamFilterConfig,
)
from apps.integrations.tasks import run_spam_filter_check, send_dm_task
from apps.workspace.models import Membership, Workspace

User = get_user_model()


def _user(email_prefix="gate", **kw):
    return User.objects.create_user(
        email=f"{email_prefix}-{uuid.uuid4().hex[:10]}@example.com",
        password="Pass1234!",
        **kw,
    )


def _ws(user):
    ws = Workspace.objects.create(name="gate-ws", slug=f"gate-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws, status=IGAccountConnection.Status.ACTIVE):
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=status,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return conn


def _campaign(conn):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        name=f"c-{uuid.uuid4().hex[:6]}",
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        media_id=f"m_{uuid.uuid4().hex[:10]}",
        message_template="hello",
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _give_plan(user, plan_name):
    plan = SubscriptionPlan.objects.get(name=plan_name)
    sub, _ = UserSubscription.objects.get_or_create(user=user, defaults={"plan": plan})
    sub.plan = plan
    sub.status = "active"
    sub.current_period_end = timezone.now() + timedelta(days=20)
    sub.save()
    return sub


# ──────────────────────────────────────────────
# DM 월 한도
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestDmMonthlyLimit:
    def test_limit_resolution_per_plan(self):
        free_user = _user()
        assert get_dm_monthly_limit(free_user) == 200  # 구독 없음 = free

        basic_user = _user()
        _give_plan(basic_user, "basic")
        assert get_dm_monthly_limit(basic_user) == 200

        pro_user = _user()
        _give_plan(pro_user, "pro")
        assert get_dm_monthly_limit(pro_user) == -1

        staff_user = _user(is_staff=True)
        assert get_dm_monthly_limit(staff_user) == -1  # 관리자 무제한

    def test_quota_boundary_199_vs_200(self, monkeypatch):
        user = _user()
        cache.clear()

        monkeypatch.setattr("apps.billing.dm_limits.count_owner_dms_this_month", lambda owner: 199)
        allowed, used, limit = check_dm_quota(user)
        assert allowed is True and limit == 200

        cache.clear()
        monkeypatch.setattr("apps.billing.dm_limits.count_owner_dms_this_month", lambda owner: 200)
        allowed, used, limit = check_dm_quota(user)
        assert allowed is False and used == 200

    def test_unlimited_skips_count_entirely(self, monkeypatch):
        pro_user = _user()
        _give_plan(pro_user, "pro")

        def boom(owner):
            raise AssertionError("무제한 플랜은 COUNT 를 호출하면 안 됨")

        monkeypatch.setattr("apps.billing.dm_limits.count_owner_dms_this_month", boom)
        allowed, _, limit = check_dm_quota(pro_user)
        assert allowed is True and limit == -1

    def test_count_failure_fails_open(self, monkeypatch):
        user = _user()
        cache.clear()

        def boom(owner):
            raise RuntimeError("db down")

        monkeypatch.setattr("apps.billing.dm_limits.count_owner_dms_this_month", boom)
        allowed, _, _ = check_dm_quota(user)
        assert allowed is True  # 무손실 원칙 — 카운트 실패로 발송을 막지 않는다

    def test_send_dm_task_skips_over_limit_and_is_revivable(self, monkeypatch):
        """한도 도달 → SKIPPED 종결 (REVIVABLE — 업그레이드 후 되살림 가능)."""
        cache.clear()
        user = _user()
        ws = _ws(user)
        conn = _conn(ws)
        campaign = _campaign(conn)
        log = SentDMLog.objects.create(
            campaign=campaign,
            comment_id=f"c{uuid.uuid4().hex[:8]}",
            recipient_user_id="ru1",
            recipient_username="run1",
            message_sent="hi",
            idempotency_key=f"quota-{uuid.uuid4().hex[:8]}",
            status=SentDMLog.Status.QUEUED,
        )
        monkeypatch.setattr("apps.billing.dm_limits.count_owner_dms_this_month", lambda owner: 200)

        result = send_dm_task.apply(args=[str(log.id)]).get()

        assert result["status"] == "skipped"
        assert result["reason"] == "monthly_dm_limit"
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.SKIPPED
        assert log.status in SentDMLog.REVIVABLE_STATUSES

    def test_owner_scope_counts_across_workspaces(self):
        """owner 스코프 집계 — 워크스페이스 분산으로 한도 우회 불가."""
        from apps.billing.dm_limits import count_owner_dms_this_month

        user = _user()
        ws1, ws2 = _ws(user), _ws(user)
        camp1, camp2 = _campaign(_conn(ws1)), _campaign(_conn(ws2))
        for i, camp in enumerate([camp1, camp2]):
            SentDMLog.objects.create(
                campaign=camp,
                comment_id=f"sc{i}-{uuid.uuid4().hex[:6]}",
                recipient_user_id=f"ru{i}",
                recipient_username=f"run{i}",
                message_sent="hi",
                idempotency_key=f"scope-{uuid.uuid4().hex[:8]}",
                status=SentDMLog.Status.DELIVERED,
            )

        assert count_owner_dms_this_month(user) == 2

    def test_count_dedups_per_campaign_recipient(self):
        """v4.2 — 같은 캠페인·같은 수신자에게 여러 DM 이 나가도 1로 카운트."""
        from apps.billing.dm_limits import count_owner_dms_this_month

        user = _user()
        camp = _campaign(_conn(_ws(user)))
        # 한 사람에게 3건 (opening + reward + 재안내 흉내)
        for _ in range(3):
            SentDMLog.objects.create(
                campaign=camp,
                comment_id=f"c-{uuid.uuid4().hex[:8]}",
                recipient_user_id="same_person",
                recipient_username="same",
                message_sent="hi",
                idempotency_key=f"dedup-{uuid.uuid4().hex[:8]}",
                status=SentDMLog.Status.DELIVERED,
            )
        assert count_owner_dms_this_month(user) == 1

    def test_same_person_across_campaigns_counts_twice(self):
        """v4.2 — 같은 사람이 서로 다른 캠페인에서 받으면 2로 카운트."""
        from apps.billing.dm_limits import count_owner_dms_this_month

        user = _user()
        ws = _ws(user)
        camp1, camp2 = _campaign(_conn(ws)), _campaign(_conn(ws))
        for camp in (camp1, camp2):
            SentDMLog.objects.create(
                campaign=camp,
                comment_id=f"c-{uuid.uuid4().hex[:8]}",
                recipient_user_id="shared_person",
                recipient_username="shared",
                message_sent="hi",
                idempotency_key=f"xcamp-{uuid.uuid4().hex[:8]}",
                status=SentDMLog.Status.DELIVERED,
            )
        assert count_owner_dms_this_month(user) == 2


# ──────────────────────────────────────────────
# 스팸필터 (pro 전용)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestSpamFilterGate:
    def _setup(self, plan_name):
        user = _user()
        if plan_name:
            _give_plan(user, plan_name)
        ws = _ws(user)
        conn = _conn(ws)
        client = APIClient()
        client.force_authenticate(user=user)
        return user, ws, conn, client

    def test_free_cannot_activate(self):
        _, _, conn, client = self._setup(None)
        resp = client.post(f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/activate/")
        assert resp.status_code == 403
        assert resp.data["error"]["details"]["feature"] == "spam_filter"

    def test_basic_cannot_patch_config(self):
        _, _, conn, client = self._setup("basic")
        resp = client.patch(
            f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/",
            {"spam_keywords": ["스팸"]},
            format="json",
        )
        assert resp.status_code == 403

    def test_pro_can_activate(self):
        _, _, conn, client = self._setup("pro")
        resp = client.post(f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/activate/")
        assert resp.status_code == 200
        assert resp.data["is_active"] is True

    def test_downgraded_user_can_still_view_and_deactivate(self):
        user, _, conn, client = self._setup("pro")
        client.post(f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/activate/")
        _give_plan(user, "free")  # 다운그레이드

        get_resp = client.get(f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/")
        assert get_resp.status_code == 200
        off_resp = client.post(
            f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/deactivate/"
        )
        assert off_resp.status_code == 200

    def test_runtime_gate_neutralizes_active_config_after_downgrade(self):
        """다운그레이드 후 config 가 active 로 남아도 런타임에서 무력화."""
        user = _user()
        _give_plan(user, "free")
        ws = _ws(user)
        conn = _conn(ws)
        SpamFilterConfig.objects.create(
            ig_connection=conn,
            status=SpamFilterConfig.Status.ACTIVE,
            spam_keywords=["아이돌"],
        )

        payload = {
            "field": "comments",
            "value": {
                "id": "c1",
                "text": "아이돌 영상 원본",
                "from": {"id": "u1", "username": "spammer"},
                "media": {"id": "m1"},
            },
            "entry_id": conn.external_account_id,
        }
        result = run_spam_filter_check.apply(args=[payload]).get()

        assert result["status"] == "skipped"
        assert result.get("reason") == "plan_not_allowed"
        # 스팸 로그도 남지 않아야 함(게이트에서 조기 종료)
        assert not SpamCommentLog.objects.filter(comment_id="c1").exists()


# ──────────────────────────────────────────────
# IG 계정 수 한도
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestIgAccountLimit:
    def test_allowance_resolution(self):
        free_user = _user()
        assert get_ig_account_allowance(free_user) == 1

        pro_user = _user()
        sub = _give_plan(pro_user, "pro")
        assert get_ig_account_allowance(pro_user) == 1
        UserSubscription.objects.filter(pk=sub.pk).update(extra_ig_accounts=2)
        pro_user = User.objects.get(pk=pro_user.pk)  # related 캐시 무효화
        assert get_ig_account_allowance(pro_user) == 3  # 기본 1 + 추가 2

        staff = _user(is_staff=True)
        assert get_ig_account_allowance(staff) == -1

    def test_connect_start_allows_reconnect_when_has_live_connection(self):
        """한도를 채운 사용자라도 살아있는 연동이 있으면 start 를 허용한다(재연동 가능).

        시작 시점엔 재연동인지 신규인지 알 수 없으므로 게이트를 열고, 신규 계정은
        콜백에서 최종 차단한다. → 사용자가 '재연동'을 눌렀는데 한도 429 가 뜨는
        비정상 UX 를 없앤다.
        """
        user = _user()
        ws = _ws(user)
        _conn(ws)  # 이미 1개 연동 (free 한도 소진 — 하지만 살아있는 연동이므로 재연동 가능)

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/")

        assert resp.status_code == 200
        assert "authorization_url" in resp.data

    def test_connect_start_blocked_at_limit_without_any_connection(self, monkeypatch):
        """살아있는 연동이 하나도 없는데 한도가 0 인 극단적 경우에만 사전 429.

        (실제로는 allowance>=1 이라 거의 발생하지 않지만, 게이트가 완전히 죽지
        않았음을 문서화.)
        """
        user = _user()
        ws = _ws(user)
        # 허용량 0 으로 강제(정상 플랜엔 없음) — 연동도 0개. connect_start 는 호출 시점에
        # apps.billing.subscription_utils.get_ig_account_allowance 를 조회하므로 거기를 패치.
        from apps.billing import subscription_utils

        monkeypatch.setattr(subscription_utils, "get_ig_account_allowance", lambda o: 0)

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/")

        assert resp.status_code == 429
        assert resp.data["error"]["code"] == "PLAN_LIMIT_EXCEEDED"

    def test_connect_start_allowed_under_limit(self):
        user = _user()
        ws = _ws(user)

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/")

        assert resp.status_code == 200
        assert "authorization_url" in resp.data

    def test_inactive_connections_do_not_consume_slots(self):
        user = _user()
        ws = _ws(user)
        _conn(ws, status=IGAccountConnection.Status.REVOKED)  # 비활성 — 슬롯 미소비

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/")
        assert resp.status_code == 200
