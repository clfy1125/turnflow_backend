"""동일 수신자 쿨다운 v2 — 세 손잡이 테스트 (2026-08-31).

판정 단일 소스 = ``tasks._recipient_cooldown_skip_reason``. 종전 v1 은 "창(5분) 안에 로그가
하나라도 있으면 차단"(``.exists()``)이었고, 이걸 아래 셋으로 분해했다.

  손잡이 1 — 통수 모수를 '살아있는 루트 DM' 으로 좁힘 (실패·reward·재안내 제외)
  손잡이 2 — 개수와 무관한 최소 간격(DM_RECIPIENT_MIN_GAP_SECONDS)
  손잡이 3 — 완화는 같은 게시물 안에서만 (media_id 결측/타 게시물 → 허용량 1)

★ 이 패치는 **순수 완화**여야 한다 — v1 에서 통과했던 건을 새로 막으면 안 된다.
  `TestNoNewBlocking` 이 그 방향을 지킨다.

NOTE(test-db-not-clean): 내가 만든 캠페인/로그 기준으로만 단언한다.
NOTE: broker 없이 enqueue 경로만 검증 — send_dm_task.delay 는 모킹.
NOTE(pytest-tests-prefix): 파일명이 `test_` 라 자동수집된다.
"""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.integrations import tasks as tasks_mod
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace

PAGE_IGID = "ig_cooldown_page_001"
IGSID = "igsid_cooldown_001"
MEDIA_A = "media_cooldown_aaa"
MEDIA_B = "media_cooldown_bbb"


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"cd_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="CD"
    )
    ws = Workspace.objects.create(name="CD WS", slug=f"cd-ws-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=PAGE_IGID,
        username="cduser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_cd"
    conn.save()
    return conn


@pytest.fixture
def no_real_send(monkeypatch):
    """send_dm_task.delay 차단 — 큐 적재 판정만 검증한다."""
    monkeypatch.setattr(tasks_mod.send_dm_task, "delay", lambda *a, **k: None)


@pytest.fixture
def relaxed(settings):
    """완화 기본값 고정 — 5분 창 / 3통 / 최소 간격 60초."""
    settings.DM_RECIPIENT_COOLDOWN_SECONDS = 300
    settings.DM_RECIPIENT_MAX_PER_COOLDOWN = 3
    settings.DM_RECIPIENT_MIN_GAP_SECONDS = 60
    return settings


def _campaign(conn, *, media_id=MEDIA_A, gate=False):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        name=f"cd-{uuid.uuid4().hex[:6]}",
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        media_id=media_id,
        keyword_filter=["신청"],
        opening_message_template="안녕하세요! 링크 보내드려요",
        reward_message_template="본 링크입니다" if gate else "",
        follow_gate_enabled=gate,
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _log(
    campaign,
    *,
    age_seconds: int,
    status=SentDMLog.Status.ACCEPTED,
    media_id=MEDIA_A,
    dm_kind=SentDMLog.DMKind.STANDALONE,
    parent_log=None,
    recovery_reply_id="",
    recipient_user_id=IGSID,
    recipient_username="commenter",
):
    """created_at 을 강제로 과거로 밀어 넣은 로그 1건.

    created_at 은 auto_now_add 라 생성 후 update() 로 덮어야 한다.
    """
    log = SentDMLog.objects.create(
        campaign=campaign,
        idempotency_key=uuid.uuid4().hex,
        comment_id=f"c_{uuid.uuid4().hex[:10]}",
        comment_text="신청",
        media_id=media_id,
        recipient_user_id=recipient_user_id,
        recipient_username=recipient_username,
        message_sent="안녕하세요",
        status=status,
        dm_kind=dm_kind,
        parent_log=parent_log,
        recovery_reply_id=recovery_reply_id,
        webhook_payload={},
    )
    SentDMLog.objects.filter(pk=log.pk).update(
        created_at=timezone.now() - timedelta(seconds=age_seconds)
    )
    log.refresh_from_db()
    return log


def _enqueue(campaign, *, media_id=MEDIA_A, user_id=IGSID, username="commenter"):
    return tasks_mod._enqueue_send_dm(
        campaign=campaign,
        comment_id=f"new_{uuid.uuid4().hex[:10]}",
        comment_text="신청",
        from_user_id=user_id,
        from_username=username,
        webhook_payload={},
        media_id=media_id,
    )


# ───────────────────────── 손잡이 2 — 최소 간격 ─────────────────────────


class TestMinGap:
    def test_within_min_gap_blocks_even_when_count_allows(
        self, ig_connection, relaxed, no_real_send
    ):
        """통수는 남았어도(1/3) 직전 발송이 60초 안이면 차단 — '10초 3연발' 방지."""
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=10)
        res = _enqueue(camp)
        assert res["status"] == "skipped"
        assert res["reason"] == "recipient_cooldown_min_gap_60s"

    def test_past_min_gap_allows_second(self, ig_connection, relaxed, no_real_send):
        """60초를 넘겼고 통수가 남았으면 두 번째가 나간다 (v1 에서는 차단됐던 지점)."""
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=90)
        res = _enqueue(camp)
        assert res["status"] == "enqueued"

    def test_min_gap_zero_disables_gap_check(self, ig_connection, relaxed, no_real_send):
        camp = _campaign(ig_connection)
        relaxed.DM_RECIPIENT_MIN_GAP_SECONDS = 0
        _log(camp, age_seconds=1)
        assert _enqueue(camp)["status"] == "enqueued"


