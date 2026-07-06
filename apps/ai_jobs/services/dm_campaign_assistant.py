"""게시물(이미지+캡션) → AutoDM 캠페인 폼 초안 생성 서비스 (비전 통합).

사용자가 캠페인 생성 폼에서 게시물만 고르면, 그 게시물의 이미지+캡션을 멀티모달 LLM
(gemma-4)으로 분석해 폼 필드 초안을 채워준다. 사용자는 결과를 그대로 쓰지 않고 직접
수정하므로 완벽함보다 합리적 기본값을 우선한다.

**공개 답글(public_reply_templates)은 LLM 으로 만들지 않는다.** "방금 DM 보냈어요" 류의
정형 인사 문구라 매번 LLM 으로 50개를 생성하면 느리기만 하다(gemma 50개 ≈ 70초). 대신
코드에 기본 문구 풀(``_REPLY_BASES``)을 두고, 끝맺음(문장부호 ``!``/``~`` · 이모지 · 이모티콘)을
변주해 수천 개 조합을 만든 뒤 그중 N개를 **즉시(수 ms)** 뽑는다(스팸 탐지 회피용으로 서로 다른
기본 문구 우선). LLM 은 게시물 맥락이 필요한 항목(이름/키워드/DM 본문/게이트 문구)만 만든다.

생성 항목 (출력 키는 AutoDMCampaignCreateSerializer 필드명과 1:1 매핑):
  - name                     캠페인 이름            ← LLM
  - keyword_filter / keyword_mode   트리거 키워드 + 매칭 방식   ← LLM
  - opening_message_template 단순 DM 본문 (게이트 off)  ← LLM
  - follow_gate.*            팔로우 게이트 문구 묶음    ← LLM (검증/버튼전용 두 하위모드 공용)
  - public_reply_templates   공개 답글 후보 N개 (기본 50)  ← 코드 풀에서 변주 추출 (LLM 미사용)

이 서비스는 DB 를 건드리지 않는다 — LLM 호출 + 정규화만. 절대 raise 하지 않고(파싱 실패
시 전 필드 폴백) 항상 사용 가능한 초안을 돌려준다.
"""

from __future__ import annotations

import base64
import logging
import random
import re
from dataclasses import dataclass, field

from .llm_client import call_llm_messages_with_usage
from .parsers import extract_json

logger = logging.getLogger(__name__)


# ── 상수 ───────────────────────────────────────────────────────

_VALID_KEYWORD_MODES = frozenset({"any", "all", "exact"})

# 링크 버튼 URL 을 사용자가 안 줬을 때 채워 넣는 예시 URL(폼에서 실제 링크로 교체).
_EXAMPLE_LINK_URL = "https://example.com"

