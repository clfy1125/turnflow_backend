"""공개 답글 서킷브레이커 재설계 테스트 (2026-08-24).

배경(prod CS #c13711f0 @manjzangi): 옛 판정은 "10분 내 영구에러 3건 → 계정 전체 OFF" 였다.
그 계정의 영구에러 203건은 **전부 code=100/subcode=33**(댓글러가 자기 댓글 삭제)이었는데
제재로 오인해 20번 발동, 캠페인 공개답글을 최대 6개씩 껐다. Action Block(code=1)은 0건.

바뀐 규칙 3가지를 잠근다:
  1) 제재성 코드(1/190/200/10)만 카운트 — 100 은 서킷을 건드리지 않는다
  2) 시도 대비 비율(과반) + 최소 건수 AND
  3) 범위는 캠페인 단위, 단 Action Block(code=1)만 계정 전체

NOTE(test-db-not-clean): 내가 만든 캠페인/로그로만 단언.
"""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.tasks import CB_ERROR_RATIO, CB_MIN_ERRORS, _public_reply_circuit_breaker
from apps.workspace.models import Membership, Workspace

DELETED_COMMENT = 100  # code=100/sub=33 — 댓글 삭제(제재 아님)
ACTION_BLOCK = 1
TOKEN_EXPIRED = 190


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"cb_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="CB"
    )
    ws = Workspace.objects.create(name="CB WS", slug=f"cb-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=f"cbuser_{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_cb"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "cb-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        "public_reply_enabled": True,
        "public_reply_template": "DM 보냈어요!",
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, *, events=(), age=timedelta(minutes=1)):
    log = SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"cmt_{uuid.uuid4().hex[:10]}",
        recipient_user_id=f"rcpt_{uuid.uuid4().hex[:8]}",
        recipient_username="buyer",
        message_sent="안녕하세요!",
        status=SentDMLog.Status.DELIVERED,
        idempotency_key=uuid.uuid4().hex,
        verification_log=list(events),
    )
    SentDMLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - age)
    log.refresh_from_db()
    return log


def _perm(code, feature="public_reply"):
    return {
        "path": "public_reply",
        "result": "abandoned_permanent",
        "feature": feature,
        "code": code,
    }


def _posted(feature="public_reply"):
    return {"path": "public_reply", "result": "posted", "feature": feature}


# ────────────────────── 1) 댓글 삭제는 서킷을 건드리지 않는다 ──────────────────────


