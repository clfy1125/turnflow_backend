"""게이트 reward '창 미개방'(Meta 10/2534022) 자동 재시도 + 재탭 복구 테스트 (2026-08-07).

배경(prod 실측 6건):
    팔로우 게이트 버튼을 누른 **직후** 나가는 reward DM 이 Meta 로부터 code=10 /
    subcode=2534022 를 받고 영구 실패했다. Conversations API 대조 결과, Meta 가 유저의
    버튼 탭을 '비즈니스 발신'으로 오귀속(4/6)하거나 창 상태 전파가 늦어(2/6) 24h 창이
    아직 안 열려 있었다. 유저가 버튼을 다시 누르면 그때는 정상 귀속되는데, 우리 코드가
    gate_status=PASSED / reward 로그 존재를 이유로 재발송을 영구 차단해 6명 전원이
    리워드를 못 받았다.

커버리지:
    - _defer_or_fail: Meta 창 오류는 종결이 아니라 짧은 재시도로 defer
    - 우리 내부 창 만료(error_code="")·오프닝 사설답장은 재시도 대상 아님(회귀 방지)
    - 재탭 시 FAILED_WINDOW reward 를 같은 row 로 복구
    - ★ 연타 방어: 쿨다운·CAS·상한으로 이중 발송 불가
    - FAILED_NO_TRACE/발송중 reward 는 절대 되살리지 않음(중복 DM 방지)

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언한다.
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations.dm_exceptions import DMWindowExpiredError
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import InstagramMessagingService
from apps.workspace.models import Membership, Workspace

IGSID = "igsid_window_repair_001"


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"win-{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="W"
    )
    slug = f"win-ws-{uuid.uuid4().hex[:8]}"
    ws = Workspace.objects.create(name="Win WS", slug=slug, owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_win_{uuid.uuid4().hex[:8]}",
        username=f"winuser{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_win"
    conn.save()
    return conn


@pytest.fixture
def no_real_send(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(tasks_mod.send_dm_task, "delay", mock)
    return mock


@pytest.fixture
def follow_check(monkeypatch):
    mock = MagicMock(return_value=True)
    monkeypatch.setattr(InstagramMessagingService, "check_user_follow_business", mock)
    return mock


def _make_campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "win-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        "follow_gate_enabled": True,
        "gate_verify_follow": False,  # 프로필 API 호출 없이 즉시 reward
        "reward_message_template": "보상 링크: https://example.com",
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _make_opening(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "설치",
        "recipient_user_id": IGSID,
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.DELIVERED,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.OPENING,
        "gate_status": SentDMLog.GateStatus.PASSED,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


def _make_reward(campaign, opening, **kwargs):
    """'창 미개방'으로 실패해 있는 reward child (= prod 실측 상태)."""
    defaults = {
        "campaign": campaign,
        "comment_id": "",  # reward 는 user_id 경로(24h 창)
        "comment_text": "",
        "recipient_user_id": IGSID,
        "recipient_username": "buyer",
        "message_sent": campaign.reward_message_template,
        "status": SentDMLog.Status.FAILED_WINDOW,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.REWARD,
        "gate_status": SentDMLog.GateStatus.PASSED,
        "parent_log": opening,
        "error_code": "10",
        "error_subcode": "2534022",
        "error_message": "이 메시지는 허용되는 창 외부로 전송됩니다. | http=403",
        "retry_count": 1,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


def _run_postback(opening, igsid=IGSID):
    return tasks_mod.process_follow_gate_postback.apply(args=[str(opening.id), igsid]).result


def _clear_cooldown(reward):
    cache.delete(f"gate_reward_repair:{reward.id}")


# ===== 1. _defer_or_fail — Meta 창 오류는 종결 대신 짧은 재시도 =====


class TestGateWindowAutoRetry:
    def test_meta_window_error_on_reward_defers_instead_of_terminating(
        self, ig_connection, no_real_send
    ):
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening, status=SentDMLog.Status.SUBMITTING, retry_count=0)

        exc = DMWindowExpiredError("outside allowed window", status=403, code=10, subcode=2534022)
        result = tasks_mod._defer_or_fail(reward, campaign, ig_connection, exc)

        assert result["status"] == "deferred"
        assert result["reason"] == "gate_window_not_open"
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.QUEUED
        assert reward.next_retry_at is not None
        # 첫 재시도는 20초 뒤 (GATE_REWARD_WINDOW_BACKOFFS[0])
        delta = (reward.next_retry_at - timezone.now()).total_seconds()
        assert 5 < delta <= 20

    def test_retry_budget_is_bounded(self, ig_connection, no_real_send):
        """백오프를 다 쓰면 기존대로 종결 — 무한 재시도 금지."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        n = len(tasks_mod.GATE_REWARD_WINDOW_BACKOFFS)
        reward = _make_reward(campaign, opening, status=SentDMLog.Status.SUBMITTING, retry_count=n)

        exc = DMWindowExpiredError("outside window", status=403, code=10, subcode=2534022)
        result = tasks_mod._defer_or_fail(reward, campaign, ig_connection, exc)

        assert result["status"] == SentDMLog.Status.FAILED_WINDOW
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.FAILED_WINDOW

    def test_opening_private_reply_window_error_still_terminal(self, ig_connection, no_real_send):
        """오프닝(사설답장·7일 창)은 재시도 대상이 아니다 — 회귀 방지."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign, status=SentDMLog.Status.SUBMITTING, retry_count=0)

        exc = DMWindowExpiredError("outside window", status=403, code=10, subcode=2534022)
        result = tasks_mod._defer_or_fail(opening, campaign, ig_connection, exc)

        assert result["status"] == SentDMLog.Status.FAILED_WINDOW

    def test_internal_window_expiry_is_not_retried(self, ig_connection, no_real_send):
        """우리 age 가드가 찍는 창 만료(code 없음)는 진짜 시간 경과 → 재시도 금지."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening, status=SentDMLog.Status.SUBMITTING, retry_count=0)

        exc = DMWindowExpiredError("window expired", status=None, code=None, subcode=None)
        result = tasks_mod._defer_or_fail(reward, campaign, ig_connection, exc)

        assert result["status"] == SentDMLog.Status.FAILED_WINDOW