# 공개 답글 기본 문구 풀 — 끝맺음(문장부호/이모지)은 _decorate 가 붙이므로 여기엔 넣지 않는다.
# "댓글 단 사람에게 공개 답글로 다는 정형 인사"라 게시물 맥락과 무관. 말투(정중/캐주얼/발랄),
# 표현(감사/발송알림/확인요청)을 골고루 섞어 서로 본문이 다르게 했다.
_REPLY_BASES: list[str] = [
    # 발송 알림
    "방금 DM 보내드렸어요",
    "디엠 보냈어요",
    "메시지 보내드렸어요",
    "DM 보내드렸어요",
    "DM 드렸어요",
    "방금 메시지 발송했어요",
    "안내 DM 보내드렸어요",
    "디엠으로 보내드렸습니다",
    "메시지함으로 보냈어요",
    "방금 DM 드렸어요",
    "지금 바로 보내드렸어요",
    "방금 막 보냈어요",
    "메시지 보냈어요",
    "DM 갔어요",
    "DM 도착했어요",
    # 확인 요청
    "디엠 확인 부탁드려요",
    "DM 확인 부탁드려요",
    "메시지 확인해 주세요",
    "디엠 확인해 주세요",
    "DM함 한번 열어봐 주세요",
    "DM 확인해 주세요",
    "받은 메시지 확인 부탁드려요",
    "DM 꼭 확인해 주세요",
    "디엠 놓치지 말고 확인해 주세요",
    "메시지함 체크 부탁드려요",
    "편하실 때 DM 확인 부탁드려요",
    "확인하시면 돼요",
    "DM 확인 한 번 부탁드릴게요",
    "메시지함 한번 확인 부탁드려요",
    # 감사
    "댓글 감사해요",
    "댓글 남겨주셔서 감사합니다",
    "관심 가져주셔서 감사해요",
    "관심 감사합니다",
    "찾아주셔서 감사해요",
    "댓글 고마워요",
    "관심 가져주셔서 고마워요",
    "문의 주셔서 감사합니다",
    "남겨주신 댓글 잘 봤어요",
    "기다려주셔서 감사해요",
    # 감사 + 발송/확인 결합
    "댓글 감사해요 방금 DM 보내드렸어요",
    "관심 감사합니다 메시지 확인해 주세요",
    "문의 감사해요 디엠 드렸어요",
    "댓글 고마워요 DM 보냈어요",
    "관심 가져주셔서 감사해요 DM 확인 부탁드려요",
    "댓글 남겨주셔서 감사해요 메시지 보내드렸어요",
    "찾아주셔서 감사해요 디엠 확인 부탁드려요",
    "문의 감사합니다 안내 메시지 보내드렸어요",
    # 캐주얼/발랄 (발랄하되 반드시 존댓말 — 반말/슬랭 금지)
    "디엠 방금 보내드렸어요",
    "따끈한 DM 보내드렸어요",
    "지금 바로 DM 확인해 보세요",
    "디엠함 얼른 확인해 보세요",
    "메시지 방금 도착했어요",
    "DM 왔어요 확인해 주세요",
    "디엠 확인만 해주시면 돼요",
    "DM 보냈으니 얼른 확인해 주세요",
    "얼른 디엠 한번 확인해 보세요",
    "지금 디엠 확인 가능하실까요",
    # 정중/완곡
    "자세한 내용 DM으로 안내드렸어요",
    "디엠 보내드렸으니 확인 부탁드립니다",
    "문의하신 내용 메시지로 보내드렸어요",
    "요청하신 정보 DM으로 전달드렸어요",
    "상세 안내는 DM으로 보내드렸습니다",
    "궁금하신 점 DM으로 답해드렸어요",
    "디엠으로 내용 확인해 주세요",
    "빠르게 디엠 확인해 주세요",
    "안내 메시지 확인 부탁드려요",
    "디엠으로 자세히 안내드릴게요",
    "DM 보냈어요 감사합니다",
    "DM에서 이어서 안내드릴게요",
    "디엠으로 자세히 보내드렸어요",
    "확인하시고 궁금한 점 있으면 답장 주세요",
    # 친근 인사
    "안녕하세요 DM 드렸어요",
    "안녕하세요 메시지 확인해 주세요",
    "반갑습니다 DM 보냈어요",
    "좋은 하루 되세요 DM 확인 부탁드려요",
    "행복한 하루 보내세요 메시지 보냈어요",
    "반가워요 디엠 보내드렸어요",
    # 기타 변형
    "곧 디엠 도착할 거예요",
    "DM 보냈어요. 확인해 주세요",
    "디엠 꼭 확인해 주세요",
    "메시지 도착했는지 확인해 주세요",
    "DM 보내드렸으니 편히 확인하세요",
    "디엠 보냈습니다 확인 부탁드려요",
    "DM 보내드렸으니 놓치지 말고 봐주세요",
    "관심 가져주셔서 감사해요 디엠 드렸어요",
    "방금 보내드렸어요 좋은 하루 되세요",
    "메시지 갔어요 확인해 주시면 돼요",
]

