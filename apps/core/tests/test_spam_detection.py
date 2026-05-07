"""Unit tests for the heuristic spam detection rule engine."""

import pytest

from apps.core.spam_detection import (
    SpamRuleConfig,
    detect_spam,
    rule_config_from_model_attrs,
)


@pytest.mark.parametrize(
    "text",
    [
        "Check this out: https://spam.com/win",
        "click www.notreal.io for free stuff",
        "best deal at example.shop now",
    ],
)
def test_url_rule_flags(text):
    verdict = detect_spam(text, config=SpamRuleConfig(block_urls=True))
    assert verdict.is_spam, f"expected spam: {text!r}, got verdict={verdict}"
    assert "contains_url" in verdict.reasons


def test_shortener_flagged_even_when_url_rule_disabled():
    verdict = detect_spam(
        "follow me bit.ly/abc",
        config=SpamRuleConfig(block_urls=False, block_shortened_urls=True),
    )
    assert verdict.is_spam
    assert "contains_shortened_url" in verdict.reasons


def test_keyword_match_korean():
    cfg = SpamRuleConfig(
        block_urls=False,
        spam_keywords=["주식리딩", "텔레"],
        score_threshold=0.5,
    )
    verdict = detect_spam("저 텔레로 연락주세요", config=cfg)
    assert verdict.is_spam
    assert any(r.startswith("keyword:") for r in verdict.reasons)


def test_clean_comment_passes():
    cfg = SpamRuleConfig(spam_keywords=["sale", "free"])
    verdict = detect_spam("정말 유익한 영상이네요. 감사합니다!", config=cfg)
    assert not verdict.is_spam
    assert verdict.score < 1.0


def test_too_short_alone_not_spam_but_added_with_emoji():
    cfg = SpamRuleConfig(min_length=3, max_emoji_ratio=0.5)
    # "🔥🔥" → 100% emoji, 2 chars → too_short + emoji_heavy → 0.4 + 0.4 = 0.8 (under threshold 1.0)
    verdict = detect_spam("🔥🔥", config=cfg)
    assert not verdict.is_spam
    assert "too_short" in verdict.reasons
    assert "emoji_heavy" in verdict.reasons


def test_too_many_mentions_flags():
    cfg = SpamRuleConfig(max_mentions=2, score_threshold=0.5)
    verdict = detect_spam("@alice @bob @carol @dave 봐주세요", config=cfg)
    assert verdict.is_spam
    assert any(r.startswith("too_many_mentions:") for r in verdict.reasons)


def test_url_plus_shortener_does_not_double_count_too_aggressively():
    cfg = SpamRuleConfig()
    verdict = detect_spam("see bit.ly/x for more", config=cfg)
    # contains_url (1.0) + extra (0.2) = 1.2 → still spam, but under 2.0
    assert verdict.is_spam
    assert verdict.score < 2.0


def test_helper_constructor_accepts_model_like_attrs():
    cfg = rule_config_from_model_attrs(
        block_urls=True, spam_keywords=["x"], score_threshold=0.5,
    )
    assert isinstance(cfg, SpamRuleConfig)
    assert cfg.spam_keywords == ["x"]
    assert cfg.score_threshold == 0.5
