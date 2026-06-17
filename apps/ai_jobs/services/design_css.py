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

이 모듈은 팔레트(buttonColor=accent, backgroundColor)와 카테고리, 그리고 **job 시드**로
variant·텍스트 장식·카드 기하를 변주해 **같은 업종도 매번 다르게** 보이게 한다(사용자 피드백:
"맨날 같은 디자인/토글 옆 똑같은 무늬"). 시드는 색을 정하지 않는다 — 색은 가드가 최종 결정.
"""

from __future__ import annotations

import logging

from . import category_profiles as CP
from . import color_utils as C
from .design_seed import pick

logger = logging.getLogger(__name__)

# 카테고리 → 허용 variant **풀**. job 시드로 그중 하나를 골라 "같은 업종=같은 모양"을 깬다.
# 각 풀은 그 카테고리에 어울리는 것만 큐레이션(취향 안에서의 회전 — 아무거나가 아님).
_VARIANT_POOL_BY_CATEGORY = {
    CP.PROFILE: ["soft", "clean", "editorial"],
    CP.BIZCARD: ["clean", "editorial"],
    CP.LANDING: ["clean", "bold"],
    CP.PORTFOLIO: ["editorial", "clean"],
    CP.BROCHURE: ["editorial", "soft", "clean"],
    CP.RENTAL: ["clean", "soft"],
    CP.GROUPBUY: ["bold", "clean"],
    CP.PROMO: ["bold", "clean"],
    CP.INVITATION: ["soft", "editorial"],  # 둘 다 우아 — bold/outline 절대 금지
    CP.AFFILIATE: ["clean", "soft"],
    # 커미션(작가/키치)은 카툰 잉크 보더 정체성 유지 — 싱글톤(회전 안 함).
    CP.COMMISSION: ["outline"],
    CP.GENERIC: ["clean", "soft"],
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

# 텍스트/토글 카드 장식 풀 — variant 별 취향에 맞는 것만. seed 로 하나 선택해
# "토글 옆에 맨날 똑같은 좌측바 무늬"를 깬다(editorial=미니멀, outline=잉크 보더 유지).
_DECO_POOL_BY_VARIANT = {
    "soft": ["left_bar", "soft_tint", "top_rule", "chip_tab"],
    "clean": ["left_bar", "top_rule", "underline_accent", "hairline_only"],
    "bold": ["left_bar", "top_rule", "chip_tab", "soft_tint"],
    "editorial": ["hairline_only", "underline_accent", "top_rule"],
    "outline": ["left_bar"],
}

# 카드 기하 변주(±) — clamp 로 취향 범위 유지. outline 은 만화 컷이라 고정.
_CARD_JITTER = [
    {"dr": 0, "da": 0.00, "dl": 0},
    {"dr": 4, "da": 0.03, "dl": 1},
    {"dr": -4, "da": -0.02, "dl": -1},
]

_MARKER = "/* tf-design-kit */"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _resolve_variant(category: str, seed: int = 0) -> str:
    """카테고리 허용 풀에서 시드로 variant 선택(commission=outline 싱글톤)."""
    pool = _VARIANT_POOL_BY_CATEGORY.get(category) or ["clean"]
    return pick(seed, pool, salt=0)


def _decoration_css(deco: str, *, acc: str, card_hairline: str, r: int, shadow: str) -> str:
    """텍스트/토글 카드 장식 한 종류의 inner CSS 선언을 반환(가드된 acc/hairline 만 사용)."""
    radius = max(10, r - 4)
    box = f"border-radius:{radius}px !important;box-shadow:{shadow} !important;"
    hair = f"1px solid {card_hairline}"
    if deco == "top_rule":
        return (
            box + f"border-top:3px solid {acc} !important;border-left:{hair} !important;"
            f"border-right:{hair} !important;border-bottom:{hair} !important;"
        )
    if deco == "underline_accent":
        return (
            box + f"border-bottom:2px solid {acc} !important;border-top:{hair} !important;"
            f"border-left:{hair} !important;border-right:{hair} !important;"
        )
    if deco == "chip_tab":
        return (
            f"box-shadow:{shadow} !important;"
            f"border-radius:{max(4, radius - 8)}px {radius}px {radius}px {radius}px !important;"
            f"border-left:3px solid {acc} !important;border-top:3px solid {acc} !important;"
            f"border-right:{hair} !important;border-bottom:{hair} !important;"
        )
    if deco == "soft_tint":
        rgb = C.parse_hex(acc)
        tint = f"rgba({rgb[0]},{rgb[1]},{rgb[2]},0.07)" if rgb else "transparent"
        return box + f"background:{tint} !important;border:{hair} !important;"
    if deco == "hairline_only":
        return box + f"border:{hair} !important;"
    # left_bar (기본/현행 호환)
    return (
        box + f"border-left:3px solid {acc} !important;border-top:{hair} !important;"
        f"border-right:{hair} !important;border-bottom:{hair} !important;"
    )


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


def build_design_css(*, accent: str, background: str, category: str, seed: int = 0) -> str:
    """팔레트+카테고리+job 시드로 page-level 디자인 킷 CSS 문자열 생성.

    같은 카테고리라도 seed 에 따라 variant·텍스트 장식·카드 기하가 달라져 "맨날 같은
    디자인"을 깬다. 색(accent/background)은 시드가 정하지 않는다 — 이미 가드로 보정된 값을 받아
    그대로 쓴다(대비/슬롭 가드는 별개로 유지).
    """
    variant = _resolve_variant(category, seed)
    spec = _VARIANT_SPEC[variant]
    dark = _is_dark(background)
    acc = accent if C.is_hex(accent) else "#111827"

    # 카드 기하 변주(outline 은 만화 컷 정체성이라 고정). clamp 으로 취향 범위 유지.
    if variant == "outline":
        r, shadow_a, lift = spec["radius"], spec["shadow_a"], spec["lift"]
    else:
        jit = pick(seed, _CARD_JITTER, salt=2)
        r = int(_clamp(spec["radius"] + jit["dr"], 8, 26))
        shadow_a = _clamp(spec["shadow_a"] + jit["da"], 0.08, 0.30)
        lift = int(_clamp(spec["lift"] + jit["dl"], 1, 3))

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
            f"0 {spec['shadow_y']}px {spec['shadow_blur']}px " f"-18px rgba(17,17,26,{shadow_a})"
        )
        border = "1px solid rgba(17,17,26,.06)"
        card_hairline = "rgba(17,17,26,.06)"

    # 엔트런스 stagger (앞쪽 블록부터 살짝 늦게 떠오름)
    stagger = "\n".join(
        f"[data-block-container] > .block-link:nth-child({i}){{animation-delay:{i * 0.05:.2f}s}}"
        for i in range(1, 11)
    )

    # 텍스트/토글 카드 장식 — variant 별 풀에서 seed 로 선택(고정 좌측바 → 다양화).
    deco = pick(seed, _DECO_POOL_BY_VARIANT.get(variant, ["left_bar"]), salt=1)
    text_card = _decoration_css(deco, acc=acc, card_hairline=card_hairline, r=r, shadow=shadow)

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
/* 텍스트 카드(boxed/toggle) — 장식 variant (seed 로 선택) */
.block-link[data-block-type="text"] > div[class*="border"],
.block-link[data-block-type="text"] > details{{
  {text_card}
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


def enhance_page_css(result: dict, category: str, seed: int = 0) -> dict:
    """result_json 의 page custom_css 에 디자인 킷을 합쳐 넣는다(in-place, 같은 객체).

    모델이 쓴 css(주로 body 배경)는 보존하고, 그 뒤에 킷을 덧붙인다(킷이 !important 라 우선).
    이미 킷이 들어가 있으면(_MARKER) 중복 주입하지 않는다. ``seed`` 로 variant/장식/카드 기하를
    job 마다 다르게 한다(결정적).
    """
    if not isinstance(result, dict):
        return result
    try:
        ds = _get_design_settings(result)
        # design_settings 가 비어 있으면 새로 만들어 result 에 연결(폰트 폴백을 위해).
        if not ds:
            ds = {}
            result.setdefault("data", {})
            if isinstance(result["data"], dict):
                result["data"]["design_settings"] = ds

        # 폰트 폴백: 모델이 fontFamily 를 안 정했을 때만 카테고리+seed 폰트 풀에서 주입.
        if not (ds.get("fontFamily") or "").strip():
            ds["fontFamily"] = CP.get_font(category, seed)

        accent = (ds.get("buttonColor") or "").strip()
        background = (ds.get("backgroundColor") or C.DEFAULT_BG).strip()

        existing = ""
        for loc in (result.get("custom_css"), (result.get("data") or {}).get("custom_css")):
            if isinstance(loc, str) and loc.strip():
                existing = loc.strip()
                break
        if _MARKER in existing:
            return result  # 이미 적용됨

        kit = build_design_css(accent=accent, background=background, category=category, seed=seed)
        combined = (existing + "\n" + kit) if existing else kit

        result.setdefault("data", {})
        if isinstance(result["data"], dict):
            result["data"]["custom_css"] = combined
        result["custom_css"] = combined
        logger.info(
            "design_css 적용(%s, variant=%s, seed=%s, +%dB)",
            category,
            _resolve_variant(category, seed),
            seed,
            len(kit),
        )
    except Exception:  # noqa: BLE001
        logger.exception("design_css 적용 실패(무시): category=%s", category)
    return result