# 끝맺음 변주 — 같은 기본 문구라도 다르게 보이도록(스팸 탐지 회피). 공백 없는 문장부호 +
# 공백 붙는 이모지/이모티콘. "" 도 넣어 "변주 없음(담백)"도 나오게 한다.
_REPLY_PUNCT: list[str] = ["", ".", "!", "!!", "~", "~~"]
_REPLY_EMOJI: list[str] = [
    "",
    " 😊",
    " 🙏",
    " 💌",
    " 🎁",
    " ✨",
    " 🙌",
    " 😉",
    " 🥰",
    " 🌷",
    " 🤍",
    " :)",
    " ^^",
]

# 변주를 모두 곱한 조합 수(≈ 7천 개). 그중 N(≤50)개를 서로 다른 기본 문구 우선으로 뽑는다.
REPLY_POOL_SIZE = len(_REPLY_BASES) * len(_REPLY_PUNCT) * len(_REPLY_EMOJI)


def _decorate(base: str, rng: random.Random) -> str:
    """기본 문구 끝에 무작위 문장부호 + 이모지를 붙여 변주."""
    return f"{base}{rng.choice(_REPLY_PUNCT)}{rng.choice(_REPLY_EMOJI)}"


def sample_replies(n: int, *, seed: int | None = None) -> list[str]:
    """공개 답글 N개를 코드 풀에서 즉시 뽑는다(LLM 미사용).

    스팸 탐지 회피를 위해 **서로 다른 기본 문구**를 우선 뽑아(본문이 달라야 안전) 끝맺음을
    변주한다. N 이 기본 문구 수보다 많을 때만 같은 문구를 다른 변주로 재사용한다.
    seed 를 주면 결정적(테스트용), 안 주면 호출마다 다른 조합(캠페인별 다양화).
    """
    n = max(1, min(int(n or 50), 50))
    rng = random.Random(seed)
    bases = _REPLY_BASES[:]
    rng.shuffle(bases)

    out: list[str] = []
    used: set[str] = set()
    # 1차: 서로 다른 기본 문구 1개씩
    for base in bases:
        if len(out) >= n:
            break
        v = _decorate(base, rng)
        if v not in used:
            used.add(v)
            out.append(v)
    # 2차: 그래도 부족하면(N > 기본 문구 수) 변주로 채움
    guard = 0
    while len(out) < n and guard < n * 50:
        guard += 1
        v = _decorate(rng.choice(bases), rng)
        if v not in used:
            used.add(v)
            out.append(v)
    return out[:n]


# ── 시스템 프롬프트 (이름/키워드/DM 본문/게이트 문구만 — 답글 제외) ──

