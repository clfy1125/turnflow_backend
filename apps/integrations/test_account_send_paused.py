"""계정 단위 발송 정지(U9 `account_send_paused`) — 표시 계층 회귀 테스트.

배경 (2026-08-26 CS #66027015):
  Meta 가 code=368 로 계정 발송을 제한 → 우리 서킷이 24h 쿨다운을 걸었는데, 그동안 쌓인
  대기 로그에는 **아무 표식이 없었다**(status=queued + next_retry_at 만 갱신). 그래서 화면은
  "발송 순서를 기다리고 있어요"(파란 정상 톤)로 떴고, 고객은 그걸 보고 **"댓글이 씹혔다"**
  고 판단해 전화했다(20분 통화로 해소).

여기서 고정하는 것:
  1. 정지 중 **대기** 로그만 U9 로 덮인다 — 종결된 실패는 절대 덮이지 않는다.
  2. 정지가 아니면 동작이 예전 그대로다(회귀 방지).
  3. 목록 배지(`status_group`)와 본문(`user_reason`)이 어긋나지 않는다 — 프론트 질문 3-(1).
  4. 계정 정지 조회가 **로그 수만큼 반복되지 않는다**(요청당 1회 메모).
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from apps.integrations import dm_status_groups as sg
from apps.integrations.dm_frontend_actions import build_frontend_action
from apps.integrations.dm_user_reasons import (
    NO_REASON,
    U_ACCOUNT_SEND_PAUSED,
    U_SEND_DELAYED,
    U_WINDOW_EXPIRED,
    user_reason_for,
)
from apps.integrations.models import AutoDMCampaign, DMAccountBlock, IGAccountConnection, SentDMLog
from apps.integrations.serializers import SentDMLogSerializer
from apps.workspace.models import Membership, Workspace

User = get_user_model()


# ===== 팩토리 =====


def _conn():
    user = User.objects.create_user(
        email=f"pause-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="pause-ws", slug=f"pz-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return conn


def _campaign(conn):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        name=f"c-{uuid.uuid4().hex[:6]}",
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        media_id=f"m_{uuid.uuid4().hex[:10]}",
        message_template="hello",
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _log(campaign, status=SentDMLog.Status.QUEUED, comment_id=None, **kw):
    # comment_id="" 는 user_id 경로(스토리 답장·게이트 리워드) = 창 24h.
    if comment_id is None:
        comment_id = f"c-{uuid.uuid4().hex[:8]}"
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=comment_id,
        recipient_user_id=f"r_{uuid.uuid4().hex[:8]}",
        message_sent="hi",
        idempotency_key=f"k-{uuid.uuid4().hex[:12]}",
        status=status,
        **kw,
    )


def _trip(conn, hours=21):
    """실제 트립 경로와 같은 두 곳(캐시+DB)에 정지를 심는다."""
    ext = str(conn.external_account_id)
    until = timezone.now() + timedelta(hours=hours)
    DMAccountBlock.objects.create(
        external_account_id=ext, cooldown_until=until, level=1, last_tripped_at=timezone.now()
    )
    cache.set(f"dm:ab:cooldown:{ext}", int(until.timestamp()), timeout=int(hours * 3600))
    return ext


# ===== 1. 판정 (순수 함수) =====


class TestUserReasonResolution:
    def test_waiting_statuses_become_account_send_paused(self):
        for status in ("queued", "submitting", "pending", "rate_limited"):
            assert (
                user_reason_for(status, account_send_paused=True) == U_ACCOUNT_SEND_PAUSED
            ), status

    def test_rate_limited_is_overridden_by_account_pause(self):
        """개별 지연(U8)보다 계정 전체 정지(U9)가 지배적 사실이다."""
        assert user_reason_for("rate_limited", "613") == U_SEND_DELAYED
        assert (
            user_reason_for("rate_limited", "613", account_send_paused=True)
            == U_ACCOUNT_SEND_PAUSED
        )

    @pytest.mark.parametrize(
        "status,code,subcode",
        [
            ("failed_window", "10", "2534022"),
            ("failed_param", "100", "2534014"),
            ("failed_token", "190", ""),
            ("skipped", "", ""),
            ("delivered", "", ""),
            ("read", "", ""),
        ],
    )
    def test_terminal_statuses_never_overridden(self, status, code, subcode):
        """확정된 건을 '정지 중'으로 덮으면 기다리면 갈 거라고 오해한다."""
        paused = user_reason_for(status, code, subcode, "", True)
        assert paused != U_ACCOUNT_SEND_PAUSED
        assert paused == user_reason_for(status, code, subcode, "")

    def test_default_is_unchanged(self):
        """플래그를 안 주면 예전 그대로 (회귀 방지)."""
        assert user_reason_for("queued") == NO_REASON
        assert user_reason_for("failed_window") == U_WINDOW_EXPIRED


# ===== 2. 표시 (문구·심각도) =====


class TestFrontendAction:
    def test_paused_queued_gets_reason_and_warning(self):
        act = build_frontend_action("queued", account_send_paused=True)
        assert act["user_reason"] == U_ACCOUNT_SEND_PAUSED
        # 파란 '정상' 톤이었던 것이 경고 톤으로 — 이게 이번 CS 의 핵심 수정.
        assert act["severity"] == "warning"
        assert act["type"] == "wait"  # 실패가 아니라 대기다
        assert act["title"] and act["cause"] and act["next_step"]

    def test_not_paused_keeps_info_tone(self):
        act = build_frontend_action("queued")
        assert act["user_reason"] == ""
        assert act["severity"] == "info"

    def test_copy_does_not_hardcode_duration(self):
        """쿨다운은 반복 제한 시 24h→48h→…7d 로 늘어난다. 숫자를 박으면 거짓이 된다."""
        act = build_frontend_action("queued", account_send_paused=True)
        blob = f"{act['title']} {act['cause']} {act['next_step']}"
        for forbidden in ("24시간", "24h", "48시간", "7일"):
            assert forbidden not in blob, forbidden

    def test_no_internal_jargon(self):
        act = build_frontend_action("queued", account_send_paused=True)
        blob = f"{act['title']} {act['cause']} {act['next_step']}".lower()
        for word in ("action block", "쿨다운", "meta ", "368", "서킷", "대기열"):
            assert word not in blob, word


# ===== 3. 배지(status_group) 와 본문이 어긋나지 않는가 — 프론트 질문 3-(1) =====


class TestStatusGroupStaysWaiting:
    def test_paused_logs_are_waiting_not_attention(self):
        """정지 대기 건이 attention 이면 화면에 '전송 실패 N' 으로 집계돼,
        안내 문구가 무슨 말을 해도 '역시 씹혔네'가 된다. waiting 이어야 한다."""
        for status in ("queued", "submitting", "rate_limited"):
            assert sg.status_group(status) == sg.WAITING, status
        assert sg.GROUP_DISPLAY[sg.WAITING] == "대기중"


# ===== 4. 직렬화 (실제 응답 · N+1) =====


@pytest.mark.django_db
class TestSerializer:
    def test_serializer_marks_paused_account(self):
        conn = _conn()
        camp = _campaign(conn)
        log = _log(camp)
        assert SentDMLogSerializer(log).data["frontend_action"]["user_reason"] == ""

        _trip(conn)
        data = SentDMLogSerializer(log).data
        assert data["frontend_action"]["user_reason"] == U_ACCOUNT_SEND_PAUSED
        assert data["frontend_action"]["severity"] == "warning"
        # 배지는 그대로 '대기중' — 실패로 집계되지 않는다.
        assert data["status_group"] == sg.WAITING

    def test_release_clears_the_marker(self):
        """쿨다운이 풀리면 표식이 저절로 사라진다(쓰기 경로 표식이 아니라 계정 상태라서)."""
        conn = _conn()
        log = _log(_campaign(conn))
        ext = _trip(conn)
        assert SentDMLogSerializer(log).data["frontend_action"]["user_reason"]

        cache.delete(f"dm:ab:cooldown:{ext}")
        DMAccountBlock.objects.filter(external_account_id=ext).update(cooldown_until=timezone.now())
        assert SentDMLogSerializer(log).data["frontend_action"]["user_reason"] == ""

    def test_lookup_is_memoized_per_account(self):
        """목록 20건이면 캐시 조회도 20번이 된다 — 계정 단위 사실이므로 1번이어야 한다."""
        conn = _conn()
        camp = _campaign(conn)
        logs = [_log(camp) for _ in range(8)]
        _trip(conn)

        with patch(
            "apps.integrations.rate_governor.action_block_cooldown_remaining", return_value=999
        ) as spy:
            data = SentDMLogSerializer(logs, many=True).data

        assert spy.call_count == 1, f"계정당 1회여야 하는데 {spy.call_count}회 조회했습니다"
        assert all(d["frontend_action"]["user_reason"] == U_ACCOUNT_SEND_PAUSED for d in data)

    def test_queue_state_exposes_window_risk(self):
        """'제한 풀리면 N명에게 발송' 문구가 지킬 수 있는 약속이 되도록 위험분을 노출한다."""
        from apps.integrations.queue_state import build_queue_state_payload

        conn = _conn()
        camp = _campaign(conn)
        # 2번째 DM(리워드, 창 24h)이 4시간 전부터 대기 중 → 21h 뒤 재개하면 이미 만료.
        old_reward = _log(camp, comment_id="", dm_kind=SentDMLog.DMKind.REWARD)
        SentDMLog.objects.filter(id=old_reward.id).update(
            created_at=timezone.now() - timedelta(hours=4)
        )
        # 첫 DM(댓글, 창 7일)은 같은 정지를 견딘다.
        _log(camp)
        _trip(conn, hours=21)

        payload = build_queue_state_payload(conn, camp)
        risk = payload["waiting_window_risk"]
        assert payload["blocking_reason"] == "action_block_cooldown"
        assert risk["events"] == 1, "만료 예정 리워드 1건이 잡혀야 한다"
        assert risk["followup_events"] == 1, "2번째 DM 축으로 분리돼야 한다"
        # ★ 사람 축은 루트 DM 기준(people_rollup 과 같은 모수)이라 리워드는 안 센다 —
        #   섞으면 `people.waiting - risk.people` 이 음수가 될 수 있다.
        assert risk["people"] == 0
        assert payload["people"]["waiting"] == 1  # 첫 DM 대기자 1명(리워드는 사람 축 밖)

    def test_root_dm_window_risk_is_subtractable_from_people_waiting(self):
        """첫 DM 이 창을 넘길 사람은 `people.waiting` 에서 그대로 뺄 수 있어야 한다."""
        from apps.integrations.queue_state import build_queue_state_payload

        conn = _conn()
        camp = _campaign(conn)
        doomed = _log(camp)  # 댓글 = 창 7일
        SentDMLog.objects.filter(id=doomed.id).update(
            created_at=timezone.now() - timedelta(days=6, hours=20)
        )
        _log(camp)  # 방금 들어온 건 — 견딘다
        _trip(conn, hours=21)

        payload = build_queue_state_payload(conn, camp)
        risk = payload["waiting_window_risk"]
        assert risk["people"] == 1 and risk["followup_events"] == 0
        assert payload["people"]["waiting"] - risk["people"] == 1

    def test_no_window_risk_without_pause(self):
        """정지가 아니면 horizon 이 0 이라 방금 들어온 건은 위험으로 잡히지 않는다."""
        from apps.integrations.queue_state import build_queue_state_payload

        conn = _conn()
        camp = _campaign(conn)
        _log(camp, comment_id="", dm_kind=SentDMLog.DMKind.REWARD)
        payload = build_queue_state_payload(conn, camp)
        assert payload["blocking_reason"] is None
        assert payload["waiting_window_risk"] == {
            "people": 0,
            "events": 0,
            "followup_events": 0,
            "horizon_s": 0,
        }

    def test_window_risk_uses_same_constants_as_the_age_guard(self):
        """표시(예고)와 실제 종결이 갈리면 안 된다 — 창 상수는 tasks 단일 소스."""
        from apps.integrations.tasks import (
            COMMENT_MESSAGING_WINDOW,
            USER_ID_MESSAGING_WINDOW,
            _messaging_window,
        )

        conn = _conn()
        camp = _campaign(conn)
        assert _messaging_window(_log(camp)) == COMMENT_MESSAGING_WINDOW
        assert _messaging_window(_log(camp, comment_id="")) == USER_ID_MESSAGING_WINDOW
        # 2번째 DM 의 창과 기본 쿨다운이 같다 — 이번 사건의 구조적 원인.
        from apps.integrations.rate_governor import _ACTION_BLOCK_BASE_HOURS

        assert USER_ID_MESSAGING_WINDOW == timedelta(hours=_ACTION_BLOCK_BASE_HOURS)

    def test_terminal_log_on_paused_account_keeps_its_reason(self):
        """같은 계정이 정지 중이어도 이미 실패한 건은 자기 사유를 유지한다."""
        conn = _conn()
        camp = _campaign(conn)
        failed = _log(
            camp, status=SentDMLog.Status.FAILED_WINDOW, error_code="10", error_subcode="2534022"
        )
        _trip(conn)
        data = SentDMLogSerializer(failed).data
        assert data["frontend_action"]["user_reason"] == U_WINDOW_EXPIRED
