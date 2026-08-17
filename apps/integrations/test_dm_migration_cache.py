"""DM 이전 — **비싼 작업 재사용** 계약 테스트.

이 기능에서 비싼 것: ①발신함 훑기(실측 122분·1,577페이지·88,899건) ②게시물별 조회
(19.6콜/게시물) ③댓글 끝까지 페이징(1만 개 게시물이 200페이지).
전부 **한 번 사고 다시 쓴다.** 매번 재조사하면 Meta 앱 쿼터를 다른 워크스페이스의
댓글 수집·DM 발송에서 빼오는 셈이다(CLAUDE.md §1).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.integrations.dm_migration import cache, pipeline
from apps.integrations.models import DMCampaignCandidate, DMMigrationJob, IGPostAnalysis
from apps.integrations.test_dm_migration import _conn, _job, _user, _ws


def _row(conn, mid, **kw):
    d = {
        "rules_version": cache.RULES_VERSION,
        "comments_count": 100,
        "grade": "auto_draft",
        "content_score": 0.95,
        "probed": 50,
        "support_hits": 29,
        "auto_hits": 17,
        "gap_median": 34,
        "has_offer_url": True,
    }
    d.update(kw)
    return IGPostAnalysis.objects.create(ig_connection=conn, media_id=mid, **d)


# ══════════════ 1. 게시물 판정 재사용 ══════════════


class TestSettled:
    @pytest.mark.django_db
    def test_auto_draft_is_settled(self):
        """자동채택 = 옮길 것을 다 얻었다 → 다시 조사하지 않는다."""
        conn = _conn(_ws(_user()))
        row = _row(conn, "m1")
        assert cache.is_settled(row, {"comments_count": 100}) is True

    @pytest.mark.django_db
    def test_clearly_not_a_campaign_is_settled(self):
        """글 점수가 캠페인 컷 미만 = 캡션이 안 바뀌니 결론도 안 바뀐다."""
        conn = _conn(_ws(_user()))
        row = _row(conn, "m2", grade="excluded", content_score=0.10)
        assert cache.is_settled(row, {"comments_count": 100}) is True

    @pytest.mark.django_db
    def test_needs_review_is_not_settled(self):
        """검수필요는 **끝난 게 아니다** — 다음 실행에서 건질 수 있다."""
        conn = _conn(_ws(_user()))
        row = _row(conn, "m3", grade="needs_review")
        assert cache.is_settled(row, {"comments_count": 100}) is False

    @pytest.mark.django_db
    def test_strong_content_without_offer_is_not_settled(self):
        """'글은 캠페인인데 문구 못 살림' 도 끝난 게 아니다 — 두 축을 더 파야 한다."""
        conn = _conn(_ws(_user()))
        row = _row(conn, "m4", grade="excluded", content_score=0.95, has_offer_url=False)
        assert cache.is_settled(row, {"comments_count": 100}) is False

    @pytest.mark.django_db
    def test_comment_growth_forces_recheck(self):
        """댓글이 늘었다 = 새 댓글러가 DM 을 받았을 수 있다 → 다시 본다."""
        conn = _conn(_ws(_user()))
        row = _row(conn, "m5", comments_count=100)
        assert cache.is_settled(row, {"comments_count": 105}) is True  # +5 는 무시
        assert cache.is_settled(row, {"comments_count": 400}) is False

    @pytest.mark.django_db
    def test_old_rules_version_invalidates_everything(self):
        """규칙을 고치면 옛 판정은 전부 무효 — 버그 있던 버전의 결론이 영구화되면 안 된다."""
        conn = _conn(_ws(_user()))
        row = _row(conn, "m6", rules_version=cache.RULES_VERSION - 1)
        assert cache.is_settled(row, {"comments_count": 100}) is False
        assert cache.probe_pool_for(row) == []


class TestReuseRestoresText:
    @pytest.mark.django_db
    def test_text_comes_from_the_durable_candidate(self):
        """캐시는 **타인 DM 원문을 담지 않는다**(7일 파기 대상) → 문구는 후보에서 되찾는다."""
        conn = _conn(_ws(_user()))
        job = _job(conn, status=DMMigrationJob.Status.READY)
        DMCampaignCandidate.objects.create(
            job=job,
            ig_connection=conn,
            band=DMCampaignCandidate.Band.AUTO_DRAFT,
            media_id="m-keep",
            draft_opening_message="요청하신 자료 보내드려요!",
            offer_url="https://ex.co/pack",
            offer_button_label="자료 받기",
            gate_detected=True,
            gate_message="팔로우 확인 부탁드려요",
        )
        row = _row(conn, "m-keep")
        texts = cache.texts_for(conn, ["m-keep"])
        rec = cache.to_recovery(
            row, {"comments_count": 100, "permalink": "https://x"}, texts["m-keep"]
        )
        assert rec["offer"]["text"] == "요청하신 자료 보내드려요!"
        assert rec["offer"]["url"] == "https://ex.co/pack"
        assert rec["offer"]["auto_hits"] == 17  # 지문 수치가 살아 있어야 등급이 유지된다
        assert rec["gate"]["text"] == "팔로우 확인 부탁드려요"
        assert rec["grade"] == "auto_draft"
        assert rec["from_cache"] is True

    @pytest.mark.django_db
    def test_no_candidate_still_yields_a_verdict(self):
        """후보가 없어도(탈락분) 판정은 되살아난다 — 문구가 필요 없다."""
        conn = _conn(_ws(_user()))
        row = _row(conn, "m-drop", grade="excluded", content_score=0.1, has_offer_url=False)
        rec = cache.to_recovery(row, {"comments_count": 10}, None)
        assert rec["offer"] is None and rec["gate"] is None
        assert rec["grade"] == "excluded"


# ══════════════ 2. 발신함 물려받기 ══════════════


class TestOutboxAdoption:
    @pytest.mark.django_db
    def test_adopts_a_recent_completed_scan(self):
        """122분짜리 훑기를 계정마다 한 번만 산다."""
        conn = _conn(_ws(_user()))
        old = _job(conn, status=DMMigrationJob.Status.READY)
        old.stage_data = {
            "outbox": [{"recipient": "u1", "msg_id": "d1", "text": "자료"}],
            "outbox_done": True,
        }
        old.save(update_fields=["stage_data"])
        new = _job(conn)
        runner = pipeline._Runner(new)
        got = runner._adopt_previous_outbox()
        assert len(got) == 1
        assert runner.sd["outbox_reused_from"] == str(old.id)

    @pytest.mark.django_db
    def test_does_not_adopt_an_unfinished_scan(self):
        """중간에 끊긴 훑기는 물려받지 않는다 — 뒤쪽 수신자가 통째로 빈다.

        (연결당 비종결 잡 1개 제약이 있어 옛 잡은 실패로 종결시켜 둔다.)
        """
        conn = _conn(_ws(_user()))
        old = _job(conn, status=DMMigrationJob.Status.FAILED)
        old.stage_data = {"outbox": [{"recipient": "u1", "msg_id": "d1"}], "outbox_cursor": "c1"}
        old.save(update_fields=["stage_data"])
        runner = pipeline._Runner(_job(conn))
        assert runner._adopt_previous_outbox() == []

    @pytest.mark.django_db
    def test_does_not_adopt_a_stale_scan(self):
        """오래된 것은 안 쓴다 — 재사용 창은 7일 파기 기한보다 짧아야 한다."""
        conn = _conn(_ws(_user()))
        old = _job(conn, status=DMMigrationJob.Status.READY)
        old.stage_data = {"outbox": [{"recipient": "u1", "msg_id": "d1"}], "outbox_done": True}
        old.save(update_fields=["stage_data"])
        stale = timezone.now() - timedelta(hours=cache.RULES_VERSION * 0 + 200)
        DMMigrationJob.objects.filter(id=old.id).update(updated_at=stale)
        runner = pipeline._Runner(_job(conn))
        assert runner._adopt_previous_outbox() == []

    @pytest.mark.django_db
    def test_does_not_adopt_purged_data(self):
        """원문이 파기된 잡은 물려받을 게 없다."""
        conn = _conn(_ws(_user()))
        old = _job(conn, status=DMMigrationJob.Status.READY)
        old.stage_data = {"outbox": [{"recipient": "u1"}], "outbox_done": True}
        old.raw_purged_at = timezone.now()
        old.save(update_fields=["stage_data", "raw_purged_at"])
        runner = pipeline._Runner(_job(conn))
        assert runner._adopt_previous_outbox() == []

    @pytest.mark.django_db
    def test_merge_dedupes_by_message_id(self):
        """물려받은 뒤 최신 페이지를 덧칠하면 겹친다 — msg_id 로 지운다."""
        prev = [{"msg_id": "a"}, {"msg_id": "b"}]
        new = [{"msg_id": "b"}, {"msg_id": "c"}]
        got = pipeline._merge_outbox(prev, new)
        assert [m["msg_id"] for m in got] == ["a", "b", "c"]
