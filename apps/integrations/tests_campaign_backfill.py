"""기존 댓글 소급 발송(backfill) 테스트.

배경: 실사용 순서가 "게시물 업로드 → 댓글 축적 → 자동화 켜기" 라서, 캠페인 시작 이전 댓글이
통째로 누락된다(2026-08-14 @highestlevel33 실측: 게시물 댓글 60건 중 32건 DM 0건).
폴링 보정은 ①앵커 종료 ②per-campaign baseline 두 겹으로 이걸 **의도적으로** 막고 있으므로,
소급은 별도 1회성 태스크가 담당한다.

커버리지:
  - baseline 무시: 캠페인 시작 이전 댓글도 enqueue (poll 경로와 정반대 — 이게 존재 이유)
  - 앵커 무시: SeenComment 에 이미 있는 댓글에서 멈추지 않는다
  - 1회성 락: 두 번 실행해도 두 번째는 no-op (재개/중복 발사 방어)
  - 토글 OFF / media 없음 / 비활성 → skip
  - 예약: 시작 전에는 락도 잡지 않고, 시작 시각이 지나면 그때 실행된다
  - 7일 창 하한, 상한(capped), self/대댓글/키워드 필터
  - 스캐너: 대기열 선정 규칙 (예약 전 제외, 이미 실행됨 제외)
  - copy(): 소급 락을 복사하지 않는다 (복사본은 다시 소급돼야 한다)

NOTE(test-db-not-clean): 전역 카운트 대신 내가 만든 캠페인 기준으로 단언한다.
NOTE(pytest-tests-prefix): 파일명이 tests_*.py 라 자동수집 안 됨 → 경로 명시 실행:
    docker compose exec web pytest apps/integrations/tests_campaign_backfill.py
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SeenComment, SentDMLog
from apps.integrations.services import InstagramMediaService
from apps.workspace.models import Membership, Workspace

MEDIA_ID = "media_backfill_x"


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"bf_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="BF"
    )
    ws = Workspace.objects.create(name="BF WS", slug=f"bf-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="bfuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_bf"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": MEDIA_ID,
        "name": "bf-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        # 캠페인은 방금 시작 → 아래 댓글들은 전부 "시작 이전" 이 된다 (소급 대상)
        "started_at": timezone.now(),
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _ts(ago: timedelta) -> str:
    return (timezone.now() - ago).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _comment(cid, *, text="가격 문의", username="buyer1", ago=timedelta(hours=1), **extra) -> dict:
    c = {"id": cid, "text": text, "username": username, "timestamp": _ts(ago)}
    c.update(extra)
    return c


def _patch_media(comments, paging_after=None, media_ago=timedelta(days=1)):
    """댓글 목록 + 게시물 업로드 시각을 함께 patch (소급 하한 계산에 둘 다 쓰인다)."""
    return (
        patch.object(
            InstagramMediaService,
            "list_media_comments",
            return_value={"data": comments, "paging_after": paging_after},
        ),
        patch.object(
            InstagramMediaService,
            "get_media_timestamp",
            return_value=timezone.now() - media_ago,
        ),
        patch("apps.integrations.tasks.send_dm_task.delay"),
    )


def _run(campaign, comments, **kw):
    """소급 태스크 1회 실행 후 (결과, send_dm_task.delay mock) 반환."""
    from apps.integrations.tasks import backfill_campaign_comments

    cm, ts, delay = _patch_media(comments, **kw)
    with cm, ts, delay as d:
        result = backfill_campaign_comments(str(campaign.id))
    return result, d


class TestBackfillCore:
    def test_sends_to_comments_from_before_campaign_start(self, ig_connection):
        """★ 존재 이유 — 캠페인 시작 이전 댓글에도 DM 이 나간다 (poll 경로는 여기서 막힌다)."""
        camp = _campaign(ig_connection)
        comments = [
            _comment("bf_a", username="buyer_a", ago=timedelta(hours=2)),
            _comment("bf_b", username="buyer_b", ago=timedelta(hours=3)),
        ]
        result, delay = _run(camp, comments)

        assert result["status"] == "done"
        assert result["enqueued"] == 2
        assert delay.call_count == 2
        assert SentDMLog.objects.filter(campaign=camp).count() == 2

    def test_poll_path_still_skips_them(self, ig_connection):
        """대조군 — 같은 댓글을 폴링에 태우면 baseline 에 걸려 발송되지 않는다."""
        from apps.integrations.tasks import poll_missed_comments

        camp = _campaign(ig_connection)
        comments = [_comment("bf_poll", username="buyer_p", ago=timedelta(hours=2))]
        cm, ts, delay = _patch_media(comments)
        with cm, ts, delay:
            poll_missed_comments()

        assert SentDMLog.objects.filter(campaign=camp).count() == 0

    def test_ignores_seen_comment_anchor(self, ig_connection):
        """앵커 무시 — 장부에 이미 있는 댓글에서 멈추지 않고 끝까지 훑는다."""
        camp = _campaign(ig_connection)
        SeenComment.objects.create(
            ig_connection=ig_connection,
            comment_id="bf_seen",
            media_id=MEDIA_ID,
            source=SeenComment.Source.WEBHOOK,
            expires_at=timezone.now() + timedelta(days=10),
        )
        comments = [
            _comment("bf_seen", username="buyer_s", ago=timedelta(hours=1)),
            _comment("bf_after_anchor", username="buyer_t", ago=timedelta(hours=2)),
        ]
        result, _ = _run(camp, comments)

        # 앵커에서 끊겼다면 두 번째 댓글에 도달조차 못 한다
        assert result["scanned"] == 2
        assert SentDMLog.objects.filter(campaign=camp, comment_id="bf_after_anchor").exists()

    def test_runs_only_once(self, ig_connection):
        """1회성 락 — 두 번째 실행은 no-op (재개/중복 발사 방어)."""
        camp = _campaign(ig_connection)
        comments = [_comment("bf_once", username="buyer_o", ago=timedelta(hours=2))]

        first, _ = _run(camp, comments)
        second, delay2 = _run(camp, comments)

        assert first["status"] == "done"
        assert second == {"status": "skipped", "reason": "already_backfilled"}
        assert delay2.call_count == 0
        assert SentDMLog.objects.filter(campaign=camp).count() == 1

    def test_records_stats(self, ig_connection):
        camp = _campaign(ig_connection)
        _run(camp, [_comment("bf_stat", username="buyer_st", ago=timedelta(hours=2))])
        camp.refresh_from_db()

        assert camp.backfill_started_at is not None
        assert camp.backfill_stats["enqueued"] == 1
        assert camp.backfill_stats["capped"] is False
        assert "floor" in camp.backfill_stats


class TestBackfillGuards:
    def test_disabled_toggle(self, ig_connection):
        camp = _campaign(ig_connection, backfill_existing_comments=False)
        result, delay = _run(camp, [_comment("bf_off", ago=timedelta(hours=2))])

        assert result == {"status": "skipped", "reason": "disabled"}
        assert delay.call_count == 0
        camp.refresh_from_db()
        # 락을 잡지 않아야 나중에 토글을 켜면 실행될 수 있다
        assert camp.backfill_started_at is None

    def test_inactive_campaign(self, ig_connection):
        camp = _campaign(ig_connection, status=AutoDMCampaign.Status.PAUSED)
        result, _ = _run(camp, [_comment("bf_pause", ago=timedelta(hours=2))])
        assert result == {"status": "skipped", "reason": "not_runnable"}

    def test_no_media(self, ig_connection):
        camp = _campaign(
            ig_connection, media_id="", trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA
        )
        result, _ = _run(camp, [_comment("bf_nomedia", ago=timedelta(hours=2))])
        assert result == {"status": "skipped", "reason": "no_media"}

    def test_skips_self_reply_and_keyword_mismatch(self, ig_connection):
        camp = _campaign(ig_connection, keyword_filter=["신청"], keyword_mode="any")
        comments = [
            _comment("bf_ok", text="신청합니다", username="buyer_k", ago=timedelta(hours=2)),
            _comment("bf_self", text="신청", username="bfuser", ago=timedelta(hours=2)),
            _comment(
                "bf_reply",
                text="신청",
                username="buyer_r",
                ago=timedelta(hours=2),
                parent_id="bf_ok",
            ),
            _comment("bf_kw", text="관심없어요", username="buyer_n", ago=timedelta(hours=2)),
        ]
        result, _ = _run(camp, comments)

        assert result["enqueued"] == 1
        assert result["skipped"]["self_comment"] == 1
        assert result["skipped"]["is_reply"] == 1
        assert result["skipped"]["keyword_mismatch"] == 1
        assert (
            SentDMLog.objects.filter(campaign=camp).values_list("comment_id", flat=True).first()
            == "bf_ok"
        )

    def test_window_floor_stops_scan(self, ig_connection, settings):
        """7일 창 밖 댓글은 Meta 가 거부하므로 스캔 자체를 중단한다."""
        settings.PRIVATE_REPLY_WINDOW_DAYS = 7
        camp = _campaign(ig_connection)
        comments = [
            _comment("bf_recent", username="buyer_rc", ago=timedelta(hours=2)),
            _comment("bf_old", username="buyer_od", ago=timedelta(days=9)),
        ]
        result, _ = _run(camp, comments, media_ago=timedelta(days=30))

        assert result["enqueued"] == 1
        assert not SentDMLog.objects.filter(campaign=camp, comment_id="bf_old").exists()

    def test_media_timestamp_narrows_floor(self, ig_connection):
        """게시물이 1시간 전 것이면 그 이전 댓글은 스캔 범위 밖 (7일보다 좁다)."""
        camp = _campaign(ig_connection)
        comments = [
            _comment("bf_in", username="buyer_i", ago=timedelta(minutes=30)),
            _comment("bf_pre", username="buyer_pr", ago=timedelta(days=3)),
        ]
        result, _ = _run(camp, comments, media_ago=timedelta(hours=1))

        assert result["enqueued"] == 1
        assert SentDMLog.objects.filter(campaign=camp, comment_id="bf_in").exists()

    def test_cap_stops_and_flags(self, ig_connection, settings):
        settings.DM_BACKFILL_MAX_COMMENTS = 2
        camp = _campaign(ig_connection)
        comments = [
            _comment(f"bf_cap_{i}", username=f"buyer_c{i}", ago=timedelta(hours=2))
            for i in range(5)
        ]
        result, _ = _run(camp, comments)

        assert result["enqueued"] == 2
        assert result["capped"] is True
        camp.refresh_from_db()
        assert camp.backfill_stats["capped"] is True


class TestBackfillScheduling:
    """예약 캠페인 — 시작 시각 도래 전에는 락도 잡지 않아야 나중에 실행된다."""

    def test_scheduled_future_not_locked(self, ig_connection):
        camp = _campaign(ig_connection, scheduled_start_at=timezone.now() + timedelta(hours=3))
        result, delay = _run(camp, [_comment("bf_sch", ago=timedelta(hours=2))])

        assert result == {"status": "skipped", "reason": "not_runnable"}
        assert delay.call_count == 0
        camp.refresh_from_db()
        assert camp.backfill_started_at is None  # ★ 락이 걸리면 영영 소급이 안 된다

    def test_runs_once_schedule_window_opens(self, ig_connection):
        camp = _campaign(ig_connection, scheduled_start_at=timezone.now() + timedelta(hours=3))
        _run(camp, [_comment("bf_sch2", username="buyer_s2", ago=timedelta(hours=2))])

        # 예약 시작 시각이 지난 상황으로 이동
        camp.scheduled_start_at = timezone.now() - timedelta(minutes=1)
        camp.save(update_fields=["scheduled_start_at"])
        result, delay = _run(
            camp, [_comment("bf_sch2", username="buyer_s2", ago=timedelta(hours=2))]
        )

        assert result["status"] == "done"
        assert result["enqueued"] == 1
        assert delay.call_count == 1

    def test_scanner_picks_only_eligible(self, ig_connection):
        from apps.integrations.tasks import scan_campaigns_for_backfill

        due = _campaign(ig_connection, name="due")
        _campaign(
            ig_connection,
            name="future",
            media_id="media_future",
            scheduled_start_at=timezone.now() + timedelta(days=1),
        )
        _campaign(
            ig_connection,
            name="done",
            media_id="media_done",
            backfill_started_at=timezone.now(),
        )
        _campaign(
            ig_connection,
            name="off",
            media_id="media_off",
            backfill_existing_comments=False,
        )

        with patch("apps.integrations.tasks.backfill_campaign_comments.delay") as delay:
            result = scan_campaigns_for_backfill()

        dispatched = {c.args[0] for c in delay.call_args_list}
        assert str(due.id) in dispatched
        assert result["dispatched"] == len(dispatched)
        # 예약 전 / 이미 실행됨 / 토글 OFF 는 대기열에 없다
        assert len(dispatched) == 1

    def test_scanner_disabled_flag(self, ig_connection, settings):
        from apps.integrations.tasks import scan_campaigns_for_backfill

        settings.DM_BACKFILL_ENABLED = False
        _campaign(ig_connection)
        assert scan_campaigns_for_backfill() == {"enabled": False}


class TestBackfillCopy:
    def test_copy_does_not_inherit_lock(self, ig_connection):
        """복사본은 한 번도 소급하지 않았으므로 락이 따라오면 안 된다."""
        camp = _campaign(ig_connection, backfill_started_at=timezone.now())
        camp.backfill_stats = {"enqueued": 5}
        camp.save(update_fields=["backfill_stats"])

        clone = camp.copy("복사본")

        assert clone.backfill_started_at is None
        assert clone.backfill_stats == {}
        assert clone.backfill_existing_comments is True  # 설정값은 복사된다