@pytest.mark.django_db
class TestDeletedCommentNeverTrips:
    def test_deleted_comments_do_not_trip(self, ig_connection):
        """CS 사고 재현 — 100/33 이 10건 쌓여도 발동하지 않는다."""
        camp = _campaign(ig_connection)
        for _ in range(10):
            _log(camp, events=[_perm(DELETED_COMMENT)])
        trigger = _log(camp, events=[_perm(DELETED_COMMENT)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=DELETED_COMMENT)

        camp.refresh_from_db()
        assert res["tripped"] is False
        assert camp.public_reply_enabled is True

    def test_would_have_tripped_under_the_old_rule(self, ig_connection, monkeypatch):
        """검출력 검증 — 100 을 제재성으로 세면(옛 규칙) 같은 시나리오가 즉시 발동한다.

        위 테스트가 "그냥 통과"하는 게 아니라 **제외 규칙 때문에** 통과하는 것임을 못 박는다.
        """
        import apps.integrations.tasks as t

        monkeypatch.setattr(t, "CB_TRIGGER_CODES", (1, 100, 190, 200, 10))
        monkeypatch.setattr(t, "CB_ACCOUNT_WIDE_CODES", (1, 100))
        camp = _campaign(ig_connection)
        other = _campaign(ig_connection, name="옆 캠페인")
        for _ in range(3):
            _log(camp, events=[_perm(DELETED_COMMENT)])
        trigger = _log(camp, events=[_perm(DELETED_COMMENT)])

        res = t._public_reply_circuit_breaker(trigger, feature="public_reply", code=DELETED_COMMENT)

        camp.refresh_from_db()
        other.refresh_from_db()
        assert res["tripped"] is True  # 옛 규칙 = 발동
        assert camp.public_reply_enabled is False
        assert other.public_reply_enabled is False  # 옛 규칙 = 계정 전체 확산

    def test_deleted_comments_never_dilute_a_real_restriction(self, ig_connection):
        """★ 100 은 분자에도 분모에도 들어가지 않는다.

        댓글이 많이 지워지는 계정(이 고객은 하루 수백 건)에서 100 을 분모에 넣으면
        진짜 제재 신호가 희석돼 서킷이 늦게 열린다 — 그러면 Action Block 이 연장된다.
        """
        camp = _campaign(ig_connection)
        for _ in range(9):
            _log(camp, events=[_perm(DELETED_COMMENT)])
        for _ in range(2):
            _log(camp, events=[_perm(TOKEN_EXPIRED)])
        trigger = _log(camp, events=[_perm(TOKEN_EXPIRED)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=TOKEN_EXPIRED)

        assert (res["errors"], res["attempts"]) == (3, 3)  # 100 은 양쪽 다 제외
        assert res["tripped"] is True


# ────────────────────── 2) 비율 판정 ──────────────────────


@pytest.mark.django_db
class TestRatioThreshold:
    def test_errors_diluted_by_success_do_not_trip(self, ig_connection):
        """성공이 많으면(=계정 멀쩡) 제재성 실패 3건으로는 끄지 않는다."""
        camp = _campaign(ig_connection)
        for _ in range(20):
            _log(camp, events=[_posted()])
        for _ in range(2):
            _log(camp, events=[_perm(TOKEN_EXPIRED)])
        trigger = _log(camp, events=[_perm(TOKEN_EXPIRED)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=TOKEN_EXPIRED)

        camp.refresh_from_db()
        assert res["tripped"] is False
        assert res["errors"] == 3 and res["attempts"] == 23
        assert res["ratio"] < CB_ERROR_RATIO
        assert camp.public_reply_enabled is True

    def test_majority_failure_trips(self, ig_connection):
        camp = _campaign(ig_connection)
        _log(camp, events=[_posted()])
        for _ in range(3):
            _log(camp, events=[_perm(TOKEN_EXPIRED)])
        trigger = _log(camp, events=[_perm(TOKEN_EXPIRED)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=TOKEN_EXPIRED)

        camp.refresh_from_db()
        assert res["tripped"] is True
        assert camp.public_reply_enabled is False

    def test_below_min_errors_does_not_trip(self, ig_connection):
        """비율이 100% 여도 최소 건수 미달이면 발동하지 않는다(단발 오류 방어)."""
        camp = _campaign(ig_connection)
        trigger = _log(camp, events=[_perm(TOKEN_EXPIRED)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=TOKEN_EXPIRED)

        camp.refresh_from_db()
        assert res["tripped"] is False
        assert res["errors"] < CB_MIN_ERRORS
        assert camp.public_reply_enabled is True

    def test_old_errors_outside_window_are_ignored(self, ig_connection):
        camp = _campaign(ig_connection)
        for _ in range(5):
            _log(camp, events=[_perm(TOKEN_EXPIRED)], age=timedelta(minutes=30))
        trigger = _log(camp, events=[_perm(TOKEN_EXPIRED)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=TOKEN_EXPIRED)

        camp.refresh_from_db()
        assert res["tripped"] is False
        assert camp.public_reply_enabled is True


# ────────────────────── 3) 범위 (캠페인 vs 계정) ──────────────────────


@pytest.mark.django_db
class TestScope:
    def test_non_action_block_disables_only_that_campaign(self, ig_connection):
        bad = _campaign(ig_connection, name="문제 게시물")
        good = _campaign(ig_connection, name="멀쩡한 게시물")
        for _ in range(3):
            _log(bad, events=[_perm(TOKEN_EXPIRED)])
        trigger = _log(bad, events=[_perm(TOKEN_EXPIRED)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=TOKEN_EXPIRED)

        bad.refresh_from_db()
        good.refresh_from_db()
        assert res["tripped"] is True and res["affected"] == 1
        assert bad.public_reply_enabled is False
        assert good.public_reply_enabled is True  # ★ 남의 캠페인은 살아있다

    def test_action_block_disables_whole_account(self, ig_connection):
        """계정 단위 제재(code=1)는 계속 두드리면 차단이 연장되므로 전 캠페인 OFF."""
        a = _campaign(ig_connection, name="a")
        b = _campaign(ig_connection, name="b")
        for _ in range(3):
            _log(a, events=[_perm(ACTION_BLOCK)])
        trigger = _log(a, events=[_perm(ACTION_BLOCK)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=ACTION_BLOCK)

        a.refresh_from_db()
        b.refresh_from_db()
        assert res["tripped"] is True
        assert a.public_reply_enabled is False and b.public_reply_enabled is False

    def test_other_campaign_errors_do_not_count_for_campaign_scope(self, ig_connection):
        """캠페인 범위 판정은 다른 캠페인의 실패를 분자에 넣지 않는다."""
        mine = _campaign(ig_connection, name="mine")
        other = _campaign(ig_connection, name="other")
        for _ in range(5):
            _log(other, events=[_perm(TOKEN_EXPIRED)])
        trigger = _log(mine, events=[_perm(TOKEN_EXPIRED)])

        res = _public_reply_circuit_breaker(trigger, feature="public_reply", code=TOKEN_EXPIRED)

        mine.refresh_from_db()
        assert res["tripped"] is False
        assert mine.public_reply_enabled is True


# ────────────────────── 4) feature 분리 (성공답글 vs 복구답글) ──────────────────────


@pytest.mark.django_db
class TestFeatureIsolation:
    def test_recovery_errors_do_not_disable_public_reply(self, ig_connection):
        camp = _campaign(ig_connection, recovery_reply_enabled=True)
        for _ in range(4):
            _log(camp, events=[_perm(TOKEN_EXPIRED, feature="recovery")])
        trigger = _log(camp, events=[_perm(TOKEN_EXPIRED, feature="recovery")])

        res = _public_reply_circuit_breaker(trigger, feature="recovery", code=TOKEN_EXPIRED)

        camp.refresh_from_db()
        assert res["tripped"] is True
        assert camp.recovery_reply_enabled is False
        assert camp.public_reply_enabled is True  # 성공답글은 유지
