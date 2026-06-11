"""사용자 업로드 이미지 라벨링 서비스 (비전 LLM).

새-페이지 생성에 앞서 업로드 이미지(최대 10장)를 비전 LLM 에 보내:
  - 각 이미지가 페이지에 배치할 **콘텐츠(content)** 인지, 분위기 참고용 **컨셉(concept)** 인지 판정
  - content 면 짧은 한국어 요약 + 추천 배치 위치(suggested_use) 산출
  - 흐림/저해상도/워터마크/텍스트범벅/NSFW 는 ``usable=false`` 로 거른다

이미지 전송 방식 (``post_classifier`` 와 동일한 멀티모달 패턴):
  - 스토리지 URL 이 공개(http) 면 그대로 ``image_url`` 패스스루 → 프로바이더가 직접 fetch (저렴)
  - 로컬(USE_R2=False, ``/media/...``) 이면 외부 LLM 이 못 받으므로 바이트를 읽어 base64 data-URI 로 전송

이 서비스는 DB(모델)를 직접 건드리지 않는다 — LLM 호출 + 정규화만 담당.
row 저장과 image_catalog 조립은 호출자(tasks.run_ai_job)가 한다.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from django.core.files.storage import default_storage

from .llm_client import call_llm_messages_with_usage
from .parsers import extract_json

logger = logging.getLogger(__name__)


# ── 시스템 프롬프트 ────────────────────────────────────────────

SYSTEM_PROMPT = """너는 링크인바이오/랜딩 페이지 디자이너의 어시스턴트다. \
사용자가 업로드한 이미지들을 보고, 각 이미지를 페이지 제작에 어떻게 쓸지 분류한다.

# 사용자 설명·의도 (가장 먼저, 가장 중요하게 반영)
사용자가 페이지를 설명하면서 이미지에 대한 의도를 함께 적었을 수 있다 (``[USER_BRIEF]`` 로 제공).
- 특정 이미지를 어떻게 쓰라고 했으면(예: "첫 번째 사진은 로고니까 프로필로", "음식 사진들은 갤러리로",
  "이 컷을 대표 배너로", "두 번째는 그냥 분위기 참고용") 그 의도를 role·usable·suggested_use·summary 에
  **최우선으로 반영**하라.
- 비전으로 본 이미지 내용과 사용자 설명을 함께 고려해, 설명이 어떤 이미지를 가리키는지 매칭하라
  (순서·내용 단서 모두 활용. 예: "딸기 케이크 사진" → 실제 딸기 케이크가 보이는 이미지).
- 사용자가 꼭 쓰라고 명시한 이미지는 가급적 ``usable=true`` 로 존중한다. 단 심각한 품질 문제
  (심한 흐림·아주 낮은 해상도·NSFW)는 솔직하게 그대로 표시(``usable=false`` 가능).
- summary 는 사용자 설명의 표현·맥락을 살려 작성하면 다음 생성 AI 가 의도대로 배치하기 쉽다.
- 설명에 이미지 언급이 없으면 아래 일반 기준으로 판단한다.

# 각 이미지에 대해 판정할 것
1. role — 이 이미지의 용도
   - "content": 페이지에 **실제로 배치**할 이미지. 제품 사진, 인물/프로필, 로고, 음식, 작품,
     배너로 쓸 만한 사진 등 그 자체로 콘텐츠 가치가 있는 것.
   - "concept": 페이지에 직접 넣기보다 **분위기·색감·무드 참고**용. 무드보드, 색 팔레트 캡처,
     레퍼런스 스크린샷, 손그림 스케치, 영감용 이미지 등.
2. usable — 페이지에 배치 가능 여부(boolean)
   - role 이 "content" 이고 화질/구도가 페이지에 써도 될 정도면 true.
   - 아래 중 하나라도 해당하면 false: 심하게 흐림, 너무 낮은 해상도, 큰 워터마크/로고가 박힘,
     텍스트로 가득 찬 스크린샷, 부적절(NSFW)·폭력 등. (이 경우 role 은 보통 "concept")
