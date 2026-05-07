"""
Heuristic spam detection for social-media comments.

This module is intentionally provider-agnostic. It takes raw comment text and a
``SpamRuleConfig`` (mirrors what each platform's ``*SpamFilterConfig`` model
stores) and returns a ``SpamVerdict``.

Platform-specific moderation actions (hide, set moderation status, etc.) live
in each integration app's services — this module decides *whether* something is
spam, not *what to do about it*.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Patterns
# ─────────────────────────────────────────────────────────────────────────────

# Match http/https URLs and bare domains. Conservative — false positives are
# tolerable because the action default is "hide" not "delete".
_URL_RE = re.compile(
    r"(?xi)"
    r"\b("
    r"  (?:https?://|www\.)"      # explicit URL
    r"|"
    r"  (?:[a-z0-9\-]+\.)+(?:com|net|org|io|kr|co|app|me|ly|tv|to|biz|info|xyz|shop|store|dev)\b"
    r")"
    r"[^\s]*"
)

# Common URL shorteners — even if not flagged by _URL_RE, these are spam-heavy.
_SHORTENER_DOMAINS = (
    "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly",
    "rebrand.ly", "cutt.ly", "shorturl.at",
)
_SHORTENER_RE = re.compile(
    "|".join(re.escape(d) for d in _SHORTENER_DOMAINS), re.IGNORECASE,
)

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"   # Misc symbols & pictographs … Symbols & Pictographs Extended
    "\U0001F600-\U0001F64F"    # Emoticons
    "\U0001F900-\U0001F9FF"    # Supplemental Symbols and Pictographs
    "☀-➿"            # Misc symbols + Dingbats
    "⌀-⏿"            # Misc Technical
    "]+",
    flags=re.UNICODE,
)

# Mention/hashtag spam: lots of @-mentions in a single short comment.
_MENTION_RE = re.compile(r"@[\w._]{2,30}")
_HASHTAG_RE = re.compile(r"#[\w가-힣]{2,30}")


# ─────────────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpamRuleConfig:
    """
    Rule knobs. Mirrors the JSON-friendly fields stored on each platform's
    ``*SpamFilterConfig`` model.
    """

    block_urls: bool = True
    block_shortened_urls: bool = True
    spam_keywords: List[str] = field(default_factory=list)

    # Soft signals — contribute to the score but don't unilaterally flag.
    min_length: int = 2
    max_emoji_ratio: float = 0.7
    max_mentions: int = 3

    # Score threshold above which the verdict is is_spam=True.
    # Each rule contributes a weight to the score; total range is [0.0, ~3.0].
    score_threshold: float = 1.0


@dataclass
class SpamVerdict:
    is_spam: bool
    score: float
    reasons: List[str] = field(default_factory=list)

    def add(self, reason: str, weight: float):
        self.reasons.append(reason)
        self.score += weight


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_spam(text: str, *, config: Optional[SpamRuleConfig] = None) -> SpamVerdict:
    """
    Score a comment using rule-based signals.

    Each rule that matches contributes a weight to the verdict score:

    - Contains URL                         → +1.0  (hard signal)
    - Contains shortened URL               → +1.0  (already covered by URL rule
                                                    most of the time, but explicit
                                                    so the reason list is clearer)
    - Spam keyword hit                     → +1.0  per keyword (capped at 1.0 total)
    - Length below min_length              → +0.4  (e.g. "ㅎㅎ", "ok")
    - Emoji ratio above max_emoji_ratio    → +0.4
    - Mentions > max_mentions              → +0.6  (likely tagging spam)

    A verdict's ``is_spam`` is True iff ``score >= config.score_threshold``.
    """
    cfg = config or SpamRuleConfig()
    verdict = SpamVerdict(is_spam=False, score=0.0)

    if not text:
        return verdict
    normalized = unicodedata.normalize("NFKC", text)

    # URL detection
    if cfg.block_urls and _URL_RE.search(normalized):
        verdict.add("contains_url", 1.0)
    if cfg.block_shortened_urls and _SHORTENER_RE.search(normalized):
        # Don't double-count if the URL rule already fired.
        if "contains_url" not in verdict.reasons:
            verdict.add("contains_shortened_url", 1.0)
        else:
            verdict.add("contains_shortened_url_extra", 0.2)

    # Keyword match (case-insensitive substring)
    if cfg.spam_keywords:
        lowered = normalized.lower()
        matched = [k for k in cfg.spam_keywords if k and k.lower() in lowered]
        if matched:
            for k in matched[:5]:
                verdict.reasons.append(f"keyword:{k}")
            verdict.score += min(1.0, 0.5 * len(matched))

    # Length
    if len(normalized.strip()) < cfg.min_length:
        verdict.add("too_short", 0.4)

    # Emoji ratio
    emoji_chars = sum(len(m.group(0)) for m in _EMOJI_RE.finditer(normalized))
    total_chars = max(1, len(normalized.strip()))
    if cfg.max_emoji_ratio > 0 and emoji_chars / total_chars > cfg.max_emoji_ratio:
        verdict.add("emoji_heavy", 0.4)

    # Mention spam
    mentions = _MENTION_RE.findall(normalized)
    if cfg.max_mentions > 0 and len(mentions) > cfg.max_mentions:
        verdict.add(f"too_many_mentions:{len(mentions)}", 0.6)

    verdict.is_spam = verdict.score >= cfg.score_threshold
    return verdict


# ─────────────────────────────────────────────────────────────────────────────
# Convenience constructor — lets each app build a SpamRuleConfig from its
# *SpamFilterConfig model without leaking the dataclass field names.
# ─────────────────────────────────────────────────────────────────────────────

def rule_config_from_model_attrs(
    *,
    block_urls: bool,
    block_shortened_urls: bool = True,
    spam_keywords: Optional[Iterable[str]] = None,
    min_length: int = 2,
    max_emoji_ratio: float = 0.7,
    max_mentions: int = 3,
    score_threshold: float = 1.0,
) -> SpamRuleConfig:
    return SpamRuleConfig(
        block_urls=block_urls,
        block_shortened_urls=block_shortened_urls,
        spam_keywords=list(spam_keywords or []),
        min_length=min_length,
        max_emoji_ratio=max_emoji_ratio,
        max_mentions=max_mentions,
        score_threshold=score_threshold,
    )
