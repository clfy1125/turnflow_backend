"""DM 링크 버튼 (web_url) 테스트.

커버리지:
  - 모델 get_link_buttons(): url 있을 때만 web_url 버튼 / 라벨 기본값 / 20자 캡
  - 메시징 서비스 _normalize_buttons + _build_message_payload: web_url 버튼 정규화/템플릿
  - 발송 경로(send_dm_task): 단순 DM(STANDALONE)·reward(REWARD)에 링크 버튼 첨부,
    opening+PENDING 은 게이트 postback 버튼(링크 아님), 링크 미설정 시 버튼 없음

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import InstagramMessagingService
from apps.workspace.models import Membership, Workspace

_SEND_OK = {"message_id": "mid_test_1", "recipient_id": "rcpt_1", "_raw": {}}


# ── 모델 helper (DB 불필요) ────────────────────────────────────


class TestGetLinkButtons:
    def test_returns_web_url_button_when_url_set(self):
        c = AutoDMCampaign(link_button_url="https://shop.test/x", link_button_label="받기")
        assert c.get_link_buttons() == [
            {"type": "web_url", "title": "받기", "url": "https://shop.test/x"}
        ]

    def test_none_when_url_empty(self):
        assert (
            AutoDMCampaign(link_button_url="", link_button_label="받기").get_link_buttons() is None
        )

    def test_label_defaults_when_empty(self):
        c = AutoDMCampaign(link_button_url="https://shop.test/x", link_button_label="")
        assert c.get_link_buttons()[0]["title"] == "자세히 보기"

    def test_label_capped_to_20(self):
        c = AutoDMCampaign(link_button_url="https://shop.test/x", link_button_label="가" * 50)
        assert len(c.get_link_buttons()[0]["title"]) <= 20


# ── 메시징 서비스 버튼 정규화 (DB 불필요) ──────────────────────


class TestNormalizeButtons:
    def test_web_url_button_kept(self):
        out = InstagramMessagingService._normalize_buttons(
            [{"type": "web_url", "title": "받기", "url": "https://x.io/a"}]
        )
        assert out == [{"type": "web_url", "title": "받기", "url": "https://x.io/a"}]

    def test_web_url_without_valid_url_dropped(self):
        out = InstagramMessagingService._normalize_buttons(
            [{"type": "web_url", "title": "받기", "url": "javascript:alert(1)"}]
        )
        assert out == []

    def test_postback_still_supported(self):
        out = InstagramMessagingService._normalize_buttons(
            [{"type": "postback", "title": "팔로우했어요", "payload": "fg:1"}]
        )
        assert out == [{"type": "postback", "title": "팔로우했어요", "payload": "fg:1"}]

    def test_build_payload_emits_web_url_template(self):
        msg = InstagramMessagingService._build_message_payload(
            text="안녕하세요!",
            buttons=[{"type": "web_url", "title": "받기", "url": "https://x.io/a"}],
        )
        btns = msg["attachment"]["payload"]["elements"][0]["buttons"]
        assert btns == [{"type": "web_url", "title": "받기", "url": "https://x.io/a"}]


# ── 발송 경로: 모드별 링크 버튼 첨부 (DB 필요) ─────────────────


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email="linkbtn@example.com", password="pw12345!", full_name="LinkBtn Tester"
    )
    ws = Workspace.objects.create(name="LinkBtn WS", slug="linkbtn-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        # 테스트별 유니크 계정 ID — 고정 ID 는 dm_pacer 버킷(Redis)을 테스트 간 공유해
        # 배치 실행 시 두 번째 발송부터 paced-defer 되는 플레이크를 만든다.
        external_account_id=f"ig_linkbtn_{uuid.uuid4().hex[:8]}",
        username="linkbtnuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_linkbtn"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "linkbtn-campaign",
        "message_template": "안녕하세요!",
        "opening_message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": "",  # user_id 발송 경로 사용
        "comment_text": "문의",
        "recipient_user_id": "igsid_buyer",
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.QUEUED,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.STANDALONE,
        "gate_status": SentDMLog.GateStatus.NONE,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


@pytest.fixture
def capture_send(monkeypatch):
    """send_dm_via_user_id / send_dm_via_comment 를 캡쳐 + verify 예약 무력화."""
    send = MagicMock(return_value=dict(_SEND_OK))
    monkeypatch.setattr(InstagramMessagingService, "send_dm_via_user_id", send)
    monkeypatch.setattr(InstagramMessagingService, "send_dm_via_comment", send)
    monkeypatch.setattr(tasks_mod.verify_dm_delivery, "apply_async", MagicMock())
    return send


def _run_send(log):
    tasks_mod.send_dm_task.apply(args=[str(log.id)])


class TestSendAttachesLinkButton:
    def test_standalone_simple_dm_gets_link_button(self, ig_connection, capture_send):
        campaign = _campaign(
            ig_connection,
            follow_gate_enabled=False,
            link_button_url="https://shop.test/x",
            link_button_label="받기",
        )
        log = _log(campaign, dm_kind=SentDMLog.DMKind.STANDALONE)
        _run_send(log)
        buttons = capture_send.call_args.kwargs["buttons"]
        assert buttons == [{"type": "web_url", "title": "받기", "url": "https://shop.test/x"}]

    def test_reward_dm_gets_link_button(self, ig_connection, capture_send):
        campaign = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            gate_verify_follow=False,
            reward_message_template="감사합니다!",
            link_button_url="https://shop.test/x",
            link_button_label="받기",
        )
        log = _log(
            campaign,
            dm_kind=SentDMLog.DMKind.REWARD,
            gate_status=SentDMLog.GateStatus.PASSED,
            message_sent="감사합니다!",
        )
        _run_send(log)
        buttons = capture_send.call_args.kwargs["buttons"]
        assert buttons == [{"type": "web_url", "title": "받기", "url": "https://shop.test/x"}]

    def test_opening_pending_gets_gate_postback_not_link(self, ig_connection, capture_send):
        campaign = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            gate_verify_follow=True,
            reward_message_template="감사합니다!",
            follow_gate_button_label="팔로우했어요",
            link_button_url="https://shop.test/x",  # 설정돼 있어도 opening 엔 안 붙어야 함
            link_button_label="받기",
        )
        log = _log(
            campaign,
            dm_kind=SentDMLog.DMKind.OPENING,
            gate_status=SentDMLog.GateStatus.PENDING,
        )
        _run_send(log)
        buttons = capture_send.call_args.kwargs["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["type"] == "postback"
        assert buttons[0]["title"] == "팔로우했어요"
        assert buttons[0]["payload"] == f"fg:{log.id}"

    def test_no_link_button_when_unset(self, ig_connection, capture_send):
        campaign = _campaign(ig_connection, follow_gate_enabled=False, link_button_url="")
        log = _log(campaign, dm_kind=SentDMLog.DMKind.STANDALONE)
        _run_send(log)
        assert capture_send.call_args.kwargs["buttons"] is None
