"""무손실 하드닝 패치(P1·P2·P4·P6·P8·P11) 테스트.

커버리지:
  - P1  SentDMLog.revive(): FAILED_TOKEN/SKIPPED 제자리 되살림(윈도우 내), 만료/비대상 거부
  - P2  revive_failed_token_logs: 연결의 FAILED_TOKEN 윈도우 내 건 되살림
  - P4  rate_governor.trip_action_block/cooldown + _rate_defer action_block 게이트 + 에스컬레이션
  - P6  send_dm_task DMAnomalyError: recent=True→delivered, None→no_trace, False→defer
  - P8  rate_governor fail-closed: 센티넬 소멸(=Redis flush) 시 차단
  - P11 AutoDMCampaignSerializer.miss_recovery: any_media/story_reply 위험고지

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언.
NOTE(override-settings-broken): settings 픽스처 사용.
NOTE(pytest-tests-prefix): tests_*.py 는 자동수집 안 됨 → 파일 경로 명시 실행.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.integrations.dm_exceptions import DMAnomalyError
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import InstagramMessagingService
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"lh_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="LH"
    )
    ws = Workspace.objects.create(name="LH WS", slug=f"lh-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="lhuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_lh"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "lh-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "가격 문의",
        "recipient_user_id": f"rcpt_{uuid.uuid4().hex[:8]}",
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.QUEUED,
        "idempotency_key": uuid.uuid4().hex,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


# ───────────────────────── P1: revive() ─────────────────────────


class TestRevive:
    def test_failed_token_revives_in_window(self, ig_connection):
        campaign = _campaign(ig_connection)
        log = _log(campaign, status=SentDMLog.Status.FAILED_TOKEN)
        ok = log.revive(reason="test", enqueue=False)
        log.refresh_from_db()
        assert ok is True
        assert log.status == SentDMLog.Status.QUEUED
        assert log.next_retry_at is None
        assert any(e.get("path") == "revive" for e in (log.verification_log or []))

    def test_skipped_revives_in_window(self, ig_connection):
        campaign = _campaign(ig_connection)
        log = _log(campaign, status=SentDMLog.Status.SKIPPED)
        assert log.revive(enqueue=False) is True
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.QUEUED

    def test_expired_window_not_revived(self, ig_connection):
        campaign = _campaign(ig_connection)
        log = _log(campaign, status=SentDMLog.Status.FAILED_TOKEN)
        # comment_id 있으니 7일 윈도우 — 8일 전이면 만료
        SentDMLog.objects.filter(id=log.id).update(created_at=timezone.now() - timedelta(days=8))
        log.refresh_from_db()
        assert log.revive(enqueue=False) is False
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_TOKEN  # 그대로

    def test_non_revivable_status_rejected(self, ig_connection):
        campaign = _campaign(ig_connection)
        for st in (
            SentDMLog.Status.FAILED_WINDOW,
            SentDMLog.Status.FAILED_PARAM,
            SentDMLog.Status.FAILED_NO_TRACE,
            SentDMLog.Status.DELIVERED,
        ):
            log = _log(campaign, status=st)
            assert log.revive(enqueue=False) is False

    def test_revive_enqueues(self, ig_connection):
        campaign = _campaign(ig_connection)
        log = _log(campaign, status=SentDMLog.Status.FAILED_TOKEN)
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            assert log.revive() is True
            delay.assert_called_once_with(str(log.id))


# ───────────────────── P2: revive_failed_token_logs ─────────────────────


class TestReviveFailedTokenLogs:
    def test_revives_connection_failed_token(self, ig_connection):
        from apps.integrations.tasks import revive_failed_token_logs

        campaign = _campaign(ig_connection)
        in_window = _log(campaign, status=SentDMLog.Status.FAILED_TOKEN)
        expired = _log(campaign, status=SentDMLog.Status.FAILED_TOKEN)
        SentDMLog.objects.filter(id=expired.id).update(
            created_at=timezone.now() - timedelta(days=8)
        )

        with patch("apps.integrations.tasks.send_dm_task.delay"):
            res = revive_failed_token_logs.apply(args=[str(ig_connection.id)]).result

        in_window.refresh_from_db()
        expired.refresh_from_db()
        assert res["revived"] == 1
        assert in_window.status == SentDMLog.Status.QUEUED
        assert expired.status == SentDMLog.Status.FAILED_TOKEN  # 만료는 그대로


# ───────────────────────── P4: action block ─────────────────────────


class TestActionBlockCooldown:
    def test_trip_sets_cooldown_and_gates_send(self, ig_connection, settings):
        from apps.integrations import rate_governor
        from apps.integrations.tasks import _rate_defer

        acct = str(ig_connection.external_account_id)
        cache.delete(f"dm:ab:cooldown:{acct}")
        cache.delete(f"dm:ab:level:{acct}")

        cooldown = rate_governor.trip_action_block(acct, base_hours=24, max_days=7)
        assert cooldown == 24 * 3600
        assert rate_governor.action_block_cooldown_remaining(acct) > 0

        campaign = _campaign(ig_connection)
        log = _log(campaign)
        decision = _rate_defer(log, campaign, ig_connection)
        assert decision is not None
        assert decision[1] == "action_block_cooldown"

    def test_escalation_doubles(self, ig_connection):
        from apps.integrations import rate_governor
        from apps.integrations.models import DMAccountBlock

        acct = f"ab_{uuid.uuid4().hex}"
        c1 = rate_governor.trip_action_block(acct, base_hours=24, max_days=7)
        assert c1 == 24 * 3600
        # 쿨다운 '만료'를 시뮬 — 레벨은 보존하되 캐시·DB 쿨다운을 둘 다 만료시켜야 다음 트립이 ×2.
        # (DR 듀얼라이트로 action_block_cooldown_remaining 이 DB cooldown_until 도 보므로 캐시만 지우면 부족)
        cache.delete(f"dm:ab:cooldown:{acct}")
        DMAccountBlock.objects.filter(external_account_id=acct).update(
            cooldown_until=timezone.now() - timedelta(seconds=1)
        )
        c2 = rate_governor.trip_action_block(acct, base_hours=24, max_days=7)
        assert c2 == 48 * 3600

    def test_double_trip_in_cooldown_is_noop(self, ig_connection):
        from apps.integrations import rate_governor

        acct = f"ab_{uuid.uuid4().hex}"
        assert rate_governor.trip_action_block(acct) > 0
        assert rate_governor.trip_action_block(acct) == 0  # 이미 쿨다운 중


# ───────────────────────── P8: governor fail-closed ─────────────────────────


class TestGovernorFailClosed:
    def test_missing_sentinel_blocks(self):
        from apps.integrations import rate_governor

        acct = f"fc_{uuid.uuid4().hex}"
        try:
            # flush 시뮬: 센티넬/리셋마커 제거
            cache.delete("dmrate:alive")
            cache.delete("dmrate:reset_until")
            d = rate_governor.check(acct, plan="pro")
            assert d.allowed is False
            assert d.reason == "redis_reset_failclosed"
            # 이후 호출도 (다른 계정도) reset_until 로 차단
            d2 = rate_governor.check(f"other_{uuid.uuid4().hex}", plan="pro")
            assert d2.allowed is False
        finally:
            # 다른 테스트 오염 방지 — 정상 상태 복구
            cache.delete("dmrate:reset_until")
            cache.set("dmrate:alive", 1, timeout=7 * 24 * 3600)

    def test_with_sentinel_normal(self):
        from apps.integrations import rate_governor

        cache.set("dmrate:alive", 1, timeout=7 * 24 * 3600)
        cache.delete("dmrate:reset_until")
        acct = f"ok_{uuid.uuid4().hex}"
        d = rate_governor.check(acct, plan="pro")
        assert d.allowed is True


# ───────────────────────── P6: anomaly verify-before-resend ─────────────────────────


class TestAnomalyResend:
    def _run(self, ig_connection, settings, recent):
        settings.DM_GOVERNOR_ENABLED = False
        from apps.integrations.tasks import send_dm_task

        campaign = _campaign(ig_connection)
        log = _log(campaign)
        with (
            patch.object(
                InstagramMessagingService,
                "send_dm_via_comment",
                side_effect=DMAnomalyError("200 no msgid", status=200),
            ),
            patch.object(
                InstagramMessagingService,
                "has_recent_message_to_recipient",
                return_value=recent,
            ),
            patch("apps.integrations.tasks.verify_dm_delivery.apply_async"),
        ):
            res = send_dm_task.apply(args=[str(log.id)]).result
        log.refresh_from_db()
        campaign.refresh_from_db()
        return res, log, campaign

    def test_recent_true_marks_delivered_no_resend(self, ig_connection, settings):
        res, log, campaign = self._run(ig_connection, settings, recent=True)
        assert log.status == SentDMLog.Status.DELIVERED
        assert campaign.total_sent == 1
        assert campaign.total_unconfirmed == 0

    def test_recent_none_marks_unconfirmed(self, ig_connection, settings):
        res, log, campaign = self._run(ig_connection, settings, recent=None)
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE
        assert campaign.total_unconfirmed == 1
        assert campaign.total_failed == 0

    def test_recent_false_defers(self, ig_connection, settings):
        res, log, campaign = self._run(ig_connection, settings, recent=False)
        assert res["status"] == "deferred"
        assert log.status == SentDMLog.Status.QUEUED


# ───────────────────────── P11: miss_recovery serializer ─────────────────────────


class TestMissRecoveryField:
    def test_any_media_warns(self, ig_connection):
        from apps.integrations.serializers import AutoDMCampaignSerializer

        c = _campaign(ig_connection, trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA)
        data = AutoDMCampaignSerializer(c).data
        assert data["miss_recovery"]["auto_recovery_supported"] is False
        assert data["miss_recovery"]["warning"]

    def test_story_reply_warns(self, ig_connection):
        from apps.integrations.serializers import AutoDMCampaignSerializer

        c = _campaign(
            ig_connection,
            trigger_type=AutoDMCampaign.TriggerType.STORY_REPLY,
            media_id="story_123",
        )
        data = AutoDMCampaignSerializer(c).data
        assert data["miss_recovery"]["auto_recovery_supported"] is False

    def test_specific_media_safe(self, ig_connection):
        from apps.integrations.serializers import AutoDMCampaignSerializer

        c = _campaign(
            ig_connection,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="media_123",
        )
        data = AutoDMCampaignSerializer(c).data
        assert data["miss_recovery"]["auto_recovery_supported"] is True
        assert data["miss_recovery"]["warning"] is None
