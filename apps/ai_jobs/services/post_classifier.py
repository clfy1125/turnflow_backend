"""인스타툰/SNS 게시물 → 카테고리/제목 분류 서비스 (비전 통합 버전).

설계:
  - 작가별 페이지를 아카이브로 정리. 한 게시물 = 한 카테고리 (중복 금지).
  - 누적 카테고리를 다음 배치에 ``existing_categories`` 로 넘겨 재사용 유도.
  - 게시물 ``thumbnail_url`` 이 있으면 멀티모달(LLM 비전)로 이미지를 직접 보고 판단.
    - 이미지 안에 **명확한 만화 제목** (한국어, 화수 (n) 포함 가능) 이 있으면 그대로 사용.
    - 이미지 텍스트가 대사/의성어/낙서 등 제목이 아니면 캡션에서 만듦.
    - 둘 다 부족하면 카테고리 기반 폴백 ("{카테고리} #{번호}").
  - 속도 우선: 1배치 ≤9개, prompt 압축.

LLM 출력 스키마(엄격):
{
  "assignments": [
    {"post_id": "...", "category_label": "...", "is_new_category": true|false,
     "suggested_title": "...", "title_source": "image"|"caption"|"fallback"}
  ],
  "new_categories": [{"label": "...", "description": "..."}]
}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .llm_client import (
    call_llm_messages_with_usage,
    call_llm_with_usage,
)
from .parsers import extract_json

logger = logging.getLogger(__name__)


# ── 시스템 프롬프트 ────────────────────────────────────────────

SYSTEM_PROMPT = """너는 한국어 인스타툰/SNS 게시물 큐레이터다. 작가의 게시물을 보고 \
독자가 '네이버 웹툰 목록'처럼 둘러볼 수 있도록 카테고리로 묶고, 각 게시물에 짧은 제목을 \
붙여주는 역할을 한다.

# 카테고리 규칙
1. 한 게시물은 정확히 **하나의** 카테고리에만 배정. 절대 중복 금지.
2. 기존 카테고리(EXISTING_CATEGORIES)가 의미상 들어맞으면 **그걸 우선 재사용**.
   라벨이 완전히 같지 않아도 의미가 같으면 재사용한다.
3. 들어맞지 않을 때만 신규 카테고리를 만든다. 가능한 적게 (이상적으로 3~6개, MAX_CATEGORIES 이하).
4. 같은 작품 제목을 공유하는 시리즈물(예: 화수만 다른 (1)/(2)/(3))은 **한 시리즈 카테고리**로 묶는다.
   - 단, 시리즈가 1편뿐이면 의미 비슷한 다른 카테고리에 흡수.
5. 카테고리 라벨: 한국어, 12자 이내, 이모지 1개로 시작 가능.
   예: "💌 사연툰", "🍼 육아툰", "💔 예랑이 사연", "🗓️ 일상", "📌 안내".

# 제목 규칙 (가장 중요 — 아래 순서대로 적용)
각 게시물의 ``suggested_title`` 은 다음 우선순위로 결정:

(0) **캡션에 누가 봐도 화수가 명시되어 있는 경우** (최우선)
    캡션이 "< 작품명 N화 >", "[작품명] N화", "작품명 N화 - ...", "(N)", "#N",
    "Ep.N", "에피소드 N" 같이 시리즈+화수가 명확히 적혀있으면 → 그 표기를 그대로 제목으로 사용.
    예: 캡션 "< 왜 그렇게 되었는가 46화 > #드라마툰..." → suggested_title = "왜 그렇게 되었는가 46화".
    이때는 이미지에 다른 텍스트가 보여도 캡션의 화수 표기가 우선이다.
    ``title_source`` = "caption"

(a) **이미지 안에 명확한 만화 제목 텍스트**가 있는 경우 (캡션에 화수 표기가 없을 때)
    - 화수 표기 (1),(2),N화 등이 있으면 그것도 포함. 예: "신혼집 안방에서 나온 예랑이 (3)"
    - "명확한 제목"이란: 한국어 굵은 큰 글자, 상단/중앙에 배치된 문구, 만화 제목 톤.
    - 인식한 텍스트가 흐릿하거나 추측이면 사용하지 말고 (b) 로 간다.
    - ``title_source`` = "image"

(b) 이미지에 제목이 없거나, 캐릭터 대사/의성어/한두 글자/낙서/광고 문구뿐이면
    캡션 첫 문장에서 핵심을 12자 내외로 뽑는다.
    - 해시태그·이모지·줄바꿈·"좋아요/팔로우" 같은 광고문구 제거.
    - ``title_source`` = "caption"

(c) (0)/(a)/(b) 모두 의미가 약하면 카테고리 + 번호로 폴백. 예: "💌 사연툰 #3".
    - ``title_source`` = "fallback"

