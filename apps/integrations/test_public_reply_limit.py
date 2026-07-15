"""공개 답글(대댓글) 누적 상한(public_reply_limit) 테스트 — Feature A.

커버리지:
  - 모델: public_reply_limit_reached() 경계 / increment_public_reply_posted() 원자 증가
  - 태스크 post_public_reply: 미달=게시+카운터+1, 도달=skipped(API 미호출·failed 아님),
    복구(recovery=True)는 상한 무관 게시·카운터 무증가, limit=0 무제한
  - send_dm_task enqueue 프리체크: 상한 도달 시 post_public_reply.apply_async 미호출
  - 시리얼라이저: 기본 200 / 음수 400 / posted_count 읽기전용

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import InstagramCommentService, InstagramMessagingService
from apps.workspace.models import Membership, Workspace

_SEND_OK = {"message_id": "mid_prl_1", "recipient_id": "rcpt_1", "_raw": {}}


# ── 모델 helper (DB 불필요) ────────────────────────────────────


class TestLimitReached:
    def test_zero_is_unlimited(self):
        c = AutoDMCampaign(public_reply_limit=0, public_reply_posted_count=999)
        assert c.public_reply_limit_reached() is False

    def test_below_limit(self):
        c = AutoDMCampaign(public_reply_limit=200, public_reply_posted_count=199)
        assert c.public_reply_limit_reached() is False

    def test_at_limit(self):
        c = AutoDMCampaign(public_reply_limit=200, public_reply_posted_count=200)
        assert c.public_reply_limit_reached() is True

    def test_over_limit(self):
        c = AutoDMCampaign(public_reply_limit=5, public_reply_posted_count=8)
        assert c.public_reply_limit_reached() is True


# ── 공용 픽스처 (DB) ───────────────────────────────────────────


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"prl_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="PRL"
    )
    ws = Workspace.objects.create(name="PRL WS", slug=f"prl-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_prl_{uuid.uuid4().hex[:8]}",
        username="prluser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_prl"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "prl-campaign",
        "message_template": "안녕하세요!",
        "opening_message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        "public_reply_enabled": True,
        "public_reply_templates": ["DM 드렸어요!"],
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": f"c-{uuid.uuid4().hex[:8]}",
        "comment_text": "문의",
        "recipient_user_id": f"igsid-{uuid.uuid4().hex[:8]}",
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.ACCEPTED,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.OPENING,
        "gate_status": SentDMLog.GateStatus.NONE,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


@pytest.fixture
def mock_reply(monkeypatch):
    m = MagicMock(return_value={"id": "reply_x"})
    monkeypatch.setattr(InstagramCommentService, "post_reply", m)
    return m


def _run_reply(log, recovery=False):
    return tasks_mod.post_public_reply.apply(args=[str(log.id)], kwargs={"recovery": recovery})


# ── 태스크 게이트 (DB) ─────────────────────────────────────────


@pytest.mark.django_db
class TestPostPublicReplyLimit:
    def test_under_limit_posts_and_increments(self, ig_connection, mock_reply):
        campaign = _campaign(ig_connection, public_reply_limit=200, public_reply_posted_count=0)
        log = _log(campaign)
        _run_reply(log)

        mock_reply.assert_called_once()
        log.refresh_from_db()
        campaign.refresh_from_db()
        assert log.public_reply_id == "reply_x"
        assert campaign.public_reply_posted_count == 1
        assert any(e.get("result") == "posted" for e in log.verification_log)

    def test_at_limit_skips_without_posting(self, ig_connection, mock_reply):
        campaign = _campaign(ig_connection, public_reply_limit=5, public_reply_posted_count=5)
        log = _log(campaign)
        res = _run_reply(log)

        assert res.result["status"] == "skipped"
        assert res.result["reason"] == "public_reply_limit_reached"
        mock_reply.assert_not_called()
        log.refresh_from_db()
        campaign.refresh_from_db()
        assert log.public_reply_id == ""  # 게시 안 됨
        assert campaign.public_reply_posted_count == 5  # 증가 안 됨
        assert log.status == SentDMLog.Status.ACCEPTED  # failed 로 만들지 않음
        assert any(e.get("result") == "limit_skipped" for e in log.verification_log)

    def test_unlimited_when_zero(self, ig_connection, mock_reply):
        campaign = _campaign(ig_connection, public_reply_limit=0, public_reply_posted_count=10000)
        log = _log(campaign)
        _run_reply(log)

        mock_reply.assert_called_once()
        log.refresh_from_db()
        assert log.public_reply_id == "reply_x"

    def test_recovery_exempt_from_limit(self, ig_connection, mock_reply):
        # 상한 초과 상태여도 복구 안내 대댓글은 항상 게시되고 카운터를 올리지 않는다.
        campaign = _campaign(
            ig_connection,
            public_reply_limit=5,
            public_reply_posted_count=5,
            recovery_reply_enabled=True,
        )
        log = _log(campaign, status=SentDMLog.Status.RECOVERY_PENDING)
        _run_reply(log, recovery=True)

        mock_reply.assert_called_once()
        log.refresh_from_db()
        campaign.refresh_from_db()
        assert log.recovery_reply_id == "reply_x"
        assert log.public_reply_id == ""
        assert campaign.public_reply_posted_count == 5  # 복구는 카운트 제외


# ── enqueue 프리체크 (DB) ──────────────────────────────────────


@pytest.fixture
def capture_send(monkeypatch):
    send = MagicMock(return_value=dict(_SEND_OK))
    monkeypatch.setattr(InstagramMessagingService, "send_dm_via_user_id", send)
    monkeypatch.setattr(InstagramMessagingService, "send_dm_via_comment", send)
    monkeypatch.setattr(tasks_mod.verify_dm_delivery, "apply_async", MagicMock())
    return send


@pytest.mark.django_db
class TestEnqueuePrecheck:
    def test_under_limit_enqueues_reply(self, ig_connection, capture_send, monkeypatch):
        enq = MagicMock()
        monkeypatch.setattr(tasks_mod.post_public_reply, "apply_async", enq)
        campaign = _campaign(ig_connection, public_reply_limit=200, public_reply_posted_count=0)
        log = _log(campaign, status=SentDMLog.Status.QUEUED, dm_kind=SentDMLog.DMKind.STANDALONE)
        tasks_mod.send_dm_task.apply(args=[str(log.id)])
        enq.assert_called_once()

    def test_at_limit_does_not_enqueue_reply(self, ig_connection, capture_send, monkeypatch):
        enq = MagicMock()
        monkeypatch.setattr(tasks_mod.post_public_reply, "apply_async", enq)
        campaign = _campaign(ig_connection, public_reply_limit=5, public_reply_posted_count=5)
        log = _log(campaign, status=SentDMLog.Status.QUEUED, dm_kind=SentDMLog.DMKind.STANDALONE)
        tasks_mod.send_dm_task.apply(args=[str(log.id)])
        enq.assert_not_called()


# ── 시리얼라이저 ───────────────────────────────────────────────


class TestSerializerLimit:
    def _create(self, **extra):
        from apps.integrations.serializers import AutoDMCampaignCreateSerializer

        data = {"trigger_type": "any_media", "name": "t", "message_template": "hi"}
        data.update(extra)
        return AutoDMCampaignCreateSerializer(data=data)

    def test_default_200(self):
        s = self._create()
        assert s.is_valid(), s.errors
        assert s.validated_data["public_reply_limit"] == 200

    def test_accepts_explicit(self):
        s = self._create(public_reply_limit=5)
        assert s.is_valid(), s.errors
        assert s.validated_data["public_reply_limit"] == 5

    def test_rejects_negative(self):
        s = self._create(public_reply_limit=-1)
        assert not s.is_valid()
        assert "public_reply_limit" in s.errors


@pytest.mark.django_db
class TestSerializerReadOnly:
    def test_read_exposes_counter_and_reached(self, ig_connection):
        from apps.integrations.serializers import AutoDMCampaignSerializer

        campaign = _campaign(ig_connection, public_reply_limit=5, public_reply_posted_count=5)
        data = AutoDMCampaignSerializer(campaign).data
        assert data["public_reply_limit"] == 5
        assert data["public_reply_posted_count"] == 5
        assert data["public_reply_limit_reached"] is True

    def test_posted_count_is_read_only(self, ig_connection):
        from apps.integrations.serializers import AutoDMCampaignSerializer

        campaign = _campaign(ig_connection, public_reply_posted_count=3)
        ser = AutoDMCampaignSerializer(
            campaign, data={"public_reply_posted_count": 999}, partial=True
        )
        assert ser.is_valid(), ser.errors
        ser.save()
        campaign.refresh_from_db()
        assert campaign.public_reply_posted_count == 3  # 쓰기 무시
