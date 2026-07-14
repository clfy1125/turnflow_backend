"""댓글 웹훅 누락 보정 (poll_missed_comments / SeenComment) 테스트.

커버리지:
  - 누락 백필: 미관측 매칭 댓글 → SentDMLog enqueue + SeenComment 기록
  - 멱등: 동일 comment+campaign 의 SentDMLog 가 이미 있으면 중복 생성 안 됨(idempotency_key)
  - 앵커 중단: 이미 본 댓글(SeenComment) 만나면 페이지네이션 즉시 종료
  - baseline skip: 캠페인 시작(started_at) 이전 댓글은 보정 발송 안 함(기록만)
  - window_floor: 7일 창 밖 댓글은 발송/기록 안 함
  - 키워드 불일치: 매칭 안 되면 발송 안 함
  - 비활성 플래그: MISSED_COMMENT_POLL_ENABLED=False → no-op
  - cleanup_comment_ledger: expires_at 만료 행 삭제
  - 웹훅 경로: process_comment_and_send_dm 가 SeenComment(webhook) 기록

NOTE(test-db-not-clean): 전역 카운트 대신 내가 만든 캠페인/연동 기준으로 단언한다.
NOTE(pytest-tests-prefix): 파일명이 tests_*.py 라 자동수집 안 됨 → 경로 명시 실행:
    docker compose exec web pytest apps/integrations/tests_missed_comments.py
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SeenComment, SentDMLog
from apps.integrations.services import InstagramMediaService
from apps.workspace.models import Membership, Workspace

MEDIA_ID = "media_poll_x"


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"mc_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="MC"
    )
    ws = Workspace.objects.create(name="MC WS", slug=f"mc-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="mcuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_mc"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": MEDIA_ID,
        "name": "mc-campaign",
        "message_template": "안녕하세요!",
        "status": AutoDMCampaign.Status.ACTIVE,
        # 기본: 캠페인이 충분히 과거에 시작 → 최근 댓글은 baseline 통과
        "started_at": timezone.now() - timedelta(days=2),
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _ts(ago: timedelta) -> str:
    """Meta 형식('...+0000') 타임스탬프 (now - ago, UTC)."""
    return (timezone.now() - ago).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _comment(cid, *, text="가격 문의", username="buyer1", ago=timedelta(hours=1)) -> dict:
    return {"id": cid, "text": text, "username": username, "timestamp": _ts(ago)}


def _patch_comments(comments, paging_after=None):
    """InstagramMediaService.list_media_comments 를 한 페이지 응답으로 patch."""
    return patch.object(
        InstagramMediaService,
        "list_media_comments",
        return_value={"data": comments, "paging_after": paging_after},
    )


class TestPollMissedComments:
    def test_backfill_enqueues_and_records(self, ig_connection):
        """미관측 매칭 댓글 → SentDMLog enqueue + SeenComment 기록."""
        from apps.integrations.tasks import poll_missed_comments

        campaign = _campaign(ig_connection)
        comments = [
            _comment("cmt_a", username="buyer_a", ago=timedelta(hours=1)),
            _comment("cmt_b", username="buyer_b", ago=timedelta(hours=2)),
        ]
        with (
            _patch_comments(comments),
            patch("apps.integrations.tasks.send_dm_task.delay") as delay,
        ):
            result = poll_missed_comments()

        assert result["misses"] == 2
        assert delay.call_count == 2
        assert SentDMLog.objects.filter(campaign=campaign).count() == 2
        seen = SeenComment.objects.filter(ig_connection=ig_connection)
        assert set(seen.values_list("comment_id", flat=True)) == {"cmt_a", "cmt_b"}
        assert seen.filter(triggered=True).count() == 2

    def test_idempotent_with_existing_sentdmlog(self, ig_connection):
        """동일 comment+campaign 의 SentDMLog 가 이미 있으면 중복 생성 안 됨."""
        from apps.integrations.services import InstagramMessagingService
        from apps.integrations.tasks import poll_missed_comments

        campaign = _campaign(ig_connection)
        key = InstagramMessagingService.build_idempotency_key(
            workspace_id=ig_connection.workspace_id,
            ig_user_id=ig_connection.external_account_id,
            comment_id="cmt_dup",
            campaign_id=campaign.id,
        )
        SentDMLog.objects.create(
            campaign=campaign,
            comment_id="cmt_dup",
            comment_text="가격 문의",
            recipient_user_id="r1",
            recipient_username="buyer",
            message_sent="안녕하세요!",
            status=SentDMLog.Status.ACCEPTED,
            idempotency_key=key,
        )
        comments = [_comment("cmt_dup", username="buyer_dup", ago=timedelta(hours=1))]
        with _patch_comments(comments), patch("apps.integrations.tasks.send_dm_task.delay"):
            poll_missed_comments()

        # 여전히 1건 (중복 INSERT 차단)
        assert SentDMLog.objects.filter(campaign=campaign, comment_id="cmt_dup").count() == 1

    def test_anchor_stops_pagination(self, ig_connection):
        """이미 본 댓글(SeenComment) 만나면 즉시 종료 — 발송 0, API 1회 호출.

        NOTE(test-db-not-clean): poll 대상은 DB 전역의 활성 specific_media 캠페인이라
        dev DB 실캠페인이 섞이면 호출 수 단언이 흔들린다 → 테스트 트랜잭션 안에서
        내 캠페인 외 전부를 일시 pause 해 대상을 고정한다(롤백되므로 실데이터 무영향).
        """
        from apps.integrations.tasks import poll_missed_comments

        campaign = _campaign(ig_connection)
        AutoDMCampaign.objects.filter(status=AutoDMCampaign.Status.ACTIVE).exclude(
            pk=campaign.pk
        ).update(status=AutoDMCampaign.Status.PAUSED)
        SeenComment.objects.create(
            ig_connection=ig_connection,
            comment_id="cmt_seen",
            media_id=MEDIA_ID,
            source=SeenComment.Source.WEBHOOK,
            expires_at=timezone.now() + timedelta(days=10),
        )
        comments = [_comment("cmt_seen", ago=timedelta(hours=1))]
        with (
            _patch_comments(comments, paging_after="NEXT") as mock_list,
            patch("apps.integrations.tasks.send_dm_task.delay") as delay,
        ):
            poll_missed_comments()

        assert delay.call_count == 0
        assert mock_list.call_count == 1  # 앵커에서 멈춰 다음 페이지 안 부름
        assert SentDMLog.objects.filter(campaign=campaign).count() == 0

    def test_baseline_skip_before_campaign_start(self, ig_connection):
        """캠페인 시작 이전 댓글 → 기록만, 발송 안 함."""
        from apps.integrations.tasks import poll_missed_comments

        campaign = _campaign(ig_connection, started_at=timezone.now())  # 방금 시작
        comments = [_comment("cmt_old", ago=timedelta(hours=3))]  # 시작 3시간 전 댓글
        with _patch_comments(comments), patch("apps.integrations.tasks.send_dm_task.delay") as d:
            poll_missed_comments()

        assert d.call_count == 0
        assert SentDMLog.objects.filter(campaign=campaign).count() == 0
        assert SeenComment.objects.filter(
            ig_connection=ig_connection, comment_id="cmt_old"
        ).exists()

    def test_window_floor_skips_old_comment(self, ig_connection):
        """7일 창 밖 댓글 → 발송/기록 안 함."""
        from apps.integrations.tasks import poll_missed_comments

        campaign = _campaign(ig_connection, started_at=timezone.now() - timedelta(days=30))
        comments = [_comment("cmt_ancient", ago=timedelta(days=8))]
        with _patch_comments(comments), patch("apps.integrations.tasks.send_dm_task.delay") as d:
            poll_missed_comments()

        assert d.call_count == 0
        assert SentDMLog.objects.filter(campaign=campaign).count() == 0
        assert not SeenComment.objects.filter(comment_id="cmt_ancient").exists()

    def test_keyword_filter_no_match(self, ig_connection):
        """키워드 불일치 → 기록만, 발송 안 함."""
        from apps.integrations.tasks import poll_missed_comments

        campaign = _campaign(
            ig_connection,
            keyword_filter=["가격"],
            keyword_mode=AutoDMCampaign.KeywordMode.ANY,
        )
        comments = [_comment("cmt_nokw", text="멋져요!", ago=timedelta(hours=1))]
        with _patch_comments(comments), patch("apps.integrations.tasks.send_dm_task.delay") as d:
            poll_missed_comments()

        assert d.call_count == 0
        assert SentDMLog.objects.filter(campaign=campaign).count() == 0
        assert SeenComment.objects.filter(comment_id="cmt_nokw").exists()

    def test_disabled_flag_noop(self, ig_connection, settings):
        """MISSED_COMMENT_POLL_ENABLED=False → 아무 동작 안 함."""
        from apps.integrations.tasks import poll_missed_comments

        settings.MISSED_COMMENT_POLL_ENABLED = False
        _campaign(ig_connection)
        with _patch_comments([_comment("cmt_z")]) as mock_list:
            result = poll_missed_comments()

        assert result == {"enabled": False}
        assert mock_list.call_count == 0


class TestCleanupCommentLedger:
    def test_deletes_expired_only(self, ig_connection):
        from apps.integrations.tasks import cleanup_comment_ledger

        SeenComment.objects.create(
            ig_connection=ig_connection,
            comment_id="cmt_expired",
            media_id=MEDIA_ID,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        fresh = SeenComment.objects.create(
            ig_connection=ig_connection,
            comment_id="cmt_fresh",
            media_id=MEDIA_ID,
            expires_at=timezone.now() + timedelta(days=5),
        )
        result = cleanup_comment_ledger()

        assert result["deleted"] >= 1
        assert not SeenComment.objects.filter(comment_id="cmt_expired").exists()
        assert SeenComment.objects.filter(pk=fresh.pk).exists()


class TestWebhookRecordsLedger:
    def test_webhook_records_seen_comment(self, ig_connection):
        """process_comment_and_send_dm 가 SeenComment(webhook) 를 기록한다."""
        from apps.integrations.tasks import process_comment_and_send_dm

        _campaign(ig_connection)
        payload = {
            "field": "comments",
            "entry_id": ig_connection.external_account_id,
            "value": {
                "id": "cmt_wh",
                "text": "가격 문의",
                "from": {"id": "user_wh", "username": "buyer_wh"},
                "media": {"id": MEDIA_ID},
            },
        }
        with patch("apps.integrations.tasks.send_dm_task.delay"):
            process_comment_and_send_dm(payload)

        rec = SeenComment.objects.filter(ig_connection=ig_connection, comment_id="cmt_wh").first()
        assert rec is not None
        assert rec.source == SeenComment.Source.WEBHOOK
        assert rec.triggered is True
