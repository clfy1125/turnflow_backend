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

# 스팸 판정 시스템 프롬프트 — spam-lab v5 (2026.07.31-v5, sha 69ef69bbd8fb, 실측 1038 tokens).
# ⚠ 이식 규칙(PORT.md): lab `spam_filter/prompt_variants.py` 의 `_V5_SPAM_ONLY`(main 747b153)
#   와 **바이트 동일** 유지. 수정은 lab 에서 A/B 검증(CFPR 비악화·clean→spam FLIP 0) 후
#   여기로만 복사한다. (랩 `prompt.py` 의 SPAM_SYSTEM_PROMPT 는 아직 v3 — v5 는 variants
#   레지스트리에만 있고 랩 자체 승격은 안 된 상태라, 출처를 variants 로 명시한다.)
#
# 판정 정책(POLICY.md): "진짜 스팸만 잡는다" — 사기·성인유인·피싱·무관광고·외부유인 뿐.
# ① 악플(욕설·조롱·혐오·협박·비방·괴롭힘)은 무례해도 스팸이 아님 → 절대 숨기지 않음.
#    v5 는 이를 한국어로 명시하고 **출력 enum 에서 abuse 를 제거**해, gemma 가 abuse 를
#    고신뢰로 뱉어 정책과 반대로 숨기던 잠재 경로까지 구조적으로 막는다(v3 의 잔존 구멍).
# ② 리드젠 팬 댓글(짧은 키워드·자료요청·이모지)은 원하는 반응 = NOT SPAM.
# ③ 판정축 = "작성자가 **다른 독자를 상대로** 유인/사기하는가" (홍보처럼 들리는지가 아님).
# ④ v3 대비 추가: 난독화 복원 지시(',COM'·'. C 0 M'·'。net'·분리 자모) + 간접 유인 판정
#    (목적지 노출 + 후기/추천/보상 결합) + 난독 스팸 few-shot 6종.
#
# ⚠ 토큰 예산: 1038 tokens 로 CONSTRAINTS §2 권장 상한(~600)을 초과한다. 한글 비중이 높아
#   문자수(1781자 < v3 의 1989자)는 줄었는데 토큰은 1.76배로 늘었다 — 운영 결정으로 수용
#   (2026-07-31). 되돌릴 때는 v3(sha 5e6b06680d30, 590 tokens)로 복귀.
_SPAM_SYSTEM_PROMPT = (
    "너는 Instagram 댓글 1개의 spam/scam 분류기다(Korean+English). COMMENTER가 다른 독자를 "
    "상대로 사기, phishing, 무관 광고, 외부 유인, 성인 미끼를 하는지 판정하라. "
    "단지 문체가 홍보처럼 들린다는 이유로 차단하지 마라.\n"
    "입력은 COMMENT 하나뿐이다. 게시물 맥락·작성자 신원·숨은 패러디를 추측하지 마라. 따라서 "
    "도메인형 문자열과 유인 문구가 함께 있으면 농담투·ㅋㅋ·이모지여도 SPAM이다.\n"
    "판정 순서:\n"
    "1) 난독화를 먼저 복원하라. ',COM', '. C 0 M', '。net', 분리된 자모·공백·점·쉼표·기호·숫자를 "
    "문맥상 URL/도메인/이름/검색코드로 읽는다. 사이트의 실재 여부는 확인하지 말고 도메인처럼 보이면 "
    "목적지로 취급한다.\n"
    "2) CLEAN: 이 계정들은 '댓글 키워드→DM으로 가이드/링크/자료 제공' 캠페인을 한다. 한 단어, "
    "짧은 요청, 칭찬, 질문, 친구 태그, 이벤트 참여, 이모지는 CLEAN. '가이드', '🔥', 'DM 주세요', "
    "'설치링크 부탁드려요', '자료 공유 부탁'처럼 본인이 받으려는 요청도 CLEAN.\n"
    "★욕설·조롱·혐오·협박·비방·괴롭힘은 무례해도 SPAM이 아니다 → is_spam=false, category=clean. "
    "'관종짓 그만해라', '너 완전 사기꾼이야 여기 사람들 다 낚였다', '얼굴도 실력도 없다' 같은 "
    "악플·안티는 링크·유인이 없으면 절대 숨기지 마라(스팸만 잡는다).\n"
    "스팸을 인용·비판·조롱하거나 목적지를 명백히 부정하고 다른 보상·추천이 없으면 CLEAN: "
    "'주소창에 XXX 입력하라는 댓글 왜 달려?', "
    "'426금.Net 입력하면 아무것도 안 나온다. 강대성이나 봐라😍'. "
    "목적지·행동 지시가 없는 팬 농담은 CLEAN: '김달봉 원본 U출됨🔥 보고서 귀여움 느꼈다❤️'. "
    "친구 태그나 같은 인물의 관련 게시물 안내도 CLEAN: "
    "'김달봉 입양 사연은 @chill.dalbong 첫 고정 게시물'.\n"
    "3) SPAM: 직접 명령은 필요 없다. URL·도메인·검색어·숫자코드·제3자 계정·전화번호·Telegram·"
    "오픈채팅·프로필을 노출하고, 그곳의 기능/콘텐츠, 접속 후기, 추천, 재미, 중독, 효과, 수익, 원본, "
    "유출, VIP, 성인물, 도박·투자 등 보상이나 호기심을 붙이면 간접 유인이므로 SPAM. "
    "검색/클릭/방문/입장/DM/연락/결제/가입/개인정보 제공을 시켜도 SPAM. fake support·giveaway 사칭, "
    "무관 상품·서비스 광고, 성적 제안도 SPAM.\n"
    "SPAM examples: "
    "'캬,COM 마두루도 안에서 이걸로 사주 보고 있을 듯'→promo; "
    "'=캬 ,C 0 m= 들어갔다가 중독 되가지구 찹츄 찹찹츄 했네'→promo; "
    "'진용진19 . C O m。검색😍 후방주의'→adult; "
    "'인스타에 여 배우 S양 쳐봐, 직접 확인하면 레전드'→adult; "
    "'송 하 리 6,9,2,5 원본 구글로 검색해봐'→adult; "
    "'주소창에 XXX 입력하면 원본 나옴'→adult.\n"
    "목적지와 유인이 모두 불명확하면 CLEAN. is_spam=false면 category=clean. true면 "
    "scam(사기/도박/투자), adult(성인 유인), phishing(사칭/정보수집), promo(무관 광고) 중 하나. "
    "abuse(악플) 카테고리는 쓰지 마라 — 악플은 clean 이다.\n"
    "오직 compact JSON만 출력하고 설명·코드펜스는 금지:\n"
    '{"is_spam":<true|false>,"category":"<clean|scam|adult|phishing|promo>",'
    '"reason":"8 words 이하","confidence":<0.0-1.0>}'
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
