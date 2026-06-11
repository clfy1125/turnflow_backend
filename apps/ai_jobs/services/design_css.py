"""결정적 '디자인 킷' — page.custom_css 를 코드로 생성/보강한다.

배경(사용자 피드백): 생성 모델이 쓰는 custom_css 는 ``body{background:...}`` 한 줄뿐이라
**카드/여백/타이포가 밋밋하고 "10년 전 웹" 같다.** 블록별 custom_css 는 공개페이지에서
**렌더되지 않으므로**(검증 완료) page.custom_css 한 곳에서 전부 처리해야 한다.

라이브 DOM 실측으로 확인한 안전한 선택자:
  - ``.page-container``                       페이지 스크롤 영역
  - ``[data-block-container]``                블록 묶음 래퍼
  - ``.block-link[data-block-type="TYPE"]``  TYPE = 서브타입(text/single_link/group_link/gallery/...)
  - ``div[data-block-id="N"]``               개별 블록
인라인 스타일(borderRadius/bg/border/color)을 이기려면 ``!important`` 필요.

이 모듈은 팔레트(buttonColor=accent, backgroundColor)와 카테고리로 톤 변주를 줘
**모든 페이지가 똑같이 보이지 않게** 한다.
"""

from __future__ import annotations

import logging

from . import category_profiles as CP
from . import color_utils as C

logger = logging.getLogger(__name__)

# 카테고리 → 시각 변주(variant). 카드 라운드/그림자/강조 방식이 달라진다.
_VARIANT_BY_CATEGORY = {
    CP.INVITATION: "soft",
    CP.PROFILE: "soft",
    CP.AFFILIATE: "soft",
    # 커미션(작가/키치)은 카툰 잉크 보더 — 레퍼런스 @none-20 (모든 카드에 동일한
    # 진한 테두리 + 단단한 오프셋 그림자 = 웹툰 컷 느낌).
    CP.COMMISSION: "outline",
    CP.GROUPBUY: "bold",
    CP.PROMO: "bold",
    CP.PORTFOLIO: "editorial",
    CP.BROCHURE: "editorial",
    CP.BIZCARD: "clean",
    CP.LANDING: "clean",
    CP.RENTAL: "clean",
    CP.GENERIC: "clean",
}

# variant → (카드 radius px, 그림자 spec 키)
_VARIANT_SPEC = {
    "soft": {"radius": 22, "shadow_y": 14, "shadow_blur": 40, "shadow_a": 0.16, "lift": 2},
    "bold": {"radius": 14, "shadow_y": 12, "shadow_blur": 30, "shadow_a": 0.26, "lift": 3},
    "editorial": {"radius": 8, "shadow_y": 10, "shadow_blur": 28, "shadow_a": 0.12, "lift": 1},
    "clean": {"radius": 16, "shadow_y": 12, "shadow_blur": 34, "shadow_a": 0.18, "lift": 2},
    # 카툰/잉크 — 그림자 대신 2px 잉크 보더 + 오프셋 하드섀도(만화 컷 느낌).
    "outline": {"radius": 16, "shadow_y": 0, "shadow_blur": 0, "shadow_a": 0.0, "lift": 2},
}

_MARKER = "/* tf-design-kit */"


def _get_design_settings(result: dict) -> dict:
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("design_settings"), dict):
        return data["design_settings"]
    if isinstance(result.get("design_settings"), dict):
        return result["design_settings"]
    return {}


def _is_dark(bg: str) -> bool:
    rgb = C.parse_hex(bg)
    return bool(rgb and C.luma(rgb) < 0.5)