3. summary — usable=true 일 때만 의미. **한국어 한 줄(40자 이내)** 로 "무엇이 담긴 이미지인지"
   구체적으로. 예: "흰 접시에 담긴 딸기 케이크 클로즈업", "검은 배경의 남성 프로필 정면".
   usable=false 면 빈 문자열.
4. suggested_use — usable=true 일 때 추천 배치 위치. 다음 중 하나:
   "hero"(상단 대형 배너) | "avatar"(프로필 사진) | "logo" | "gallery"(갤러리/그리드) |
   "product"(제품 카드) | "background"(배경) | "thumbnail"(링크 썸네일) | "general".
5. quality — {"blurry": bool, "low_res": bool, "has_text": bool, "nsfw": bool}.

# 디자인 방향 분석 (mood_notes) — 컨셉(concept) 이미지가 있으면 특히 깊게
mood_notes: 사용자가 올린 **컨셉 이미지**(무드보드·레퍼런스 사이트 스크린샷·영감 이미지 등)와
전반적 톤을 보고 "사용자가 **어떤 성격의 사이트/페이지**를 원하는지"를 한국어 2~5문장으로 분석한다.
단순 색감 나열이 아니라 아래를 모두 담아라:
  - 색감·분위기 (팔레트, 명도/채도, 밝음/어두움)
  - **사이트 성격·업종 느낌** (예: 감성 카페, 미니멀 포트폴리오, 하이엔드 뷰티 브랜드, 발랄한 굿즈샵)
  - **레이아웃·구성 스타일** (여백 많은 미니멀, 풀블리드 이미지 중심, 카드 그리드, 잡지형 편집 등)
  - 브랜드 톤·타깃 무드 (고급스러움/친근함/빈티지/모던 등)
컨셉 이미지가 **레퍼런스 사이트 스크린샷**이면 그 사이트의 구조·구성·스타일을 적극적으로 읽어 반영하라.
사용자 설명([USER_BRIEF])에 원하는 컨셉/방향이 적혀 있으면 그 의도와 합쳐 정리한다.
**concept 이미지가 한 장이라도 있거나 [USER_BRIEF]에 방향이 있으면 mood_notes 를 반드시 채워라 (빈 문자열 금지).**
이미지가 다소 추상적이어도 읽히는 만큼 색감·톤·구성 인상을 적는다.
업로드 이미지가 전혀 없고 [USER_BRIEF]도 비어 있을 때만 빈 문자열.