SYSTEM_PROMPT = """너는 한국어 인스타그램 마케팅 카피라이터 어시스턴트다. 사장님이 올린 \
인스타그램 게시물(이미지 + 캡션)을 보고, 그 게시물 댓글에 자동으로 DM을 보내주는 "AutoDM 캠페인" \
폼의 초안을 한국어로 작성한다. 사용자는 네 결과를 그대로 쓰지 않고 직접 수정한다. 그러니 \
완벽함보다 합리적인 기본값을 우선한다.

# 가장 중요한 원칙
- 이미지를 실제로 보고(제품/혜택/분위기/색감/문구) 캡션과 합쳐 무엇을 주는 게시물인지 파악한 뒤 거기에 맞춰 써라.
  이미지에서 읽히지 않는 혜택/가격/링크를 지어내지 마라. 모르면 일반적이고 안전한 표현을 쓴다.
- 모든 출력 텍스트는 자연스러운 한국어. 과장·허위·의학적 단정·"100% 보장" 류 금지.
- 이모지는 양념이다. 전부에 넣지 말고 일부에만 0~2개. 같은 이모지를 반복 남발하지 마라.

# 생성할 항목
1. name — 캠페인 이름. 게시물/혜택에서 뽑은 짧은 한국어 제목. 10~20자 권장, 최대 40자. 이모지 없이 담백하게.
2. kw — 댓글 작성자가 DM을 받으려고 입력할 만한 트리거 단어 3~6개(한국어 위주, 1~5자 짧은 명사).
   게시물 맥락에서 추론한다. 예: ["정보","가격","구매","참여","신청"]. 흔한 잡담어("ㅋㅋ","좋아요")는 제외.
3. kw_mode — 특별한 이유가 없으면 "any".
4. opening_dm — SIMPLE 모드 DM. 댓글 단 모두에게 즉시 보내는 첫 DM. 캠페인 목적/업종을 반영해 따뜻하고 브랜드톤 있게.
   2~4줄, 이모지 적당히. 게시물 혜택을 가볍게 언급해도 좋다.
   **링크 URL 은 본문에 절대 넣지 마라** — 링크는 시스템이 별도 "버튼"으로 붙인다(아래 link_label 참고).
5. follow_gate 묶음 — 버튼 게이트용. 아래 두 하위 모드 **모두에서 자연스럽게** 읽혀야 한다:
   (A) 팔로우 검증 모드: 버튼 클릭 → 팔로우 확인 → 통과 시 reward. (B) 버튼 전용 모드: 버튼 클릭 즉시 reward(검증 없음).
   - gate_prompt — 버튼이 달린 안내 DM. "댓글 감사 + 아래 버튼을 눌러 자료를 받아가세요" 톤. 2~3줄.
     팔로우를 강요하지 말고 "팔로우하고 버튼을 눌러주세요" 정도로 부드럽게(검증 모드에도 맞고 버튼 전용에도 어색하지 않게).
   - gate_button — 버튼 글자. **반드시 20자 이내**. 두 모드 다 자연스러운 중립 표현 권장: "자료 받기" / "받기" / "혜택 받기".
   - gate_button_alt — 팔로우 검증 모드용 대안 버튼 글자(20자 이내). 예: "팔로우했어요".
   - reward_dm — 버튼 통과 후 보내는 본 DM. 약속한 실제 내용을 따뜻하게 2~4줄.
     **링크 URL 은 본문에 넣지 마라**(시스템이 버튼으로 첨부).
   - gate_retry — (검증 모드에서 팔로우 확인 실패 시에만 노출) "앗, 팔로우 확인이 안 됐어요. 프로필에서 팔로우 후
     버튼을 다시 눌러주세요" 톤. 1~2줄, 정중하고 가볍게. 버튼 전용 모드에선 안 쓰이지만 항상 채워서 출력.
6. link_label — 링크 버튼 글자. **항상 채운다**(LINK 유무와 무관). 게시물/혜택에 맞는 행동 유도형.
   한국어, 20자 이내. 예: "받으러 가기" / "무료로 받기" / "방법 확인하기" / "자세히 보기".

(공개 답글 문구는 시스템이 따로 만든다 — 네가 만들지 마라.)

# 링크 처리
링크 URL 은 어떤 본문(opening_dm/reward_dm/gate_prompt)에도 넣지 마라. 실제 URL 은 시스템이 DM 카드에
"버튼"으로 붙인다. 너는 그 버튼 글자(link_label)만 제안하면 된다(URL 은 시스템이 채움 — LINK 가 없으면 예시 URL).

# 출력
JSON 객체 1개만 출력. 코드펜스(```), 주석, 설명 텍스트, 인사말 금지. 반드시 한국어."""


# ── 스키마 힌트 / 메시지 빌더 ──────────────────────────────────


