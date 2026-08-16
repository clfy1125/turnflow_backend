"""DM 이전 — **글·댓글로 캠페인을 판별하는** 규칙 계약 테스트.

이 기능의 목표는 "타 도구의 DM 캠페인을 하나도 빠짐없이 이전"이다(CLAUDE.md §1).
그런데 판정이 오래도록 **DM 을 찾았느냐** 한 가지에만 걸려 있었고, 그래서 양쪽으로 틀렸다
(실측 @highestlevel33, 게시물 459개):

  · 글·댓글이 "여기 캠페인 돌았다" 고 말하는데 DM 을 못 건져 **52개를 통째로 버렸다**
  · 글·댓글이 "캠페인 아니다" 라는데 DM 한 통 나왔다고 **35개를 통과시켰다**

여기 고정하는 계약은 "여러 신호를 **종합**해서 판정하고, 애매하면 버리지 말고 등급을 낮춘다".
가중치는 감이 아니라 실측이다 — 확실한 캠페인 147개 vs 오탐 의심 59개에서 각 신호가
몇 %나 나오는지 재서 정했다(recover.CONTENT_WEIGHTS 주석).
"""

from __future__ import annotations

import pytest

from apps.integrations.dm_migration.analyze import EMOJI_TOKEN, comment_key, comment_shape
from apps.integrations.dm_migration.recover import (
    CONTENT_CAMPAIGN_MIN,
    CONTENT_STRONG_MIN,
    PostRecovery,
    judge_content,
)


def _cs(*texts):
    return [{"text": t} for t in texts]


# ══════════════ 1. 이모지만 있는 댓글이 보여야 한다 ══════════════
#
# "아무 댓글이나 달면 DM" 캠페인은 사람들이 이모지 하나만 단다. 정규화가 이모지를 지우고
# 호출부가 빈 문자열을 버리면서 **그 게시물은 댓글이 0개인 것처럼** 보였다.


class TestEmojiComments:
    def test_emoji_only_comment_is_not_dropped(self):
        assert comment_key("🙌") == EMOJI_TOKEN
        assert comment_key("🔥🔥") == EMOJI_TOKEN
        assert comment_key("") == ""  # 진짜 빈 것만 빈 것

    def test_emoji_flood_is_measured_as_repetition(self):
        """이모지 도배 게시물의 반복률이 0 이면 캠페인 탐지가 통째로 실패한다."""
        shape = comment_shape(["🙌", "🔥", "❤️", "🙌🙌", "감사합니다"])
        assert shape["repetition"] >= 0.6, shape  # 이모지 4개가 한 종류로 묶인다
        assert shape["emoji_only_ratio"] >= 0.6
        assert shape["top_key"] == EMOJI_TOKEN

    def test_emoji_flood_post_is_judged_campaign(self):
        media = {"caption": "새 영상 올렸어요! 아무 댓글 남기면 자료 보내드려요"}
        v = judge_content(media, _cs("🙌", "🔥", "❤️", "😍", "🙏", "좋아요"))
        assert v.is_campaign, (v.score, v.reasons)
        assert "tiny_comments" in v.reasons

    def test_emoji_trigger_does_not_become_a_keyword(self):
        """이모지 뭉치를 캠페인 트리거 키워드로 삼으면 안 된다(발동 조건이 깨진다)."""
        v = judge_content({"caption": "댓글 남겨주세요"}, _cs("🙌", "🔥", "❤️", "😍"))
        assert v.is_campaign  # 캠페인으로는 잡힌다
        assert v.trigger is None, v.trigger  # 트리거 키워드는 안 만든다
        assert v.trigger != EMOJI_TOKEN


# ══════════════ 2. 종합 판정 ══════════════