# ───────────────────────── 손잡이 1 — 통수 모수 ─────────────────────────


class TestAllowanceCounting:
    def test_third_send_allowed(self, ig_connection, relaxed, no_real_send):
        """창 안 루트 DM 2건 + 간격 충족 → 3번째는 나간다."""
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=90)
        _log(camp, age_seconds=150)
        assert _enqueue(camp)["status"] == "enqueued"

    def test_fourth_send_blocked_by_allowance(self, ig_connection, relaxed, no_real_send):
        """루트 DM 3건이 차 있으면 간격을 넘겨도 상한으로 차단."""
        camp = _campaign(ig_connection)
        for age in (90, 150, 210):
            _log(camp, age_seconds=age)
        res = _enqueue(camp)
        assert res["status"] == "skipped"
        assert res["reason"] == "recipient_cooldown_max_3_per_300s"

    def test_failure_in_window_blocks_entirely(self, ig_connection, relaxed, no_real_send):
        """실패 로그는 통수를 소진하지 않고 **전체 차단** — 완화분이 '실패 3회'로 쓰이면 안 된다."""
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=90, status=SentDMLog.Status.FAILED_PARAM)
        res = _enqueue(camp)
        assert res["status"] == "skipped"
        assert res["reason"] == "recipient_cooldown_recent_failure_300s"

    def test_unposted_recovery_pending_blocks(self, ig_connection, relaxed, no_real_send):
        """안내 댓글이 아직 안 붙은 복구대기는 면제 대상이 아니다(재시도 무한반복 방지)."""
        camp = _campaign(ig_connection)
        _log(
            camp,
            age_seconds=90,
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_reply_id="",
        )
        res = _enqueue(camp)
        assert res["status"] == "skipped"
        assert res["reason"] == "recipient_cooldown_recent_failure_300s"

    def test_posted_recovery_pending_is_exempt(self, ig_connection, relaxed, no_real_send):
        """게시된 복구 안내는 모수에서 제외 — '수락 후 재댓글' 정상 흐름 보존(v1 예외 유지)."""
        camp = _campaign(ig_connection)
        _log(
            camp,
            age_seconds=10,  # 최소 간격 안이어도 통과해야 한다
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_reply_id="reply_123",
        )
        assert _enqueue(camp)["status"] == "enqueued"

    def test_reward_child_does_not_consume_allowance(self, ig_connection, relaxed, no_real_send):
        """게이트 캠페인의 reward(자식 DM)는 통수를 먹지 않는다 → 게이트/비게이트 동일 동작."""
        camp = _campaign(ig_connection, gate=True)
        opening = _log(camp, age_seconds=200, dm_kind=SentDMLog.DMKind.OPENING)
        # reward 2건을 더 붙여도 루트는 여전히 1건
        _log(camp, age_seconds=150, dm_kind=SentDMLog.DMKind.REWARD, parent_log=opening)
        _log(camp, age_seconds=100, dm_kind=SentDMLog.DMKind.REWARD, parent_log=opening)
        res = _enqueue(camp)
        assert res["status"] == "enqueued"

    def test_outside_window_not_counted(self, ig_connection, relaxed, no_real_send):
        camp = _campaign(ig_connection)
        for age in (400, 500, 600):
            _log(camp, age_seconds=age)
        assert _enqueue(camp)["status"] == "enqueued"

    def test_other_campaign_logs_not_counted(self, ig_connection, relaxed, no_real_send):
        """쿨다운은 (캠페인 × 수신자) 스코프 — 다른 캠페인 발송은 모수 밖."""
        camp_a = _campaign(ig_connection)
        camp_b = _campaign(ig_connection, media_id=MEDIA_B)
        _log(camp_b, age_seconds=10, media_id=MEDIA_B)
        assert _enqueue(camp_a)["status"] == "enqueued"

    def test_other_recipient_not_counted(self, ig_connection, relaxed, no_real_send):
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=10, recipient_user_id="igsid_someone_else", recipient_username="x")
        assert _enqueue(camp)["status"] == "enqueued"


# ───────────────────────── 손잡이 3 — 게시물 범위 ─────────────────────────