def _output_schema_hint() -> str:
    return (
        "{\n"
        '  "name": "캠페인 이름 (한국어, 10~20자 권장, 최대 40자, 이모지 없이)",\n'
        '  "kw": ["트리거 키워드 3~6개 (짧은 한국어 명사)"],\n'
        '  "kw_mode": "any | all | exact",\n'
        '  "opening_dm": "SIMPLE 모드 DM 본문 (2~4줄, 링크 URL 넣지 말 것)",\n'
        '  "gate_prompt": "버튼 게이트 안내 DM (2~3줄)",\n'
        "  \"gate_button\": \"버튼 글자 (20자 이내, 중립: '자료 받기'/'받기')\",\n"
        '  "gate_button_alt": "팔로우 검증 모드용 대안 버튼 글자 (20자 이내, 예: \'팔로우했어요\')",\n'
        '  "reward_dm": "버튼 통과 후 본 DM (2~4줄, 링크 URL 넣지 말 것)",\n'
        '  "gate_retry": "팔로우 확인 실패 시 재안내 (1~2줄)",\n'
        '  "link_label": "링크 버튼 글자 (LINK 있을 때만, 20자 이내; 없으면 빈 문자열)"\n'
        "}\n"
    )


def _image_block(image_bytes: bytes | None, image_mime: str, image_url: str) -> dict | None:
    """게시물 이미지를 OpenAI ``image_url`` content 블록으로 변환.

    base64 data-URI 우선(자체호스팅 gemma-4 vLLM 은 IG CDN 등 원격 URL fetch 가 불안정).
    바이트가 없으면 공개 http(s) URL 패스스루. 둘 다 없으면 None(텍스트-only 로 진행).
    ``image_labeler._image_url_block`` 와 동일한 정신.
    """
    if image_bytes:
        mime = image_mime or "image/jpeg"
        b64 = base64.b64encode(image_bytes).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    url = (image_url or "").strip()
    if url.startswith("http"):
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def _header_block(
    *,
    caption: str,
    business_type: str,
    campaign_goal: str,
    tone: str,
    link_url: str,
) -> str:
    cap = (caption or "").strip().replace("\n", " ")
    if len(cap) > 600:
        cap = cap[:600] + "…"
    link_line = (
        "있음 (URL 은 시스템이 버튼으로 첨부 — 본문엔 넣지 말고 link_label 만 제안)"
        if (link_url or "").strip()
        else "미지정 (예시 URL 로 채워짐 — link_label 은 그래도 반드시 제안)"
    )
    return (
        "[BUSINESS_CONTEXT]\n"
        f"- 업종: {business_type.strip() or '(미지정)'}\n"
        f"- 캠페인 목적: {campaign_goal.strip() or '(미지정)'}\n"
        f"- 원하는 말투: {tone.strip() or '(미지정 — 따뜻하고 친근하게)'}\n"
        f"- LINK: {link_line}\n\n"
        "[POST_CAPTION]\n"
        f'"{cap or "(캡션 없음 — 이미지로 판단)"}"\n\n'
        "[INSTRUCTIONS]\n"
        "아래 게시물 이미지를 직접 보고, 위 비즈니스 맥락과 캡션을 합쳐서 AutoDM 캠페인 폼 초안을 만들어줘."
    )


def _build_messages(
    *,
    caption: str,
    image_block: dict | None,
    business_type: str,
    campaign_goal: str,
    tone: str,
    link_url: str,
) -> list[dict]:
    header = _header_block(
        caption=caption,
        business_type=business_type,
        campaign_goal=campaign_goal,
        tone=tone,
        link_url=link_url,
    )
    user_content: list[dict] = [{"type": "text", "text": header}]
    if image_block is not None:
        user_content.append(image_block)
    user_content.append(
        {
            "type": "text",
            "text": (
                "\n[OUTPUT_SCHEMA]\n"
                + _output_schema_hint()
                + "\n위 스키마의 JSON 객체만 출력. 다른 텍스트/코드펜스/주석 금지."
            ),
        }
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ── 결과 dataclass ─────────────────────────────────────────────


@dataclass
class DmAssistResult:
    name: str = ""
    keyword_filter: list[str] = field(default_factory=list)
    keyword_mode: str = "any"
    public_reply_enabled: bool = True
    public_reply_templates: list[str] = field(default_factory=list)
    opening_message_template: str = ""
    follow_gate: dict | None = None  # {follow_gate_prompt, follow_gate_button_label, ...} 또는 None
    link_button: dict | None = (
        None  # {link_button_label, link_button_url} 또는 None (link_url 있을 때만)
    )
    # 텔레메트리 (ClassifyResult 와 동일 패턴)
    model: str = ""
    elapsed_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    estimated_cost_usd: float = 0.0
    vision_used: bool = False
    raw_content: str = ""
    reply_count: int = 0


# ── 정규화 헬퍼 (모델 절대 신뢰 금지) ──────────────────────────

_BULLET_RE = re.compile(r"^\s*(?:\d+[.)]|[-•*·])\s*")
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))
_LINK_RE = re.compile(r"\{\{?\s*link\s*\}?\}", re.IGNORECASE)