# 출력
JSON 객체만 출력. 코드펜스(```), 주석, 설명 텍스트 금지. 한국어 사용.
assignments 의 post_id 는 입력 POSTS 의 id 와 정확히 일치해야 한다."""


# ── 입력 정규화 ────────────────────────────────────────────────

def _post_text_brief(post: dict, idx: int) -> str:
    """LLM 입력용 한 게시물 텍스트 블록."""
    pid = post.get("id") or f"post-{idx}"
    caption = (post.get("caption") or "").strip().replace("\n", " ")
    if len(caption) > 220:
        caption = caption[:220] + "…"
    tags = post.get("hashtags") or []
    if isinstance(tags, list):
        tags_s = " ".join(f"#{t}" for t in tags[:8] if t)
    else:
        tags_s = ""
    likes = post.get("likes") or 0
    comments = post.get("comments") or 0
    ptype = post.get("type") or ""

    parts = [f"[POST {pid}]"]
    if ptype:
        parts.append(f"타입={ptype}")
    parts.append(f"좋아요={likes} 댓글={comments}")
    if caption:
        parts.append(f'캡션="{caption}"')
    if tags_s:
        parts.append(f"태그: {tags_s}")
    return " · ".join(parts)


def _artist_block(artist_context: dict | None) -> str:
    if not artist_context:
        return "(작가 정보 없음)"
    lines = []
    for key, label in (
        ("name", "이름"),
        ("category", "주 카테고리"),
        ("genre", "장르"),
        ("bio", "소개"),
    ):
        v = (artist_context.get(key) or "").strip()
        if v:
            if len(v) > 160:
                v = v[:160] + "…"
            lines.append(f"- {label}: {v}")
    return "\n".join(lines) or "(작가 정보 없음)"


def _existing_block(existing_categories: list[dict] | None) -> str:
    if not existing_categories:
        return "(없음 — 이번에 새로 만든다)"
    lines = []
    for c in existing_categories:
        label = (c.get("label") or "").strip()
        desc = (c.get("description") or "").strip()
        if not label:
            continue
        if desc:
            lines.append(f"- {label} :: {desc}")
        else:
            lines.append(f"- {label}")
    return "\n".join(lines) or "(없음 — 이번에 새로 만든다)"


def _output_schema_hint() -> str:
    return (
        '{\n'
        '  "assignments": [\n'
        '    {"post_id": "...", "category_label": "...", "is_new_category": true|false,\n'
        '     "suggested_title": "...", "title_source": "image"|"caption"|"fallback"}\n'
        '  ],\n'
        '  "new_categories": [\n'
        '    {"label": "...", "description": "한 줄로 카테고리 설명"}\n'
        '  ]\n'
        '}\n'
    )


# ── 메시지 빌더 ────────────────────────────────────────────────

def _build_messages(
    *,
    posts: list[dict],
    existing_categories: list[dict],
    artist_context: dict,
    max_categories: int,
    use_vision: bool,
) -> tuple[list[dict], bool]:
    """OpenAI-compatible messages 배열을 만든다.

    Returns:
        (messages, vision_used) — vision_used 는 실제로 image_url 블록을 1개 이상 넣었는지.
    """
    header = (
        f"[ARTIST_CONTEXT]\n{_artist_block(artist_context)}\n\n"
        f"[EXISTING_CATEGORIES]\n{_existing_block(existing_categories)}\n\n"
        f"[CONSTRAINTS]\nMAX_CATEGORIES = {max_categories}\n"
        f"기존 카테고리는 가능한 한 재사용. 신규는 의미가 정말 다를 때만 추가.\n\n"
        f"[POSTS] — 아래 N개 게시물을 한 번에 분류해줘. "
        f"각 게시물은 [POST id] 텍스트 블록과 (있다면) 바로 뒤따라오는 썸네일 이미지로 구성."
    )

    user_content: list[dict[str, Any]] = [{"type": "text", "text": header}]
    vision_used = False
    for i, p in enumerate(posts):
        user_content.append({"type": "text", "text": _post_text_brief(p, i)})
        thumb = (p.get("thumbnail_url") or "").strip()
        if use_vision and thumb.startswith("http"):
            user_content.append({
                "type": "image_url",
                "image_url": {"url": thumb},
            })
            vision_used = True

    user_content.append({
        "type": "text",
        "text": (
            "\n[OUTPUT_SCHEMA]\n" + _output_schema_hint()
            + "\n위 스키마의 JSON 객체만 출력. 다른 텍스트/코드펜스/주석 금지."
        ),
    })

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    return messages, vision_used


# ── 결과 정규화 ────────────────────────────────────────────────

@dataclass
class ClassifyResult:
    assignments: list[dict] = field(default_factory=list)
    new_categories: list[dict] = field(default_factory=list)
    model: str = ""
    elapsed_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    estimated_cost_usd: float = 0.0
    raw_content: str = ""
    vision_used: bool = False


def _clean_title(t: str, max_len: int = 40) -> str:
    t = (t or "").strip()
    if not t:
        return ""
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t


def _normalize_assignments(
    raw: dict, posts: list[dict], existing_labels: set[str], max_categories: int
) -> tuple[list[dict], list[dict]]:
    assigns_raw = raw.get("assignments") or []
    new_cats_raw = raw.get("new_categories") or []

    posts_by_id = {(p.get("id") or f"post-{i}"): p for i, p in enumerate(posts)}
    assigned: dict[str, dict] = {}

    for a in assigns_raw:
        if not isinstance(a, dict):
            continue
        pid = a.get("post_id")
        if pid not in posts_by_id or pid in assigned:
            continue
        label = (a.get("category_label") or "").strip()
        if not label:
            continue
        title = _clean_title(a.get("suggested_title") or "", max_len=40) or "(제목 없음)"
        src = (a.get("title_source") or "").strip().lower()
        if src not in ("image", "caption", "fallback"):
            src = "caption"
        assigned[pid] = {
            "post_id": pid,
            "category_label": label,
            "is_new_category": bool(a.get("is_new_category"))
            and label not in existing_labels,
            "suggested_title": title,
            "title_source": src,
        }

    # 누락 게시물: "🗂️ 기타" 폴백
    for pid, p in posts_by_id.items():
        if pid not in assigned:
            fallback = _clean_title((p.get("caption") or "")[:20]) or "기타 게시물"
            assigned[pid] = {
                "post_id": pid,
                "category_label": "🗂️ 기타",
                "is_new_category": "🗂️ 기타" not in existing_labels,
                "suggested_title": fallback,
                "title_source": "fallback",
            }

    # 카테고리 상한 초과 시 작은 카테고리를 가장 큰 카테고리로 흡수
    label_count: dict[str, int] = {}
    for a in assigned.values():
        label_count[a["category_label"]] = label_count.get(a["category_label"], 0) + 1
    if len(label_count) > max_categories:
        sorted_labels = sorted(label_count.items(), key=lambda x: x[1], reverse=True)
        keep = {lbl for lbl, _ in sorted_labels[:max_categories]}
        biggest = sorted_labels[0][0]
        for a in assigned.values():
            if a["category_label"] not in keep:
                a["category_label"] = biggest
                a["is_new_category"] = False

    used_new = {a["category_label"] for a in assigned.values() if a["is_new_category"]}
    new_cats_clean: list[dict] = []
    seen = set()
    for c in new_cats_raw:
        if not isinstance(c, dict):
            continue
        label = (c.get("label") or "").strip()
        if not label or label in seen or label not in used_new:
            continue
        seen.add(label)
        new_cats_clean.append({
            "label": label, "description": (c.get("description") or "").strip(),
        })
    for label in used_new - seen:
        new_cats_clean.append({"label": label, "description": ""})

    return list(assigned.values()), new_cats_clean


# ── 메인 진입점 ────────────────────────────────────────────────

def classify_posts(
    *,
    posts: list[dict],
    existing_categories: list[dict] | None = None,
    artist_context: dict | None = None,
    max_categories: int = 6,
    model_name: str = "gemma-4",
    max_tokens: int = 2500,
    temperature: float = 0.1,
    use_vision: bool = True,
) -> ClassifyResult:
    """게시물 배치를 LLM 으로 분류.

    Args:
        posts: 각 항목 키: id, caption, hashtags, type, likes, comments, timestamp, thumbnail_url
        use_vision: True 면 thumbnail_url 이 있는 게시물에 한해 image_url 블록을 함께 보낸다.
    """
    if not posts:
        return ClassifyResult(model=model_name)

    messages, vision_used = _build_messages(
        posts=posts,
        existing_categories=existing_categories or [],
        artist_context=artist_context or {},
        max_categories=max_categories,
        use_vision=use_vision,
    )

    # vision 미사용이면 텍스트 단일 호출이 더 가볍지만, 코드 단순성 위해 동일 경로.
    result = call_llm_messages_with_usage(
        model=model_name,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    existing_labels = {(c.get("label") or "").strip()
                       for c in (existing_categories or []) if c.get("label")}

    try:
        parsed = extract_json(result.content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM 응답이 dict가 아님")
    except Exception as exc:  # noqa: BLE001
        logger.warning("classify_posts: JSON 파싱 실패 → 폴백 모드. %s", exc)
        parsed = {"assignments": [], "new_categories": []}

    assignments, new_cats = _normalize_assignments(
        parsed, posts, existing_labels, max_categories
    )

    return ClassifyResult(
        assignments=assignments,
        new_categories=new_cats,
        model=result.model,
        elapsed_seconds=result.elapsed_seconds,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        cache_hit_tokens=result.cache_hit_tokens,
        cache_miss_tokens=result.cache_miss_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        raw_content=result.content,
        vision_used=vision_used,
    )


# 후방 호환: 텍스트 전용 호출(이전 동작)이 필요한 경우.
# (현재는 classify_posts(use_vision=False) 로 동일하게 처리되므로 별도 함수는 두지 않음.)
_ = call_llm_with_usage  # 사용처 보존: 향후 텍스트-only 폴백 분기에서 사용 가능