class TestMediaScope:
    def test_different_media_falls_back_to_one(self, ig_connection, relaxed, no_real_send):
        """다른 게시물에 이미 보냈으면 허용량 1 — any_media 팬아웃은 종전대로."""
        camp = _campaign(ig_connection, media_id="")  # any_media 성격
        _log(camp, age_seconds=90, media_id=MEDIA_A)
        res = _enqueue(camp, media_id=MEDIA_B)
        assert res["status"] == "skipped"
        assert res["reason"] == "recipient_cooldown_max_1_per_300s"

    def test_same_media_relaxes(self, ig_connection, relaxed, no_real_send):
        camp = _campaign(ig_connection, media_id="")
        _log(camp, age_seconds=90, media_id=MEDIA_A)
        assert _enqueue(camp, media_id=MEDIA_A)["status"] == "enqueued"

    def test_missing_media_on_new_send_falls_back_to_one(
        self, ig_connection, relaxed, no_real_send
    ):
        """이번 발송의 media 를 모르면 완화하지 않는다(보수적 폴백)."""
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=90, media_id=MEDIA_A)
        res = _enqueue(camp, media_id="")
        assert res["reason"] == "recipient_cooldown_max_1_per_300s"

    def test_legacy_row_without_media_falls_back_to_one(self, ig_connection, relaxed, no_real_send):
        """배포 직전에 생긴 media_id="" 행(구 데이터)이 창 안에 있으면 완화하지 않는다."""
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=90, media_id="")
        res = _enqueue(camp, media_id=MEDIA_A)
        assert res["reason"] == "recipient_cooldown_max_1_per_300s"

    def test_media_id_persisted_on_log(self, ig_connection, relaxed, no_real_send):
        camp = _campaign(ig_connection)
        res = _enqueue(camp, media_id=MEDIA_A)
        assert res["status"] == "enqueued"
        assert SentDMLog.objects.get(id=res["log_id"]).media_id == MEDIA_A


# ───────────────────────── 회귀 방어 — 순수 완화인가 ─────────────────────────


class TestNoNewBlocking:
    """MAX=1 이면 v1 과 동일해야 한다 (즉시 롤백 경로의 정당성)."""

    @pytest.mark.parametrize(
        "status",
        [
            SentDMLog.Status.QUEUED,
            SentDMLog.Status.ACCEPTED,
            SentDMLog.Status.DELIVERED,
            SentDMLog.Status.READ,
            SentDMLog.Status.FAILED_PARAM,
            SentDMLog.Status.FAILED_TOKEN,
            SentDMLog.Status.SKIPPED,
        ],
    )
    def test_max_one_blocks_like_v1(self, ig_connection, relaxed, no_real_send, status):
        camp = _campaign(ig_connection)
        relaxed.DM_RECIPIENT_MAX_PER_COOLDOWN = 1
        relaxed.DM_RECIPIENT_MIN_GAP_SECONDS = 0  # 간격이 아니라 통수/실패로 막히는지 본다
        _log(camp, age_seconds=200, status=status)
        res = _enqueue(camp)
        assert res["status"] == "skipped"
        assert res["reason"].startswith("recipient_cooldown")

    def test_cooldown_zero_disables_guard(self, ig_connection, relaxed, no_real_send):
        camp = _campaign(ig_connection)
        relaxed.DM_RECIPIENT_COOLDOWN_SECONDS = 0
        _log(camp, age_seconds=1)
        assert _enqueue(camp)["status"] == "enqueued"

    def test_username_keyspace_still_matches(self, ig_connection, relaxed, no_real_send):
        """폴링(username 키) 로그가 웹훅(IGSID) 발송을 여전히 막는다 — v1 매칭 유지."""
        camp = _campaign(ig_connection)
        _log(camp, age_seconds=10, recipient_user_id="commenter", recipient_username="commenter")
        res = _enqueue(camp, user_id=IGSID, username="commenter")
        assert res["status"] == "skipped"


# ───────────────────────── 스토리 답장 경로 ─────────────────────────


class TestStoryReplyPathUnchanged:
    def test_story_reply_keeps_one_per_window(self, ig_connection, relaxed, no_real_send):
        """스토리 답장은 media_id 를 안 넘겨 허용량 1 = 종전 동작 유지."""
        camp = AutoDMCampaign.objects.create(
            ig_connection=ig_connection,
            name=f"cd-story-{uuid.uuid4().hex[:6]}",
            trigger_type=AutoDMCampaign.TriggerType.STORY_REPLY,
            media_id="story_abc",
            keyword_filter=[],
            opening_message_template="스토리 답장 감사합니다",
            status=AutoDMCampaign.Status.ACTIVE,
        )
        _log(camp, age_seconds=200, media_id="")
        res = tasks_mod._enqueue_send_dm_for_story_reply(
            campaign=camp,
            story_id="story_abc",
            message_mid=f"mid_{uuid.uuid4().hex[:10]}",
            message_text="좋아요",
            sender_user_id=IGSID,
            sender_username="commenter",
            payload={},
        )
        assert res["status"] == "skipped"
        assert res["reason"] == "recipient_cooldown_max_1_per_300s"
