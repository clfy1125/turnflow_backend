"""IG 토큰 사망 스트라이크 + 웹훅 점검 알림 조건 회귀 테스트 (2026-08-31).

배경 사고:
    매시간 도는 웹훅 점검이 토큰이 죽은 계정 6개를 매번 다시 발견하면서 결과를 어디에도
    남기지 않아, 텔레그램 `🔔 IG 웹훅 구독 점검: 재구독 0 · 실패 6` 이 일주일 넘게 1시간
    주기로 반복 발사됐다. status 는 `active` 로 남아 점검 모수에서 빠지지도 않았다.

여기서 지키는 것:
    1. 사망은 **연속 N회** 확인해야 확정된다 (중간 부활 시 초기화).
    2. 애매한 실패(네트워크·5xx·판정불가)는 스트라이크로 세지 않는다 — Meta 장애로 남의
       연결을 죽이지 않는다.
    3. 알림은 **상태 변화**일 때만 나간다 (확정 1회 / 재구독 / 대량 실패).
       → 이 테스트들은 수정 전 코드(`resubscribed or failed`)에서 반드시 실패해야 한다.
    4. 재연동·토큰갱신 성공은 사망 흔적을 지운다.
"""

import uuid
from datetime import timedelta

import pytest
import requests
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.integrations import tasks as t
from apps.integrations import token_health as th
from apps.integrations.models import IGAccountConnection
from apps.integrations.services import InstagramOAuthService
from apps.workspace.models import Membership, Workspace

User = get_user_model()

DEAD_SESSION_MSG = (
    "Error validating access token: The session has been invalidated because the user "
    "changed their password or Facebook has changed the session for security reasons."
)
DEAD_CHECKPOINT_MSG = (
    "Error validating access token: You cannot access the app till you log in to "
    "www.instagram.com and follow the instructions given."
)


def _conn(expires_in_days=47):
    user = User.objects.create_user(
        email=f"strike-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="s-ws", slug=f"s-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
        token_expires_at=timezone.now() + timedelta(days=expires_in_days),
    )
    conn.access_token = "live_token_xyz"
    conn.save()
    return conn


def _patch_verify(monkeypatch, valid, error_code=None, error_message=""):
    monkeypatch.setattr(
        InstagramOAuthService,
        "verify_token",
        classmethod(
            lambda cls, tok: {
                "valid": valid,
                "error_code": error_code,
                "error_message": error_message,
            }
        ),
    )


def _patch_subs_raises(monkeypatch, status_code=400):
    """subscribed_apps GET 이 Meta 400 으로 죽는 상황 (raise_for_status_clean 이 본문을 지운 형태)."""

    def _boom(cls, ig_id, tok):
        raise requests.HTTPError(f"Instagram Graph API error: {status_code}")

    monkeypatch.setattr(InstagramOAuthService, "get_webhook_subscriptions", classmethod(_boom))


def _isolate(conn):
    """스윕 루프 테스트용 격리.

    ⚠️ pytest DB 는 dev DB 다(깨끗하지 않다) — 격리 없이 ``resubscribe_active_connections``
    를 부르면 기존 연동 수십 건까지 훑어서 카운트 단언이 무너지고, 패치된 verify 때문에
    남의 연동까지 사망 판정을 받는다. 점검 모수가 ``is_active=True`` 이므로 나머지를 내려
    이 연동만 보이게 만든다. pytest-django 트랜잭션 롤백으로 테스트 후 원복된다.
    """
    IGAccountConnection.objects.exclude(pk=conn.pk).update(is_active=False)


def _patch_subs_ok(monkeypatch, fields=("comments", "messages")):
    monkeypatch.setattr(
        InstagramOAuthService,
        "get_webhook_subscriptions",
        classmethod(lambda cls, ig_id, tok: {"data": [{"subscribed_fields": list(fields)}]}),
    )


# ──────────────────────────────────────────────
# 사유 분류
# ──────────────────────────────────────────────


class TestReasonClassification:
    def test_session_invalidated(self):
        assert th.classify_dead_reason(DEAD_SESSION_MSG, 190) == th.REASON_TOKEN_INVALIDATED

    def test_account_checkpoint(self):
        """체크포인트는 '재연동'이 아니라 '인스타에서 본인확인'이라 문구가 달라야 한다."""
        assert th.classify_dead_reason(DEAD_CHECKPOINT_MSG, 190) == th.REASON_ACCOUNT_CHECKPOINT

    def test_unknown_message_falls_back(self):
        assert th.classify_dead_reason("something new from Meta", 190) == (
            th.REASON_RECONNECT_REQUIRED
        )


