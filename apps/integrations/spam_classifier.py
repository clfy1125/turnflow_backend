"""Instagram 댓글 스팸 분류 (하이브리드: 규칙 pre-filter + gemma LLM).

설계 원칙
---------
1) **하이브리드**: 규칙(URL/키워드)으로 명백한 스팸은 0초·LLM 없이 즉시 차단하고,
   애매한 댓글만 gemma 로 판정한다(gemma ~14 tok/s 부하 최소화).
2) **fail-open**: LLM 예외/타임아웃/파싱실패/낮은 신뢰도는 모두 "스팸 아님"으로 처리한다.
   불확실할 때 절대 숨기지 않는다 → LLM 장애가 정상 댓글 대량 숨김을 유발하지 않게.
3) 규칙 히트는 authoritative — 규칙에서 스팸이면 LLM 을 호출하지 않는다(가장 저렴·확실).

반환값 ``SpamVerdict`` 는 태스크가 그대로 로그(SpamCommentLog)에 기록한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apps.ai_jobs.services.llm_client import call_llm_messages_with_usage
from apps.ai_jobs.services.model_router import resolve_model
from apps.ai_jobs.services.parsers import extract_json

from .services import SpamDetectionService

logger = logging.getLogger(__name__)

# 이 길이 미만이면 LLM 을 부르지 않고 정상 처리(이모지·"👍"·빈 문자 등).
MIN_LEN_FOR_LLM = 3
# LLM 입력 텍스트 상한 — 스팸은 대개 짧고, 잘라도 판정에 무해.
CHAR_CAP = 500
# 스팸이라도 이 신뢰도 미만이면 숨기지 않음(fail-open).
SPAM_CONFIDENCE_THRESHOLD = 0.7
# 판정 JSON 은 매우 짧으므로 출력 토큰을 작게 → 빠르고 이어받기(continuation) 불필요.
GEMMA_MAX_TOKENS = 64

_SPAM_SYSTEM_PROMPT = (
    "You are a spam/scam detector for Instagram comments (Korean + English). "
    "Classify the single comment the user sends. "
    "Reply with ONLY a compact JSON object, no prose, no code fence:\n"
    '{"is_spam": <true|false>, '
    '"category": "<clean|scam|adult|phishing|promo|abuse>", '
    '"reason": "<= 8 words", "confidence": <0.0-1.0>}\n'
    "SPAM = scam/betting links, adult-bait ('원본영상', 'DM 주세요' 유인), phishing, "
    "mass promotion/ads, off-topic link farming, '주소창'/'실시간검색' 류 유인 문구. "
    "NOT SPAM = genuine questions, praise, criticism, normal conversation, emojis."
)


@dataclass
class SpamVerdict:
    """스팸 판정 결과."""

    is_spam: bool
    category: str = "clean"
    reasons: list = field(default_factory=list)
    confidence: float = 0.0
    engine: str = "rule"  # rule / rule_trivial / rule_only / llm / llm_lowconf / llm_failopen
    error: str = ""


def classify_comment(
    text: str,
    *,
    spam_keywords: list | None = None,
    block_urls: bool = True,
    use_llm: bool = True,
) -> SpamVerdict:
    """댓글 1건을 스팸 판정한다. 규칙 우선, 애매하면 gemma.

    Args:
        text: 댓글 본문
        spam_keywords: 계정별 차단 키워드(없으면 기본 키워드 사용)
        block_urls: URL 포함을 스팸으로 볼지
        use_llm: False면 규칙만으로 판정(gemma 미호출)
    """
    text = (text or "").strip()

    # 1) 규칙 즉시차단 (0초, LLM 없음) — authoritative
    is_rule_spam, reasons = SpamDetectionService.is_spam(
        text=text, spam_keywords=spam_keywords, check_urls=block_urls
    )
    if is_rule_spam:
        return SpamVerdict(
            is_spam=True, category="rule", reasons=reasons, confidence=1.0, engine="rule"
        )

    # 2) 너무 짧은 댓글(이모지 등)은 LLM 없이 정상 처리
    if len(text) < MIN_LEN_FOR_LLM:
        return SpamVerdict(is_spam=False, engine="rule_trivial")

    # 3) LLM 비활성(kill-switch)이면 규칙만으로 정상 판정
    if not use_llm:
        return SpamVerdict(is_spam=False, engine="rule_only")

    # 4) 애매한 나머지 → gemma
    return _classify_with_gemma(text)


def _classify_with_gemma(text: str) -> SpamVerdict:
    """gemma-4 로 스팸 판정. 실패는 전부 fail-open(스팸 아님)."""
    try:
        result = call_llm_messages_with_usage(
            model=resolve_model("gemma"),
            messages=[
                {"role": "system", "content": _SPAM_SYSTEM_PROMPT},
                {"role": "user", "content": text[:CHAR_CAP]},
            ],
            max_tokens=GEMMA_MAX_TOKENS,
            temperature=0.0,
        )
        obj = extract_json(result.content)
    except Exception as exc:  # 예외/타임아웃/파싱실패 → fail-open
        logger.warning("스팸 gemma 판정 실패(fail-open 처리): %s", exc)
        return SpamVerdict(is_spam=False, engine="llm_failopen", error=str(exc)[:200])

    is_spam = bool(obj.get("is_spam"))
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    category = (str(obj.get("category") or "clean"))[:32]
    reason = (str(obj.get("reason") or ""))[:120]

    if not is_spam:
        return SpamVerdict(is_spam=False, category="clean", confidence=confidence, engine="llm")

    # 스팸이라도 신뢰도가 낮으면 숨기지 않음(fail-open)
    if confidence < SPAM_CONFIDENCE_THRESHOLD:
        return SpamVerdict(
            is_spam=False, category=category, confidence=confidence, engine="llm_lowconf"
        )

    reasons = [f"llm:{category}"]
    if reason:
        reasons.append(reason)
    return SpamVerdict(
        is_spam=True, category=category, reasons=reasons, confidence=confidence, engine="llm"
    )
