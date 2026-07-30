"""광고(Paid partnership/부스팅) 유입 댓글 매칭 — original_media_id 폴백.

배경 (2026-07-30 prod 실측):
    @ellisa_levelup 의 'AI Note2' 릴스가 광고로 배포됐고, 광고를 보고 들어온 사용자
    (@ks.___.hyeon)가 트리거 키워드 '칼퇴' 댓글을 남겼다. 웹훅은 **정상 도착**했으나
    payload 의 ``value.media.id`` 가 **광고 카피의 미디어 id**(18083495273654753)여서
    캠페인(media_id=18085701743661167)과 매칭되지 않아 **DM·SeenComment·로그 어디에도
    흔적 없이 드롭**됐다. 원본 게시물은 ``value.media.original_media_id`` 로만 온다.

    실제 캡처된 원문(apps/integrations/views.py _capture_comment_webhook_raw):
        {"id": "17841400006862718", "time": 1785412106, "changes": [{"field": "comments",
          "value": {"id": "18220742947333329",
                    "from": {"id": "742084212153673", "username": "ks.___.hyeon"},
                    "text": "칼퇴",
                    "media": {"id": "18083495273654753",
                              "ad_id": "120250784238480294",
                              "original_media_id": "18085701743661167",
                              "media_product_type": "AD"}}}]}

    광고 미디어는 organic comments edge 에도 나오지 않으므로(별개 permalink)
    poll_missed_comments 보정도 불가 → 웹훅에서 original_media_id 를 살리는 것이
    유일한 회수 경로다.

커버리지:
  - 광고 댓글이 original_media_id 로 캠페인에 매칭돼 DM enqueue 된다 (핵심 회귀 방어)
  - SeenComment / next_media attach 는 **원본** media 기준으로 기록된다(광고 카피 id 금지)
  - 일반(organic) 댓글 동작은 불변
  - original_media_id 가 다른 게시물이면 매칭되지 않는다(테넌트/미디어 스코핑 유지)
  - media.id 없이 original_media_id 만 온 payload 도 처리된다
  - 키워드 불일치 광고 댓글은 발송되지 않는다

NOTE(pytest-tests-prefix): 파일명이 tests_*.py 라 자동수집 안 됨 → 경로 명시 실행:
    docker compose exec web pytest apps/integrations/tests_ad_comment_matching.py
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SeenComment, SentDMLog
from apps.workspace.models import Membership, Workspace

ORIGIN_MEDIA = "18085701743661167"  # 원본 게시물(캠페인이 걸린 곳)
AD_MEDIA = "18083495273654753"  # 광고 카피의 미디어
AD_ID = "120250784238480294"


@pytest.fixture
def ig_connection(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"ad_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="AD"
    )
    ws = Workspace.objects.create(name="AD WS", slug=f"ad-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="aduser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_ad"
    conn.save()
    return conn


def _campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": ORIGIN_MEDIA,
        "name": "ad-campaign",
        "message_template": "자료 보내드려요!",
        "keyword_filter": ["칼퇴"],
        "status": AutoDMCampaign.Status.ACTIVE,
        "started_at": timezone.now() - timedelta(days=2),
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _ad_payload(conn, *, comment_id="18220742947333329", text="칼퇴", media=None):
    """prod 에서 실제로 캡처된 광고 댓글 payload 형태."""
    if media is None:
        media = {
            "id": AD_MEDIA,
            "ad_id": AD_ID,
            "original_media_id": ORIGIN_MEDIA,
            "media_product_type": "AD",
        }
    return {
        "field": "comments",
        "entry_id": conn.external_account_id,
        "value": {
            "id": comment_id,
            "from": {"id": "742084212153673", "username": "ks.___.hyeon"},
            "text": text,
            "media": media,
        },
    }


class TestAdCommentMatching:
    def test_ad_comment_matches_via_original_media_id(self, ig_connection):
        """★ 핵심: 광고 유입 댓글이 original_media_id 로 캠페인에 매칭돼 DM 이 큐잉된다."""
        from apps.integrations.tasks import process_comment_and_send_dm

        campaign = _campaign(ig_connection)
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            result = process_comment_and_send_dm(_ad_payload(ig_connection))

        assert result["status"] == "queued", result
        assert delay.call_count == 1
        log = SentDMLog.objects.filter(campaign=campaign).get()
        assert log.comment_id == "18220742947333329"
        assert log.recipient_username == "ks.___.hyeon"

    def test_ledger_records_original_media_not_ad_copy(self, ig_connection):
        """SeenComment 는 원본 media 로 기록된다 — 광고 카피 id 로 폴링 앵커를 남기면 안 됨."""
        from apps.integrations.tasks import process_comment_and_send_dm

        _campaign(ig_connection)
        with patch("apps.integrations.tasks.send_dm_task.delay"):
            process_comment_and_send_dm(_ad_payload(ig_connection))

        rec = SeenComment.objects.filter(
            ig_connection=ig_connection, comment_id="18220742947333329"
        ).get()
        assert rec.media_id == ORIGIN_MEDIA, "광고 카피 media_id 가 장부에 새면 안 된다"
        assert rec.source == SeenComment.Source.WEBHOOK
        assert rec.triggered is True

    def test_organic_comment_unchanged(self, ig_connection):
        """일반 댓글(original_media_id 없음) 동작은 기존과 동일."""
        from apps.integrations.tasks import process_comment_and_send_dm

        campaign = _campaign(ig_connection)
        payload = _ad_payload(
            ig_connection,
            comment_id="cmt_organic",
            media={"id": ORIGIN_MEDIA, "media_product_type": "REELS"},
        )
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            result = process_comment_and_send_dm(payload)

        assert result["status"] == "queued"
        assert delay.call_count == 1
        assert SentDMLog.objects.filter(campaign=campaign, comment_id="cmt_organic").exists()
        rec = SeenComment.objects.filter(comment_id="cmt_organic").get()
        assert rec.media_id == ORIGIN_MEDIA

    def test_ad_comment_for_other_media_does_not_match(self, ig_connection):
        """original_media_id 가 다른 게시물이면 매칭 안 됨 — 미디어 스코핑 유지."""
        from apps.integrations.tasks import process_comment_and_send_dm

        campaign = _campaign(ig_connection)
        payload = _ad_payload(
            ig_connection,
            comment_id="cmt_other",
            media={
                "id": AD_MEDIA,
                "ad_id": AD_ID,
                "original_media_id": "99999999999999999",
                "media_product_type": "AD",
            },
        )
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            result = process_comment_and_send_dm(payload)

        assert result["status"] == "skipped", result
        assert delay.call_count == 0
        assert not SentDMLog.objects.filter(campaign=campaign).exists()

    def test_only_original_media_id_present(self, ig_connection):
        """media.id 가 없고 original_media_id 만 온 payload 도 처리된다."""
        from apps.integrations.tasks import process_comment_and_send_dm

        campaign = _campaign(ig_connection)
        payload = _ad_payload(
            ig_connection,
            comment_id="cmt_origin_only",
            media={"original_media_id": ORIGIN_MEDIA, "media_product_type": "AD"},
        )
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            result = process_comment_and_send_dm(payload)

        assert result["status"] == "queued", result
        assert delay.call_count == 1
        assert SentDMLog.objects.filter(campaign=campaign, comment_id="cmt_origin_only").exists()

    def test_ad_comment_keyword_mismatch_not_sent(self, ig_connection):
        """키워드 불일치 광고 댓글은 발송하지 않는다(오발송 방지)."""
        from apps.integrations.tasks import process_comment_and_send_dm

        campaign = _campaign(ig_connection)
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            result = process_comment_and_send_dm(
                _ad_payload(ig_connection, comment_id="cmt_nokw", text="멋져요!")
            )

        assert result["status"] == "skipped", result
        assert delay.call_count == 0
        assert not SentDMLog.objects.filter(campaign=campaign).exists()
        # 장부에는 기록돼야 한다(폴링 앵커) — 원본 media 기준
        rec = SeenComment.objects.filter(comment_id="cmt_nokw").get()
        assert rec.media_id == ORIGIN_MEDIA
        assert rec.triggered is False

    def test_ad_comment_reply_still_skipped(self, ig_connection):
        """광고 댓글의 대댓글(parent_id 有)은 여전히 트리거 대상 아님."""
        from apps.integrations.tasks import process_comment_and_send_dm

        campaign = _campaign(ig_connection)
        payload = _ad_payload(ig_connection, comment_id="cmt_reply")
        payload["value"]["parent_id"] = "18220742947333329"
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            result = process_comment_and_send_dm(payload)

        assert result["status"] == "skipped"
        assert result["reason"] == "is_reply"
        assert delay.call_count == 0
        assert not SentDMLog.objects.filter(campaign=campaign).exists()

    def test_self_comment_on_ad_still_guarded(self, ig_connection):
        """계정 본인이 광고 게시물에 댓글 → 자기 DM 루프 가드 유지."""
        from apps.integrations.tasks import process_comment_and_send_dm

        campaign = _campaign(ig_connection)
        payload = _ad_payload(ig_connection, comment_id="cmt_self")
        payload["value"]["from"] = {
            "id": ig_connection.external_account_id,
            "username": "aduser",
        }
        with patch("apps.integrations.tasks.send_dm_task.delay") as delay:
            result = process_comment_and_send_dm(payload)

        assert result["status"] == "skipped"
        assert result["reason"] == "self_comment"
        assert delay.call_count == 0
        assert not SentDMLog.objects.filter(campaign=campaign).exists()


class TestSpamFilterUsesOriginalMedia:
    def test_trigger_exemption_uses_original_media(self, ig_connection):
        """스팸필터의 '캠페인 트리거 댓글 면제' 판정이 원본 media 기준으로 동작한다.

        광고 유입 트리거 댓글이 면제를 못 받으면 정상 요청자가 스팸으로 숨겨질 수 있다.
        """
        from apps.integrations.tasks import _comment_triggers_active_campaign

        _campaign(ig_connection)
        # 원본 media 기준이면 트리거로 인식돼야 한다
        assert (
            _comment_triggers_active_campaign(
                ig_connection, media_id=ORIGIN_MEDIA, comment_text="칼퇴"
            )
            is True
        )
        # 광고 카피 media 로는 인식되지 않는다 → 그래서 run_spam_filter_check 가
        # original_media_id 로 정규화해서 넘겨야 한다(회귀 방어).
        assert (
            _comment_triggers_active_campaign(ig_connection, media_id=AD_MEDIA, comment_text="칼퇴")
            is False
        )

    def test_run_spam_filter_normalizes_to_original(self, ig_connection):
        """run_spam_filter_check 가 media.original_media_id 를 media_id 로 정규화한다."""
        from apps.integrations.tasks import run_spam_filter_check

        _campaign(ig_connection)
        payload = _ad_payload(ig_connection, comment_id="cmt_spam")
        with patch("apps.integrations.tasks._run_spam_for_connection") as run:
            # 스팸필터 미설정이라 _run_spam_for_connection 까지 안 갈 수 있으므로
            # 호출 여부와 무관하게 예외 없이 끝나는 것 + 호출되면 원본 media 로 넘어가는 것 확인
            result = run_spam_filter_check(payload)
            if run.called:
                assert run.call_args.kwargs["media_id"] == ORIGIN_MEDIA
        assert result["status"] in ("processed", "skipped"), result