# ──────────────────────────────────────────────
# 스트라이크 누적/초기화
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestStrikeAccounting:
    def test_first_dead_probe_does_not_brick(self, monkeypatch, settings):
        settings.IG_TOKEN_DEAD_STRIKES = 3
        conn = _conn()
        _patch_verify(monkeypatch, False, 190, DEAD_SESSION_MSG)

        out = th.probe_and_record(conn, source="test")

        assert out["verdict"] == th.STRIKE
        assert out["strikes"] == 1
        conn.refresh_from_db()
        assert conn.status == IGAccountConnection.Status.ACTIVE  # 아직 안 죽인다
        assert conn.token_dead_strikes == 1
        assert conn.token_dead_first_seen_at is not None
        assert conn.reconnect_reason == th.REASON_TOKEN_INVALIDATED

    def test_confirms_at_threshold(self, monkeypatch, settings):
        settings.IG_TOKEN_DEAD_STRIKES = 3
        conn = _conn()
        _patch_verify(monkeypatch, False, 190, DEAD_SESSION_MSG)

        assert th.probe_and_record(conn, source="test")["verdict"] == th.STRIKE
        assert th.probe_and_record(conn, source="test")["verdict"] == th.STRIKE
        third = th.probe_and_record(conn, source="test")

        assert third["verdict"] == th.CONFIRMED_DEAD
        conn.refresh_from_db()
        assert conn.status == IGAccountConnection.Status.ERROR
        assert conn.token_dead_strikes == 3
        assert conn.reconnect_reason == th.REASON_TOKEN_INVALIDATED
        assert "token dead confirmed" in conn.error_message

    def test_revival_resets_strikes(self, monkeypatch, settings):
        """중간에 한 번이라도 살아나면 처음부터 — '연속' N회여야 한다."""
        settings.IG_TOKEN_DEAD_STRIKES = 3
        conn = _conn()
        _patch_verify(monkeypatch, False, 190, DEAD_SESSION_MSG)
        th.probe_and_record(conn, source="test")
        th.probe_and_record(conn, source="test")
        conn.refresh_from_db()
        assert conn.token_dead_strikes == 2

        _patch_verify(monkeypatch, True)
        out = th.probe_and_record(conn, source="test")

        assert out["verdict"] == th.ALIVE
        conn.refresh_from_db()
        assert conn.token_dead_strikes == 0
        assert conn.token_dead_first_seen_at is None
        assert conn.reconnect_reason == ""
        assert conn.status == IGAccountConnection.Status.ACTIVE

    def test_ambiguous_never_strikes(self, monkeypatch, settings):
        """네트워크·5xx·판정불가는 세지 않는다 (Meta 장애로 남의 연결을 죽이지 않기)."""
        settings.IG_TOKEN_DEAD_STRIKES = 3
        conn = _conn()
        _patch_verify(monkeypatch, None)

        for _ in range(5):
            out = th.probe_and_record(conn, source="test")
            assert out["verdict"] == th.UNKNOWN

        conn.refresh_from_db()
        assert conn.token_dead_strikes == 0
        assert conn.status == IGAccountConnection.Status.ACTIVE

    def test_ambiguous_does_not_reset_existing_strikes(self, monkeypatch, settings):
        """애매한 판정은 보류 — 누적을 늘리지도, 지우지도 않는다."""
        settings.IG_TOKEN_DEAD_STRIKES = 3
        conn = _conn()
        _patch_verify(monkeypatch, False, 190, DEAD_SESSION_MSG)
        th.probe_and_record(conn, source="test")

        _patch_verify(monkeypatch, None)
        th.probe_and_record(conn, source="test")

        conn.refresh_from_db()
        assert conn.token_dead_strikes == 1

    def test_record_false_does_not_write(self, monkeypatch, settings):
        """--check-only 는 상태를 바꾸지 않는다."""
        settings.IG_TOKEN_DEAD_STRIKES = 3
        conn = _conn()
        _patch_verify(monkeypatch, False, 190, DEAD_SESSION_MSG)

        out = th.probe_and_record(conn, source="test", record=False)

        assert out["verdict"] == th.STRIKE
        conn.refresh_from_db()
        assert conn.token_dead_strikes == 0
        assert conn.status == IGAccountConnection.Status.ACTIVE


