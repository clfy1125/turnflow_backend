"""실패 DM 복구(recovery) 테스트 — v2 (재댓글 트리거, 2026-07-14).

커버리지:
  - 진입: opening 2534025 확정 실패 → RECOVERY_PENDING + 안내 대댓글 예약 (_maybe_enter_recovery)
  - 확정 실패 가드: 전달 흔적(accepted/echo)·기게시 답글·동일 수신자 중복 안내 → 댓글 미게시
  - 회귀 없음: 복구 비활성/다른 subcode/child → 기존 FAILED_PARAM 경로 유지
  - post_public_reply(recovery=True): 복구 템플릿·필드·멱등 + 승격 후 게시 취소(레이스 방어)
  - 재댓글 성공 정산: 같은 수신자 ACCEPTED → 이전 RECOVERY_PENDING 승격 (_flip_recovery_on_success)
  - TTL 스윕(handle_recovery_pending_expiry)
  - 셀프 DM 루프 방어: _poll_one_media 대댓글/self-comment 스킵, _enqueue_send_dm username 가드
    (2026-07-14 prod 실측: 자기 공개답글에 매시간 셀프 opening 50건 자기증식)
  - 복구 재댓글의 수신자 쿨다운 면제
  - 상태 표시(_STATUS_DISPLAY) / 프론트 액션(build_frontend_action)

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언한다.
NOTE: broker 없이 enqueue 경로만 검증 — send_dm_task.delay / apply_async 는 모킹.
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations.dm_exceptions import DMInvalidParamError
from apps.integrations.dm_frontend_actions import build_frontend_action
from apps.integrations.models import (
    RECOVERY_CLOSER_PHRASES,
    RECOVERY_FIRST_PHRASES,
    RECOVERY_MID_EMOJIS,
    RECOVERY_REPLY_COMBINATIONS,
    RECOVERY_TRAIL_EMOJIS,
    AutoDMCampaign,
    IGAccountConnection,
    SentDMLog,
    compose_recovery_reply,
)
from apps.integrations.serializers import _STATUS_DISPLAY
from apps.integrations.services import InstagramCommentService, InstagramMediaService
from apps.workspace.models import Membership, Workspace

IGSID = "igsid_recovery_001"
PAGE_IGID = "ig_recovery_page_001"


def _make_connection(*, pro: bool, page_igid=PAGE_IGID, username="recuser"):
    """워크스페이스 owner 에게 (pro=True 면) 프로 구독을 부여한 IG 연결 생성.

    실패 DM 복구는 dm_recovery(프로 전용) 게이트를 타므로, 기본 테스트는 프로 소유자를 쓴다.
    """
    from django.contrib.auth import get_user_model

    from apps.billing.models import SubscriptionPlan, UserSubscription

    User = get_user_model()
    user = User.objects.create_user(
        email=f"rec_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="Rec"
    )
    if pro:
        pro_plan = SubscriptionPlan.objects.get(name="pro")
        UserSubscription.objects.update_or_create(user=user, defaults={"plan": pro_plan})
    ws = Workspace.objects.create(name="Rec WS", slug=f"rec-ws-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=page_igid,
        username=username,
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_rec"
    conn.save()
    return conn


@pytest.fixture
def ig_connection(db):
    # 실패 DM 복구는 프로 전용 → 기본 픽스처는 프로 소유자.
    return _make_connection(pro=True, page_igid=f"ig_rec_{uuid.uuid4().hex[:10]}")


@pytest.fixture
def ig_connection_free(db):
    # 프로 미보유(무료) 소유자 — 플랜 게이트 검증용. 페이지 ID 를 분리해 매칭 간섭 방지.
    return _make_connection(
        pro=False, page_igid=f"ig_recfree_{uuid.uuid4().hex[:10]}", username="recfree"
    )


@pytest.fixture
def no_real_send(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(tasks_mod.send_dm_task, "delay", mock)
    return mock


@pytest.fixture
def no_reply_enqueue(monkeypatch):
    """post_public_reply.apply_async 무력화 (예약 여부만 검증)."""
    mock = MagicMock()
    monkeypatch.setattr(tasks_mod.post_public_reply, "apply_async", mock)
    return mock


def _campaign(conn, **kw):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": "rec-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        "follow_gate_enabled": True,
        "reward_message_template": "보상 https://x.co",
        "recovery_reply_enabled": True,
        "recovery_reply_templates": [
            "DM이 숨겨진 요청함으로 갔어요 🥲 수락하시고 다시 댓글 남겨주시면 바로 보내드릴게요!"
        ],
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _opening(campaign, **kw):
    defaults = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "코워크",
        "recipient_user_id": IGSID,
        "recipient_username": "buyer",
        "message_sent": "팔로우하고 버튼 눌러주세요!",
        "status": SentDMLog.Status.SUBMITTING,
        "idempotency_key": uuid.uuid4().hex,
        "dm_kind": SentDMLog.DMKind.OPENING,
        "gate_status": SentDMLog.GateStatus.PENDING,
    }
    defaults.update(kw)
    return SentDMLog.objects.create(**defaults)


def _err(code=100, subcode=2534025):
    return DMInvalidParamError(
        "댓글이 비공개 답글에 유효하지 않습니다",
        status=400,
        code=code,
        subcode=subcode,
        api_response={"error": {"code": code, "error_subcode": subcode}},
    )


# ===== 진입: 2534025 확정 실패 → RECOVERY_PENDING =====


class TestEnterRecovery:
    def test_2534025_marks_pending_and_enqueues_reply(self, ig_connection, no_reply_enqueue):
        c = _campaign(ig_connection)
        op = _opening(c)
        assert tasks_mod._maybe_enter_recovery(op, c, _err()) is True
        op.refresh_from_db()
        assert op.status == SentDMLog.Status.RECOVERY_PENDING
        assert op.recovery_pending_at is not None
        assert op.error_subcode == "2534025"
        # 안내 대댓글이 recovery=True 로 예약됨
        assert no_reply_enqueue.called
        assert no_reply_enqueue.call_args.kwargs["kwargs"] == {"recovery": True}

    def test_disabled_falls_through(self, ig_connection, no_reply_enqueue):
        c = _campaign(ig_connection, recovery_reply_enabled=False)
        op = _opening(c)
        assert tasks_mod._maybe_enter_recovery(op, c, _err()) is False
        op.refresh_from_db()
        assert op.status == SentDMLog.Status.SUBMITTING  # 변경 없음
        assert not no_reply_enqueue.called

    def test_empty_templates_uses_server_composer(self, ig_connection, no_reply_enqueue):
        # 캠페인이 템플릿을 안 넣어도 서버 조합기로 폴백 → 복구 진입
        c = _campaign(ig_connection, recovery_reply_templates=[])
        op = _opening(c)
        assert tasks_mod._maybe_enter_recovery(op, c, _err()) is True
        # pick 은 조합기에서 무작위로 나옴 (빈 문자열 아님)
        assert c.pick_recovery_reply_template()

    def test_other_subcode_falls_through(self, ig_connection, no_reply_enqueue):
        c = _campaign(ig_connection)
        op = _opening(c)
        assert tasks_mod._maybe_enter_recovery(op, c, _err(subcode=33)) is False
        assert tasks_mod._maybe_enter_recovery(op, c, _err(subcode=2018292)) is False
        op.refresh_from_db()
        assert op.status == SentDMLog.Status.SUBMITTING

    def test_child_and_no_comment_fall_through(self, ig_connection, no_reply_enqueue):
        c = _campaign(ig_connection)
        parent = _opening(c)
        # reward/재안내 child (parent_log 있음) 은 복구 대상 아님
        child = _opening(c, dm_kind=SentDMLog.DMKind.REWARD, comment_id="", parent_log=parent)
        assert tasks_mod._maybe_enter_recovery(child, c, _err()) is False

    # ── v2 확정 실패 가드 (2026-07-14 prod 이중 댓글 버그 재발 방지) ──

    def test_delivery_trace_blocks_recovery(self, ig_connection, no_reply_enqueue):
        """전달 흔적(accepted/echo/delivered/read)이 있으면 '확정 실패' 아님 → 복구 미진입.

        revive(제자리 되살림) 재시도가 2534025 를 맞아도 원 DM 은 이미 전달됐을 수 있다 —
        이때 '못 드렸어요' 안내를 달면 거짓 안내 + 이중 댓글.
        """
        c = _campaign(ig_connection)
        for trace in (
            {"meta_message_id": "mid_x"},
            {"echo_mid": "echo_x"},
            {"accepted_at": timezone.now()},
            {"delivered_at": timezone.now()},
            {"read_at": timezone.now()},
        ):
            op = _opening(c, **trace)
            assert tasks_mod._maybe_enter_recovery(op, c, _err()) is False, trace
            op.refresh_from_db()
            assert op.status == SentDMLog.Status.SUBMITTING
        assert not no_reply_enqueue.called

    def test_existing_reply_blocks_recovery(self, ig_connection, no_reply_enqueue):
        """이 댓글에 이미 우리 답글(성공/복구)이 달려 있으면 추가 게시 금지."""
        c = _campaign(ig_connection)
        op1 = _opening(c, public_reply_id="pub_1")
        assert tasks_mod._maybe_enter_recovery(op1, c, _err()) is False
        op2 = _opening(c, recovery_reply_id="rec_1")
        assert tasks_mod._maybe_enter_recovery(op2, c, _err()) is False
        assert not no_reply_enqueue.called

    def test_duplicate_pending_same_recipient_skips_guide_reply(
        self, ig_connection, no_reply_enqueue
    ):
        """같은 수신자에 RECOVERY_PENDING 이 이미 있으면(=안내 이미 나감) 상태만 전이하고
        안내 댓글은 다시 달지 않는다 — 수락 전 재댓글이 또 실패한 경우 안내 반복 게시 방지."""
        c = _campaign(ig_connection)
        _opening(c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now())
        second = _opening(c)  # 같은 IGSID 의 두 번째 실패
        assert tasks_mod._maybe_enter_recovery(second, c, _err()) is True
        second.refresh_from_db()
        assert second.status == SentDMLog.Status.RECOVERY_PENDING
        assert not no_reply_enqueue.called  # 댓글 예약 없음


# ===== post_public_reply recovery 모드 =====


class TestRecoveryReplyPost:
    def test_recovery_mode_posts_and_writes_field(self, ig_connection, monkeypatch):
        c = _campaign(ig_connection)
        op = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        mock_reply = MagicMock(return_value={"id": "reply_r1"})
        monkeypatch.setattr(InstagramCommentService, "post_reply", mock_reply)

        res = tasks_mod.post_public_reply.apply(args=[str(op.id)], kwargs={"recovery": True}).result
        assert res["status"] == "posted"
        assert res["feature"] == "recovery"
        op.refresh_from_db()
        assert op.recovery_reply_id == "reply_r1"
        assert op.public_reply_id == ""  # 성공답글 필드와 분리

        # 이미 게시했으면 재게시 skip
        res2 = tasks_mod.post_public_reply.apply(
            args=[str(op.id)], kwargs={"recovery": True}
        ).result
        assert res2["status"] == "skipped"

    def test_recovery_post_cancelled_if_no_longer_pending(self, ig_connection, monkeypatch):
        """예약~게시 사이에 재댓글 발송이 성공해 RECOVERY_DELIVERED 로 승격됐으면
        '못 드렸어요' 안내는 거짓 → 게시 취소 (레이스 방어)."""
        c = _campaign(ig_connection)
        op = _opening(
            c, status=SentDMLog.Status.RECOVERY_DELIVERED, recovery_pending_at=timezone.now()
        )
        mock_reply = MagicMock(return_value={"id": "reply_never"})
        monkeypatch.setattr(InstagramCommentService, "post_reply", mock_reply)
        res = tasks_mod.post_public_reply.apply(args=[str(op.id)], kwargs={"recovery": True}).result
        assert res["status"] == "skipped"
        assert "no_longer_pending" in res["reason"]
        assert not mock_reply.called


# ===== 재댓글 성공 정산: 같은 수신자 ACCEPTED → 이전 RECOVERY_PENDING 승격 =====


class TestRecommentFlip:
    def test_new_send_accepted_flips_pending_same_recipient(self, ig_connection):
        c = _campaign(ig_connection)
        old = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        # 사용자가 요청함 수락 후 다시 댓글 → 새 opening 이 정상 경로로 ACCEPTED
        new = _opening(c, status=SentDMLog.Status.ACCEPTED)
        assert tasks_mod._flip_recovery_on_success(new, c) == 1
        old.refresh_from_db()
        assert old.status == SentDMLog.Status.RECOVERY_DELIVERED
        assert old.is_delivered() is True
        new.refresh_from_db()
        assert new.status == SentDMLog.Status.ACCEPTED  # 자기 자신은 건드리지 않음

    def test_flip_scopes_to_recipient_and_campaign(self, ig_connection):
        c = _campaign(ig_connection)
        other_c = _campaign(ig_connection, name="rec-campaign-2")
        same_user_other_campaign = _opening(
            other_c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        other_user = _opening(
            c,
            recipient_user_id="igsid_other_999",
            recipient_username="other_buyer",  # username 도 다른 실제 타인 (IG username 유일)
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now(),
        )
        new = _opening(c, status=SentDMLog.Status.ACCEPTED)
        assert tasks_mod._flip_recovery_on_success(new, c) == 0
        same_user_other_campaign.refresh_from_db()
        other_user.refresh_from_db()
        assert same_user_other_campaign.status == SentDMLog.Status.RECOVERY_PENDING
        assert other_user.status == SentDMLog.Status.RECOVERY_PENDING

    def test_flip_covers_legacy_v1_child(self, ig_connection):
        """배포 전환기: v1 인바운드 재전송 자식이 늦게 ACCEPTED 돼도 recipient 가 같아
        부모 RECOVERY_PENDING 이 자연 승격된다."""
        c = _campaign(ig_connection)
        parent = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        child = _opening(c, comment_id="", parent_log=parent, status=SentDMLog.Status.ACCEPTED)
        assert tasks_mod._flip_recovery_on_success(child, c) == 1
        parent.refresh_from_db()
        assert parent.status == SentDMLog.Status.RECOVERY_DELIVERED

    def test_flip_ignores_without_recipient(self, ig_connection):
        c = _campaign(ig_connection)
        _opening(c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now())
        new = _opening(
            c, recipient_user_id="", recipient_username="", status=SentDMLog.Status.ACCEPTED
        )
        assert tasks_mod._flip_recovery_on_success(new, c) == 0

    def test_flip_matches_across_key_spaces(self, ig_connection):
        """poll 경로 pending(recipient_user_id=username) 이 웹훅 경로 성공 로그(IGSID 키,
        username 은 recipient_username)로도 승격된다 — recipient 키 이원화 대응."""
        c = _campaign(ig_connection)
        poll_pending = _opening(
            c,
            recipient_user_id="buyer",  # ← poll 폴백(username)
            recipient_username="buyer",
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now(),
        )
        webhook_success = _opening(
            c,
            recipient_user_id=IGSID,  # ← 웹훅(IGSID)
            recipient_username="buyer",
            status=SentDMLog.Status.ACCEPTED,
        )
        assert tasks_mod._flip_recovery_on_success(webhook_success, c) == 1
        poll_pending.refresh_from_db()
        assert poll_pending.status == SentDMLog.Status.RECOVERY_DELIVERED


# ===== TTL 스윕 =====


class TestExpiry:
    def test_ttl_sweep(self, ig_connection):
        c = _campaign(ig_connection, recovery_ttl_seconds=3600)
        old = _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(hours=2),
        )
        fresh = _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now(),
        )
        tasks_mod.handle_recovery_pending_expiry()
        old.refresh_from_db()
        fresh.refresh_from_db()
        assert old.status == SentDMLog.Status.RECOVERY_EXPIRED
        assert fresh.status == SentDMLog.Status.RECOVERY_PENDING


# ===== 셀프 DM 루프 방어 (2026-07-14 prod 실측 버그) =====


class TestSelfDmLoopGuards:
    def _poll_comments(self, ig_connection, comments, monkeypatch):
        """_poll_one_media 를 크래프트된 comments 응답으로 1회 실행."""
        monkeypatch.setattr(
            InstagramMediaService,
            "list_media_comments",
            MagicMock(return_value={"data": comments, "paging_after": None}),
        )
        return tasks_mod._poll_one_media(ig_connection, "media_poll_1", timezone.now())

    def test_poller_skips_reply_and_self_comments(self, ig_connection, no_real_send, monkeypatch):
        """우리가 단 공개답글(대댓글·계정 본인 작성)을 poller 가 주워 셀프 DM 을 만드는
        자기증식 루프 차단: parent_id 있는 댓글 + 본인 username 댓글은 트리거 제외."""
        c = _campaign(
            ig_connection,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="media_poll_1",
            started_at=timezone.now() - timedelta(days=1),
        )
        now_iso = timezone.now().strftime("%Y-%m-%dT%H:%M:%S%z")
        comments = [
            # (a) 우리 공개답글: 대댓글 + 본인 username → 둘 중 하나만으로도 스킵돼야 함
            {
                "id": "cmt_self_reply",
                "text": "댓글 남겨주셔서 감사해요! DM 확인해주세요",
                "username": ig_connection.username,
                "timestamp": now_iso,
                "parent_id": "cmt_someone",
            },
            # (b) 본인 top-level 댓글 (고정댓글 등)
            {
                "id": "cmt_self_top",
                "text": "이벤트 안내입니다",
                "username": ig_connection.username.upper(),  # 케이스 무시 확인
                "timestamp": now_iso,
            },
            # (c) 타인 대댓글 → top-level 아님 → 스킵
            {
                "id": "cmt_user_reply",
                "text": "코워크",
                "username": "someone_else",
                "timestamp": now_iso,
                "parent_id": "cmt_root",
            },
            # (d) 정상 타인 top-level 댓글 → 트리거되어야 함
            {
                "id": "cmt_user_top",
                "text": "코워크",
                "username": "real_user",
                "timestamp": now_iso,
            },
        ]
        result = self._poll_comments(ig_connection, comments, monkeypatch)
        assert result["misses"] == 1  # (d) 만 발송 enqueue
        sent_comment_ids = set(
            SentDMLog.objects.filter(campaign=c).values_list("comment_id", flat=True)
        )
        assert sent_comment_ids == {"cmt_user_top"}

    def test_enqueue_guard_blocks_username_as_recipient(self, ig_connection, no_real_send):
        """poll 경로는 from.id 부재 시 username 을 recipient 로 넘긴다 — IGSID 비교만으로는
        본인 댓글을 못 거른다(prod 셀프 DM 50건의 직접 원인). username 비교로 차단."""
        c = _campaign(ig_connection)
        res = tasks_mod._enqueue_send_dm(
            campaign=c,
            comment_id="cmt_self_enq",
            comment_text="아무 텍스트",
            from_user_id=ig_connection.username,  # ← poll 폴백 형태
            from_username="",
            webhook_payload={},
        )
        assert res["status"] == "skipped"
        assert res["reason"] == "self_comment"
        res2 = tasks_mod._enqueue_send_dm(
            campaign=c,
            comment_id="cmt_self_enq2",
            comment_text="아무 텍스트",
            from_user_id="some_numeric_id",
            from_username=ig_connection.username.upper(),  # username 자리로도 차단
            webhook_payload={},
        )
        assert res2["status"] == "skipped"
        assert res2["reason"] == "self_comment"
        assert not SentDMLog.objects.filter(campaign=c).exists()


# ===== 복구 재댓글의 수신자 쿨다운 면제 =====


class TestRecoveryCooldownExemption:
    def test_guided_pending_does_not_block_recomment(self, ig_connection, no_real_send):
        """안내 댓글이 실제 게시된(recovery_reply_id 有) RECOVERY_PENDING 은 쿨다운
        모수에서 제외 — 안내를 보고 5분 내 재댓글을 단 사용자의 재발송이 막히면 안 된다."""
        c = _campaign(ig_connection)
        _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now(),
            recovery_reply_id="rec_guide_1",  # 안내 게시됨
        )  # created_at = 지금 (쿨다운 창 안)
        res = tasks_mod._enqueue_send_dm(
            campaign=c,
            comment_id=f"cmt_re_{uuid.uuid4().hex[:8]}",
            comment_text="다시 댓글",
            from_user_id=IGSID,
            from_username="buyer",
            webhook_payload={},
        )
        assert res["status"] == "enqueued"
        assert no_real_send.called

    def test_unguided_pending_still_cools_down(self, ig_connection, no_real_send):
        """안내 미게시 pending(중복 실패의 silent 전이)은 면제 아님 — 채널 닫힌 유저의
        재댓글 연타가 실패 시도를 무한 반복(페이서 소모·실패통계 증폭)하지 못하게 캡."""
        c = _campaign(ig_connection)
        _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now(),
            recovery_reply_id="",  # 안내 미게시 (already_guided silent 전이)
        )
        res = tasks_mod._enqueue_send_dm(
            campaign=c,
            comment_id=f"cmt_cd2_{uuid.uuid4().hex[:8]}",
            comment_text="연타 재댓글",
            from_user_id=IGSID,
            from_username="buyer",
            webhook_payload={},
        )
        assert res["status"] == "skipped"
        assert res["reason"].startswith("recipient_cooldown")

    def test_normal_recent_log_still_cools_down(self, ig_connection, no_real_send):
        c = _campaign(ig_connection)
        _opening(c, status=SentDMLog.Status.ACCEPTED)  # 방금 정상 발송
        res = tasks_mod._enqueue_send_dm(
            campaign=c,
            comment_id=f"cmt_cd_{uuid.uuid4().hex[:8]}",
            comment_text="연타 댓글",
            from_user_id=IGSID,
            from_username="buyer",
            webhook_payload={},
        )
        assert res["status"] == "skipped"
        assert res["reason"].startswith("recipient_cooldown")


# ===== 복구 재댓글 라우팅 (스레드 답글·키워드 불일치 구제) =====


class TestRecommentRouting:
    def _route(self, conn, **kw):
        defaults = {
            "page_ig_user_id": str(conn.external_account_id),
            "from_user_id": IGSID,
            "from_username": "buyer",
            "comment_id": f"cmt_rt_{uuid.uuid4().hex[:8]}",
            "comment_text": "수락했어요",  # 캠페인 키워드와 무관
            "source": "test",
        }
        defaults.update(kw)
        return tasks_mod._maybe_route_recovery_recomment(**defaults)

    def test_routes_pending_user_recomment(self, ig_connection, no_real_send):
        c = _campaign(ig_connection)
        pending = _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(minutes=10),
            recovery_reply_id="rec_g1",
        )
        assert self._route(ig_connection) == 1
        new = SentDMLog.objects.filter(campaign=c, status=SentDMLog.Status.QUEUED).first()
        assert new is not None and new.id != pending.id
        assert no_real_send.called

    def test_username_keyed_pending_matches_igsid_recomment(self, ig_connection, no_real_send):
        """폴링 경로가 만든 pending(recipient_user_id=username)도 웹훅 재댓글(IGSID+username)
        이 매칭한다 — recipient 키 이원화 대응."""
        c = _campaign(ig_connection)
        _opening(
            c,
            recipient_user_id="buyer",  # ← poll 폴백 형태(username)
            recipient_username="buyer",
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(minutes=10),
            recovery_reply_id="rec_g2",
        )
        assert self._route(ig_connection) == 1

    def test_no_pending_no_route(self, ig_connection, no_real_send):
        _campaign(ig_connection)
        assert self._route(ig_connection) == 0
        assert not no_real_send.called

    def test_ttl_passed_pending_not_routed(self, ig_connection, no_real_send):
        c = _campaign(ig_connection, recovery_ttl_seconds=3600)
        _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(hours=2),
            recovery_reply_id="rec_g3",
        )
        assert self._route(ig_connection) == 0

    def test_free_owner_not_routed(self, ig_connection_free, no_real_send):
        c = _campaign(ig_connection_free)
        _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(minutes=10),
            recovery_reply_id="rec_g4",
        )
        assert self._route(ig_connection_free) == 0

    def test_self_not_routed(self, ig_connection, no_real_send):
        c = _campaign(ig_connection)
        _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(minutes=10),
            recovery_reply_id="rec_g5",
        )
        assert self._route(ig_connection, from_user_id=str(ig_connection.external_account_id)) == 0

    def test_webhook_reply_from_pending_user_routes(self, ig_connection, no_real_send):
        """스레드 답글(가장 자연스러운 응답)이 웹훅 대댓글 가드에 막히지 않고 복구 라우팅된다."""
        c = _campaign(ig_connection)
        _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(minutes=10),
            recovery_reply_id="rec_g6",
        )
        payload = {
            "field": "comments",
            "entry_id": str(ig_connection.external_account_id),
            "value": {
                "id": f"cmt_reply_{uuid.uuid4().hex[:8]}",
                "text": "수락했어요!",
                "parent_id": "cmt_parent_x",  # ← 스레드 답글
                "from": {"id": IGSID, "username": "buyer"},
                "media": {"id": "media_x"},
            },
        }
        res = tasks_mod.process_comment_and_send_dm.apply(args=[payload]).result
        assert res["status"] == "queued"
        assert res.get("routed") == 1

    def test_webhook_reply_without_pending_still_skipped(self, ig_connection, no_real_send):
        _campaign(ig_connection)
        payload = {
            "field": "comments",
            "entry_id": str(ig_connection.external_account_id),
            "value": {
                "id": f"cmt_reply_{uuid.uuid4().hex[:8]}",
                "text": "그냥 답글",
                "parent_id": "cmt_parent_y",
                "from": {"id": "someone_else_123", "username": "other"},
                "media": {"id": "media_x"},
            },
        }
        res = tasks_mod.process_comment_and_send_dm.apply(args=[payload]).result
        assert res["status"] == "skipped"
        assert res["reason"] == "is_reply"


# ===== 발송 시점 최후 방어선 (self recipient) =====


class TestSendTaskSelfGuard:
    def test_queued_self_log_skipped_at_send(self, ig_connection):
        """사고 백로그/우회 경로(requeue·revive)로 이미 적재된 셀프 로그는 발송 시점에 차단."""
        c = _campaign(ig_connection)
        self_log = _opening(
            c,
            recipient_user_id=ig_connection.username,  # prod 사고 형태 (username 키)
            recipient_username="",
            status=SentDMLog.Status.QUEUED,
        )
        res = tasks_mod.send_dm_task.apply(args=[str(self_log.id)]).result
        assert res["status"] == "skipped"
        assert res["reason"] == "self_recipient"
        self_log.refresh_from_db()
        assert self_log.status == SentDMLog.Status.SKIPPED

    def test_igsid_self_log_skipped_at_send(self, ig_connection):
        c = _campaign(ig_connection)
        self_log = _opening(
            c,
            recipient_user_id=str(ig_connection.external_account_id),
            status=SentDMLog.Status.QUEUED,
        )
        res = tasks_mod.send_dm_task.apply(args=[str(self_log.id)]).result
        assert res["status"] == "skipped"
        assert res["reason"] == "self_recipient"


# ===== 모델/표시 유닛 =====


class TestModelAndDisplay:
    def test_status_display_and_frontend_action(self):
        for st in ("recovery_pending", "recovery_delivered", "recovery_expired"):
            assert st in _STATUS_DISPLAY
            action = build_frontend_action(st)
            # 정의된 분기여야 함 (fallthrough=title==status 아님)
            assert action["title"] != st
            assert action["severity"] in ("info", "warning", "success", "error")

    def test_composition_randomness_and_combinations(self):
        # 경우의 수 = FIRST × MID × CLOSER × (TRAIL + 끝이모지 없음 1)
        expected = (
            len(RECOVERY_FIRST_PHRASES)
            * len(RECOVERY_MID_EMOJIS)
            * len(RECOVERY_CLOSER_PHRASES)
            * (len(RECOVERY_TRAIL_EMOJIS) + 1)
        )
        assert RECOVERY_REPLY_COMBINATIONS == expected
        assert RECOVERY_REPLY_COMBINATIONS >= 10000  # 강한 랜덤성
        # 조합 생성 유효성 + 다양성 (50회 생성 시 최소 10종 이상)
        outs = {compose_recovery_reply() for _ in range(50)}
        assert len(outs) >= 10
        assert all(o.strip() for o in outs)

    def test_v2_phrases_guide_recomment_not_inbound_dm(self):
        """v2 문구 계약: 재댓글 유도(댓글 언급 필수), 'DM 보내달라' 지시 금지."""
        for closer in RECOVERY_CLOSER_PHRASES:
            assert "댓글" in closer, closer
        joined = " ".join(RECOVERY_FIRST_PHRASES + RECOVERY_CLOSER_PHRASES)
        assert "DM 아무거나" not in joined
        assert "아무 DM" not in joined

    def test_recovery_terminal_and_delivered_sets(self):
        assert SentDMLog.Status.RECOVERY_DELIVERED in SentDMLog.TERMINAL_STATUSES
        assert SentDMLog.Status.RECOVERY_EXPIRED in SentDMLog.TERMINAL_STATUSES
        assert SentDMLog.Status.RECOVERY_PENDING not in SentDMLog.TERMINAL_STATUSES
        assert SentDMLog.Status.RECOVERY_DELIVERED in SentDMLog.DELIVERED_STATUSES
        assert SentDMLog.Status.RECOVERY_PENDING not in SentDMLog.REVIVABLE_STATUSES

    def test_recovery_enabled_default_is_true(self, ig_connection):
        # 필드 미지정 시 모델 기본값이 활성(True) — 프로 캠페인은 자동으로 복구 켜짐
        c = AutoDMCampaign.objects.create(
            ig_connection=ig_connection,
            trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
            name="default-check",
            message_template="안녕하세요!",
        )
        assert c.recovery_reply_enabled is True


# ===== 프로 전용 플랜 게이트 =====


class TestPlanGate:
    def test_free_owner_falls_through(self, ig_connection_free, no_reply_enqueue):
        # 복구를 켜도 무료 플랜이면 진입하지 않고 기존 실패 경로 유지 (fail-closed)
        c = _campaign(ig_connection_free)  # recovery_reply_enabled=True
        op = _opening(c)
        assert tasks_mod._maybe_enter_recovery(op, c, _err()) is False
        op.refresh_from_db()
        assert op.status == SentDMLog.Status.SUBMITTING  # 변경 없음
        assert not no_reply_enqueue.called

    def test_pro_owner_enters(self, ig_connection, no_reply_enqueue):
        c = _campaign(ig_connection)
        op = _opening(c)
        assert tasks_mod._maybe_enter_recovery(op, c, _err()) is True

    def test_available_flag_in_serializer(self, ig_connection, ig_connection_free):
        from apps.integrations.serializers import AutoDMCampaignSerializer

        pro_c = _campaign(ig_connection)
        free_c = _campaign(ig_connection_free)
        assert AutoDMCampaignSerializer(pro_c).data["recovery_reply_available"] is True
        assert AutoDMCampaignSerializer(free_c).data["recovery_reply_available"] is False


# ===== 추천 문구 엔드포인트 =====


class TestSuggestionsEndpoint:
    def _client(self, user):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_returns_unique_templates(self, ig_connection):
        user = ig_connection.workspace.owner
        client = self._client(user)
        url = "/api/v1/integrations/auto-dm-campaigns/recovery-reply-suggestions/"
        res = client.get(url, {"count": 30, "workspace_id": str(ig_connection.workspace_id)})
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 30
        assert len(body["templates"]) == 30
        assert len(set(body["templates"])) == 30  # 중복 없음
        assert body["available"] is True
        assert body["plan_required"] == "pro"
        assert body["generator_combinations"] >= 10000
        # v2 계약: 추천 문구는 재댓글 유도
        assert all("댓글" in t for t in body["templates"])

    def test_available_null_without_workspace(self, ig_connection):
        client = self._client(ig_connection.workspace.owner)
        url = "/api/v1/integrations/auto-dm-campaigns/recovery-reply-suggestions/"
        res = client.get(url)
        assert res.status_code == 200
        assert res.json()["available"] is None

    def test_available_false_for_free(self, ig_connection_free):
        client = self._client(ig_connection_free.workspace.owner)
        url = "/api/v1/integrations/auto-dm-campaigns/recovery-reply-suggestions/"
        res = client.get(url, {"workspace_id": str(ig_connection_free.workspace_id)})
        assert res.status_code == 200
        assert res.json()["available"] is False


# ===== v1 문구 정리 마이그레이션 =====


class TestV1TemplateCleanup:
    def test_migration_filter_logic(self):
        import importlib

        mig = importlib.import_module(
            "apps.integrations.migrations.0038_clear_v1_recovery_templates"
        )
        assert mig._is_v1_style("DM 전송에 실패했어요 😢 아무거나 보내주시면 다시 보내드릴게요!")
        assert mig._is_v1_style("이 계정으로 DM 하나만 주세요!")
        # v2 문구(댓글 유도)는 보존
        assert not mig._is_v1_style(
            "DM이 숨겨진 요청함으로 갔어요 🥲 수락하시고 다시 댓글 남겨주시면 바로 보내드릴게요!"
        )
        # DM 언급 없는 커스텀 문구 보존
        assert not mig._is_v1_style("이벤트 참여 감사합니다!")