# 색 팔레트 추출 (palette) — 실제로 본 색만 #hex 로
참고/컨셉 이미지(또는 콘텐츠 이미지의 지배적 톤)에서 실제로 보이는 색을 골라 페이지 디자인용 팔레트를 #hex 로 제시한다:
  - background: 페이지 전체 배경색  · surface: 카드/블록 배경색  · text: 본문 텍스트색(배경과 충분한 대비)
  - accent: 버튼·강조색  · brightness: 전체가 어두우면 "dark", 밝으면 "light"  · dominant_colors: 두드러진 색 최대 5개(#hex)
**눈으로 확인한 색만 적어라. 이미지를 못 봤거나 색을 못 읽으면 palette 를 빈 객체로 둬라 — 색을 지어내지 마라.**
palette 의 #hex 는 mood_notes 의 색 묘사와 모순되지 않게 맞춘다.

# 구조 힌트 (structure) — 레퍼런스 페이지 스크린샷일 때만, 실제로 본 것만
참고 이미지가 **레퍼런스 페이지/사이트 스크린샷**이면 그 구조를 읽어 structure 로 제시한다:
  - hero: "cover"(풀블리드 커버/배너 히어로) | "avatar"(원형 프로필 중심) | "none"
  - card_style: "filled_white"(흰 카드) | "translucent"(반투명) | "solid_color"(컬러 카드) | "minimal"(테두리만)
  - block_order: 위에서 아래로 관찰된 섹션 순서 배열. 다음 토큰만 사용:
    ["hero","social","cta","links","text","gallery","video","schedule","footer"]
**스크린샷에서 실제로 본 구조만 적어라. 레퍼런스 스크린샷이 아니거나 구조가 안 읽히면 structure 를 빈 객체로 둬라 — 지어내지 마라.**

# 텍스트 추출 (text_content) — 이미지에 보이는 글자를 그대로(verbatim)
이미지에 글자가 있으면(특히 레퍼런스/홍보 페이지 스크린샷) 읽어서 text_content 로 추출한다.
다음 생성 AI 가 이 글을 페이지의 실제 콘텐츠로 쓰고, 사용자에게도 정보로 제공된다:
  - title: 가장 크게 보이는 헤드라인/브랜드명
  - tagline: 보조 슬로건/소개 문구
  - buttons: 버튼·CTA 에 적힌 라벨 (배열)
  - items: 그 외 카드/항목/섹션 제목/제품명 등 읽히는 짧은 텍스트 (배열, 위→아래 순서)
**이미지에 실제로 보이는 글자만 그대로 적어라(번역·요약·창작 금지). 없는 문구를 지어내지 마라.
글자가 안 보이면 text_content 를 빈 객체로 둬라.**

# 출력 규칙
- 입력으로 준 모든 이미지 id 를 빠짐없이 포함한다.
- JSON 객체만 출력. 코드펜스(```), 주석, 설명 텍스트 금지. 한국어 사용."""


def _output_schema_hint() -> str:
    return (
        "{\n"
        '  "images": [\n'
        '    {"id": "<입력 id 그대로>", "role": "content"|"concept", "usable": true|false,\n'
        '     "summary": "한 줄 요약(한국어, usable=false면 빈 문자열)",\n'
        '     "suggested_use": "hero"|"avatar"|"logo"|"gallery"|"product"|"background"|"thumbnail"|"general",\n'
        '     "quality": {"blurry": false, "low_res": false, "has_text": false, "nsfw": false}}\n'
        "  ],\n"
        '  "mood_notes": "디자인 방향 분석 — 색감 + 사이트 성격/업종 + 레이아웃·구성 스타일 + 브랜드 톤 (없으면 빈 문자열)",\n'
        '  "palette": {"brightness": "dark", "background": "#0b132b", "surface": "#111827",\n'
        '              "text": "#f5f5f5", "accent": "#3b82f6", "dominant_colors": ["#0b132b", "#3b82f6"]},\n'
        '  "structure": {"hero": "cover", "card_style": "filled_white",\n'
        '                "block_order": ["hero", "social", "cta", "links", "gallery"]},\n'
        '  "text_content": {"title": "AI 구독클럽", "tagline": "AI 가전 구독의 완성",\n'
        '                   "buttons": ["제품 모두 보기"], "items": ["전제품 A/S 패스트트랙", "Galaxy S26 Ultra"]}\n'
        "}\n"
    )


# ── 이미지 → LLM content 블록 ──────────────────────────────────


def _image_url_block(img: dict) -> dict | None:
    """이미지 한 장을 OpenAI ``image_url`` content 블록으로 변환.

    img: {"id", "url", "storage_name", "mime"}

    **base64 우선** — 자체호스팅 vLLM(gemma-4)은 image_url 의 원격 URL 을 호스트에 따라
    fetch 못 하기도 한다(R2/Pixabay 는 OK, wikimedia 등은 NO_IMAGE). base64 data-URI 는
    호스트 의존이 없고 서버 fetch 왕복도 없어 더 빠르고 견고하다(이미지 토큰은 크기 무관 고정).
    스토리지 읽기에 실패할 때만 공개 http(s) URL 패스스루로 폴백한다.
    실패하면 None (해당 이미지는 비전 입력에서 빠지지만 텍스트로는 남는다).
    """
    storage_name = img.get("storage_name") or ""
    if storage_name:
        try:
            with default_storage.open(storage_name, "rb") as fh:
                raw = fh.read()
            mime = img.get("mime") or "image/jpeg"
            b64 = base64.b64encode(raw).decode("ascii")
            return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "라벨링: 스토리지 읽기 실패 (%s): %s → URL 패스스루 폴백", storage_name, exc
            )

    url = (img.get("url") or "").strip()
    if url.startswith("http"):
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def _build_messages(images: list[dict], concept: str) -> tuple[list[dict], int]:
    """멀티모달 messages 배열 구성. Returns (messages, vision_count)."""
    brief = (concept or "").strip()
    header = (
        f"[USER_BRIEF — 사용자가 페이지/이미지에 대해 적은 설명·의도]\n"
        f"{brief or '(설명 없음 — 일반 기준으로 판단)'}\n\n"
        f"위 설명에 이미지 사용 의도가 있으면 분류·요약에 최우선 반영하라.\n\n"
        f"[IMAGES] — 아래 {len(images)}장의 이미지를 분류해줘. "
        f"각 이미지는 [IMAGE id] 텍스트 블록과 바로 뒤따르는 이미지로 구성된다."
    )
    user_content: list[dict[str, Any]] = [{"type": "text", "text": header}]
    vision_count = 0
    for img in images:
        user_content.append({"type": "text", "text": f"[IMAGE {img['id']}]"})
        block = _image_url_block(img)
        if block is not None:
            user_content.append(block)
            vision_count += 1

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
    ], vision_count


# ── 결과 정규화 ────────────────────────────────────────────────

_VALID_USES = frozenset(
    {
        "hero",
        "avatar",
        "logo",
        "gallery",
        "product",
        "background",
        "thumbnail",
        "general",
    }
)


@dataclass
class ImageLabel:
    id: str
    role: str = "concept"  # "content" | "concept"
    usable: bool = False
    summary: str = ""
    suggested_use: str = "general"
    quality: dict = field(default_factory=dict)


@dataclass
class LabelResult:
    labels: dict[str, ImageLabel] = field(default_factory=dict)  # by image id
    mood_notes: str = ""
    # {background, surface, text, accent, brightness, dominant_colors} — 유효 #hex 만. 비전 실패 시 {}.
    palette: dict = field(default_factory=dict)
    # {hero, card_style, block_order} — 레퍼런스 스크린샷 구조. 화이트리스트 토큰만. 비전 실패 시 {}.
    structure: dict = field(default_factory=dict)
    # {title, tagline, buttons[], items[]} — 이미지에서 읽은 실제 글자(OCR). 비전 실패/글자 없으면 {}.
    text_content: dict = field(default_factory=dict)
    model: str = ""
    elapsed_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    vision_count: int = 0
    raw_content: str = ""


def _clean_summary(s: str, max_len: int = 60) -> str:
    s = (s or "").strip().replace("\n", " ")
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def _normalize(parsed: dict, image_ids: list[str]) -> tuple[dict[str, ImageLabel], str]:
    """LLM 응답을 입력 id 기준으로 방어적 정규화.

    - 입력에 없는 id 는 버린다.
    - 응답에서 누락된 이미지는 ``{role: concept, usable: false}`` 로 기본 처리 (절대 임의 usable X).
    - role/usable/suggested_use 이상값 보정.
    """
    id_set = set(image_ids)
    by_id: dict[str, ImageLabel] = {}

    raw_images = parsed.get("images") if isinstance(parsed, dict) else None
    if isinstance(raw_images, list):
        for it in raw_images:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or "").strip()
            if iid not in id_set or iid in by_id:
                continue
            role = (it.get("role") or "").strip().lower()
            if role not in ("content", "concept"):
                role = "concept"
            usable = bool(it.get("usable")) and role == "content"
            use = (it.get("suggested_use") or "general").strip().lower()
            if use not in _VALID_USES:
                use = "general"
            quality = it.get("quality") if isinstance(it.get("quality"), dict) else {}
            by_id[iid] = ImageLabel(
                id=iid,
                role=role,
                usable=usable,
                summary=_clean_summary(it.get("summary") or "") if usable else "",
                suggested_use=use if usable else "general",
                quality=quality,
            )

    # 누락 이미지 기본값
    for iid in image_ids:
        if iid not in by_id:
            by_id[iid] = ImageLabel(id=iid, role="concept", usable=False)

    mood = ""
    if isinstance(parsed, dict):
        mood = (parsed.get("mood_notes") or "").strip()
    return by_id, mood


_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_PALETTE_KEYS = ("background", "surface", "text", "accent")


def _norm_hex(v) -> str:
    s = str(v or "").strip()
    return s if _HEX_RE.match(s) else ""


def _extract_palette(parsed: dict) -> dict:
    """LLM 응답의 palette 를 방어적으로 정규화 — 유효한 #hex 만 남긴다 (환각 색 차단)."""
    raw = parsed.get("palette") if isinstance(parsed, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in _PALETTE_KEYS:
        hx = _norm_hex(raw.get(k))
        if hx:
            out[k] = hx
    brightness = (raw.get("brightness") or "").strip().lower()
    if brightness in ("dark", "light"):
        out["brightness"] = brightness
    if isinstance(raw.get("dominant_colors"), list):
        colors = [c for c in (_norm_hex(x) for x in raw["dominant_colors"]) if c][:5]
        if colors:
            out["dominant_colors"] = colors
    return out


_VALID_HERO = frozenset({"cover", "avatar", "none"})
_VALID_CARD_STYLE = frozenset({"filled_white", "translucent", "solid_color", "minimal"})
_VALID_SECTIONS = frozenset(
    {"hero", "social", "cta", "links", "text", "gallery", "video", "schedule", "footer"}
)


def _extract_structure(parsed: dict) -> dict:
    """LLM 응답의 structure 를 방어적으로 정규화 — 화이트리스트 토큰만 통과 (환각 차단)."""
    raw = parsed.get("structure") if isinstance(parsed, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    hero = (raw.get("hero") or "").strip().lower()
    if hero in _VALID_HERO:
        out["hero"] = hero
    card = (raw.get("card_style") or "").strip().lower()
    if card in _VALID_CARD_STYLE:
        out["card_style"] = card
    if isinstance(raw.get("block_order"), list):
        order = [
            s for s in (str(x).strip().lower() for x in raw["block_order"]) if s in _VALID_SECTIONS
        ][:10]
        if order:
            out["block_order"] = order
    return out


def _extract_text_content(parsed: dict) -> dict:
    """LLM 응답의 text_content(이미지 OCR)를 방어적으로 정규화 — 문자열만, 길이/개수 제한."""
    raw = parsed.get("text_content") if isinstance(parsed, dict) else None
    if not isinstance(raw, dict):
        return {}

    def _one(v: object) -> str:
        return str(v or "").strip().replace("\n", " ")[:200]

    def _many(v: object) -> list:
        if not isinstance(v, list):
            return []
        return [s for s in (_one(x) for x in v) if s][:30]

    out: dict = {}
    title = _one(raw.get("title"))
    if title:
        out["title"] = title
    tagline = _one(raw.get("tagline"))
    if tagline:
        out["tagline"] = tagline
    buttons = _many(raw.get("buttons"))
    if buttons:
        out["buttons"] = buttons
    items = _many(raw.get("items"))
    if items:
        out["items"] = items
    return out


# ── 메인 진입점 ────────────────────────────────────────────────


def label_images(
    *,
    images: list[dict],
    concept: str = "",
    model_name: str = "deepseek",
    max_tokens: int = 2000,
    temperature: float = 0.1,
) -> LabelResult:
    """업로드 이미지 배치를 비전 LLM 으로 라벨링.

    Args:
        images: ``[{"id", "url", "storage_name", "mime"}, ...]`` (업로드 순서)
        concept: 사용자가 입력한 페이지 컨셉 (판단 힌트)

    Raises:
        Exception: LLM 호출 자체가 실패하면 예외 전파 — 호출자(tasks)가 비치명적으로 처리.
    """
    if not images:
        return LabelResult(model=model_name)

    image_ids = [str(img["id"]) for img in images]
    messages, vision_count = _build_messages(images, concept)

    result = call_llm_messages_with_usage(
        model=model_name,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    try:
        parsed = extract_json(result.content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM 응답이 dict가 아님")
    except Exception as exc:  # noqa: BLE001
        logger.warning("label_images: JSON 파싱 실패 → 전부 concept 폴백. %s", exc)
        parsed = {"images": [], "mood_notes": ""}

    labels, mood_notes = _normalize(parsed, image_ids)
    palette = _extract_palette(parsed)
    structure = _extract_structure(parsed)
    text_content = _extract_text_content(parsed)

    return LabelResult(
        labels=labels,
        mood_notes=mood_notes,
        palette=palette,
        structure=structure,
        text_content=text_content,
        model=result.model,
        elapsed_seconds=result.elapsed_seconds,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        vision_count=vision_count,
        raw_content=result.content,
    )
