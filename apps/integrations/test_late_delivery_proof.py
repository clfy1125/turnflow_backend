"""뒤늦은 도착 증거로 '증거 없는 발송'을 도착 승격 (2026-08-31).

배경(prod 실측):
    사설답장(첫 DM)은 '댓글당 1회'라 비멱등이다. 1차 POST 가 **실제로 도착했는데** Meta 가
    500 을 반환하면(성공 ack 유실) message_id 를 못 받고, 재시도는 2534023("이미 답글")을
    받아 **도착한 DM 이 '도착 미확인'으로** 남는다. 재시도 직전 Conversations 재확인 게이트가
    있지만 백오프가 짧으면 인덱싱 전이라 not-found 를 받는다 → 2026-08-10~08-28 6건 재발.

    결정적 관측: mini_ai_ 08-28 건은 로그 생성 12초 뒤 **echo 웹훅이 실제로 도착**해 있었고
    (EventInbox 2건), 16초 뒤 유저가 게이트 버튼을 눌러 reward 가 delivered 됐다. 즉 도착
    증거가 두 번 왔는데 둘 다 버려졌다 — echo 는 mid·ACCEPTED 어느 쪽에도 안 붙어서,
    버튼 탭은 아무도 오프닝 상태를 다시 안 봐서.

커버리지:
    - echo(mid 미매칭) → QUEUED/FAILED_NO_TRACE 발송 승격 + 카운터 보정
    - echo 승격 후 예약된 재시도는 send_dm_task 진입 가드에 걸려 재POST 하지 않음
    - 미발송(submitted_at 없음)·창 밖·retry_count=0·mid 있는 행은 **건드리지 않음**
    - 게이트 버튼 탭 → 오프닝 승격 (Graph 호출 0회)
    - 기존 echo 경로(mid 직매칭 / ACCEPTED 폴백) 회귀 없음
    - _confirm_delivered_via_conv 가 이미 도착한 로그를 두 번 세지 않음

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언한다.
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import InstagramMessagingService
from apps.workspace.models import Membership, Workspace

RECIPIENT = "igsid_late_proof_001"


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"lp-{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="LP"
    )
    ws = Workspace.objects.create(name="LP WS", slug=f"lp-ws-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_lp_{uuid.uuid4().hex[:8]}",
        username=f"lpuser{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_lp"
    conn.save()
    return conn


@pytest.fixture
def no_real_send(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(tasks_mod.send_dm_task, "delay", mock)
    return mock


@pytest.fixture
def no_public_reply(monkeypatch):
    """공개 답글 예약은 별 관심사 — 태스크 발사만 막아 둔다."""
    mock = MagicMock()
    monkeypatch.setattr(tasks_mod.post_public_reply, "apply_async", mock)
    return mock


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "lp-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _lost_ack_log(campaign, **kwargs):
    """성공 ack 를 잃은 발송 1건 — POST 는 했고(submitted_at·retry_count) mid 는 없다."""
    now = timezone.now()
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "정보",
        "recipient_user_id": RECIPIENT,
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.QUEUED,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.OPENING,
        "submitted_at": now - timedelta(seconds=20),
        "retry_count": 1,
        "next_retry_at": now + timedelta(seconds=40),
    }
    defaults.update(kwargs)
    return SentDMLog.objects.create(**defaults)


def _echo(conn, recipient=RECIPIENT, mid=None):
    return tasks_mod._apply_echo_delivered(
        mid=mid or f"mid_unknown_{uuid.uuid4().hex[:10]}",
        page_ig_user_id=conn.external_account_id,
        recipient_user_id=recipient,
    )


class TestEchoRescue:
    def test_echo_promotes_waiting_send(self, ig_connection, no_public_reply):
        """mid 가 어디에도 안 붙는 echo → 재시도 대기 중인 그 발송을 도착으로 승격."""
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp)

        assert _echo(ig_connection) == 1

        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED
        assert log.verified_via == SentDMLog.VerifiedVia.ECHO
        assert log.delivered_at is not None
        assert log.next_retry_at is None, "예약이 남으면 스위퍼가 다시 집는다"
        assert any(e.get("path") == "echo_no_mid" for e in log.verification_log)

        camp.refresh_from_db()
        assert camp.total_sent == 1
        # 대기 중이던 건은 미확인 카운터가 올라간 적이 없다 → 내리면 음수가 된다
        assert camp.total_unconfirmed == 0

    def test_echo_promotes_terminal_no_trace_and_fixes_counter(
        self, ig_connection, no_public_reply
    ):
        """이미 '도착 미확인'으로 종결된 건은 승격 + total_unconfirmed 를 내린다."""
        camp = _campaign(ig_connection, total_unconfirmed=1)
        log = _lost_ack_log(
            camp,
            status=SentDMLog.Status.FAILED_NO_TRACE,
            error_code="-1",
            error_subcode="2534023",
            retry_count=2,
            next_retry_at=None,
        )

        assert _echo(ig_connection) == 1

        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED
        camp.refresh_from_db()
        assert camp.total_sent == 1
        assert camp.total_unconfirmed == 0

    def test_promoted_log_is_not_resent(self, ig_connection, no_public_reply):
        """승격 뒤 예약돼 있던 재시도가 실행돼도 재POST 하지 않는다(중복 DM 방지)."""
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp)
        assert _echo(ig_connection) == 1

        result = tasks_mod.send_dm_task.apply(args=[str(log.pk)]).get()
        assert result["status"] == "skipped"
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED

    def test_child_promotion_leaves_counters_alone(self, ig_connection, no_public_reply):
        """child(reward) 는 애초에 캠페인 카운트에서 제외 — 승격해도 total_sent 불변."""
        camp = _campaign(ig_connection)
        parent = _lost_ack_log(camp, status=SentDMLog.Status.DELIVERED, submitted_at=None)
        child = _lost_ack_log(
            camp,
            dm_kind=SentDMLog.DMKind.REWARD,
            comment_id="",
            parent_log=parent,
        )

        assert _echo(ig_connection) == 1
        child.refresh_from_db()
        assert child.status == SentDMLog.Status.DELIVERED
        camp.refresh_from_db()
        assert camp.total_sent == 0


class TestEchoRescueGuards:
    """구제가 **안 되어야** 하는 경우 — 안 보낸 DM 을 '보냈다'고 표시하면 영구 미발송이 된다."""

    def test_never_submitted_log_is_untouched(self, ig_connection):
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp, submitted_at=None, retry_count=0)

        assert _echo(ig_connection) == 0
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.QUEUED

    def test_zero_retry_log_is_untouched(self, ig_connection):
        """첫 POST 가 아직 결론 나지 않은 행(retry_count=0)은 대상 아님."""
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp, retry_count=0)

        assert _echo(ig_connection) == 0
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.QUEUED

    def test_stale_submit_is_untouched(self, ig_connection):
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp, submitted_at=timezone.now() - timedelta(hours=3))

        assert _echo(ig_connection) == 0
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.QUEUED

    def test_submitting_log_is_untouched(self, ig_connection):
        """in-flight(SUBMITTING)는 제외 — 응답 처리가 status·카운터를 되돌려 이중 계상된다."""
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp, status=SentDMLog.Status.SUBMITTING)

        assert _echo(ig_connection) == 0
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.SUBMITTING

    def test_other_account_is_untouched(self, ig_connection, db):
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp)

        assert (
            tasks_mod._apply_echo_delivered(
                mid="mid_x", page_ig_user_id="ig_someone_else", recipient_user_id=RECIPIENT
            )
            == 0
        )
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.QUEUED

    def test_real_failure_without_send_attempt_is_untouched(self, ig_connection):
        """윈도우 만료처럼 '보내지 않고' 종결된 실패는 승격 대상 상태가 아니다."""
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp, status=SentDMLog.Status.FAILED_WINDOW)

        assert _echo(ig_connection) == 0
        log.refresh_from_db()
        assert log.status == SentDMLog.Status.FAILED_WINDOW


class TestExistingEchoPathsUnchanged:
    def test_mid_match_still_wins(self, ig_connection, no_public_reply):
        camp = _campaign(ig_connection)
        target = _lost_ack_log(
            camp,
            status=SentDMLog.Status.ACCEPTED,
            meta_message_id="mid_real_001",
            submitted_at=None,
            retry_count=0,
        )
        decoy = _lost_ack_log(camp)

        assert _echo(ig_connection, mid="mid_real_001") == 1
        target.refresh_from_db()
        decoy.refresh_from_db()
        assert target.status == SentDMLog.Status.DELIVERED
        assert decoy.status == SentDMLog.Status.QUEUED, "mid 가 붙었으면 구제는 돌지 않는다"

    def test_accepted_fallback_still_wins(self, ig_connection, no_public_reply):
        camp = _campaign(ig_connection)
        accepted = _lost_ack_log(
            camp,
            status=SentDMLog.Status.ACCEPTED,
            meta_message_id="mid_other",
            accepted_at=timezone.now(),
        )
        waiting = _lost_ack_log(camp)

        assert _echo(ig_connection) == 1
        accepted.refresh_from_db()
        waiting.refresh_from_db()
        assert accepted.status == SentDMLog.Status.DELIVERED
        assert waiting.status == SentDMLog.Status.QUEUED


class TestGateTapProof:
    def test_button_tap_promotes_failed_opening(self, ig_connection, no_real_send, no_public_reply):
        """버튼 탭 = 오프닝이 도착했다는 증거 → 도착 승격 + reward 는 그대로 발송."""
        camp = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            gate_verify_follow=False,
            reward_message_template="보상 링크: https://example.com",
            total_unconfirmed=1,
        )
        opening = _lost_ack_log(
            camp,
            status=SentDMLog.Status.FAILED_NO_TRACE,
            error_code="-1",
            error_subcode="2534023",
            retry_count=2,
            next_retry_at=None,
            gate_status=SentDMLog.GateStatus.PENDING,
        )

        res = tasks_mod.process_follow_gate_postback.apply(
            args=[str(opening.pk), RECIPIENT, ig_connection.external_account_id]
        ).get()
        assert res["status"] == "reward_enqueued"

        opening.refresh_from_db()
        assert opening.status == SentDMLog.Status.DELIVERED
        assert any(e.get("path") == "gate_tap" for e in opening.verification_log)
        camp.refresh_from_db()
        assert camp.total_sent == 1
        assert camp.total_unconfirmed == 0

    def test_retap_promotes_even_when_already_passed(
        self, ig_connection, no_real_send, no_public_reply
    ):
        """이미 PASSED 라 early return 하는 재탭에서도 정정이 일어난다."""
        camp = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            gate_verify_follow=False,
            reward_message_template="보상",
        )
        opening = _lost_ack_log(
            camp,
            status=SentDMLog.Status.FAILED_NO_TRACE,
            retry_count=2,
            next_retry_at=None,
            gate_status=SentDMLog.GateStatus.PASSED,
        )

        tasks_mod.process_follow_gate_postback.apply(
            args=[str(opening.pk), RECIPIENT, ig_connection.external_account_id]
        ).get()

        opening.refresh_from_db()
        assert opening.status == SentDMLog.Status.DELIVERED

    def test_delivered_opening_untouched(self, ig_connection, no_real_send, no_public_reply):
        """정상 발송(mid 있음)된 오프닝은 승격 로직이 손대지 않는다."""
        camp = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            gate_verify_follow=False,
            reward_message_template="보상",
        )
        opening = _lost_ack_log(
            camp,
            status=SentDMLog.Status.DELIVERED,
            meta_message_id="mid_ok",
            gate_status=SentDMLog.GateStatus.PENDING,
        )

        tasks_mod.process_follow_gate_postback.apply(
            args=[str(opening.pk), RECIPIENT, ig_connection.external_account_id]
        ).get()

        camp.refresh_from_db()
        assert camp.total_sent == 0, "이미 집계된 발송을 다시 세면 안 된다"

    def test_account_mismatch_does_not_promote(self, ig_connection, no_real_send):
        """다른 계정으로 잘못 라우팅된 postback 은 승격도 하지 않는다."""
        camp = _campaign(
            ig_connection,
            follow_gate_enabled=True,
            gate_verify_follow=False,
            reward_message_template="보상",
        )
        opening = _lost_ack_log(
            camp, status=SentDMLog.Status.FAILED_NO_TRACE, retry_count=2, next_retry_at=None
        )

        res = tasks_mod.process_follow_gate_postback.apply(
            args=[str(opening.pk), RECIPIENT, "ig_other_account"]
        ).get()
        assert res["reason"] == "account_mismatch"
        opening.refresh_from_db()
        assert opening.status == SentDMLog.Status.FAILED_NO_TRACE


class TestNoDoubleCount:
    def test_conv_confirm_on_delivered_log_is_noop(
        self, ig_connection, no_public_reply, monkeypatch
    ):
        """echo 로 먼저 승격된 로그에 Conversations 승격이 또 들어와도 두 번 세지 않는다."""
        monkeypatch.setattr(
            InstagramMessagingService,
            "has_recent_message_to_recipient",
            MagicMock(return_value=True),
        )
        camp = _campaign(ig_connection)
        log = _lost_ack_log(camp)
        assert _echo(ig_connection) == 1
        log.refresh_from_db()

        res = tasks_mod._confirm_delivered_via_conv(log, camp, "verify_before_resend")
        assert res.get("already") is True

        log.refresh_from_db()
        assert log.status == SentDMLog.Status.DELIVERED, "ACCEPTED 로 되돌아가면 안 된다"
        camp.refresh_from_db()
        assert camp.total_sent == 1
