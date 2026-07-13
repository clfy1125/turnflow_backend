"""실패 DM 복구(recovery) 테스트.

커버리지:
  - Hop1: opening 2534025 실패 → RECOVERY_PENDING + 안내 대댓글 예약 (_maybe_enter_recovery)
  - 회귀 없음: 복구 비활성/다른 subcode/child → 기존 FAILED_PARAM 경로 유지
  - post_public_reply(recovery=True): 복구 템플릿·필드·멱등
  - Hop2: 인바운드 DM → IGSID DB 매칭 → 재전송 자식 생성 (process_inbound_recovery_dm)
  - 멱등(웹훅 재전송)·키워드 필터·TTL 만료
  - 성공 flip: 자식 ACCEPTED → 부모 RECOVERY_DELIVERED (_maybe_flip_recovery_delivered)
  - TTL 스윕(handle_recovery_pending_expiry)
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
from apps.integrations.services import InstagramCommentService
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
    return _make_connection(pro=True)


@pytest.fixture
def ig_connection_free(db):
    # 프로 미보유(무료) 소유자 — 플랜 게이트 검증용. 페이지 ID 를 분리해 매칭 간섭 방지.
    return _make_connection(pro=False, page_igid="ig_recovery_free_001", username="recfree")


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
        "recovery_reply_templates": ["DM 실패했어요 😢 아무거나 보내주시면 다시 보내드릴게요!"],
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


# ===== Hop1: 2534025 → RECOVERY_PENDING =====


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

    def test_empty_templates_uses_server_defaults(self, ig_connection, no_reply_enqueue):
        # 캠페인이 템플릿을 안 넣어도 서버 기본 세트로 폴백 → 복구 진입
        c = _campaign(ig_connection, recovery_reply_templates=[])
        op = _opening(c)
        assert tasks_mod._maybe_enter_recovery(op, c, _err()) is True
        # pick 은 기본 세트에서 무작위로 나옴 (빈 문자열 아님)
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


# ===== Hop2: 인바운드 DM → 재전송 =====


class TestInboundRecovery:
    def _payload(self, text="아무거나", sender=IGSID):
        return {
            "page_ig_user_id": PAGE_IGID,
            "sender_user_id": sender,
            "message_mid": "mid_1",
            "message_text": text,
        }

    def test_igsid_match_creates_child(self, ig_connection, no_real_send):
        c = _campaign(ig_connection)
        op = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        res = tasks_mod.process_inbound_recovery_dm.apply(args=[self._payload()]).result
        assert res["status"] == "resend_enqueued"
        child = SentDMLog.objects.get(parent_log=op)
        assert child.comment_id == ""  # user_id 경로
        assert child.status == SentDMLog.Status.QUEUED
        assert child.dm_kind == SentDMLog.DMKind.OPENING  # 버튼 재첨부 위해 계승
        assert child.gate_status == SentDMLog.GateStatus.PENDING
        assert no_real_send.called

    def test_duplicate_inbound_single_resend(self, ig_connection, no_real_send):
        c = _campaign(ig_connection)
        op = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        tasks_mod.process_inbound_recovery_dm.apply(args=[self._payload()])
        res2 = tasks_mod.process_inbound_recovery_dm.apply(args=[self._payload()]).result
        assert res2["status"] == "duplicate"
        assert SentDMLog.objects.filter(parent_log=op).count() == 1

    def test_no_match(self, ig_connection, no_real_send):
        _campaign(ig_connection)  # 대기 opening 없음
        res = tasks_mod.process_inbound_recovery_dm.apply(
            args=[self._payload(sender="someone_else")]
        ).result
        assert res["status"] == "no_match"

    def test_keyword_filter(self, ig_connection, no_real_send):
        c = _campaign(ig_connection, recovery_keyword="코워크")
        op = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        # 키워드 불일치 → 대기 유지
        r1 = tasks_mod.process_inbound_recovery_dm.apply(args=[self._payload(text="안녕")]).result
        assert r1["status"] == "keyword_no_match"
        assert not SentDMLog.objects.filter(parent_log=op).exists()
        # 키워드 일치 → 재전송
        r2 = tasks_mod.process_inbound_recovery_dm.apply(
            args=[self._payload(text="코워크 주세요")]
        ).result
        assert r2["status"] == "resend_enqueued"

    def test_ttl_expired_on_inbound(self, ig_connection, no_real_send):
        c = _campaign(ig_connection, recovery_ttl_seconds=3600)
        op = _opening(
            c,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at=timezone.now() - timedelta(hours=2),
        )
        res = tasks_mod.process_inbound_recovery_dm.apply(args=[self._payload()]).result
        assert res["status"] == "expired"
        op.refresh_from_db()
        assert op.status == SentDMLog.Status.RECOVERY_EXPIRED
        assert not no_real_send.called


# ===== 성공 flip / TTL 스윕 =====


class TestFlipAndExpiry:
    def test_child_accepted_flips_parent(self, ig_connection):
        c = _campaign(ig_connection)
        parent = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        child = _opening(c, comment_id="", parent_log=parent, status=SentDMLog.Status.ACCEPTED)
        assert tasks_mod._maybe_flip_recovery_delivered(child, c) is True
        parent.refresh_from_db()
        assert parent.status == SentDMLog.Status.RECOVERY_DELIVERED
        assert parent.is_delivered() is True

    def test_flip_ignores_non_recovery_parent(self, ig_connection):
        c = _campaign(ig_connection)
        parent = _opening(c, status=SentDMLog.Status.DELIVERED)
        child = _opening(c, comment_id="", parent_log=parent, status=SentDMLog.Status.ACCEPTED)
        assert tasks_mod._maybe_flip_recovery_delivered(child, c) is False

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


# ===== 모델/표시 유닛 =====


class TestModelAndDisplay:
    def test_matches_recovery_keyword(self, ig_connection):
        c = _campaign(ig_connection, recovery_keyword="")
        assert c.matches_recovery_keyword("아무거나") is True
        assert c.matches_recovery_keyword("   ") is False
        c.recovery_keyword = "코워크"
        assert c.matches_recovery_keyword("코워크 주세요") is True
        assert c.matches_recovery_keyword("안녕") is False

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

    def test_inbound_resend_gated_after_downgrade(self, ig_connection_free, no_real_send):
        # 다운그레이드 방어: RECOVERY_PENDING 이어도 소유자가 프로 미보유면 재전송 안 함(대기 유지).
        c = _campaign(ig_connection_free)
        op = _opening(
            c, status=SentDMLog.Status.RECOVERY_PENDING, recovery_pending_at=timezone.now()
        )
        payload = {
            "page_ig_user_id": ig_connection_free.external_account_id,
            "sender_user_id": IGSID,
            "message_mid": "mid_dg",
            "message_text": "아무거나",
        }
        res = tasks_mod.process_inbound_recovery_dm.apply(args=[payload]).result
        assert res["status"] == "plan_gated"
        assert not SentDMLog.objects.filter(parent_log=op).exists()
        assert not no_real_send.called
        op.refresh_from_db()
        assert op.status == SentDMLog.Status.RECOVERY_PENDING  # 대기 유지 → TTL 스윕이 만료 정산


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