# ===== 2. 재탭 복구 =====


class TestRetapRepair:
    def test_retap_repairs_failed_window_reward(self, ig_connection, no_real_send, follow_check):
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening)
        _clear_cooldown(reward)

        result = _run_postback(opening)

        assert result["status"] == "reward_repaired"
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.QUEUED
        assert reward.error_code == ""
        assert reward.error_subcode == ""
        no_real_send.assert_called_once_with(str(reward.id))

    def test_repair_stamps_window_anchor(self, ig_connection, no_real_send, follow_check):
        """복구 흔적이 창 기준 시각이 돼야 며칠 뒤 재탭도 age 가드를 통과한다."""
        campaign = _make_campaign(ig_connection)
        # 8일 전에 생성된 reward (실측 sayafiit 케이스)
        old = timezone.now() - timedelta(days=8)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening)
        SentDMLog.objects.filter(pk=reward.pk).update(created_at=old)
        reward.refresh_from_db()
        assert tasks_mod._window_anchor(reward) == reward.created_at
        _clear_cooldown(reward)

        _run_postback(opening)

        reward.refresh_from_db()
        anchor = tasks_mod._window_anchor(reward)
        assert anchor > reward.created_at
        # 24h 창 안으로 들어와야 send_dm_task 진입부 가드를 통과
        assert (timezone.now() - anchor) < tasks_mod._messaging_window(reward)


# ===== 3. ★ 연타 방어 =====


