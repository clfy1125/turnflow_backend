"""재개 시 '정지 중 밀린 DM' 자동 되살림 테스트 (2026-08-22).

배경: `resume` 은 status 만 ACTIVE 로 올려서, 정지 중 큐에서 빠져나와 SKIPPED 로 종결된
건들이 재개 뒤에도 영구 미발송으로 남았다(prod @pre2entt: 소급 발송 238건 직후 정지 →
191명 미수신). 이제 모든 재개 경로가 창 안의 '정지 스킵' 건을 큐로 되돌린다.

커버리지:
  - 대상 판정 단일 소스 `SentDMLog.revivable_paused_logs`: 사유·윈도우 경계
  - `AutoDMCampaign.enqueue_paused_backlog_revive`: 전이일 때만, 예상 건수 반환
  - 태스크 `revive_paused_skipped_logs`: 실제 QUEUED 전이 + 재정지 시 no-op + 상한
  - API: 단건 resume / 일괄 bulk-resume / PATCH status=active 응답의 `revive_queued`

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언.
NOTE(pytest-tests-prefix): 파일명이 test_ 라 자동수집되지만, 실행은 경로 명시를 권장.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.tasks import revive_paused_skipped_logs
from apps.workspace.models import Membership, Workspace

PAUSED_SKIP = f"{SentDMLog.SKIP_REASON_CAMPAIGN_INACTIVE} (status=paused)"


@pytest.fixture
def owner(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create_user(
        email=f"rv_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="RV"
    )


@pytest.fixture
def ig_connection(owner):
    ws = Workspace.objects.create(name="RV WS", slug=f"rv-{uuid.uuid4().hex[:8]}", owner=owner)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="rvuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_rv"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "rv-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.PAUSED,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, *, age=timedelta(hours=1), **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "정보 주세요",
        "recipient_user_id": f"rcpt_{uuid.uuid4().hex[:8]}",
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.SKIPPED,
        "error_message": PAUSED_SKIP,
        "idempotency_key": uuid.uuid4().hex,
    }
    defaults.update(kwargs)
    log = SentDMLog.objects.create(**defaults)
    if age is not None:
        # created_at 은 auto_now_add — 나이는 직접 밀어넣는다.
        SentDMLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - age)
        log.refresh_from_db()
    return log


def _auth(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client


# ─────────────────── 대상 판정 (revivable_paused_logs) ───────────────────


@pytest.mark.django_db
class TestRevivableSelection:
    def test_picks_paused_skips_in_window(self, ig_connection):
        camp = _campaign(ig_connection)
        a = _log(camp, age=timedelta(days=6, hours=23))  # 댓글 7일 창 안
        b = _log(camp, age=timedelta(hours=2))
        ids = set(SentDMLog.revivable_paused_logs(camp).values_list("id", flat=True))
        assert ids == {a.id, b.id}

    def test_oldest_first(self, ig_connection):
        camp = _campaign(ig_connection)
        old = _log(camp, age=timedelta(days=5))
        new = _log(camp, age=timedelta(minutes=5))
        assert list(SentDMLog.revivable_paused_logs(camp).values_list("id", flat=True)) == [
            old.id,
            new.id,
        ]

    def test_excludes_expired_comment_window(self, ig_connection):
        camp = _campaign(ig_connection)
        _log(camp, age=timedelta(days=7, hours=1))
        assert SentDMLog.revivable_paused_logs(camp).count() == 0

    def test_userid_log_uses_24h_window(self, ig_connection):
        """comment_id 없는 건(스토리답장·보상 DM)은 24h 창 — 7일이 아니다."""
        camp = _campaign(ig_connection)
        alive = _log(camp, comment_id="", age=timedelta(hours=23))
        _log(camp, comment_id="", age=timedelta(hours=25))
        ids = set(SentDMLog.revivable_paused_logs(camp).values_list("id", flat=True))
        assert ids == {alive.id}

    def test_excludes_other_skip_reasons(self, ig_connection):
        """월 한도·셀프수신·계정비활성·예약창 스킵은 재개와 무관 → 대상 아님."""
        camp = _campaign(ig_connection)
        for reason in (
            "monthly_dm_limit_reached",
            "self recipient (account itself)",
            "IG account deactivated (activation_limit)",
            "Campaign outside active schedule window",
        ):
            _log(camp, error_message=reason)
        assert SentDMLog.revivable_paused_logs(camp).count() == 0

    def test_excludes_non_skipped_statuses(self, ig_connection):
        camp = _campaign(ig_connection)
        _log(camp, status=SentDMLog.Status.DELIVERED, error_message="")
        _log(camp, status=SentDMLog.Status.QUEUED, error_message="")
        _log(camp, status=SentDMLog.Status.FAILED_WINDOW, error_message="window expired")
        assert SentDMLog.revivable_paused_logs(camp).count() == 0

    def test_scoped_to_campaign(self, ig_connection):
        mine, other = _campaign(ig_connection), _campaign(ig_connection)
        _log(other)
        assert SentDMLog.revivable_paused_logs(mine).count() == 0


# ─────────────────── 진입점 (enqueue_paused_backlog_revive) ───────────────────


@pytest.mark.django_db
class TestEnqueueEntryPoint:
    def test_returns_count_and_dispatches(self, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        _log(camp)
        _log(camp)
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            assert camp.enqueue_paused_backlog_revive(previous_status="paused") == 2
        delay.assert_called_once_with(str(camp.id))

    def test_noop_when_already_active(self, ig_connection):
        """활성 캠페인의 흔한 문구 수정 PATCH 마다 백로그를 훑지 않는다."""
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        _log(camp)
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            assert camp.enqueue_paused_backlog_revive(previous_status="active") == 0
        delay.assert_not_called()

    def test_noop_when_target_not_active(self, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.PAUSED)
        _log(camp)
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            assert camp.enqueue_paused_backlog_revive(previous_status="paused") == 0
        delay.assert_not_called()

    def test_no_task_when_nothing_pending(self, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            assert camp.enqueue_paused_backlog_revive(previous_status="paused") == 0
        delay.assert_not_called()

    def test_reported_count_capped_at_limit(self, ig_connection, monkeypatch):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        for _ in range(3):
            _log(camp)
        monkeypatch.setattr(AutoDMCampaign, "RESUME_REVIVE_MAX", 2)
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay"):
            assert camp.enqueue_paused_backlog_revive(previous_status="paused") == 2


# ─────────────────── 태스크 (revive_paused_skipped_logs) ───────────────────


@pytest.mark.django_db
class TestReviveTask:
    def test_revives_to_queued_and_enqueues_send(self, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        log = _log(camp)
        key_before = log.idempotency_key
        with patch("apps.integrations.tasks.send_dm_task.delay") as send:
            res = revive_paused_skipped_logs(str(camp.id))
        log.refresh_from_db()
        assert res["revived"] == 1
        assert log.status == SentDMLog.Status.QUEUED
        # 같은 row·같은 키 재사용 = 중복 발송 구조적 차단
        assert log.idempotency_key == key_before
        send.assert_called_once_with(str(log.id))
        assert any(e.get("reason") == "campaign_resumed" for e in (log.verification_log or []))

    def test_noop_if_paused_again_before_task_runs(self, ig_connection):
        """태스크가 큐에서 대기하는 동안 다시 정지됐으면 되살리지 않는다(즉시 재스킵될 뿐)."""
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.PAUSED)
        log = _log(camp)
        with patch("apps.integrations.tasks.send_dm_task.delay") as send:
            res = revive_paused_skipped_logs(str(camp.id))
        log.refresh_from_db()
        assert res == {"revived": 0, "scanned": 0, "reason": "campaign_not_active"}
        assert log.status == SentDMLog.Status.SKIPPED
        send.assert_not_called()

    def test_expired_logs_left_alone(self, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        expired = _log(camp, age=timedelta(days=8))
        with patch("apps.integrations.tasks.send_dm_task.delay"):
            res = revive_paused_skipped_logs(str(camp.id))
        expired.refresh_from_db()
        assert res["revived"] == 0
        assert expired.status == SentDMLog.Status.SKIPPED

    def test_limit_truncates_and_reports(self, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        for _ in range(3):
            _log(camp)
        with patch("apps.integrations.tasks.send_dm_task.delay"):
            res = revive_paused_skipped_logs(str(camp.id), limit=2)
        assert (res["revived"], res["truncated"]) == (2, True)
        assert SentDMLog.objects.filter(campaign=camp, status=SentDMLog.Status.SKIPPED).count() == 1

    def test_missing_campaign_is_graceful(self):
        assert revive_paused_skipped_logs(str(uuid.uuid4()))["reason"] == "campaign_not_found"

    def test_second_run_is_idempotent(self, ig_connection):
        """재개를 두 번 눌러도 이미 QUEUED 인 건은 다시 큐에 넣지 않는다."""
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        _log(camp)
        with patch("apps.integrations.tasks.send_dm_task.delay") as send:
            revive_paused_skipped_logs(str(camp.id))
            second = revive_paused_skipped_logs(str(camp.id))
        assert second["revived"] == 0
        assert send.call_count == 1


# ─────────────────────────── API 경로 ───────────────────────────


@pytest.mark.django_db
class TestResumeEndpoints:
    def test_resume_returns_revive_queued(self, owner, ig_connection):
        camp = _campaign(ig_connection)
        _log(camp)
        _log(camp)
        _log(camp, age=timedelta(days=9))  # 창 만료 → 세지 않는다
        url = f"/api/v1/integrations/auto-dm-campaigns/{camp.id}/resume/"
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            res = _auth(owner).post(url)
        assert res.status_code == 200
        assert res.data["status"] == "active"
        assert res.data["revive_queued"] == 2
        delay.assert_called_once_with(str(camp.id))

    def test_resume_without_backlog_reports_zero(self, owner, ig_connection):
        camp = _campaign(ig_connection)
        url = f"/api/v1/integrations/auto-dm-campaigns/{camp.id}/resume/"
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            res = _auth(owner).post(url)
        assert res.data["revive_queued"] == 0
        delay.assert_not_called()

    def test_bulk_resume_sums_across_campaigns(self, owner, ig_connection):
        a, b = _campaign(ig_connection), _campaign(ig_connection)
        _log(a)
        _log(b)
        _log(b)
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            res = _auth(owner).post(
                "/api/v1/integrations/auto-dm-campaigns/bulk-resume/",
                {"ids": [str(a.id), str(b.id)]},
                format="json",
            )
        assert res.status_code == 200
        assert set(res.data["succeeded"]) == {str(a.id), str(b.id)}
        assert res.data["revive_queued"] == 3
        assert delay.call_count == 2

    def test_bulk_pause_has_no_revive_key(self, owner, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        res = _auth(owner).post(
            "/api/v1/integrations/auto-dm-campaigns/bulk-pause/",
            {"ids": [str(camp.id)]},
            format="json",
        )
        assert res.status_code == 200
        assert "revive_queued" not in res.data

    def test_patch_to_active_revives(self, owner, ig_connection):
        """프론트가 resume 대신 PATCH {status:active} 를 쓰는 경로도 되살린다."""
        camp = _campaign(ig_connection)
        _log(camp)
        url = f"/api/v1/integrations/auto-dm-campaigns/{camp.id}/"
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            res = _auth(owner).patch(url, {"status": "active"}, format="json")
        assert res.status_code == 200
        assert res.data["revive_queued"] == 1
        delay.assert_called_once_with(str(camp.id))

    def test_patch_text_edit_on_active_does_not_revive(self, owner, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.ACTIVE)
        _log(camp)
        url = f"/api/v1/integrations/auto-dm-campaigns/{camp.id}/"
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay") as delay:
            res = _auth(owner).patch(url, {"name": "문구만 수정"}, format="json")
        assert res.status_code == 200
        assert res.data["revive_queued"] == 0
        delay.assert_not_called()

    def test_free_plan_can_revive_on_resume(self, owner, ig_connection):
        """되살림은 프리미엄 retry-failed 와 달리 전 플랜 — 무료도 밀린 건을 이어받는다.

        (무료 플랜의 폭주는 send_dm_task 의 월 200건 한도 가드가 막는다.)
        """
        camp = _campaign(ig_connection)
        _log(camp)
        url = f"/api/v1/integrations/auto-dm-campaigns/{camp.id}/resume/"
        with patch("apps.integrations.tasks.revive_paused_skipped_logs.delay"):
            res = _auth(owner).post(url)
        assert res.status_code == 200
        assert res.data["revive_queued"] == 1
