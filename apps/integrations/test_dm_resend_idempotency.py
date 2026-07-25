"""사설답장 재시도 멱등화 — 2534023 '도착 미확인' 오탐 방지 (2026-07-23).

배경(prod 실측, @ellisa_levelup):
    사설답장(첫 DM: comment_id 有 + parent 無)은 '댓글당 1회'라 비멱등이다. 1차 발송이 실제로
    도착했는데 Meta 가 HTTP 500/타임아웃을 반환하면(=성공 ack 유실, message_id 못 받음)
    재시도가 돌고, 그 재시도는 2534023("이미 답글")을 받아 '이미 도착한 DM'을 FAILED_NO_TRACE
    (도착 미확인)으로 잃는다. retry_count=2, message_id 빈값, api_response=2534023 로 확인됨.
    실제 IG 대화창엔 우리 문구 그대로의 DM 이 도착해 있었다.

가드(두 겹):
  P0.1 재시도 전 재확인 게이트(send_dm_task): 사설답장 + retry_count>0 이면 재발송 전
       Conversations 로 '이미 보냈는지' 확인 → True 면 도착 확정, 재POST 스킵.
  P0.2 1차 에러 검증 확대(except): 사설답장의 5xx/타임아웃·2534023 도 도착 확인 대상에
       포함(단, 명시적 rate-limit 4/17/32/368/613 은 '요청 거부→미전달'이라 제외).

NOTE(pytest-tests-prefix): test_*.py 라 자동수집됨.
NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언.
NOTE(override-settings-broken): settings 픽스처 사용.
"""

import uuid
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
        email=f"ri_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="RI"
    )
    ws = Workspace.objects.create(name="RI WS", slug=f"ri-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="riuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_ri"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "ri-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",  # 사설답장(첫 DM)
        "comment_text": "자격증",
        "recipient_user_id": f"rcpt_{uuid.uuid4().hex[:8]}",
        "recipient_username": "buyer",
        "message_sent": "플래너 자료를 확인해 보세요.",
        "status": SentDMLog.Status.QUEUED,
        "idempotency_key": uuid.uuid4().hex,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


def _run(log, *, send_side_effect=None, send_return=None, recent):
    """send_dm_task 를 실행하며 send_dm_via_comment / has_recent_* 를 mock. (send mock 반환)."""
    from apps.integrations.tasks import send_dm_task

    with (
        patch.object(
            InstagramMessagingService,
            "send_dm_via_comment",
            side_effect=send_side_effect,
            return_value=send_return,
        ) as send_mock,
        patch.object(
            InstagramMessagingService,
            "has_recent_message_to_recipient",
            return_value=recent,
        ) as recent_mock,
        patch("apps.integrations.tasks.verify_dm_delivery.apply_async"),
    ):
        res = send_dm_task.apply(args=[str(log.id)]).result
    log.refresh_from_db()
    return res, send_mock, recent_mock


# ───────────── P0.2: 1차 에러(5xx / 2534023) 검증 확대 ─────────────


class TestFirstAttemptDeliveredButError:
    def test_private_reply_500_delivered_promotes(self, ig_connection, settings):
        """사설답장 5xx(delivered-but-500) + recent=True → 도착 확정(재시도/실패 아님)."""
        settings.DM_GOVERNOR_ENABLED = False
        campaign = _campaign(ig_connection)
        log = _log(campaign)  # retry_count=0 (첫 시도)
        res, _, _ = _run(
            log,
            send_side_effect=DMTransientError("server error", status=500, code=-1),
            recent=True,
        )
        campaign.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED
        assert res["status"] == "delivered"
        assert campaign.total_sent == 1
        assert campaign.total_unconfirmed == 0

    def test_private_reply_2534023_recent_true_promotes(self, ig_connection, settings):
        """2534023('이미 답글') + recent=True → 우리 1차 성공으로 보고 도착 확정."""
        settings.DM_GOVERNOR_ENABLED = False
        campaign = _campaign(ig_connection)
        log = _log(campaign)
        res, _, _ = _run(
            log,
            send_side_effect=DMTransientError(
                "이미 답글이 있습니다", status=500, code=-1, subcode=2534023
            ),
            recent=True,
        )
        assert log.status == SentDMLog.Status.DELIVERED
        assert res["status"] == "delivered"

    def test_2534023_unconfirmed_terminates_no_loop(self, ig_connection, settings):
        """2534023 + recent=None(확인불가) → 무한루프 없이 FAILED_NO_TRACE 로 종결(보수)."""
        settings.DM_GOVERNOR_ENABLED = False
        campaign = _campaign(ig_connection)
        log = _log(campaign)
        res, _, _ = _run(
            log,
            send_side_effect=DMTransientError(
                "이미 답글이 있습니다", status=500, code=-1, subcode=2534023
            ),
            recent=None,
        )
        campaign.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_NO_TRACE
        assert campaign.total_unconfirmed == 1

    def test_rate_limit_code_not_treated_as_delivered(self, ig_connection, settings):
        """명시적 rate-limit(code 4)은 '요청 거부→미전달' → 도착 확인 안 하고 defer."""
        settings.DM_GOVERNOR_ENABLED = False
        campaign = _campaign(ig_connection)
        log = _log(campaign)  # retry_count=0
        res, _, recent_mock = _run(
            log,
            send_side_effect=DMTransientError("rate limited", status=400, code=4),
            recent=True,  # 패치돼 있어도 참조되면 안 됨
        )
        assert res["status"] == "deferred"
        assert log.status == SentDMLog.Status.QUEUED
        recent_mock.assert_not_called()  # code 4 는 검증 대상 아님


# ───────────── P0.1: 재시도 전 재확인 게이트 ─────────────


class TestVerifyBeforeResend:
    def test_retry_recent_true_skips_resend(self, ig_connection, settings):
        """retry_count>0 + recent=True → 재POST 없이 도착 확정(send 호출 안 됨)."""
        settings.DM_GOVERNOR_ENABLED = False
        campaign = _campaign(ig_connection)
        log = _log(campaign, retry_count=1)
        res, send_mock, _ = _run(
            log, send_return={"message_id": "x", "recipient_id": "r"}, recent=True
        )
        campaign.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED
        assert res["via"] == "verify_before_resend_conv_api"
        send_mock.assert_not_called()  # ★ 핵심: 재발송 안 함
        assert campaign.total_sent == 1

    def test_retry_recent_false_proceeds_to_send(self, ig_connection, settings):
        """retry_count>0 + recent=False(정말 미도착) → 정상 발송 진행(send 호출됨)."""
        settings.DM_GOVERNOR_ENABLED = False
        campaign = _campaign(ig_connection)
        log = _log(campaign, retry_count=1)
        res, send_mock, _ = _run(
            log, send_return={"message_id": "mid_ok", "recipient_id": "r"}, recent=False
        )
        send_mock.assert_called_once()
        assert log.status == SentDMLog.Status.ACCEPTED

    def test_first_attempt_skips_gate(self, ig_connection, settings):
        """retry_count=0 이면 게이트 미발동 → 곧장 발송(불필요한 API 호출 없음)."""
        settings.DM_GOVERNOR_ENABLED = False
        campaign = _campaign(ig_connection)
        log = _log(campaign)  # retry_count=0
        res, send_mock, recent_mock = _run(
            log, send_return={"message_id": "mid_ok", "recipient_id": "r"}, recent=True
        )
        send_mock.assert_called_once()
        recent_mock.assert_not_called()  # 게이트/에러검증 모두 미발동(성공 경로)