def build_design_css(*, accent: str, background: str, category: str) -> str:
    """팔레트+카테고리로 page-level 디자인 킷 CSS 문자열 생성."""
    variant = _VARIANT_BY_CATEGORY.get(category, "clean")
    spec = _VARIANT_SPEC[variant]
    r = spec["radius"]
    dark = _is_dark(background)
    acc = accent if C.is_hex(accent) else "#111827"

    # 그림자/테두리는 배경 명암에 맞춰. 다크는 은은한 글로우 + 밝은 헤어라인.
    if variant == "outline":
        # 카툰 잉크 보더 — 모든 카드에 동일한 진한 테두리 + 단단한 오프셋 그림자.
        ink = "rgba(232,230,227,.85)" if dark else "#2D3142"
        shadow = f"3px 3px 0 {'rgba(232,230,227,.25)' if dark else 'rgba(45,49,66,.18)'}"
        border = f"2px solid {ink}"
        card_hairline = ink
    elif dark:
        shadow = f"0 {spec['shadow_y']}px {spec['shadow_blur']}px -16px rgba(0,0,0,.55)"
        border = "1px solid rgba(255,255,255,.08)"
        card_hairline = "rgba(255,255,255,.10)"
    else:
        shadow = (
            f"0 {spec['shadow_y']}px {spec['shadow_blur']}px "
            f"-18px rgba(17,17,26,{spec['shadow_a']})"
        )
        border = "1px solid rgba(17,17,26,.06)"
        card_hairline = "rgba(17,17,26,.06)"

    lift = spec["lift"]

    # 엔트런스 stagger (앞쪽 블록부터 살짝 늦게 떠오름)
    stagger = "\n".join(
        f"[data-block-container] > .block-link:nth-child({i}){{animation-delay:{i * 0.05:.2f}s}}"
        for i in range(1, 11)
    )

    # editorial 은 그림자 대신 얇은 라인 강조(미니멀). soft 는 라운드 크게.
    text_accent = (
        f"border-left:3px solid {acc} !important;"
        if variant != "editorial"
        else f"border-left:2px solid {acc} !important;"
    )

    css = f"""{_MARKER}
.page-container{{ padding-left:18px !important; padding-right:18px !important; }}
[data-block-container]{{ margin-top:18px !important; }}
/* 블록 등장 애니메이션 */
@keyframes tfUp{{from{{opacity:0;transform:translateY(12px)}}to{{opacity:1;transform:none}}}}
.block-link{{ animation:tfUp .5s cubic-bezier(.22,.61,.36,1) both; }}
{stagger}
/* 카드(single_link / group_link 항목) — 라운드 + 부드러운 그림자 + 헤어라인 */
.block-link[data-block-type="single_link"] > a,
.block-link[data-block-type="group_link"] a{{
  border-radius:{r}px !important;
  box-shadow:{shadow} !important;
  border:{border} !important;
}}
.block-link[data-block-type="single_link"] > a{{ transition:transform .18s ease, box-shadow .18s ease; }}
.block-link[data-block-type="single_link"] > a:hover{{ transform:translateY(-{lift}px); }}
/* 텍스트 카드(boxed/toggle) — 강조 컬러 좌측 바 + 라운드 */
.block-link[data-block-type="text"] > div[class*="border"],
.block-link[data-block-type="text"] > details{{
  border-radius:{max(10, r - 4)}px !important;
  {text_accent}
  border-top:1px solid {card_hairline} !important;
  border-right:1px solid {card_hairline} !important;
  border-bottom:1px solid {card_hairline} !important;
  box-shadow:{shadow} !important;
}}
/* 갤러리/이미지 라운드 */
.block-link[data-block-type="gallery"] img,
.block-link[data-block-type="image"] img{{ border-radius:{max(10, r - 6)}px !important; }}
/* 강조 뱃지 또렷하게 */
.block-link [class*="rounded-full"]{{ letter-spacing:.2px; }}
/* 강조 컬러 존재감(개성) — 구분선에 컨셉색 살짝, 선택 영역 톤 */
.block-link[data-block-type="spacer"] hr,
.block-link[data-block-type="spacer"] div[class*="border"]{{ border-color:{acc} !important; opacity:.55; }}
::selection{{ background:{acc}; color:#fff; }}
"""
    return css


def enhance_page_css(result: dict, category: str) -> dict:
    """result_json 의 page custom_css 에 디자인 킷을 합쳐 넣는다(in-place, 같은 객체).

    모델이 쓴 css(주로 body 배경)는 보존하고, 그 뒤에 킷을 덧붙인다(킷이 !important 라 우선).
    이미 킷이 들어가 있으면(_MARKER) 중복 주입하지 않는다.
    """
    if not isinstance(result, dict):
        return result
    try:
        ds = _get_design_settings(result)
        accent = (ds.get("buttonColor") or "").strip()
        background = (ds.get("backgroundColor") or C.DEFAULT_BG).strip()

        existing = ""
        for loc in (result.get("custom_css"), (result.get("data") or {}).get("custom_css")):
            if isinstance(loc, str) and loc.strip():
                existing = loc.strip()
                break
        if _MARKER in existing:
            return result  # 이미 적용됨

        kit = build_design_css(accent=accent, background=background, category=category)
        combined = (existing + "\n" + kit) if existing else kit

        result.setdefault("data", {})
        if isinstance(result["data"], dict):
            result["data"]["custom_css"] = combined
        result["custom_css"] = combined
        logger.info(
            "design_css 적용(%s, variant=%s, +%dB)",
            category,
            _VARIANT_BY_CATEGORY.get(category, "clean"),
            len(kit),
        )
    except Exception:  # noqa: BLE001
        logger.exception("design_css 적용 실패(무시): category=%s", category)
    return result
