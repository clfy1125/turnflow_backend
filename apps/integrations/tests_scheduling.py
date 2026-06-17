"""DM 예약 발송 (활성 기간 한정 + 자동 종료) 테스트.

커버리지:
  - 모델 윈도우 헬퍼: is_within_schedule / is_runnable_now / schedule_state / schedule_window_q
  - 자동 종료 Beat 태스크: enforce_campaign_schedules
  - 생성 시리얼라이저 검증 (종료 ≤ 시작 / 종료 과거)
  - schedule 엔드포인트 + resume 의 과거-종료 해제

NOTE(test-db-not-clean): 테스트 DB 가 깨끗하지 않을 수 있어 전역 카운트 대신
내가 만든 캠페인 id 기준으로 단언한다.
"""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def workspace_and_user(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email="sched@example.com", password="pw12345!", full_name="Sched Tester"
    )
    ws = Workspace.objects.create(name="Sched WS", slug="sched-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


@pytest.fixture
def ig_connection(workspace_and_user):
    ws, _ = workspace_and_user
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id="ig_sched_001",
        username="scheduser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_sched"
    conn.save()
    return conn


def _make_campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "sched-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _make_log(campaign, **kwargs):
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


# ===== 모델 윈도우 헬퍼 =====


class TestScheduleWindowHelpers:
    def test_no_bounds_is_always_within_and_running(self, ig_connection):
        c = _make_campaign(ig_connection)
        assert c.is_within_schedule() is True
        assert c.is_runnable_now() is True
        assert c.schedule_state() == "always_on"

    def test_before_start_not_within(self, ig_connection):
        now = timezone.now()
        c = _make_campaign(ig_connection, scheduled_start_at=now + timedelta(hours=1))
        assert c.is_within_schedule() is False
        assert c.is_runnable_now() is False
        assert c.schedule_state() == "scheduled"

    def test_after_end_not_within(self, ig_connection):
        now = timezone.now()
        c = _make_campaign(ig_connection, scheduled_end_at=now - timedelta(minutes=1))
        assert c.is_within_schedule() is False
        assert c.is_runnable_now() is False
        assert c.schedule_state() == "ended"

    def test_inside_window_running(self, ig_connection):
        now = timezone.now()
        c = _make_campaign(
            ig_connection,
            scheduled_start_at=now - timedelta(hours=1),
            scheduled_end_at=now + timedelta(hours=1),
        )
        assert c.is_within_schedule() is True
        assert c.is_runnable_now() is True
        assert c.schedule_state() == "running"

    def test_paused_in_window_not_runnable(self, ig_connection):
        now = timezone.now()
        c = _make_campaign(
            ig_connection,
            status=AutoDMCampaign.Status.PAUSED,
            scheduled_end_at=now + timedelta(hours=1),
        )
        # 창 안이지만 status 가 ACTIVE 가 아니므로 발송 불가
        assert c.is_within_schedule() is True
        assert c.is_runnable_now() is False

    def test_schedule_window_q_filters_only_in_window(self, ig_connection):
        now = timezone.now()
        in_window = _make_campaign(
            ig_connection,
            scheduled_start_at=now - timedelta(hours=1),
            scheduled_end_at=now + timedelta(hours=1),
        )
        future = _make_campaign(ig_connection, scheduled_start_at=now + timedelta(hours=1))
        ended = _make_campaign(ig_connection, scheduled_end_at=now - timedelta(minutes=1))
        unbounded = _make_campaign(ig_connection)

        ids = set(
            AutoDMCampaign.objects.filter(ig_connection=ig_connection)
            .filter(AutoDMCampaign.schedule_window_q())
            .values_list("id", flat=True)
        )
        assert in_window.id in ids
        assert unbounded.id in ids
        assert future.id not in ids
        assert ended.id not in ids


# ===== 자동 종료 Beat 태스크 =====


class TestEnforceCampaignSchedules:
    def test_auto_completes_ended_active_campaign(self, ig_connection):
        from apps.integrations.tasks import enforce_campaign_schedules

        now = timezone.now()
        ended = _make_campaign(ig_connection, scheduled_end_at=now - timedelta(minutes=5))
        running = _make_campaign(ig_connection, scheduled_end_at=now + timedelta(hours=1))
        unbounded = _make_campaign(ig_connection)

        enforce_campaign_schedules()

        ended.refresh_from_db()
        running.refresh_from_db()
        unbounded.refresh_from_db()

        assert ended.status == AutoDMCampaign.Status.COMPLETED
        assert ended.ended_at is not None
        assert running.status == AutoDMCampaign.Status.ACTIVE
        assert unbounded.status == AutoDMCampaign.Status.ACTIVE

    def test_does_not_touch_paused_ended_campaign(self, ig_connection):
        from apps.integrations.tasks import enforce_campaign_schedules

        now = timezone.now()
        paused = _make_campaign(
            ig_connection,
            status=AutoDMCampaign.Status.PAUSED,
            scheduled_end_at=now - timedelta(minutes=5),
        )
        enforce_campaign_schedules()
        paused.refresh_from_db()
        assert paused.status == AutoDMCampaign.Status.PAUSED


# ===== 생성 시리얼라이저 검증 =====


class TestCreateSerializerScheduleValidation:
    def _base_payload(self, **extra):
        data = {
            "trigger_type": "specific_media",
            "media_id": "111222333",
            "name": "검증 캠페인",
            "message_template": "hi",
        }
        data.update(extra)
        return data

    def test_rejects_end_before_start(self, db):
        from apps.integrations.serializers import AutoDMCampaignCreateSerializer

        now = timezone.now()
        s = AutoDMCampaignCreateSerializer(
            data=self._base_payload(
                scheduled_start_at=(now + timedelta(hours=2)).isoformat(),
                scheduled_end_at=(now + timedelta(hours=1)).isoformat(),
            )
        )
        assert s.is_valid() is False
        assert "scheduled_end_at" in s.errors

    def test_rejects_past_end(self, db):
        from apps.integrations.serializers import AutoDMCampaignCreateSerializer

        now = timezone.now()
        s = AutoDMCampaignCreateSerializer(
            data=self._base_payload(
                scheduled_end_at=(now - timedelta(hours=1)).isoformat(),
            )
        )
        assert s.is_valid() is False
        assert "scheduled_end_at" in s.errors

    def test_accepts_valid_window(self, db):
        from apps.integrations.serializers import AutoDMCampaignCreateSerializer

        now = timezone.now()
        s = AutoDMCampaignCreateSerializer(
            data=self._base_payload(
                scheduled_start_at=(now + timedelta(hours=1)).isoformat(),
                scheduled_end_at=(now + timedelta(days=1)).isoformat(),
            )
        )
        assert s.is_valid(), s.errors


# ===== schedule 엔드포인트 + resume =====


class TestScheduleEndpoint:
    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _url(self, pk):
        return f"/api/v1/integrations/auto-dm-campaigns/{pk}/schedule/"

    def test_sets_window_and_activates(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        campaign = _make_campaign(ig_connection, status=AutoDMCampaign.Status.PAUSED)
        now = timezone.now()
        resp = self._client(user).post(
            self._url(campaign.id),
            {
                "scheduled_start_at": (now + timedelta(hours=1)).isoformat(),
                "scheduled_end_at": (now + timedelta(days=1)).isoformat(),
                "activate": True,
            },
            format="json",
        )
        assert resp.status_code == 200, resp.content
        campaign.refresh_from_db()
        assert campaign.status == AutoDMCampaign.Status.ACTIVE
        assert campaign.scheduled_start_at is not None
        assert campaign.scheduled_end_at is not None
        assert resp.data["schedule_state"] == "scheduled"

    def test_validation_error_returns_400(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        campaign = _make_campaign(ig_connection)
        now = timezone.now()
        resp = self._client(user).post(
            self._url(campaign.id),
            {"scheduled_end_at": (now - timedelta(hours=1)).isoformat()},
            format="json",
        )
        assert resp.status_code == 400

    def test_clear_schedule_back_to_always_on(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        now = timezone.now()
        campaign = _make_campaign(
            ig_connection,
            scheduled_start_at=now - timedelta(hours=1),
            scheduled_end_at=now + timedelta(hours=1),
        )
        resp = self._client(user).post(
            self._url(campaign.id),
            {"scheduled_start_at": None, "scheduled_end_at": None},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        campaign.refresh_from_db()
        assert campaign.scheduled_start_at is None
        assert campaign.scheduled_end_at is None
        assert resp.data["schedule_state"] == "always_on"

    def test_patch_rejects_past_end(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        campaign = _make_campaign(ig_connection)
        now = timezone.now()
        resp = self._client(user).patch(
            f"/api/v1/integrations/auto-dm-campaigns/{campaign.id}/",
            {"scheduled_end_at": (now - timedelta(hours=1)).isoformat()},
            format="json",
        )
        # PATCH 경로도 create/schedule 와 동일하게 과거 종료일을 거부해야 함
        assert resp.status_code == 400, resp.content

    def test_resume_clears_elapsed_end(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        now = timezone.now()
        campaign = _make_campaign(
            ig_connection,
            status=AutoDMCampaign.Status.COMPLETED,
            scheduled_end_at=now - timedelta(minutes=1),
        )
        resp = self._client(user).post(
            f"/api/v1/integrations/auto-dm-campaigns/{campaign.id}/resume/",
            format="json",
        )
        assert resp.status_code == 200, resp.content
        campaign.refresh_from_db()
        assert campaign.status == AutoDMCampaign.Status.ACTIVE
        # 과거가 된 종료 예약은 해제되어 즉시 재종료되지 않아야 함
        assert campaign.scheduled_end_at is None
        # 자동 종료로 남았던 ended_at 도 비워져야 함 (ACTIVE 인데 과거 종료시각 모순 방지)
        assert campaign.ended_at is None


# ===== 활성 기간 밖 발송 차단 (모든 실행 경로 — opening/reward/retry/수동재시도) =====


class TestPostWindowSendGuards:
    """예약 종료 후(또는 시작 전) 어떤 경로로도 DM 이 나가지 않아야 한다."""

    def test_enqueue_send_dm_skipped_when_ended(self, ig_connection):
        from apps.integrations.tasks import _enqueue_send_dm

        now = timezone.now()
        campaign = _make_campaign(ig_connection, scheduled_end_at=now - timedelta(minutes=1))
        result = _enqueue_send_dm(
            campaign=campaign,
            comment_id="cmt_x",
            comment_text="가격",
            from_user_id="user_x",
            from_username="buyer",
            webhook_payload={},
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "outside_schedule_window"
        # 발송 로그 자체가 생성되지 않아야 함
        assert not SentDMLog.objects.filter(campaign=campaign).exists()

    def test_send_dm_task_skips_ended_campaign(self, ig_connection):
        from apps.integrations.tasks import send_dm_task

        now = timezone.now()
        campaign = _make_campaign(ig_connection, scheduled_end_at=now - timedelta(minutes=1))
        log = _make_log(campaign, status=SentDMLog.Status.QUEUED)

        res = send_dm_task.apply(args=[str(log.id)])
        assert res.result["reason"] == "outside_schedule_window"
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.SKIPPED

    def test_send_dm_task_skips_before_start(self, ig_connection):
        from apps.integrations.tasks import send_dm_task

        now = timezone.now()
        campaign = _make_campaign(ig_connection, scheduled_start_at=now + timedelta(hours=1))
        log = _make_log(campaign, status=SentDMLog.Status.QUEUED)

        res = send_dm_task.apply(args=[str(log.id)])
        assert res.result["reason"] == "outside_schedule_window"
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.SKIPPED

    def test_send_reward_dm_skips_ended_campaign(self, ig_connection):
        from apps.integrations.tasks import send_reward_dm

        now = timezone.now()
        campaign = _make_campaign(
            ig_connection,
            follow_gate_enabled=True,
            reward_message_template="보상 링크: https://example.com",
            scheduled_end_at=now - timedelta(minutes=1),
        )
        opening = _make_log(
            campaign,
            status=SentDMLog.Status.DELIVERED,
            dm_kind=SentDMLog.DMKind.OPENING,
            gate_status=SentDMLog.GateStatus.PENDING,
        )
        res = send_reward_dm.apply(args=[str(opening.id)])
        assert res.result["reason"] == "outside_schedule_window"
        # reward 로그가 생성되지 않아야 함
        assert not SentDMLog.objects.filter(
            campaign=campaign, dm_kind=SentDMLog.DMKind.REWARD
        ).exists()

    def test_manual_retry_blocked_when_ended(self, workspace_and_user, ig_connection):
        _, user = workspace_and_user
        now = timezone.now()
        campaign = _make_campaign(ig_connection, scheduled_end_at=now - timedelta(minutes=1))
        log = _make_log(campaign, status=SentDMLog.Status.RATE_LIMITED)

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(
            f"/api/v1/integrations/dm-verification/{log.id}/retry/",
            format="json",
        )
        assert resp.status_code == 409, resp.content
        log.refresh_from_db()
        # 재시도가 막혀 QUEUED 로 바뀌지 않아야 함
        assert log.status == SentDMLog.Status.RATE_LIMITED
