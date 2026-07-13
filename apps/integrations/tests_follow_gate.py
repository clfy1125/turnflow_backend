"""버튼 게이트 (follow / button-only) 테스트.

커버리지:
  - button-only 모드(gate_verify_follow=False): 버튼 클릭 → 팔로우 검증 없이 즉시 reward
  - follow 모드(gate_verify_follow=True, 기본): 기존 동작 회귀 (검증 후 통과/재안내)
  - 멱등성/쿨다운/dm_kind 가드는 두 모드 공통 적용
  - 예약 발송 윈도우 가드는 button-only reward 에도 유효
  - opening DM enqueue 시 OPENING/PENDING 분류
  - 생성 시리얼라이저의 gate_verify_follow 노출/검증

NOTE(test-db-not-clean): 테스트 DB 가 깨끗하지 않을 수 있어, 내가 만든 캠페인/로그
기준으로만 단언한다.

NOTE: dev/CI 에 broker 가 없을 수 있어 _enqueue_reward_dm/_enqueue_follow_retry 가
부르는 send_dm_task.delay 는 모킹한다 (로그 생성/상태전이만 검증, 실제 발송은 별도).
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.serializers import AutoDMCampaignCreateSerializer
from apps.integrations.services import InstagramMessagingService
from apps.workspace.models import Membership, Workspace

IGSID = "igsid_buyer_001"


@pytest.fixture
def workspace_and_user(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email="gate@example.com", password="pw12345!", full_name="Gate Tester"
    )
    ws = Workspace.objects.create(name="Gate WS", slug="gate-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


@pytest.fixture
def ig_connection(workspace_and_user):
    ws, _ = workspace_and_user
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id="ig_gate_001",
        username="gateuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_gate"
    conn.save()
    return conn


@pytest.fixture
def no_real_send(monkeypatch):
    """send_dm_task.delay 를 무력화 (broker 없이 enqueue 경로만 검증)."""
    mock = MagicMock()
    monkeypatch.setattr(tasks_mod.send_dm_task, "delay", mock)
    return mock


@pytest.fixture
def follow_check(monkeypatch):
    """check_user_follow_business 를 MagicMock 으로 대체 (호출 여부/반환 제어)."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr(InstagramMessagingService, "check_user_follow_business", mock)
    return mock


def _make_campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "gate-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        "follow_gate_enabled": True,
        "reward_message_template": "보상 링크: https://example.com",
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _make_opening(campaign, **kwargs):
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "가격 문의",
        "recipient_user_id": IGSID,
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.DELIVERED,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.OPENING,
        "gate_status": SentDMLog.GateStatus.PENDING,
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


def _run_postback(opening, igsid=IGSID):
    res = tasks_mod.process_follow_gate_postback.apply(args=[str(opening.id), igsid])
    return res.result


# ===== button-only 모드 (gate_verify_follow=False) =====


