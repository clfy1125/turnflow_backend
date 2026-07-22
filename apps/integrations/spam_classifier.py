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
# 0.9 로 상향(2026-07-22): gemma 가 짧은 리드젠 요청("설치링크 부탁드려요")을 phishing 0.85 로
# 오탐한 사례가 있어, 프롬프트 개선과 함께 문턱을 올려 0.7~0.9 구간 오탐을 자동 억제한다.
# (auto_hide 는 기본 off·불확실할 때 숨기지 않는 fail-open 철학과 일관 — 진짜 스팸을 놓치더라도
#  정상 팬 댓글을 숨기지 않는 쪽을 택한다.)
SPAM_CONFIDENCE_THRESHOLD = 0.9
# 판정 JSON 은 매우 짧으므로 출력 토큰을 작게 → 빠르고 이어받기(continuation) 불필요.
GEMMA_MAX_TOKENS = 64

# 스팸 판정 시스템 프롬프트.
#
# 핵심 원칙: "댓글이 홍보처럼 들리는가"가 아니라 "댓글 작성자가 **다른 독자를 상대로**
# 스팸/사기/유인 행위를 하는가"를 판정한다. 이 계정들은 대부분 리드젠/이벤트 캠페인
# ("댓글에 키워드 달면 DM 으로 가이드/링크/자료 드려요")을 돌리므로, 팬들은 **의도적으로**
# 아주 짧은 댓글(키워드 한 단어·자료 요청·칭찬·이모지)을 단다 = 원하는 반응이지 스팸이 아님.
#
# 회귀 방지(2026-07-21~22 3dragon_pd): "설치링크 부탁드려요"→phishing, 짧은 리드젠 키워드
# ("가이드"·"용피디"·"클로드")·이모지가 promo/scam/adult 로 오분류되던 문제. 특히 이전 프롬프트가
# 'DM 주세요' 유인 을 SPAM 예시로 넣어, 가장 흔한 정상 리드젠 요청을 스팸으로 유도했음.
_SPAM_SYSTEM_PROMPT = (
    "You are a spam/scam moderation classifier for Instagram comments (Korean + English). "
    "Judge ONE comment: decide whether the COMMENTER is spamming — trying to scam, phish, "
    "mass-advertise, or lure OTHER readers somewhere. Do NOT judge whether the comment merely "
    "'sounds' promotional.\n"
    "CONTEXT: These accounts run lead-generation / giveaway campaigns — the creator says "
    "'comment a keyword to get a guide/link/freebie via DM'. So fans intentionally leave VERY "
    "SHORT comments: a single keyword, a request for the offered item, praise, or an emoji. "
    "That is the DESIRED response, NOT spam.\n"
    "NOT SPAM (is_spam=false):\n"
    "- Short comments, a single word/keyword, or emoji-only "
    "(e.g. '가이드', '신청', '클로드', '용피디', '60분', '🔥', '👏').\n"
    "- Asking for what the creator offered: '링크 주세요', 'DM 주세요', '가이드 부탁드려요', "
    "'설치링크 알려주세요', '자료 공유 부탁드립니다' — the commenter REQUESTS for themselves, "
    "they are not luring others.\n"
    "- Genuine questions, interest, praise, criticism, normal talk, tagging a friend, "
    "giveaway participation.\n"
    "SPAM (is_spam=true) — ONLY when the commenter clearly acts against other readers:\n"
    "- Posts scam/betting/investment/adult links, or drives traffic elsewhere "
    "('주소창 ○○', '실시간검색 ○○', '원본영상' 프사 유인, telegram/kakao open-chat to third parties).\n"
    "- Phishing or impersonation (fake giveaway / fake support account harvesting info).\n"
    "- Mass unsolicited advertising of an unrelated product or service.\n"
    "- Sexual solicitation / adult-content baiting aimed at readers; abuse, hate, harassment.\n"
    "RULES: A short comment with NO link, NO third-party @handle, and NO lure aimed at others "
    "is CLEAN — default is_spam=false. 'Requesting X for myself' is CLEAN; only 'DM me / click "
    "here to get X' from an unrelated promoter is SPAM. When unsure, choose is_spam=false. "
    "Set is_spam=true only at high confidence.\n"
    "Reply with ONLY a compact JSON object, no prose, no code fence:\n"
    '{"is_spam": <true|false>, '
    '"category": "<clean|scam|adult|phishing|promo|abuse>", '
    '"reason": "<= 8 words", "confidence": <0.0-1.0>}'
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