# ──────────────────────────────────────────────
# 웹훅 점검 루프 — 실패 분류
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestWebhookCheckBuckets:
    def test_dead_token_goes_to_dead_bucket_not_failed(self, monkeypatch, settings):
        """예전엔 죽은 토큰이 failed 에 섞여 알림을 영구 반복시켰다."""
        settings.IG_TOKEN_DEAD_STRIKES = 1  # 즉시 확정시켜 버킷만 검증
        conn = _conn()
        _isolate(conn)
        _patch_subs_raises(monkeypatch)
        _patch_verify(monkeypatch, False, 190, DEAD_CHECKPOINT_MSG)

        res = t.resubscribe_active_connections()

        assert res["token_dead_confirmed"] == 1
        assert res["failed"] == 0
        assert res["dead_accounts"][0]["reason"] == th.REASON_ACCOUNT_CHECKPOINT
        conn.refresh_from_db()
        assert conn.status == IGAccountConnection.Status.ERROR

    def test_live_token_webhook_error_stays_failed(self, monkeypatch, settings):
        """토큰은 살아있는데 웹훅 호출이 실패한 건은 여전히 failed 로 보고돼야 한다."""
        settings.IG_TOKEN_DEAD_STRIKES = 3
        _isolate(_conn())
        _patch_subs_raises(monkeypatch, 500)
        _patch_verify(monkeypatch, True)

        res = t.resubscribe_active_connections()

        assert res["failed"] == 1
        assert res["token_dead_confirmed"] == 0
        assert res["token_dead_pending"] == 0

    def test_confirmed_dead_leaves_the_pool_next_run(self, monkeypatch, settings):
        """확정 후 다음 실행에서는 아예 점검 대상이 아니어야 한다(= 알림도 안 온다)."""
        settings.IG_TOKEN_DEAD_STRIKES = 1
        _isolate(_conn())
        _patch_subs_raises(monkeypatch)
        _patch_verify(monkeypatch, False, 190, DEAD_SESSION_MSG)

        first = t.resubscribe_active_connections()
        second = t.resubscribe_active_connections()

        assert first["checked"] == 1
        assert second["checked"] == 0
        assert second["token_dead_confirmed"] == 0

    def test_ok_path_clears_strikes(self, monkeypatch, settings):
        settings.IG_TOKEN_DEAD_STRIKES = 3
        conn = _conn()
        conn.token_dead_strikes = 2
        conn.token_dead_first_seen_at = timezone.now()
        conn.reconnect_reason = th.REASON_TOKEN_INVALIDATED
        conn.save()
        _isolate(conn)
        _patch_subs_ok(monkeypatch)

        res = t.resubscribe_active_connections()

        assert res["ok"] == 1
        conn.refresh_from_db()
        assert conn.token_dead_strikes == 0
        assert conn.reconnect_reason == ""


# ──────────────────────────────────────────────
# 알림 발사 조건 (핵심 회귀 — 수정 전 코드에서 실패해야 한다)
# ──────────────────────────────────────────────


class TestAlertCondition:
    def test_no_alert_when_only_pending_strikes(self):
        """확정 전 스트라이크 누적만으로는 조용하다."""
        assert not t._should_alert_webhook_check(
            {"checked": 218, "ok": 212, "resubscribed": 0, "failed": 0, "token_dead_pending": 6}
        )

    def test_no_alert_for_small_ambiguous_failures(self):
        """이게 예전에 매시간 울린 그 조건이다 — 이제는 조용해야 한다."""
        assert not t._should_alert_webhook_check(
            {
                "checked": 218,
                "ok": 212,
                "resubscribed": 0,
                "failed": 6,
                "token_dead_confirmed": 0,
            }
        )

    def test_alert_on_confirmed_dead(self):
        assert t._should_alert_webhook_check(
            {"checked": 218, "ok": 212, "resubscribed": 0, "failed": 0, "token_dead_confirmed": 6}
        )

    def test_alert_on_resubscribe(self):
        """웹훅 실소실 — 이 잡의 존재 이유. 절대 묻히면 안 된다."""
        assert t._should_alert_webhook_check(
            {"checked": 218, "ok": 217, "resubscribed": 1, "failed": 0}
        )

    def test_alert_on_mass_failure(self):
        """Meta 장애/우리 장애 신호는 여전히 잡는다."""
        assert t._should_alert_webhook_check(
            {"checked": 100, "ok": 50, "resubscribed": 0, "failed": 50}
        )

    def test_summary_message_names_accounts_and_reasons(self):
        """숫자만 보내던 탓에 일주일간 원인을 몰랐다."""
        msg = t._build_webhook_check_summary(
            {
                "checked": 218,
                "ok": 212,
                "resubscribed": 0,
                "failed": 0,
                "token_dead_pending": 0,
                "dead_accounts": [
                    {
                        "ig_user_id": "17841408028431417",
                        "username": "jjurimam",
                        "reason": th.REASON_TOKEN_INVALIDATED,
                        "error_code": 190,
                        "strikes": 3,
                    }
                ],
            }
        )
        assert "jjurimam" in msg
        assert th.REASON_TOKEN_INVALIDATED in msg
