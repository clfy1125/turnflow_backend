"""LLM 의 스타일 패치 응답 + 기존 페이지 상태 → ``page_applier`` 가 적용 가능한 full result_json.

두 모드 (``full_restyle`` / ``style_only``) 각각 머지 함수가 다르다. 공통적으로:
  - 콘텐츠 필드는 **기존 페이지 값을 그대로 유지** (AI 가 보낸 콘텐츠 키는 silently drop).
  - 스타일/세팅 필드만 화이트리스트 통과시켜 패치.
  - 결과 dict 는 기존 ``page_applier.apply_result_json_to_page`` 가 이해하는 스키마.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 화이트리스트 — Block.data 안에서 AI 가 변경할 수 있는 스타일/세팅 키.
# 키가 여기 없으면 silently drop (콘텐츠 보호).
# ``"*"`` 은 모든 _type 공통. ``_subtype_for_whitelist`` 로 매칭.
# 출처: ai_assets/rules/block_rules.md
# ─────────────────────────────────────────────────────────────

_STYLE_WHITELIST: dict[str, set[str]] = {
    "*": {
        "custom_bg_color",
        "custom_border_color",
        "custom_text_color",
        "custom_button_color",
    },
    "profile": {"profile_layout", "font_size"},
    "single_link": {"layout", "text_align"},
    "group_link": {"group_layout", "display_mode", "text_align"},
    "social": {"custom_icon_color"},
    "video": {"video_layout", "autoplay"},
    "text": {"text_layout", "text_align", "text_size", "custom_sub_text_color"},
    "gallery": {"gallery_layout", "auto_slide", "keep_ratio"},
    "spacer": {"divider_style", "divider_width", "divider_color", "spacing"},
    "notice": {"notice_layout"},
    "customer": {"custom_input_bg_color"},
    "folder": {
        "folder_icon",
        "folder_icon_color",
        "is_collapsed_default",
        "folder_display_mode",
        "text_align",
        "folder_toggle_bg",
        "folder_popup_bg",
        "folder_popup_text",
        "folder_popup_accent",
    },
    "schedule": {"schedule_layout"},
    # map / search / inquiry / contact / music — 스타일/세팅 키 없음 (공통만 적용).
}

# full_restyle 모드에서 AI 가 자유롭게 새로 작성할 수 있는 텍스트 콘텐츠 키.
# (URL/이미지/비디오/연락처는 placeholder freeze 로 보호되므로 자동으로 보존됨.)
# 기존 블록 패치 시에도, 신규 블록(``_new``) 생성 시에도 동일하게 사용된다.
_TEXT_CONTENT_KEYS: dict[str, set[str]] = {
    "profile": {"headline", "subline"},
    "single_link": {"label", "description"},
    "group_link": {"label", "description"},
    "social": set(),
    "video": set(),
    "text": {"headline", "content"},
    "gallery": set(),
    "spacer": set(),
    "notice": {"title", "content"},
    "map": {"map_name"},
    "inquiry": {"inquiry_title", "button_text"},
    "customer": {"customer_headline", "customer_description", "button_text"},
    "search": {"search_placeholder"},
    "folder": {"label"},
    "schedule": {"label"},
}


def _block_subtype_for_whitelist(b: dict) -> str:
    """Block.type 이 single_link 면 data._type, 그 외엔 type 자체.

    화이트리스트 키 매칭용. profile/contact 는 그대로, single_link 는 data._type 분기.
    """
    btype = b.get("type") or b.get("_type") or ""
    if btype == "single_link":
        sub = (b.get("data") or {}).get("_type")
        if isinstance(sub, str) and sub:
            return sub
        return "single_link"
    return btype


def _allowed_style_keys(subtype: str) -> set[str]:
    return _STYLE_WHITELIST["*"] | _STYLE_WHITELIST.get(subtype, set())


def _allowed_full_restyle_keys(subtype: str) -> set[str]:
    """full_restyle 모드: 스타일 + 텍스트 콘텐츠 키 모두 허용.

    URL/이미지/비디오/연락처는 placeholder freeze 로 보호되어 있으므로 AI 응답에서
    바뀌어도 thaw 단계에서 원본으로 복원되거나(echo) drop 된다. 따라서 텍스트는
    자유롭게 새로 작성하게 두어 극적 리뉴얼을 가능하게 한다.
    """
    return _allowed_style_keys(subtype) | _TEXT_CONTENT_KEYS.get(subtype, set())


def _filter_style_patch(patch: dict, subtype: str) -> dict:
    """``patch`` 에서 ``subtype`` 의 스타일 화이트리스트 키만 통과시킨다.

    style_only 모드에서 사용. 화이트리스트 외 키는 silently drop.
    """
    if not isinstance(patch, dict):
        return {}
    allowed = _allowed_style_keys(subtype)
    out: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in patch.items():
        if k in allowed:
            out[k] = v
        else:
            dropped.append(k)
    if dropped:
        logger.debug("style_patcher: subtype=%s 에서 drop 된 키 %s", subtype, dropped)
    return out


def _filter_full_restyle_patch(patch: dict, subtype: str) -> dict:
    """full_restyle 모드: 스타일 + 텍스트 콘텐츠 키 통과. 그 외는 drop."""
    if not isinstance(patch, dict):
        return {}
    allowed = _allowed_full_restyle_keys(subtype)
    out: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in patch.items():
        if k in allowed:
            out[k] = v
        else:
            dropped.append(k)
    if dropped:
        logger.debug("style_patcher(full_restyle): subtype=%s drop %s", subtype, dropped)
    return out


# ─────────────────────────────────────────────────────────────
# 페이지 레벨 머지
# ─────────────────────────────────────────────────────────────

_PAGE_META_FIELDS = ("title", "is_public", "data", "custom_css")


def _extract_response_page(llm_response: dict | None) -> dict:
    """LLM 응답에서 페이지 메타 추출.

    AI 가 두 가지 형식으로 응답할 수 있다:
      1. ``{"page": {title, is_public, data, custom_css}, "blocks": [...]}`` (지시한 형식)
      2. ``{"title": ..., "is_public": ..., "data": ..., "custom_css": ..., "blocks": [...]}``
         (평평한 형식 — 모델이 자주 이렇게 응답)

    어느 쪽이든 페이지 메타 dict 를 반환한다.
    """
    if not isinstance(llm_response, dict):
        return {}
    page = llm_response.get("page")
    if isinstance(page, dict) and page:
        return page
    # 평평한 형식 — 최상위에서 직접 추출
    flat = {k: llm_response[k] for k in _PAGE_META_FIELDS if k in llm_response}
    return flat


def _merge_page_meta(
    existing: dict,
    response_page: dict | None,
    *,
    preserve_content: bool = False,
) -> dict:
    """페이지 메타 머지. response_page 의 키는 전체 교체, 없으면 기존 유지.

    안전망:
      1. AI 가 ``custom_css`` 를 빈 문자열로 보내면 그 값으로 덮어쓰지 않고 기존 값을 보존.
      2. 머지 후 ``custom_css`` 가 여전히 비어있고 design_settings 가 있으면,
         backgroundColor 기준 최소 fallback css 를 자동 생성 — 페이지가 시각적으로
         의도치 않게 평이해지는 것 방지.
    """
    merged = dict(existing or {})
    if isinstance(response_page, dict):
        for field in _PAGE_META_FIELDS:
            if field not in response_page:
                continue
            val = response_page[field]
            # AI 가 빈 css 를 보낸 경우 무시 (기존 보존).
            if field == "custom_css" and (val is None or (isinstance(val, str) and not val.strip())):
                continue
            merged[field] = val

    # ── Fallback: design_settings 가 채워졌는데 custom_css 가 비어있으면 최소 css 자동 생성 ──
    current_css = merged.get("custom_css") or ""
    if not current_css.strip():
        ds = (merged.get("data") or {}).get("design_settings") or {}
        if isinstance(ds, dict):
            bg = ds.get("backgroundColor")
            if isinstance(bg, str) and bg.strip():
                # 단순 body 배경 + 기본 typography 한 줄.
                merged["custom_css"] = (
                    f"body{{background:{bg};font-feature-settings:'ss01';}}"
                )
                logger.info(
                    "_merge_page_meta: page.custom_css fallback 자동 생성 (bg=%s)", bg,
                )
    return merged


# ─────────────────────────────────────────────────────────────
# full_restyle 머지
# ─────────────────────────────────────────────────────────────

# preserve_content 모드에서 "기존 블록에 없던 경우 임의 추가 차단" 할 시각 키 목록.
# (기존에 이미 값이 있는 블록은 색 변경 OK — 추가가 아닌 변경이므로.)
_VISUAL_KEYS_GUARDED = frozenset({"custom_border_color"})


# 블록 무리(연속된 같은 subtype) 내에서 강제로 통일할 시각 키.
# 같은 기능의 연속 블록이 각기 다른 톤으로 나오는 것은 나쁜 디자인 습관 — 첫 블록 기준으로 통일.
_GROUP_UNIFORM_DATA_KEYS = frozenset({
    "custom_bg_color",
    "custom_border_color",
    "custom_text_color",
    "custom_button_color",
    "layout",
    "text_align",
})


def _full_subtype(b: dict) -> str:
    """그룹화용 subtype. ``profile``/``contact`` 는 그대로, ``single_link`` 는 ``data._type`` 까지 보고 분기."""
    btype = b.get("type") or ""
    if btype == "single_link":
        sub = (b.get("data") or {}).get("_type") or ""
        return f"single_link/{sub}" if sub else "single_link"
    return btype


def _enforce_group_uniformity(blocks: list[dict]) -> list[dict]:
    """연속된 같은 subtype 블록 무리(2개 이상) 의 시각 스타일을 **첫 블록 기준**으로 통일.

    같은 기능을 가진 연속 블록은 동일한 디자인이어야 한다는 규칙. AI 가 안 지켜도
    백엔드가 강제. 그룹 안의 텍스트 콘텐츠(label, headline, content 등) 는 그대로 둔다.

    통일 대상:
      - data 내부의 색·layout·정렬 키 (``_GROUP_UNIFORM_DATA_KEYS``)
      - block.custom_css (블록 레벨 CSS)

    예외 — **쇼케이스 남발 방지**:
      - 그룹의 첫 블록이 ``layout: "large"`` 여도 나머지 블록은 ``"small"`` 로 강등한다.
        large 는 강조용이라 그룹 안에서 1개만 의미가 있다.
    """
    if not blocks:
        return blocks

    n = len(blocks)
    i = 0
    while i < n:
        sub = _full_subtype(blocks[i])
        j = i + 1
        while j < n and _full_subtype(blocks[j]) == sub:
            j += 1
        # blocks[i:j] 가 같은 subtype 그룹.
        if j - i >= 2:
            first_data = blocks[i].get("data") or {}
            first_css = blocks[i].get("custom_css", "")
            first_layout_is_large = first_data.get("layout") == "large"
            for k in range(i + 1, j):
                target_data = dict(blocks[k].get("data") or {})
                for key in _GROUP_UNIFORM_DATA_KEYS:
                    if key == "layout" and first_layout_is_large:
                        # 쇼케이스 남발 방지 — 첫 블록만 large, 나머지는 small.
                        target_data[key] = "small"
                        continue
                    if key in first_data:
                        target_data[key] = first_data[key]
                    else:
                        target_data.pop(key, None)
                blocks[k]["data"] = target_data
                blocks[k]["custom_css"] = first_css
        i = j
    return blocks


def merge_full_restyle(
    *,
    existing_page_meta: dict,
    existing_blocks: list[dict],
    llm_response: dict,
    preserve_content: bool = True,
) -> dict:
    """LLM full_restyle 응답 → ``page_applier`` 적용 가능한 full result_json.

    Args:
        existing_page_meta: ``{"title", "is_public", "data", "custom_css"}``
        existing_blocks: 직렬화된 기존 블록. 각 항목은
            ``{"id", "type", "order", "is_enabled", "data", "custom_css",
               "schedule_enabled", "publish_at", "hide_at"}``.
        llm_response: LLM 출력 (placeholder 가 thaw 된 상태). 형식:
            ``{"page": {...}, "blocks": [{"id"|"_new", "_type", "order",
               "is_enabled", "data": {...style + 새 블록의 텍스트 콘텐츠...}}]}``.

    Returns:
        page_applier 가 그대로 처리할 수 있는 ``{title, is_public, data, custom_css, blocks}``.
        응답에 없는 기존 id 는 결과 blocks 에 미포함 → page_applier 가 삭제 처리.
    """
    response_page = _extract_response_page(llm_response)
    response_blocks = llm_response.get("blocks") if isinstance(llm_response, dict) else None

    merged_meta = _merge_page_meta(
        existing_page_meta, response_page, preserve_content=preserve_content,
    )

    # 기존 블록을 id 로 매핑
    existing_by_id: dict[int, dict] = {}
    for b in existing_blocks or []:
        bid = b.get("id")
        if isinstance(bid, int):
            existing_by_id[bid] = b

    if not isinstance(response_blocks, list):
        # 블록 응답이 빠지면 메타만 적용.
        return {**merged_meta}

    merged_blocks: list[dict] = []
    for i, raw in enumerate(response_blocks):
        if not isinstance(raw, dict):
            continue

        is_new = bool(raw.get("_new")) or raw.get("id") is None
        bid = raw.get("id") if not is_new else None
        order = raw.get("order") or (i + 1)
        response_data = raw.get("data") or {}

        if not is_new and isinstance(bid, int) and bid in existing_by_id:
            # ── 기존 블록 패치 ─────────────────────────
            # full_restyle 모드는 "극적 리뉴얼" — URL/이미지(placeholder 보호) 외엔
            # 텍스트 콘텐츠도 AI 가 새로 작성 가능. 화이트리스트 통과 키만 덮어쓰기.
            base = existing_by_id[bid]
            subtype = _block_subtype_for_whitelist(base)
            base_data = dict(base.get("data") or {})
            full_patch = _filter_full_restyle_patch(response_data, subtype)

            # preserve_content 모드 안전망:
            #  - 기존에 없던 시각 속성(custom_border_color) 임의 추가 차단.
            #  - text 블록의 text_layout 이 plain 이면 카드형(default) 으로 못 바꾸게.
            if preserve_content:
                for key in _VISUAL_KEYS_GUARDED:
                    if key in full_patch and key not in base_data:
                        full_patch.pop(key, None)
                if subtype == "text" and base_data.get("text_layout") == "plain":
                    if full_patch.get("text_layout") not in (None, "plain"):
                        full_patch.pop("text_layout", None)

            base_data.update(full_patch)
            merged_blocks.append({
                "id": bid,
                "type": base.get("type"),
                "order": order,
                "is_enabled": raw.get("is_enabled", base.get("is_enabled", True)),
                "data": base_data,
                "custom_css": raw.get("custom_css", base.get("custom_css", "")),
                "schedule_enabled": base.get("schedule_enabled", False),
                "publish_at": base.get("publish_at"),
                "hide_at": base.get("hide_at"),
            })
            continue

        # ── 새 블록 생성 ──
        raw_type = raw.get("type")
        raw_subtype = raw.get("_type")
        # type 정규화: profile/contact 는 type, 그 외는 single_link + data._type=raw_subtype.
        if raw_type in ("profile", "contact"):
            db_type = raw_type
            subtype = raw_type
            new_data: dict[str, Any] = {}
        else:
            db_type = "single_link"
            subtype = raw_subtype or raw_type or "single_link"
            new_data = {"_type": subtype}

        # ── 안전망: _new single_link/single_link 는 url 이 필수인데 신규 블록은
        # url placeholder 가 없어 빈 채로 들어간다. AI 가 spacer/notice/customer/
        # inquiry 의도였는데 _type 을 잘못 분류한 경우가 많음. 라벨/콘텐츠가 있으면
        # text 블록으로 fallback, 없으면 그 블록 자체를 누락시킨다.
        if subtype == "single_link":
            has_text = bool(
                (response_data.get("label") or "").strip()
                or (response_data.get("description") or "").strip()
            )
            if has_text:
                subtype = "text"
                new_data = {"_type": "text"}
                if response_data.get("label"):
                    new_data["headline"] = response_data["label"]
                if response_data.get("description"):
                    new_data["content"] = response_data["description"]
            else:
                logger.info(
                    "style_patcher: _new single_link 누락 (url 비어있고 라벨도 없음) "
                    "— AI 가 _type 분류 실패한 듯"
                )
                continue

        allowed_content = _TEXT_CONTENT_KEYS.get(subtype, set())
        allowed_style = _allowed_style_keys(subtype)
        for k, v in response_data.items():
            if k == "_type":
                continue
            if k in allowed_style or k in allowed_content:
                new_data[k] = v
            # URL/이미지 필드는 무시 (새 블록은 URL 비어둠 — 사용자가 추후 입력).

        merged_blocks.append({
            "type": db_type,
            "order": order,
            "is_enabled": raw.get("is_enabled", True),
            "data": new_data,
            "custom_css": raw.get("custom_css", ""),
            "schedule_enabled": False,
            "publish_at": None,
            "hide_at": None,
        })

    # order 정규화 — 1 부터 순차 (충돌 방지).
    for idx, b in enumerate(merged_blocks):
        b["order"] = idx + 1

    # 그룹 통일 — 연속된 같은 subtype 블록 무리는 동일 시각 스타일.
    merged_blocks = _enforce_group_uniformity(merged_blocks)

    return {**merged_meta, "blocks": merged_blocks}


# ─────────────────────────────────────────────────────────────
# style_only 머지
# ─────────────────────────────────────────────────────────────

def merge_style_only(
    *,
    existing_page_meta: dict,
    existing_blocks: list[dict],
    llm_response: dict,
) -> dict:
    """LLM style_only 응답 → full result_json.

    LLM 응답 ``block_styles`` 형식:
      ``{"*": {...글로벌...}, "<subtype>": {...}, "_by_id": {"<id>": {...}}}``

    각 블록에 글로벌 → subtype → _by_id 순서로 화이트리스트 통과해 패치.
    블록 추가/삭제/순서/타입 변경 없음.
    """
    response_page = _extract_response_page(llm_response)
    block_styles = llm_response.get("block_styles") if isinstance(llm_response, dict) else None
    if not isinstance(block_styles, dict):
        block_styles = {}

    merged_meta = _merge_page_meta(existing_page_meta, response_page)
    # style_only 모드에서는 title/is_public 변경 무시 — 콘텐츠 보존이 핵심.
    if isinstance(existing_page_meta, dict):
        for k in ("title", "is_public"):
            if k in existing_page_meta:
                merged_meta[k] = existing_page_meta[k]

    global_patch = block_styles.get("*") if isinstance(block_styles.get("*"), dict) else {}
    by_id_raw = block_styles.get("_by_id") if isinstance(block_styles.get("_by_id"), dict) else {}
    # _by_id 키는 JSON 직렬화 과정에서 문자열일 가능성 — int 로 정규화.
    by_id: dict[int, dict] = {}
    for k, v in by_id_raw.items():
        try:
            by_id[int(k)] = v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            continue

    def _css_from(patch: Any) -> str | None:
        """patch dict 에서 ``custom_css`` 만 안전하게 추출."""
        if isinstance(patch, dict):
            css = patch.get("custom_css")
            if isinstance(css, str):
                return css
        return None

    merged_blocks: list[dict] = []
    for b in existing_blocks or []:
        subtype = _block_subtype_for_whitelist(b)
        base_data = dict(b.get("data") or {})
        merged_css = b.get("custom_css", "")

        # 1) 글로벌
        base_data.update(_filter_style_patch(global_patch, subtype))
        css = _css_from(global_patch)
        if css is not None:
            merged_css = css
        # 2) subtype 별
        subtype_patch = block_styles.get(subtype)
        if isinstance(subtype_patch, dict):
            base_data.update(_filter_style_patch(subtype_patch, subtype))
            css = _css_from(subtype_patch)
            if css is not None:
                merged_css = css
        # 3) _by_id override
        bid = b.get("id")
        if isinstance(bid, int) and bid in by_id:
            patch = by_id[bid]
            base_data.update(_filter_style_patch(patch, subtype))
            css = _css_from(patch)
            if css is not None:
                merged_css = css

        merged_blocks.append({
            "id": bid,
            "type": b.get("type"),
            "order": b.get("order"),
            "is_enabled": b.get("is_enabled", True),
            "data": base_data,
            "custom_css": merged_css,
            "schedule_enabled": b.get("schedule_enabled", False),
            "publish_at": b.get("publish_at"),
            "hide_at": b.get("hide_at"),
        })

    # 그룹 통일 — 연속된 같은 subtype 블록 무리는 동일 시각 스타일.
    merged_blocks = _enforce_group_uniformity(merged_blocks)

    return {**merged_meta, "blocks": merged_blocks}