class TestButtonGatePostback:
    def test_button_only_skips_profile_and_sends_reward(
        self, ig_connection, no_real_send, follow_check
    ):
        campaign = _make_campaign(ig_connection, gate_verify_follow=False)
        opening = _make_opening(campaign)

        result = _run_postback(opening)

        # 팔로우 검증 API 는 절대 호출되지 않아야 함
        assert follow_check.called is False
        assert result["status"] == "reward_enqueued"

        # REWARD child 로그 생성 + opening 은 PASSED
        reward = SentDMLog.objects.get(
            campaign=campaign, dm_kind=SentDMLog.DMKind.REWARD, parent_log=opening
        )
        assert reward.recipient_user_id == IGSID
        assert reward.message_sent == campaign.reward_message_template
        opening.refresh_from_db()
        assert opening.gate_status == SentDMLog.GateStatus.PASSED

        # 실제 발송은 send_dm_task 로 위임 (큐 등록만 확인)
        no_real_send.assert_called_once_with(str(reward.id))

    def test_button_only_idempotent_when_already_passed(
        self, ig_connection, no_real_send, follow_check
    ):
        campaign = _make_campaign(ig_connection, gate_verify_follow=False)
        opening = _make_opening(campaign, gate_status=SentDMLog.GateStatus.PASSED)

        result = _run_postback(opening)

        assert result["status"] == "already_passed"
        assert not SentDMLog.objects.filter(
            campaign=campaign, dm_kind=SentDMLog.DMKind.REWARD
        ).exists()
        assert follow_check.called is False

    def test_button_only_cooldown_blocks_double_tap(
        self, ig_connection, no_real_send, follow_check
    ):
        campaign = _make_campaign(ig_connection, gate_verify_follow=False)
        opening = _make_opening(campaign)
        # 직전 child 발송 (쿨다운 윈도우 내)
        _make_opening(
            campaign,
            parent_log=opening,
            dm_kind=SentDMLog.DMKind.REWARD,
            gate_status=SentDMLog.GateStatus.PASSED,
            status=SentDMLog.Status.QUEUED,
        )

        result = _run_postback(opening)

        assert result["status"] == "skipped"
        assert result["reason"] == "cooldown_30s"
        assert follow_check.called is False

    def test_non_opening_log_is_skipped(self, ig_connection, no_real_send, follow_check):
        campaign = _make_campaign(ig_connection, gate_verify_follow=False)
        standalone = _make_opening(
            campaign,
            dm_kind=SentDMLog.DMKind.STANDALONE,
            gate_status=SentDMLog.GateStatus.NONE,
        )

        result = _run_postback(standalone)

        assert result["status"] == "skipped"
        assert result["reason"] == "not_opening_dm"

    def test_button_only_reward_still_blocked_outside_schedule(
        self, ig_connection, no_real_send, follow_check
    ):
        """button-only reward 도 send_dm_task 의 예약 창 가드를 통과해야 발송된다."""
        from apps.integrations.tasks import send_dm_task

        now = timezone.now()
        campaign = _make_campaign(
            ig_connection,
            gate_verify_follow=False,
            scheduled_end_at=now - timedelta(minutes=1),
        )
        opening = _make_opening(campaign)

        # postback → reward 로그 생성 (send_dm_task.delay 는 모킹돼 실제 실행 안 됨)
        result = _run_postback(opening)
        reward_id = result["reward_log_id"]

        # 실제 발송 단계 실행 → 예약 창 밖이라 SKIPPED
        send_res = send_dm_task.apply(args=[reward_id])
        assert send_res.result["reason"] == "outside_schedule_window"
        reward = SentDMLog.objects.get(id=reward_id)
        assert reward.status == SentDMLog.Status.SKIPPED


# ===== follow 모드 회귀 (gate_verify_follow=True, 기본) =====


class TestFollowGateRegression:
    def test_follow_mode_verifies_and_rewards_when_following(
        self, ig_connection, no_real_send, follow_check
    ):
        follow_check.return_value = True
        campaign = _make_campaign(ig_connection)  # gate_verify_follow 기본 True
        opening = _make_opening(campaign)

        result = _run_postback(opening)

        assert follow_check.called is True
        assert result["status"] == "reward_enqueued"
        assert SentDMLog.objects.filter(
            campaign=campaign, dm_kind=SentDMLog.DMKind.REWARD, parent_log=opening
        ).exists()
        opening.refresh_from_db()
        assert opening.gate_status == SentDMLog.GateStatus.PASSED

    def test_follow_mode_retries_when_not_following(
        self, ig_connection, no_real_send, follow_check
    ):
        follow_check.return_value = False
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)

        result = _run_postback(opening)

        assert follow_check.called is True
        assert result["status"] == "retry_enqueued"
        # reward 는 없고, 재안내 OPENING/PENDING child 가 생성
        assert not SentDMLog.objects.filter(
            campaign=campaign, dm_kind=SentDMLog.DMKind.REWARD
        ).exists()
        retry = SentDMLog.objects.get(
            campaign=campaign,
            parent_log=opening,
            dm_kind=SentDMLog.DMKind.OPENING,
        )
        assert retry.gate_status == SentDMLog.GateStatus.PENDING
        # opening 은 여전히 통과 대기
        opening.refresh_from_db()
        assert opening.gate_status == SentDMLog.GateStatus.PENDING


# ===== 체인(재안내 경유) 게이트 통과 — 루트 마킹/중복 방어 =====