class TestRapidRetapSafety:
    def test_burst_retap_sends_only_once(self, ig_connection, no_real_send, follow_check):
        """버튼 연타 — 첫 탭만 재발송한다.

        2·3번째 탭은 reward 가 이미 QUEUED 라 '되살릴 대상 없음'으로 걸러진다
        (쿨다운까지 갈 것도 없이 상태 가드에서 끝난다).
        """
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening)
        _clear_cooldown(reward)

        first = _run_postback(opening)
        second = _run_postback(opening)
        third = _run_postback(opening)

        assert first["status"] == "reward_repaired"
        assert second["status"] == "already_passed"
        assert third["status"] == "already_passed"
        assert no_real_send.call_count == 1

    def test_cooldown_throttles_repeated_failure_repair_loop(
        self, ig_connection, no_real_send, follow_check
    ):
        """복구 → 또 실패 → 재탭 루프를 쿨다운이 조인다.

        상태 가드만으로는 '복구했는데 또 창 미개방으로 실패' 한 뒤의 재탭을 못 막는다.
        버튼을 계속 눌러도 Meta 호출이 쿨다운 간격으로만 나가야 한다.
        """
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening)
        _clear_cooldown(reward)

        first = _run_postback(opening)
        assert first["status"] == "reward_repaired"

        # send_dm_task 가 또 창 미개방으로 실패한 상황을 재현 (쿨다운은 그대로 살아있음)
        SentDMLog.objects.filter(pk=reward.pk).update(status=SentDMLog.Status.FAILED_WINDOW)

        second = _run_postback(opening)

        assert second["status"] == "repair_cooldown"
        assert no_real_send.call_count == 1
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.FAILED_WINDOW

    def test_cas_blocks_second_repair_even_without_cooldown(
        self, ig_connection, no_real_send, follow_check
    ):
        """쿨다운이 없어도 조건부 UPDATE(CAS)가 이중 dispatch 를 막는다."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening)

        _clear_cooldown(reward)
        first = _run_postback(opening)
        _clear_cooldown(reward)  # 쿨다운을 인위적으로 무력화
        second = _run_postback(opening)

        assert first["status"] == "reward_repaired"
        # 이미 QUEUED 라 FAILED_WINDOW 조건이 안 맞음 → 재발송 없음
        assert second["status"] in ("repair_raced", "duplicate_reward", "already_passed")
        assert no_real_send.call_count == 1

    def test_inflight_reward_is_not_disturbed(self, ig_connection, no_real_send, follow_check):
        """자동 재시도 대기(QUEUED) 중인 reward 는 탭이 와도 건드리지 않는다."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(
            campaign,
            opening,
            status=SentDMLog.Status.QUEUED,
            next_retry_at=timezone.now() + timedelta(seconds=60),
        )
        _clear_cooldown(reward)

        result = _run_postback(opening)

        assert result["status"] == "already_passed"
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.QUEUED
        assert reward.next_retry_at is not None  # 예약이 리셋되지 않아야 함
        no_real_send.assert_not_called()

    def test_no_trace_reward_is_never_revived(self, ig_connection, no_real_send, follow_check):
        """FAILED_NO_TRACE 는 '도착했을 수도 있음' → 되살리면 중복 DM."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening, status=SentDMLog.Status.FAILED_NO_TRACE)
        _clear_cooldown(reward)

        result = _run_postback(opening)

        assert result["status"] == "already_passed"
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.FAILED_NO_TRACE
        no_real_send.assert_not_called()

    def test_delivered_reward_is_never_resent(self, ig_connection, no_real_send, follow_check):
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        _make_reward(campaign, opening, status=SentDMLog.Status.DELIVERED)

        result = _run_postback(opening)

        assert result["status"] == "already_passed"
        no_real_send.assert_not_called()

    def test_repair_attempts_are_capped(self, ig_connection, no_real_send, follow_check):
        """상한을 넘기면 더 이상 Meta 를 부르지 않는다."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening, retry_count=tasks_mod.GATE_REWARD_MAX_ATTEMPTS)
        _clear_cooldown(reward)

        result = _run_postback(opening)

        assert result["status"] == "repair_exhausted"
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.FAILED_WINDOW
        no_real_send.assert_not_called()

    def test_repair_skipped_when_connection_dead(self, ig_connection, no_real_send, follow_check):
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        reward = _make_reward(campaign, opening)
        _clear_cooldown(reward)
        IGAccountConnection.objects.filter(pk=ig_connection.pk).update(
            status=IGAccountConnection.Status.ERROR
        )

        result = _run_postback(opening)

        assert result["status"] == "already_passed"
        reward.refresh_from_db()
        assert reward.status == SentDMLog.Status.FAILED_WINDOW
        no_real_send.assert_not_called()