def _clean_list_item(s: object) -> str:
    """리스트 항목 1개 정리: 선행 번호/불릿 제거 → 감싼 따옴표 한 겹 제거 → 개행→공백 → trim."""
    if not isinstance(s, str):
        return ""
    t = s.strip()
    t = _BULLET_RE.sub("", t).strip()
    for lq, rq in _QUOTE_PAIRS:
        if len(t) >= 2 and t[0] == lq and t[-1] == rq:
            t = t[1:-1].strip()
            break
    t = re.sub(r"\s*\n\s*", " ", t)
    return t.strip()


def _clip(text: str, max_chars: int, ellipsis: bool = True) -> str:
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    if ellipsis and max_chars >= 1:
        return t[: max_chars - 1].rstrip() + "…"
    return t[:max_chars].rstrip()


def _strip_link_lines(text: str) -> str:
    """본문에서 {{link}} 플레이스홀더가 든 줄을 제거. 링크는 별도 버튼으로 첨부되므로 본문엔 안 넣는다."""
    t = text or ""
    if _LINK_RE.search(t):
        kept = [ln for ln in t.splitlines() if not _LINK_RE.search(ln)]
        return "\n".join(kept).strip()
    return t.strip()


def _normalize_meta_fields(raw: dict, *, link_url: str, include_follow_gate: bool) -> dict:
    """LLM 이 만든 항목(이름/키워드/DM 본문/게이트/링크버튼 라벨)을 정규화. dict 로 반환."""
    # name
    name = _clip(_clean_list_item(raw.get("name")), 40)
    if not name:
        name = "AutoDM 캠페인"

    # keyword_filter
    kw_raw = raw.get("kw") if isinstance(raw.get("kw"), list) else []
    kw_out: list[str] = []
    kw_seen: set[str] = set()
    for item in kw_raw:
        cleaned = _clip(_clean_list_item(item), 20, ellipsis=False)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in kw_seen:
            continue
        kw_seen.add(key)
        kw_out.append(cleaned)
        if len(kw_out) >= 6:
            break

    # keyword_mode
    mode = str(raw.get("kw_mode", "any")).strip().lower()
    if mode not in _VALID_KEYWORD_MODES:
        mode = "any"

    # opening DM — 링크 URL 은 본문에 넣지 않는다(버튼으로 첨부).
    opening = _strip_link_lines(str(raw.get("opening_dm") or ""))
    if not opening:
        opening = "댓글 남겨주셔서 감사해요! 자세한 내용은 DM으로 보내드릴게요 😊"

    # 링크 버튼 — 항상 제안한다. 라벨은 모델 제안(link_label) 또는 기본값.
    # URL 은 입력 link_url 있으면 그걸, 없으면 예시 URL(사용자가 폼에서 실제 링크로 교체)로 채운다.
    label = _clip(_clean_list_item(raw.get("link_label")), 20, ellipsis=False) or "자세히 보기"
    link_button = {
        "link_button_label": label,
        "link_button_url": (link_url or "").strip() or _EXAMPLE_LINK_URL,
    }

    result = {
        "name": name,
        "keyword_filter": kw_out,
        "keyword_mode": mode,
        "opening_message_template": opening,
        "link_button": link_button,
    }

    if include_follow_gate:
        prompt = _strip_link_lines(str(raw.get("gate_prompt") or ""))
        if not prompt:
            prompt = "댓글 감사합니다! 아래 버튼을 눌러 자료를 받아가세요 🎁"

        button = _clip(_clean_list_item(raw.get("gate_button")), 20, ellipsis=False)
        if not button:
            button = "자료 받기"
        button_alt = _clip(_clean_list_item(raw.get("gate_button_alt")), 20, ellipsis=False)
        if not button_alt:
            button_alt = "팔로우했어요"

        reward = _strip_link_lines(str(raw.get("reward_dm") or ""))
        if not reward:
            reward = "기다려주셔서 감사해요! 약속드린 자료 보내드립니다 🙌"

        retry = str(raw.get("gate_retry") or "").strip()
        if not retry:
            retry = "앗! 팔로우 확인이 안 됐어요 😣 프로필에서 팔로우 후 버튼을 다시 눌러주세요!"

        result["follow_gate"] = {
            "follow_gate_prompt": prompt,
            "follow_gate_button_label": button,
            "follow_gate_button_label_alt": button_alt,
            "reward_message_template": reward,
            "follow_gate_retry_message": retry,
        }
    else:
        result["follow_gate"] = None

    return result


