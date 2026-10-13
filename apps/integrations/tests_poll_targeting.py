"""댓글 누락 보정 폴러의 **대상 선정**과 웹훅 침묵 신호 테스트.

왜 이 파일이 생겼나 (2026-10-13 prod 실측)
──────────────────────────────────────────────────────────────────────
1. 폴러가 **활성 타깃 1,867 개 중 1,000 개만** 돌고 있었다. MISSED_COMMENT_POLL_MAX_TARGETS
   상한에 AutoDMCampaign.Meta.ordering=['-created_at'] 가 얹혀 "최신 캠페인 1,000 개"만
   잘려 들어가고 867 개(46%)는 영구 제외였다. 상한에 걸려도 아무 경고가 없어 몇 달간
   아무도 몰랐다.
2. 2026-09 중순부터 Meta 동작이 **반쪽만** 바뀌었다 — 연령 제한 게시물에서 private reply 는
   다시 성공하는데(실측 12,539건 중 2534066 0건) 댓글 웹훅은 여전히 안 온다. 제한 신호
   3종이 전부 2534066 건수를 전제로 하므로 지금은 아무것도 못 잡고, 사용자는 "DM 이 최대
   1시간 늦게 온다"만 겪는다. 남은 유일한 공짜 신호가 **웹훅 침묵**이다.

NOTE(pytest-tests-prefix): 파일명이 tests_*.py 라 자동수집 안 됨 → 경로 명시 실행:
    docker compose exec web pytest apps/integrations/tests_poll_targeting.py
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SeenComment
from apps.integrations.services import InstagramMediaService
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"pt_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="PT"
    )
    ws = Workspace.objects.create(name="PT WS", slug=f"pt-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="ptuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_pt"
    conn.save()
    return conn


def _campaign(conn, media_id, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": media_id,
        "name": "pt-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        "started_at": timezone.now() - timedelta(days=2),
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _age_campaign(campaign, *, ago):
    """auto_now_add 를 우회해 캠페인 생성·시작 시각을 과거로 민다."""
    past = timezone.now() - ago
    AutoDMCampaign.objects.filter(pk=campaign.pk).update(created_at=past, started_at=past)
    campaign.refresh_from_db()
    return campaign


def _seen(conn, cid, media_id, *, source, ago=timedelta(minutes=5)):
    row = SeenComment.objects.create(
        ig_connection=conn,
        comment_id=cid,
        media_id=media_id,
        source=source,
        expires_at=timezone.now() + timedelta(days=10),
    )
    # created_at 은 auto_now_add 라 create() 인자로는 안 먹는다 — 저장 후 밀어야 한다.
    SeenComment.objects.filter(pk=row.pk).update(created_at=timezone.now() - ago)
    row.refresh_from_db()
    return row


def _patch_comments(comments, paging_after=None):
    return patch.object(
        InstagramMediaService,
        "list_media_comments",
        return_value={"data": comments, "paging_after": paging_after},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. 웹훅 침묵 판정 (단일 소스)
# ─────────────────────────────────────────────────────────────────────────────


class TestWebhookSilenceSignal:
    def test_born_silent(self):
        """제한된 채로 태어난 게시물 — 웹훅이 한 건도 없다."""
        from apps.integrations.ig_content_restriction import is_webhook_silent

        now = timezone.now()
        assert is_webhook_silent(
            n_webhook=0, n_poll=50, last_webhook_at=None, last_poll_at=now, now=now
        )

    def test_born_silent_needs_enough_polls(self):
        """폴링 한두 건으로 침묵이라 부르지 않는다 (한산한 게시물 오탐 방지)."""
        from apps.integrations.ig_content_restriction import is_webhook_silent

        now = timezone.now()
        assert not is_webhook_silent(
            n_webhook=0, n_poll=1, last_webhook_at=None, last_poll_at=now, now=now
        )

    def test_went_silent_midway(self):
        """오다가 끊긴 모양. '웹훅 총 0건' 으로만 보면 이걸 통째로 놓친다.

        prod 18019915838941500 은 웹훅 2,998건을 정상 수신하다 중간에 끊겼다
        (제한이 걸린 시각이 거기서 특정된다).
        """
        from apps.integrations.ig_content_restriction import is_webhook_silent

        now = timezone.now()
        assert is_webhook_silent(
            n_webhook=2998,
            n_poll=722,
            last_webhook_at=now - timedelta(hours=9),
            last_poll_at=now - timedelta(minutes=2),
            now=now,
        )

    def test_healthy_media_not_silent(self):
        from apps.integrations.ig_content_restriction import is_webhook_silent

        now = timezone.now()
        assert not is_webhook_silent(
            n_webhook=500,
            n_poll=3,
            last_webhook_at=now - timedelta(minutes=1),
            last_poll_at=now - timedelta(minutes=2),
            now=now,
        )

    def test_dormant_media_not_silent(self):
        """활동이 멈춘 게시물은 침묵이라 불러봐야 할 일이 없다."""
        from apps.integrations.ig_content_restriction import is_webhook_silent

        now = timezone.now()
        assert not is_webhook_silent(
            n_webhook=0,
            n_poll=500,
            last_webhook_at=None,
            last_poll_at=now - timedelta(days=3),
            now=now,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 판정 결과가 침묵 플래그를 실어 나르는가 (프론트 계약)
# ─────────────────────────────────────────────────────────────────────────────


class TestRestrictionVerdictCarriesSilence:
    def test_runtime_flags_silence_without_changing_state(self, ig_connection):
        """침묵은 state 를 올리지 않는다.

        올리면 sweep_restricted_campaigns 가 **잘 나가고 있는 캠페인**을 정지시킨다.
        2026-09 중순부터 제한 게시물도 private reply 는 성공하기 때문이다.
        """
        from apps.integrations.ig_content_restriction import (
            STATE_UNKNOWN,
            check_runtime,
            restriction_payload,
        )

        media = "silent_" + uuid.uuid4().hex[:8]
        for i in range(8):
            _seen(ig_connection, "s%d_%s" % (i, media), media, source=SeenComment.Source.POLL)

        v = check_runtime(media)
        assert v.webhook_silent is True
        assert v.state == STATE_UNKNOWN  # 실패가 0건이라 제한 판정은 내리지 않는다
        assert v.blocking is False

        payload = restriction_payload(v)
        assert payload["webhook_silent"] is True
        assert payload["delay_notice"]["reason"] == "webhook_silent"
        assert payload["delay_notice"]["max_delay_minutes"] == 60
        # 기존 키는 하나도 사라지지 않는다 (프론트 계약 — 병기만 한다)
        for key in ("state", "source", "user_reason", "blocking", "user_message", "next_steps"):
            assert key in payload

    def test_healthy_media_has_no_delay_notice(self, ig_connection):
        from apps.integrations.ig_content_restriction import check_runtime, restriction_payload

        media = "ok_" + uuid.uuid4().hex[:8]
        for i in range(8):
            _seen(ig_connection, "w%d_%s" % (i, media), media, source=SeenComment.Source.WEBHOOK)

        v = check_runtime(media)
        assert v.webhook_silent is False
        assert restriction_payload(v)["delay_notice"] is None

    def test_inspect_media_propagates_silence(self, ig_connection):
        """best verdict 가 runtime 이 아니어도 침묵 플래그는 살아남아야 한다."""
        from apps.integrations.ig_content_restriction import inspect_media

        media = "prop_" + uuid.uuid4().hex[:8]
        for i in range(8):
            _seen(ig_connection, "x%d_%s" % (i, media), media, source=SeenComment.Source.POLL)

        v = inspect_media(media)
        assert v.webhook_silent is True


class TestGateKeysOnFailureNotClassification:
    """★ 잠재 함정 — ``IG_RESTRICTION_LIVE_CHECK_ENABLED`` 를 켜는 순간 터질 뻔한 것.

    인스타의 '연령 제한' 분류만으로 캠페인 생성을 막으면, **DM 이 잘 나가고 있는** 게시물을
    409 로 막고 "다른 게시물로 만드세요" 라고 정반대 안내를 하게 된다.
    """

    def test_live_restricted_without_failures_does_not_block(self, db, settings):
        from unittest.mock import MagicMock

        import apps.integrations.ig_content_restriction as icr

        settings.IG_RESTRICTION_LIVE_CHECK_ENABLED = True
        media = "liveonly_" + uuid.uuid4().hex[:8]
        resp = MagicMock(status_code=200, text="x" * 700_000 + icr._LIVE_MARKER)

        with patch("apps.integrations.ig_content_restriction.requests.get", return_value=resp):
            v = icr.inspect_media(
                media, permalink="https://www.instagram.com/reel/LIVEONLY/", allow_live=True
            )

        assert v.state == icr.STATE_RESTRICTED  # 분류는 사실대로 전달한다
        assert v.blocking is False, "발송 실패 이력이 없는데 생성을 막았다"

        payload = icr.restriction_payload(v)
        assert "최대 1시간" in payload["user_message"]
        assert "보낼 수 없습니다" not in payload["user_message"]

    def test_live_restricted_with_failure_history_still_blocks(self, ig_connection, settings):
        """실제로 2534066 이 쌓인 게시물은 그대로 막는다 (기존 방어 유지)."""
        from unittest.mock import MagicMock

        import apps.integrations.ig_content_restriction as icr
        from apps.integrations.models import SentDMLog

        settings.IG_RESTRICTION_LIVE_CHECK_ENABLED = True
        media = "deadmedia_" + uuid.uuid4().hex[:8]
        campaign = _campaign(ig_connection, media, name="dead")
        for i in range(icr.HISTORY_MIN_FAILURES + 2):
            SentDMLog.objects.create(
                campaign=campaign,
                comment_id="f%d_%s" % (i, media),
                media_id=media,
                recipient_user_id="u%d" % i,
                recipient_username="u%d" % i,
                message_sent="x",
                idempotency_key=uuid.uuid4().hex,  # 빈 값이면 UNIQUE 충돌
                status=SentDMLog.Status.FAILED_NO_TRACE,
                error_code="200",
                error_subcode="2534066",
            )

        resp = MagicMock(status_code=200, text="x" * 700_000 + icr._LIVE_MARKER)
        with patch("apps.integrations.ig_content_restriction.requests.get", return_value=resp):
            v = icr.inspect_media(
                media, permalink="https://www.instagram.com/reel/DEAD/", allow_live=True
            )

        assert v.blocking is True, "실패가 쌓인 게시물인데 게이트가 풀렸다"


# ─────────────────────────────────────────────────────────────────────────────
# 3. 폴링 대상 선정 — 커버리지 결함 회귀 방어
# ─────────────────────────────────────────────────────────────────────────────


class TestPollTargetSelection:
    def test_selection_is_by_activity_not_by_created_at(self, ig_connection, settings):
        """★ 결함 그 자체의 회귀 테스트 — 우선순위가 '나이'가 아니라 '활동성'인가.

        옛 구현은 ``[:상한]`` 에 ``Meta.ordering=['-created_at']`` 가 얹혀 **최신순**으로
        잘랐다. 그래서 90일 된 활동 중 캠페인이 60일 된 휴면 캠페인들에게 밀려 나갔다.
        이 테스트는 그 역전을 못 박는다: 오래됐어도 활동 중이면 들어오고,
        더 최신이어도 휴면이면 (회전 슬라이스를 끄면) 안 들어온다.

        NOTE(test-db-not-clean): dev DB 에 남의 캠페인이 섞여 있으므로 전역 개수가 아니라
        **내가 만든 것**만 본다.
        """
        from apps.integrations.tasks import _select_poll_targets

        settings.MISSED_COMMENT_POLL_MAX_TARGETS = 5000
        settings.MISSED_COMMENT_POLL_IDLE_SLICE = 0  # 휴면 회전 끔 → 휴면은 안 들어온다

        old_media = "old_" + uuid.uuid4().hex[:8]
        old = _campaign(ig_connection, old_media, name="old")
        _age_campaign(old, ago=timedelta(days=90))
        _seen(ig_connection, "c_" + old_media, old_media, source=SeenComment.Source.POLL)

        newer_idle = []
        for i in range(5):  # 더 최신 캠페인들 — 활동 없음(= idle)
            mid = "new%d_%s" % (i, uuid.uuid4().hex[:6])
            newer_idle.append(mid)
            _age_campaign(_campaign(ig_connection, mid, name="new%d" % i), ago=timedelta(days=60))

        targets, meta = _select_poll_targets(timezone.now())
        picked = {m for _c, m in targets}

        assert old_media in picked, "활동 중인 오래된 캠페인이 빠졌다"
        assert not (picked & set(newer_idle)), "휴면인데 더 최신이라는 이유로 들어왔다"
        assert meta["hot"] >= 1

    def test_cap_drops_idle_first_never_hot(self, ig_connection, settings):
        """상한이 걸리면 **휴면부터** 버린다 — 활동 중인 것을 먼저 버리지 않는다."""
        from apps.integrations.tasks import _select_poll_targets

        settings.MISSED_COMMENT_POLL_MAX_TARGETS = 1
        settings.MISSED_COMMENT_POLL_IDLE_SLICE = 50

        idle_media = []
        for i in range(4):
            mid = "capidle%d_%s" % (i, uuid.uuid4().hex[:6])
            idle_media.append(mid)
            _age_campaign(_campaign(ig_connection, mid, name="ci%d" % i), ago=timedelta(days=60))

        targets, meta = _select_poll_targets(timezone.now())
        picked = {m for _c, m in targets}

        assert meta["capped"] is True
        assert len(targets) == 1
        assert not (picked & set(idle_media)), "상한이 걸렸는데 휴면이 활동분을 밀어냈다"

    def test_webhook_silent_media_is_hot(self, ig_connection, settings):
        """웹훅이 끊긴 게시물은 아무리 오래됐어도 매 회차 돈다."""
        from apps.integrations.tasks import _select_poll_targets

        settings.MISSED_COMMENT_POLL_IDLE_SLICE = 0
        media = "silent_" + uuid.uuid4().hex[:8]
        c = _campaign(ig_connection, media, name="silent")
        _age_campaign(c, ago=timedelta(days=60))
        for i in range(8):
            _seen(ig_connection, "p%d_%s" % (i, media), media, source=SeenComment.Source.POLL)

        targets, meta = _select_poll_targets(timezone.now())
        assert (ig_connection.id, media) in targets
        assert meta["hot"] >= 1

    def test_fresh_campaign_is_hot_even_without_ledger(self, ig_connection, settings):
        """갓 켠 캠페인은 장부가 비어 있다 — 휴면 회전에 걸리면 첫 보정이 몇 시간 늦는다."""
        from apps.integrations.tasks import _select_poll_targets

        settings.MISSED_COMMENT_POLL_IDLE_SLICE = 0
        media = "fresh_" + uuid.uuid4().hex[:8]
        _campaign(ig_connection, media, name="fresh", started_at=timezone.now())

        targets, _meta = _select_poll_targets(timezone.now())
        assert (ig_connection.id, media) in targets

    def test_idle_targets_rotate_across_runs(self, ig_connection, settings):
        """휴면은 회차마다 다른 슬라이스를 돈다 — 상한에 걸려 영영 제외되지 않게."""
        from django.core.cache import cache

        from apps.integrations.tasks import _IDLE_CURSOR_KEY, _select_poll_targets

        cache.delete(_IDLE_CURSOR_KEY)
        settings.MISSED_COMMENT_POLL_IDLE_SLICE = 2

        idle_media = []
        for i in range(6):
            mid = "idle%d_%s" % (i, uuid.uuid4().hex[:6])
            idle_media.append(mid)
            _age_campaign(_campaign(ig_connection, mid, name="idle%d" % i), ago=timedelta(days=60))

        first, meta1 = _select_poll_targets(timezone.now())
        cursor1 = cache.get(_IDLE_CURSOR_KEY)
        second, _m2 = _select_poll_targets(timezone.now())
        cursor2 = cache.get(_IDLE_CURSOR_KEY)

        assert meta1["idle"] >= len(idle_media)
        assert meta1["idle_picked"] == 2
        # NOTE(test-db-not-clean): 남의 휴면 캠페인이 섞여 내 것이 첫 슬라이스에 안 올 수 있다.
        #   그래서 '내 미디어가 뽑혔나'가 아니라 **커서가 전진했나 / 회차가 달라졌나**를 본다.
        assert cursor1 and cursor2 and cursor1 != cursor2, "커서가 안 움직였다"
        assert set(first) != set(second), "회차마다 같은 휴면 타깃만 반복해서 돈다"
        cache.delete(_IDLE_CURSOR_KEY)

    def test_degrades_to_full_scan_when_ledger_query_fails(self, ig_connection, settings):
        """장부 집계가 깨지면 커버리지를 잃느니 비용을 쓴다 (전수 폴백)."""
        from apps.integrations.tasks import _select_poll_targets

        settings.MISSED_COMMENT_POLL_IDLE_SLICE = 0
        media = "deg_" + uuid.uuid4().hex[:8]
        _age_campaign(_campaign(ig_connection, media, name="deg"), ago=timedelta(days=60))

        with patch("apps.integrations.tasks._poll_ledger_digest", return_value=None):
            targets, meta = _select_poll_targets(timezone.now())

        assert meta["degraded"] is True
        assert (ig_connection.id, media) in targets

    def test_paused_campaign_is_not_polled(self, ig_connection, settings):
        """정지된 캠페인은 대상이 아니다 (기존 계약 — 티어링이 이걸 깨면 안 된다)."""
        from apps.integrations.tasks import _select_poll_targets

        settings.MISSED_COMMENT_POLL_IDLE_SLICE = 500
        media = "paused_" + uuid.uuid4().hex[:8]
        _campaign(ig_connection, media, name="paused", status=AutoDMCampaign.Status.PAUSED)

        targets, _meta = _select_poll_targets(timezone.now())
        assert (ig_connection.id, media) not in targets


# ─────────────────────────────────────────────────────────────────────────────
# 4. 벽시계 예산 — 조용히 자르지 않는다
# ─────────────────────────────────────────────────────────────────────────────


class TestSilentMediaFastLane:
    """웹훅이 끊긴 게시물만 3분마다 도는 레인 (2026-10-13)."""

    def test_silent_and_active_media_is_selected(self, ig_connection, settings):
        from apps.integrations.tasks import _select_silent_targets

        settings.SILENT_MEDIA_POLL_ACTIVITY_MINUTES = 120
        media = "fast_" + uuid.uuid4().hex[:8]
        _campaign(ig_connection, media, name="fast")
        for i in range(8):
            _seen(ig_connection, "f%d_%s" % (i, media), media, source=SeenComment.Source.POLL)

        targets, meta = _select_silent_targets(timezone.now())
        assert (ig_connection.id, media) in targets
        assert meta["capped"] is False

    def test_quiet_silent_media_is_demoted(self, ig_connection, settings):
        """웹훅이 끊겼어도 **최근 댓글이 없으면** 빠른 레인에서 뺀다.

        조용한 게시물을 3분마다 두드릴 이유가 없다 — 1시간 레인이 맡는다.
        실측상 침묵 게시물 11개 중 4개가 댓글의 대부분이라 이 한 줄이 호출을 절반 이하로 줄인다.
        """
        from apps.integrations.tasks import _select_silent_targets

        settings.SILENT_MEDIA_POLL_ACTIVITY_MINUTES = 120
        media = "quiet_" + uuid.uuid4().hex[:8]
        _campaign(ig_connection, media, name="quiet")
        for i in range(8):
            _seen(
                ig_connection,
                "q%d_%s" % (i, media),
                media,
                source=SeenComment.Source.POLL,
                ago=timedelta(hours=5),  # 침묵 판정 창(6h) 안이지만 활동 창(2h) 밖
            )

        targets, _meta = _select_silent_targets(timezone.now())
        assert (ig_connection.id, media) not in targets

    def test_healthy_media_is_not_selected(self, ig_connection):
        """웹훅이 잘 오는 게시물은 빠른 레인 대상이 아니다."""
        from apps.integrations.tasks import _select_silent_targets

        media = "healthy_" + uuid.uuid4().hex[:8]
        _campaign(ig_connection, media, name="healthy")
        for i in range(8):
            _seen(ig_connection, "h%d_%s" % (i, media), media, source=SeenComment.Source.WEBHOOK)

        targets, _meta = _select_silent_targets(timezone.now())
        assert (ig_connection.id, media) not in targets

    def test_cap_is_reported_not_silently_trimmed(self, ig_connection, settings):
        from apps.integrations.tasks import _select_silent_targets

        settings.SILENT_MEDIA_POLL_MAX_TARGETS = 1
        for j in range(3):
            media = "cap%d_%s" % (j, uuid.uuid4().hex[:6])
            _campaign(ig_connection, media, name="cap%d" % j)
            for i in range(8):
                _seen(
                    ig_connection,
                    "c%d_%d_%s" % (j, i, media),
                    media,
                    source=SeenComment.Source.POLL,
                )

        targets, meta = _select_silent_targets(timezone.now())
        assert len(targets) == 1
        assert meta["capped"] is True

    def test_overlapping_run_is_skipped(self, ig_connection, settings):
        """회차가 밀려 겹치면 같은 게시물을 동시에 긁는다 — 락으로 막는다."""
        from django.core.cache import cache

        from apps.integrations.tasks import _SILENT_POLL_LOCK_KEY, poll_silent_media_comments

        cache.delete(_SILENT_POLL_LOCK_KEY)
        cache.add(_SILENT_POLL_LOCK_KEY, "1", 60)  # 직전 회차가 도는 중인 상태
        try:
            out = poll_silent_media_comments()
        finally:
            cache.delete(_SILENT_POLL_LOCK_KEY)
        assert out == {"skipped": "already_running"}

    def test_disabled_flag_is_noop(self, settings):
        from apps.integrations.tasks import poll_silent_media_comments

        settings.SILENT_MEDIA_POLL_ENABLED = False
        assert poll_silent_media_comments() == {"enabled": False}

    def test_end_to_end_sends_missed_dm(self, ig_connection, settings):
        """빠른 레인도 1시간 레인과 **같은 발송 경로**를 쓴다."""
        from django.core.cache import cache

        from apps.integrations.tasks import _SILENT_POLL_LOCK_KEY, poll_silent_media_comments

        cache.delete(_SILENT_POLL_LOCK_KEY)
        settings.SILENT_MEDIA_POLL_ENABLED = True
        media = "e2e_" + uuid.uuid4().hex[:8]
        _campaign(ig_connection, media, name="e2e", started_at=timezone.now() - timedelta(days=1))
        # 이미 폴링으로만 들어온 이력 → 침묵 + 활동 판정 성립
        for i in range(8):
            _seen(ig_connection, "e%d_%s" % (i, media), media, source=SeenComment.Source.POLL)

        new_comment = {
            "id": "new_" + media,
            "text": "가격 문의",
            "username": "buyer_fast",
            "timestamp": (timezone.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
        }
        with (
            _patch_comments([new_comment]),
            patch("apps.integrations.tasks.send_dm_task.delay") as send,
        ):
            out = poll_silent_media_comments()

        assert out["polled"] == 1
        assert out["misses"] == 1
        assert send.called
        cache.delete(_SILENT_POLL_LOCK_KEY)


class TestPollBudget:
    def test_budget_stops_run_and_alerts(self, ig_connection, settings):
        """예산을 넘으면 중단하되 **반드시 운다**.

        이 결함이 몇 달간 안 보였던 이유가 정확히 "잘려도 아무 말을 안 해서" 였다.
        """
        from apps.integrations.tasks import poll_missed_comments

        settings.MISSED_COMMENT_POLL_MAX_SECONDS = -1  # 즉시 초과
        _campaign(ig_connection, "budget_" + uuid.uuid4().hex[:8])

        with (
            _patch_comments([]),
            patch("apps.core.telegram.send_telegram_notification") as alert,
        ):
            result = poll_missed_comments()

        assert result["budget_stopped"] is True
        assert result["polled"] == 0
        assert alert.called, "전수를 못 돌았는데 경고가 없다"

    def test_normal_run_does_not_alert(self, ig_connection, settings):
        """정상 회차는 조용해야 한다 (경보 피로 방지)."""
        from apps.integrations.tasks import poll_missed_comments

        settings.MISSED_COMMENT_POLL_MAX_SECONDS = 1500
        settings.MISSED_COMMENT_POLL_MAX_TARGETS = 5000
        _campaign(ig_connection, "normal_" + uuid.uuid4().hex[:8])

        with (
            _patch_comments([]),
            patch("apps.core.telegram.send_telegram_notification") as alert,
        ):
            result = poll_missed_comments()

        assert result["budget_stopped"] is False
        assert not alert.called
