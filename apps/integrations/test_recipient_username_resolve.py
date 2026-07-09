"""수신자 username 지연(lazy) 해석 + 폴백 표기 테스트.

Story 답장 트리거는 messaging 웹훅에 username 이 없어 SentDMLog.recipient_username 이
빈 값으로 저장된다. 수신자 목록 열람 시 IG User Profile API 로 지연 해석하고, 실패/미해석
시 응답 계층에서 user_{IGSID} 폴백을 표기한다(DB 컬럼은 빈 채 유지).

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언한다.
NOTE: 캐시(dm:uname:*)는 실제 Redis 를 쓰므로 IGSID/campaign_id 를 매 테스트 uuid 로
      유니크하게 만들어 상태 오염을 피한다.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations import verification_views as vv
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.serializers import SentDMLogSerializer
from apps.integrations.services import InstagramMessagingService
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"uname_{uuid.uuid4().hex[:8]}@example.com",
        password="pw12345!",
        full_name="Uname Tester",
    )
    ws = Workspace.objects.create(
        name="Uname WS", slug=f"uname-ws-{uuid.uuid4().hex[:8]}", owner=user
    )
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:8]}",
        username="bizuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return conn


def _story_campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.STORY_REPLY,
        "name": "story-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, igsid, username="", **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": "",
        "comment_text": "스토리 답장",
        "recipient_user_id": igsid,
        "recipient_username": username,
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.DELIVERED,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.STANDALONE,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


# ===== 서비스: resolve_username =====


class TestResolveUsername:
    def test_returns_username_and_caches(self, monkeypatch):
        igsid = f"igsid_{uuid.uuid4().hex}"
        monkeypatch.setattr(InstagramMessagingService, "_is_mock", MagicMock(return_value=False))
        gp = MagicMock(return_value={"username": "realhandle", "name": "Real"})
        monkeypatch.setattr(InstagramMessagingService, "get_user_profile", gp)

        assert InstagramMessagingService.resolve_username(igsid, "tok") == "realhandle"
        # 두 번째 호출은 캐시 히트 → get_user_profile 재호출 없음
        assert InstagramMessagingService.resolve_username(igsid, "tok") == "realhandle"
        assert gp.call_count == 1

    def test_failure_returns_empty(self, monkeypatch):
        igsid = f"igsid_{uuid.uuid4().hex}"
        monkeypatch.setattr(InstagramMessagingService, "_is_mock", MagicMock(return_value=False))
        monkeypatch.setattr(
            InstagramMessagingService,
            "get_user_profile",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        # best-effort: 예외를 삼키고 "" 반환 (표시/발송 절대 미차단)
        assert InstagramMessagingService.resolve_username(igsid, "tok") == ""

    def test_mock_mode_returns_fake_handle(self, monkeypatch):
        monkeypatch.setattr(InstagramMessagingService, "_is_mock", MagicMock(return_value=True))
        gp = MagicMock()
        monkeypatch.setattr(InstagramMessagingService, "get_user_profile", gp)
        out = InstagramMessagingService.resolve_username("778309888048688", "tok")
        assert out.startswith("mock_user_")
        assert gp.call_count == 0  # mock 모드는 실제 API 미호출


# ===== 지연 해석 Celery 태스크 =====


class TestLazyResolveTask:
    def test_fills_empty_username(self, ig_connection, monkeypatch):
        campaign = _story_campaign(ig_connection)
        igsid = f"igsid_{uuid.uuid4().hex}"
        log = _log(campaign, igsid, username="")
        monkeypatch.setattr(
            InstagramMessagingService, "resolve_username", MagicMock(return_value="resolved_handle")
        )

        res = tasks_mod.resolve_recipient_usernames_for_campaign.apply(
            args=[str(campaign.id), [igsid]]
        ).result

        assert res["status"] == "done"
        assert res["resolved"] == 1
        log.refresh_from_db()
        assert log.recipient_username == "resolved_handle"

    def test_does_not_overwrite_existing(self, ig_connection, monkeypatch):
        campaign = _story_campaign(ig_connection)
        igsid = f"igsid_{uuid.uuid4().hex}"
        log = _log(campaign, igsid, username="already")
        monkeypatch.setattr(
            InstagramMessagingService, "resolve_username", MagicMock(return_value="new_handle")
        )

        tasks_mod.resolve_recipient_usernames_for_campaign.apply(args=[str(campaign.id), [igsid]])

        log.refresh_from_db()
        assert log.recipient_username == "already"

    def test_resolution_failure_leaves_empty(self, ig_connection, monkeypatch):
        campaign = _story_campaign(ig_connection)
        igsid = f"igsid_{uuid.uuid4().hex}"
        log = _log(campaign, igsid, username="")
        monkeypatch.setattr(
            InstagramMessagingService, "resolve_username", MagicMock(return_value="")
        )

        res = tasks_mod.resolve_recipient_usernames_for_campaign.apply(
            args=[str(campaign.id), [igsid]]
        ).result

        assert res["resolved"] == 0
        log.refresh_from_db()
        assert log.recipient_username == ""


# ===== 시리얼라이저 폴백 (개별 로그 엔드포인트) =====


class TestSerializerFallback:
    def test_empty_username_falls_back(self, ig_connection):
        campaign = _story_campaign(ig_connection)
        igsid = "778309888048688"
        log = _log(campaign, igsid, username="")
        data = SentDMLogSerializer(log).data
        assert data["recipient_username"] == f"user_{igsid}"

    def test_real_username_unchanged(self, ig_connection):
        campaign = _story_campaign(ig_connection)
        log = _log(campaign, f"igsid_{uuid.uuid4().hex}", username="realguy")
        data = SentDMLogSerializer(log).data
        assert data["recipient_username"] == "realguy"


# ===== 뷰 헬퍼: 표시 폴백 + 지연 해석 enqueue =====


class TestViewHelpers:
    def test_display_fallback(self):
        assert vv._recipient_username_display("778", "") == "user_778"
        assert vv._recipient_username_display("778", "real") == "real"

    def test_enqueue_dedup(self, monkeypatch):
        delay = MagicMock()
        monkeypatch.setattr(tasks_mod.resolve_recipient_usernames_for_campaign, "delay", delay)
        cid = uuid.uuid4()
        igsid = f"igsid_{uuid.uuid4().hex}"

        vv._maybe_enqueue_username_resolution(cid, [igsid])
        assert delay.call_count == 1
        # 60s pending 플래그로 재-enqueue 억제
        vv._maybe_enqueue_username_resolution(cid, [igsid])
        assert delay.call_count == 1

    def test_toggle_off(self, monkeypatch, settings):
        settings.DM_RESOLVE_RECIPIENT_USERNAME = False
        delay = MagicMock()
        monkeypatch.setattr(tasks_mod.resolve_recipient_usernames_for_campaign, "delay", delay)
        vv._maybe_enqueue_username_resolution(uuid.uuid4(), [f"igsid_{uuid.uuid4().hex}"])
        assert delay.call_count == 0