class TestGateChainRootMarking:
    """재안내(retry) 버튼으로 통과한 플로우의 루트 마킹/중복 방어.

    prod 실측(2026-07-07, 재안내 경유 통과 플로우): PASSED 가 클릭된 재안내 행에만
    붙고 루트 opening 은 PENDING 잔존 → 기본 로그 API 의 follow_passed=false
    (영구 '미전환' 표시) + 원래 opening 버튼 재클릭 시 reward 중복 발송 구멍.
    """

    def _pass_via_retry(self, campaign, follow_check):
        """opening → (미팔로우) 재안내 → (팔로우 후) 재안내 버튼으로 통과하는 공통 시나리오."""
        opening = _make_opening(campaign)
        follow_check.return_value = False
        res1 = _run_postback(opening)
        assert res1["status"] == "retry_enqueued"
        retry = SentDMLog.objects.get(id=res1["retry_log_id"])

        follow_check.return_value = True
        res2 = _run_postback(retry)  # 사용자는 '재안내 DM 의 버튼'을 누른다
        assert res2["status"] == "reward_enqueued"
        return opening, retry

    def test_pass_via_retry_marks_root_passed(self, ig_connection, no_real_send, follow_check):
        campaign = _make_campaign(ig_connection)
        opening, retry = self._pass_via_retry(campaign, follow_check)

        opening.refresh_from_db()
        retry.refresh_from_db()
        # 핵심 회귀 지점 — 기존 버그: 루트가 PENDING 잔존해 follow_passed=false 로 표시
        assert opening.gate_status == SentDMLog.GateStatus.PASSED
        assert retry.gate_status == SentDMLog.GateStatus.PASSED

    def test_root_button_after_retry_pass_is_noop(self, ig_connection, no_real_send, follow_check):
        campaign = _make_campaign(ig_connection)
        opening, _ = self._pass_via_retry(campaign, follow_check)

        res = _run_postback(opening)  # 통과 후 원래 opening 버튼 재클릭
        assert res["status"] == "already_passed"
        assert (
            SentDMLog.objects.filter(campaign=campaign, dm_kind=SentDMLog.DMKind.REWARD).count()
            == 1
        )

    def test_historical_pending_root_does_not_double_reward(
        self, ig_connection, no_real_send, follow_check
    ):
        """수정 배포 전 데이터 모양(루트 PENDING + 재안내 PASSED + reward 는 재안내에 부착)
        재현 — 루트 버튼 클릭이 두 번째 reward 를 만들지 않고 루트를 PASSED 로 복구해야 한다."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)  # 루트: 구버전 버그로 PENDING 잔존
        retry = _make_opening(campaign, parent_log=opening, gate_status=SentDMLog.GateStatus.PASSED)
        _make_opening(  # 기존 reward — 구버전 멱등키(클릭된 재안내 기준)로 발송된 상태
            campaign,
            parent_log=retry,
            dm_kind=SentDMLog.DMKind.REWARD,
            gate_status=SentDMLog.GateStatus.PASSED,
        )
        # postback 쿨다운(클릭 노드의 최근 자식) 회피 — 과거 플로우이므로 시간도 과거로
        SentDMLog.objects.filter(campaign=campaign).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )

        follow_check.return_value = True
        res = _run_postback(opening)

        assert res["status"] == "duplicate_reward"
        assert (
            SentDMLog.objects.filter(campaign=campaign, dm_kind=SentDMLog.DMKind.REWARD).count()
            == 1
        )
        opening.refresh_from_db()
        assert opening.gate_status == SentDMLog.GateStatus.PASSED  # 데이터 자가 복구

    def test_retry_throttled_within_30s_across_chain(
        self, ig_connection, no_real_send, follow_check
    ):
        """재안내 직후(30초 내) 새 재안내 버튼을 또 눌러도 재안내가 중복 발송되지 않는다.

        재안내마다 자기 id 의 새 버튼이 생겨 '클릭 노드의 자식' 쿨다운은 새 버튼에 무력
        — 플로우 전체 스로틀로 막는다. 통과(reward) 경로는 스로틀 대상이 아니다.
        """
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        follow_check.return_value = False
        res1 = _run_postback(opening)
        assert res1["status"] == "retry_enqueued"
        retry = SentDMLog.objects.get(id=res1["retry_log_id"])

        res2 = _run_postback(retry)  # 여전히 미팔로우 상태로 즉시 재클릭
        assert res2["status"] == "skipped"
        assert res2["reason"] == "retry_cooldown"
        assert (
            SentDMLog.objects.filter(
                campaign=campaign, parent_log__isnull=False, dm_kind=SentDMLog.DMKind.OPENING
            ).count()
            == 1
        )

    def test_follow_check_result_recorded(self, ig_connection, no_real_send, follow_check):
        """판정 근거(verification_log path=follow_check)가 클릭된 로그에 남는다."""
        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        follow_check.return_value = False

        _run_postback(opening)

        opening.refresh_from_db()
        entries = [e for e in opening.verification_log if e.get("path") == "follow_check"]
        assert len(entries) == 1
        assert entries[0]["result"] is False
        assert entries[0]["igsid"] == IGSID


# ===== flow_role 시리얼라이저 라벨 =====


class TestFlowRoleSerializer:
    def test_flow_role_labels(self, ig_connection):
        from apps.integrations.serializers import SentDMLogSerializer

        campaign = _make_campaign(ig_connection)
        opening = _make_opening(campaign)
        retry = _make_opening(campaign, parent_log=opening)
        reward = _make_opening(
            campaign,
            parent_log=retry,
            dm_kind=SentDMLog.DMKind.REWARD,
            gate_status=SentDMLog.GateStatus.PASSED,
        )
        standalone = _make_opening(
            campaign, dm_kind=SentDMLog.DMKind.STANDALONE, gate_status=SentDMLog.GateStatus.NONE
        )

        def role(log):
            return SentDMLogSerializer(log).data["flow_role"]

        assert role(opening) == "opening"
        assert role(retry) == "retry"  # dm_kind 는 opening 이지만 child → retry 로 라벨
        assert role(reward) == "reward"
        assert role(standalone) == "standalone"


# ===== opening DM enqueue 분류 =====


class TestEnqueueClassification:
    def test_button_only_campaign_enqueues_opening_pending(self, ig_connection, no_real_send):
        campaign = _make_campaign(ig_connection, gate_verify_follow=False)

        result = tasks_mod._enqueue_send_dm(
            campaign=campaign,
            comment_id=f"cmt_{uuid.uuid4().hex[:10]}",
            comment_text="가격 문의",
            from_user_id="commenter_001",
            from_username="commenter",
            webhook_payload={},
        )

        assert result["status"] == "enqueued"
        assert result["dm_kind"] == SentDMLog.DMKind.OPENING
        assert result["gate_status"] == SentDMLog.GateStatus.PENDING

    def test_gate_without_reward_falls_back_to_standalone(self, ig_connection, no_real_send):
        campaign = _make_campaign(
            ig_connection, gate_verify_follow=False, reward_message_template=""
        )

        result = tasks_mod._enqueue_send_dm(
            campaign=campaign,
            comment_id=f"cmt_{uuid.uuid4().hex[:10]}",
            comment_text="가격 문의",
            from_user_id="commenter_002",
            from_username="commenter",
            webhook_payload={},
        )

        assert result["dm_kind"] == SentDMLog.DMKind.STANDALONE
        assert result["gate_status"] == SentDMLog.GateStatus.NONE


# ===== 생성 시리얼라이저 =====


class TestCreateSerializerGateVerify:
    BASE = {
        "trigger_type": "any_media",
        "name": "btn-campaign",
        "opening_message_template": "안녕하세요!",
    }

    def test_button_only_valid_with_reward(self):
        s = AutoDMCampaignCreateSerializer(
            data={
                **self.BASE,
                "follow_gate_enabled": True,
                "gate_verify_follow": False,
                "reward_message_template": "보상 링크",
            }
        )
        assert s.is_valid(), s.errors
        assert s.validated_data["gate_verify_follow"] is False

    def test_gate_verify_defaults_true_when_omitted(self):
        s = AutoDMCampaignCreateSerializer(
            data={
                **self.BASE,
                "follow_gate_enabled": True,
                "reward_message_template": "보상 링크",
            }
        )
        assert s.is_valid(), s.errors
        assert s.validated_data["gate_verify_follow"] is True

    def test_gate_requires_reward_even_in_button_only(self):
        s = AutoDMCampaignCreateSerializer(
            data={
                **self.BASE,
                "follow_gate_enabled": True,
                "gate_verify_follow": False,
                # reward 누락
            }
        )
        assert not s.is_valid()
        assert "reward_message_template" in s.errors
