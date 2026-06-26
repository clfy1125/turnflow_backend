"""DM 발송 하드닝 (v3.9) 테스트 — rate-limit defer / 윈도우 종결 / no_trace 분리 / requeue / governor.

커버리지:
  - send_dm_task: transient(rate-limit) → 종결 아닌 defer(QUEUED+next_retry_at), 통계 미증가
  - send_dm_task: 메시징 윈도우 만료 → FAILED_WINDOW graceful 종결
  - verify_dm_delivery 35분 cutoff → FAILED_NO_TRACE + increment_unconfirmed (total_failed 불변)
  - can_send_more: QUEUED(=defer 대기) 는 시간당 한도 카운트에서 제외 (데드락 방지)
  - requeue_deferred_dms: next_retry_at 도래 QUEUED 를 send_dm_task 로 재투입 + next_retry_at 비움
  - rate_governor: 계정당 시간당 상한이 Meta 700(IG_PRIVATE_REPLY_HOURLY_CAP) 으로 캡

NOTE(test-db-not-clean): 전역 카운트 대신 내가 만든 캠페인/로그 기준으로 단언한다.
NOTE(override-settings-broken): 클래스 데코레이터 대신 pytest `settings` 픽스처 사용.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.integrations.dm_exceptions import DMTransientError
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import InstagramMessagingService
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"rd_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="RD"
    )
    ws = Workspace.objects.create(
        name="RD WS", slug=f"rd-{uuid.uuid4().hex[:8]}", owner=user
    )
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="rduser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_rd"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "rd-campaign",
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


class TestTransientDefer:
    def test_rate_limit_defers_not_terminal(self, ig_connection, settings):
        """transient(rate-limit) 발송 실패 → QUEUED 로 defer, total_failed 미증가."""
        settings.DM_GOVERNOR_ENABLED = False  # 거버너 격리 (defer 동작만 검증)
        from apps.integrations.tasks import send_dm_task

        campaign = _campaign(ig_connection)
        log = _log(campaign)

        with patch.object(
            InstagramMessagingService,
            "send_dm_via_comment",
            side_effect=DMTransientError("rate limited", code=4),
        ):
            res = send_dm_task.apply(args=[str(log.id)]).result

        log.refresh_from_db()
        campaign.refresh_from_db()
        assert res["status"] == "deferred"
        assert log.status == SentDMLog.Status.QUEUED  # 종결 아님
        assert log.next_retry_at is not None
        assert log.retry_count == 1
        assert campaign.total_failed == 0  # rate-limit 은 실패로 집계 안 함
        assert campaign.total_unconfirmed == 0

    def test_window_expired_graceful_stop(self, ig_connection, settings):
        """메시징 윈도우(comment 7일) 만료 시 FAILED_WINDOW 로 종결."""
        settings.DM_GOVERNOR_ENABLED = False
        from apps.integrations.tasks import send_dm_task

        campaign = _campaign(ig_connection)
        log = _log(campaign)
        # created_at 을 8일 전으로 (auto_now_add 우회 → queryset update)
        SentDMLog.objects.filter(id=log.id).update(
            created_at=timezone.now() - timedelta(days=8)
        )

        with patch.object(InstagramMessagingService, "send_dm_via_comment") as send:
            res = send_dm_task.apply(args=[str(log.id)]).result
            send.assert_not_called()  # 윈도우 만료라 발송 시도조차 안 함

        log.refresh_from_db()
        campaign.refresh_from_db()
        assert res["status"] == "failed_window"
        assert log.status == SentDMLog.Status.FAILED_WINDOW
        assert campaign.total_failed == 1  # 윈도우 만료는 진짜 실패로 집계


class TestNoTraceCounter:
    def test_cutoff_increments_unconfirmed_not_failed(self, ig_connection):
        """verify 35분 cutoff → FAILED_NO_TRACE + total_unconfirmed (total_failed 불변)."""
        from apps.integrations.tasks import verify_dm_delivery

        campaign = _campaign(ig_connection)
        log = _log(
            campaign,
            status=SentDMLog.Status.ACCEPTED,
            meta_message_id="mid_x",
            accepted_at=timezone.now() - timedelta(minutes=40),
        )

        with patch.object(InstagramMessagingService, "fetch_message", return_value=None):
            res = verify_dm_delivery.apply(args=[str(log.id)]).result

        log.refresh_from_db()
        campaign.refresh_from_db()
        assert res["status"] == "failed_no_trace"
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE
        assert campaign.total_unconfirmed == 1
        assert campaign.total_failed == 0  # '미확인' 은 실패로 집계 안 함


class TestCanSendMore:
    def test_queued_not_counted(self, ig_connection):
        """QUEUED(=defer 대기) 는 시간당 한도 카운트에서 제외되어 데드락을 막는다."""
        campaign = _campaign(ig_connection, max_sends_per_hour=2)
        for _ in range(5):
            _log(campaign, status=SentDMLog.Status.QUEUED)
        assert campaign.can_send_more() is True  # QUEUED 5개여도 한도 미소진

        for _ in range(2):
            _log(campaign, status=SentDMLog.Status.ACCEPTED)
        assert campaign.can_send_more() is False  # ACCEPTED 2개로 한도 도달


class TestRequeueDeferred:
    def test_requeue_dispatches_and_clears_marker(self, ig_connection):
        from apps.integrations.tasks import requeue_deferred_dms

        campaign = _campaign(ig_connection)
        due = _log(
            campaign,
            status=SentDMLog.Status.QUEUED,
            next_retry_at=timezone.now() - timedelta(seconds=10),
        )
        not_due = _log(
            campaign,
            status=SentDMLog.Status.QUEUED,
            next_retry_at=timezone.now() + timedelta(hours=1),
        )

        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            res = requeue_deferred_dms.apply().result
            dispatched = {c.args[0] for c in delay.call_args_list}

        assert str(due.id) in dispatched
        assert str(not_due.id) not in dispatched
        due.refresh_from_db()
        assert due.next_retry_at is None  # 픽업 표식 비워짐
        assert res["requeued"] >= 1


class TestGovernorCap:
    def test_hourly_cap_overrides_high_plan(self, settings):
        """enterprise 처럼 750 초과 plan 도 IG_PRIVATE_REPLY_HOURLY_CAP 으로 캡."""
        from apps.integrations import rate_governor

        settings.IG_PRIVATE_REPLY_HOURLY_CAP = 2  # 테스트용 낮은 캡
        acct = f"acct_{uuid.uuid4().hex}"  # Redis 캐시 충돌 방지

        d1 = rate_governor.check(acct, plan="enterprise")
        d2 = rate_governor.check(acct, plan="enterprise")
        d3 = rate_governor.check(acct, plan="enterprise")

        assert d1.allowed and d2.allowed
        assert d3.allowed is False
        assert d3.reason == "per_hour"
        assert d3.retry_after > 0