# ── 메인 진입점 ────────────────────────────────────────────────


def suggest_campaign_fields(
    *,
    caption: str = "",
    image_url: str = "",
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    media_type: str = "",
    business_type: str = "",
    campaign_goal: str = "",
    tone: str = "",
    link_url: str = "",
    include_follow_gate: bool = True,
    reply_variant_count: int = 50,
    model_name: str = "gemma-4",
    max_tokens: int = 4000,
    temperature: float = 0.5,
) -> DmAssistResult:
    """게시물 이미지+캡션으로 AutoDM 캠페인 폼 초안을 생성한다.

    LLM(gemma-4) 단일 호출로 맥락이 필요한 항목(이름/키워드/DM 본문/게이트)만 만들고,
    공개 답글 N개는 코드 풀에서 즉시 변주 추출한다. 절대 raise 하지 않는다(파싱 실패 시 폴백).
    """
    n = max(1, min(int(reply_variant_count or 50), 50))
    image_block = _image_block(image_bytes, image_mime, image_url)
    vision_used = image_block is not None

    messages = _build_messages(
        caption=caption,
        image_block=image_block,
        business_type=business_type,
        campaign_goal=campaign_goal,
        tone=tone,
        link_url=link_url,
    )

    primary = call_llm_messages_with_usage(
        model=model_name, messages=messages, max_tokens=max_tokens, temperature=temperature
    )
    raw_content = primary.content

    try:
        parsed = extract_json(primary.content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM 응답이 dict 가 아님")
    except Exception as exc:  # noqa: BLE001
        logger.warning("suggest_campaign_fields: JSON 파싱 실패 → 전 필드 폴백. %s", exc)
        parsed = {}

    meta = _normalize_meta_fields(
        parsed, link_url=link_url, include_follow_gate=include_follow_gate
    )

    # 공개 답글: LLM 안 거치고 코드 풀에서 즉시 N개 변주 추출.
    replies = sample_replies(n)

    return DmAssistResult(
        name=meta["name"],
        keyword_filter=meta["keyword_filter"],
        keyword_mode=meta["keyword_mode"],
        public_reply_enabled=True,
        public_reply_templates=replies,
        opening_message_template=meta["opening_message_template"],
        follow_gate=meta["follow_gate"],
        link_button=meta["link_button"],
        model=primary.model,
        elapsed_seconds=round(primary.elapsed_seconds, 3),
        prompt_tokens=primary.prompt_tokens,
        completion_tokens=primary.completion_tokens,
        total_tokens=primary.total_tokens,
        cache_hit_tokens=primary.cache_hit_tokens,
        cache_miss_tokens=primary.cache_miss_tokens,
        estimated_cost_usd=primary.estimated_cost_usd,
        vision_used=vision_used,
        raw_content=raw_content,
        reply_count=len(replies),
    )