class TestContentJudgement:
    def test_keyword_campaign(self):
        """캡션이 키워드를 지정하고 댓글이 그 단어로 도배 = 가장 흔한 형태."""
        media = {"caption": "인스타 성장 자료 드려요! 댓글에 '자료' 남겨주세요"}
        v = judge_content(media, _cs(*(["자료"] * 8), "잘 보고 갑니다"))
        assert v.is_campaign and v.is_strong, (v.score, v.reasons)
        assert v.trigger == "자료"

    def test_ordinary_post_is_not_campaign(self):
        """일상 게시물 — 댓글이 길고 제각각이면 캠페인이 아니다."""
        media = {"caption": "오늘 제주도 다녀왔어요 날씨가 정말 좋았습니다"}
        v = judge_content(
            media,
            _cs(
                "우와 너무 예쁘네요",
                "저도 가고 싶어요 언제 가셨어요?",
                "사진 진짜 잘 찍으시네요",
                "다음에 같이 가요",
            ),
        )
        assert not v.is_campaign, (v.score, v.reasons)

    def test_verb_ending_variation_is_caught(self):
        """'남기면' 을 못 잡아 실제 캠페인 하나가 0.34 로 탈락했다(기준 0.35)."""
        for cta in ("아무 댓글 남기면 보내드려요", "댓글 남겨주세요", "댓글 달아주시면 DM 드려요"):
            v = judge_content({"caption": cta}, _cs("ㅇㅇ", "신청", "저도요"))
            assert "caption_cta" in v.reasons, cta

    def test_no_single_signal_decides(self):
        """신호 하나만으로는 캠페인이 되지 않는다 — 종합 판단이라는 계약."""
        only_offer = judge_content(
            {"caption": "무료 자료 정리해뒀어요"},
            _cs("멋져요", "대단하십니다", "잘 보고 있습니다", "감사합니다 항상"),
        )
        assert not only_offer.is_campaign, (only_offer.score, only_offer.reasons)

    def test_owner_reply_saying_sent_counts(self):
        """계정이 '보내드렸어요' 라고 단 대댓글은 사실상 자백이다 — 판정에 반영한다."""
        media = {"caption": "새 영상입니다"}
        base = judge_content(media, _cs("ㅋㅋ", "좋아요", "굿", "최고"))
        with_reply = judge_content(
            media, _cs("ㅋㅋ", "좋아요", "굿", "최고"), ["DM 보내드렸어요! 확인해주세요"]
        )
        assert with_reply.score > base.score


# ══════════════ 3. 등급 — 콘텐츠와 DM 증거를 함께 본다 ══════════════


def _rec(**kw):
    r = PostRecovery(media_id="m1")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class TestGrading:
    def test_strong_support_is_auto(self):
        r = _rec(offer={"text": "자료 보내드려요", "url": "https://x", "hits": 9, "score": 0.75})
        assert r.grade == "auto_draft"
        assert r.confirm_required is False

    def test_no_dm_but_strong_content_is_kept(self):
        """글·댓글이 캠페인이라고 말하면 DM 이 없어도 후보로 남는다(밴드는 excluded)."""
        r = _rec(is_campaign_signal=True, content_score=0.85)
        assert r.grade == "excluded"  # 문구가 없으니 밴드는 excluded 유지
        assert r.content_score >= CONTENT_STRONG_MIN  # 파이프라인이 이걸 보고 생성한다

    def test_shallow_support_without_content_is_dropped(self):
        """글은 캠페인이 아니라는데 1명만 받은 DM = 남의 게시물에서 흘러든 것(실측 오답 89%)."""
        r = _rec(
            offer={"text": "안내드려요", "url": "", "hits": 1, "score": 0.05},
            is_campaign_signal=False,
            content_score=0.1,
        )
        assert r.grade == "excluded"

    def test_shallow_support_with_content_is_reviewed_not_dropped(self):
        """같은 1명이어도 글이 캠페인이라고 말하면 버리지 않고 검수로 내린다."""
        r = _rec(
            offer={"text": "안내드려요", "url": "", "hits": 1, "score": 0.05},
            is_campaign_signal=True,
            content_score=0.7,
        )
        assert r.grade == "needs_review"
        assert r.confirm_required is True


# ══════════════ 4. 탐색 깊이 — 목표(전수 이전)와 직결 ══════════════


class TestProbeDepth:
    def test_campaign_posts_get_deeper_probe_than_seed(self):
        """콘텐츠가 캠페인이라고 하면 3명에서 포기하지 않는다.

        실측: 그렇게 버려진 52개를 15명까지 조회하니 28개에서 DM 이 나왔고,
        8번째까지 71% · 10번째 82% 가 걸렸다. 3명은 구조적으로 모자란다.
        """
        from apps.integrations.dm_migration import collect as C

        assert C.CAMPAIGN_PROBE >= 8, C.CAMPAIGN_PROBE
        assert C.CAMPAIGN_PROBE > C.SEED_PROBE

    def test_thresholds_are_ordered(self):
        assert 0 < CONTENT_CAMPAIGN_MIN < CONTENT_STRONG_MIN <= 1.0


@pytest.mark.parametrize(
    "caption,comments,expect",
    [
        ("댓글에 '링크' 남겨주시면 보내드려요", ["링크"] * 6 + ["좋아요"], True),
        ("아무 댓글이나 남겨주세요 자료 드립니다", ["🙌", "🔥", "ㅇㅇ", "신청", "😍"], True),
        ("오늘 점심 맛있었어요", ["맛있겠다", "어디예요?", "저도 먹고 싶네요"], False),
    ],
)
def test_end_to_end_shapes(caption, comments, expect):
    v = judge_content({"caption": caption}, _cs(*comments))
    assert v.is_campaign is expect, (caption, v.score, v.reasons)
