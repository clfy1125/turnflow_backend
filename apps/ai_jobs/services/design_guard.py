"""새-페이지 result_json 의 디자인 품질을 코드로 보정(가드).

생성 모델은 (1) 'AI 슬롭' 기본색(보라 #8c25f4), (2) 배경↔카드 명도차 부족(muddy),
(3) 블록 custom 색의 대비 미달(어두운 카드에 어두운 글씨 → 안 보임) 같은 값을 자주 만든다.
스키마는 유효해도 **값 품질**은 별개라, 저장 직전에 한 번 보정한다.

프론트 렌더러(``PublicLinkPage.tsx`` / ``useBlockColors.ts``)의 실제 색 결정 로직을
``color_utils`` 로 포팅해 "렌더 후 보일 색"을 예측하고, WCAG 4.5:1 미달만 최소 침습으로 고친다.

리메이크(full_restyle/style_only)에도 적용된다 — ``style_patcher`` 가 구조/콘텐츠를 보존·머지한
뒤, ``tasks.run_ai_job`` 이 이 모듈로 시각 품질(대비/슬롭색/빈 히어로)을 새-페이지 수준으로
끌어올린다. ``fix_hero``/``pin_palette`` 로 리메이크 특성(기존 레이아웃 존중·컨셉 이미지 주도)을 제어.
"""

from __future__ import annotations

import logging

from . import color_utils as C

logger = logging.getLogger(__name__)

# WCAG AA — 본문 4.5:1, 큰 글씨 3:1. 보수적으로 본문 기준 사용.
MIN_CONTRAST = 4.5
LARGE_MIN_CONTRAST = 3.0
# 배경↔카드 최소 명도차 (muddy 방지)
MIN_BG_CARD_SPREAD = 0.05


def _get_design_settings(result: dict) -> dict | None:
    """result_json 에서 design_settings dict 를 찾는다 (data.design_settings 우선)."""
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("design_settings"), dict):
        return data["design_settings"]
    if isinstance(result.get("design_settings"), dict):
        return result["design_settings"]
    return None


def _predicted_page_text(bg: str) -> str:
    """렌더러가 backgroundColor 위에 깔 페이지 텍스트색 (isDarkBg 분기 포팅)."""
    return "#FFFFFF" if C.contrast_text(bg) == "#FFFFFF" else "#0f172a"


def _fix_bg_text_contrast(bg: str) -> str:
    """배경이 중간톤이라 자동 텍스트색과 대비가 부족하면 배경 명도를 한쪽으로 밀어 보정."""
    txt = _predicted_page_text(bg)
    if C.wcag_contrast(bg, txt) >= MIN_CONTRAST:
        return bg
    dark_bg = C.contrast_text(bg) == "#FFFFFF"  # 흰 글씨가 깔린다 = 어두운 배경
    step = -0.05 if dark_bg else 0.05  # 어두운 배경은 더 어둡게, 밝은 배경은 더 밝게
    cur = bg
    for _ in range(12):
        cur = C.adjust_lightness(cur, step)
        if C.wcag_contrast(cur, _predicted_page_text(cur)) >= MIN_CONTRAST:
            return cur
    return cur


def _card_defaults(ds: dict) -> tuple[str, str]:
    """design_settings 기준 카드 기본 (bg, text) 예측 — 블록이 custom 안 줄 때 값."""
    bg = ds.get("backgroundColor") or C.DEFAULT_BG
    is_dark = C.contrast_text(bg) == "#FFFFFF"
    block_bg = (ds.get("blockBgColor") or "").strip()
    if C.is_hex(block_bg):
        # 카드 글씨는 blockBgColor 대비로 결정됨(렌더러 포팅)
        card_text = "#ffffff" if C.contrast_text(block_bg) == "#FFFFFF" else "#111827"
        return block_bg, card_text
    # blockBgColor 미설정: 다크면 반투명 흰(거의 흰 글씨), 라이트면 흰 카드(어두운 글씨)
    if is_dark:
        return "#2b2b33", "#FFFFFF"  # 반투명 흰 카드 ~ 어두운 톤 근사
    return "#ffffff", "#0f172a"


def enforce_design_quality(
    result: dict,
    palette: dict | None = None,
    fix_hero: bool = True,
    pin_palette: bool = False,
) -> dict:
    """result_json 을 in-place 보정하고 같은 객체 반환.

    Args:
        result: LLM 생성 결과 (data.design_settings + blocks).
        palette: (선택) 결정적 추출 팔레트 — 슬롭색 교체 시 accent 후보로 사용.
        fix_hero: 빈 커버 히어로 승격/강등 보정 여부. **리메이크는 False** —
            사용자의 기존 프로필 레이아웃/이미지 선택을 존중한다(색 대비 보정만 수행).
        pin_palette: True 면 design_settings 의 배경/카드/버튼색을 **팔레트 값으로 강제
            스냅**한다(모델이 바꿨어도 되돌림). 컨셉 이미지가 디자인을 주도할 때 사용 —
            프롬프트의 "정확한 #hex" 지시를 모델이 밝기 지시와 충돌시키며 무시하는 사고
            (시안 로열바이올렛 → 결과 검은네이비, 2026-06-11) 를 코드로 차단한다.
            스냅 후에도 WCAG 대비 가드는 그대로 적용된다(가독성 우선).
    """
    if not isinstance(result, dict):
        return result

    report: dict[str, int] = {
        "slop_color_replaced": 0,
        "frame_bg_filled": 0,
        "bg_text_fixed": 0,
        "bg_card_spread_fixed": 0,
        "block_contrast_fixed": 0,
        "block_bg_spread_fixed": 0,
        "palette_pinned": 0,
        "pure_black_softened": 0,
        "hero_image_promoted": 0,
        "hero_downgraded": 0,
        "gallery_keep_ratio_off": 0,
    }

    ds = _get_design_settings(result)
    if ds is not None:
        if pin_palette and palette:
            _pin_palette(ds, palette, report)
        _guard_design_settings(ds, palette, report)

    blocks = result.get("blocks")
    if isinstance(blocks, list):
        if fix_hero:
            _fix_empty_hero(blocks, report)
        # gallery keep_ratio 는 색이 아니라 레이아웃이므로 design_settings 유무와 무관하게 강제.
        for b in blocks:
            if isinstance(b, dict):
                _force_gallery_keep_ratio_off(b, report)
        if ds is not None:
            for b in blocks:
                if isinstance(b, dict):
                    _guard_block_colors(b, ds, report)

    if any(report.values()):
        logger.info("design_guard 보정: %s", {k: v for k, v in report.items() if v})
    return result


def _force_gallery_keep_ratio_off(block: dict, report: dict) -> None:
    """gallery 의 ``keep_ratio`` 를 코드로 항상 OFF — 시스템 결정(프롬프트 의존 X).

    keep_ratio=True 는 썸네일이 원본 비율대로 들쭉날쭉 늘어나 갤러리 레이아웃을 깨는 주범.
    OFF(고정 비율 크롭)가 가장 깔끔하다는 디자인 판단을 코드로 강제한다(멱등).
    """
    d = block.get("data")
    if not isinstance(d, dict) or d.get("_type") != "gallery":
        return
    if d.get("keep_ratio") is not False:
        d["keep_ratio"] = False
        report["gallery_keep_ratio_off"] += 1


def _is_profile(block: dict) -> bool:
    return block.get("type") == "profile" or (block.get("data") or {}).get("_type") == "profile"


# 연락/예약/주문/채널 류 보조 버튼 — 큰 카드로 공간 낭비하면 안 됨. 기본 small.
_COMPACT_HINTS = (
    "카카오",
    "카톡",
    "kakao",
    "pf.kakao",
    "문의",
    "상담",
    "예약",
    "naver.me",
    "booking",
    "채널",
    "신청",
    "구독",
    "알림",
    "디엠",
    " dm",
    "dm ",
    "전화",
    "톡",
    "오픈채팅",
    "송금",
    "입금",
    "계좌",
    "다운로드",
    "이력서",
    "resume",
)

# 페이지의 **주요 전환 CTA** — 비즈니스 직결 소통/전환 창구는 한 줄짜리 컴팩트보다
# medium(에디터 명칭 '스탠다드')가 적합하다(사용자 피드백: 카톡 문의하기·무료 체험시작).
# 쇼케이스(large)는 과하고, 첫 매칭 1개만 스탠다드로 승격한다.
_PRIMARY_CTA_HINTS = (
    "문의",
    "상담",
    "체험",
    "예약",
    "주문",
    "신청",
    "구매",
    "시작하기",
    "시작",
    "kakao",
    "카톡",
    "카카오",
)


def _looks_compact(data: dict) -> bool:
    text = " ".join(
        str(data.get(k) or "") for k in ("label", "url", "description", "badge")
    ).lower()
    return any(h in text for h in _COMPACT_HINTS)


def _looks_primary_cta(data: dict) -> bool:
    text = str(data.get("label") or "").lower()
    return any(h in text for h in _PRIMARY_CTA_HINTS)


def enforce_compact_links(result: dict, max_showcase: int = 1) -> dict:
    """링크 카드 크기 정책 보정 — 사용자 핵심 피드백 반영.

    정책 3단계:
    1. **주요 전환 CTA 1개는 medium(스탠다드)** — 카톡 문의/무료체험/예약 같은 전환 버튼은
       페이지에서 가장 중요한 소통 창구라 한 줄(small)로 묻히면 안 되고, 쇼케이스(large)는
       과하다. 문서 순서상 첫 매칭 1개를 medium 으로 승격(이미 large 면 medium 으로 강등).
    2. 그 외 연락/예약/주문/채널 류 보조 버튼은 small (큰 카드 공간 낭비 금지).
    3. **상품 카드 medium 은 허용** — 썸네일이나 가격이 있는 medium 은 상품 진열이라
       여러 개여도 자연스럽다(레퍼런스 @none-21 패턴, 사용자 피드백). 강등하지 않는다.
    4. 비주얼 쇼케이스(large)와 맨몸 medium(썸네일·가격 없음)은 페이지당
       ``max_showcase`` 개 — 초과분은 small 로 강등.
    (group_link 그리드는 상품 진열이라 별개 — 손대지 않는다.)
    """
    if not isinstance(result, dict):
        return result
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return result

    report = {"primary_cta_standard": 0, "forced_small_contact": 0, "demoted_excess_showcase": 0}
    showcase_used = 0
    primary_used = False
    for b in blocks:
        if not isinstance(b, dict):
            continue
        d = b.get("data")
        if not isinstance(d, dict) or d.get("_type") != "single_link":
            continue
        layout = d.get("layout")

        # 1) 주요 전환 CTA — 첫 1개를 medium(스탠다드)로. 모델이 CTA 에 붙인 {{image:..}}
        # 스톡 썸네일은 떼어낸다(카톡 문의에 엉뚱한 제품사진이 붙는 사고 — 텍스트형이 깔끔).
        # 사용자 업로드({{user_image:N}})나 실제 URL 은 의도가 분명하니 존중.
        if not primary_used and _looks_primary_cta(d):
            primary_used = True
            if layout != "medium":
                d["layout"] = "medium"
                report["primary_cta_standard"] += 1
            thumb = str(d.get("thumbnail_url") or "")
            if thumb.startswith("{{image:"):
                d["thumbnail_url"] = ""
            continue

        if layout not in ("large", "medium"):
            continue
        # 2) 보조 연락/예약 류 — small.
        if _looks_compact(d):
            d["layout"] = "small"
            report["forced_small_contact"] += 1
            continue
        # 3) 상품성 medium(썸네일 또는 가격 보유)은 진열 카드 — 여러 개 허용.
        has_thumb = bool(str(d.get("thumbnail_url") or "").strip())
        has_price = bool(str(d.get("price") or "").strip())
        if layout == "medium" and (has_thumb or has_price):
            continue
        # 4) 쇼케이스 쿼터 (large + 맨몸 medium).
        if showcase_used < max_showcase:
            showcase_used += 1  # 첫 쇼케이스는 유지
        else:
            d["layout"] = "small"
            report["demoted_excess_showcase"] += 1

    if any(report.values()):
        logger.info("compact_links 보정: %s", {k: v for k, v in report.items() if v})
    return result


def _first_body_image(blocks: list, exclude: dict) -> str:
    """본문 블록들에서 첫 실제(http) 이미지 URL 을 찾는다 (히어로로 승격할 후보).

    갤러리 이미지를 가장 먼저(여러 장 중 1장 빌리는 게 자연스러움), 그다음 썸네일/이미지/링크 썸네일.
    """

    def _ok(u) -> bool:
        return isinstance(u, str) and u.strip().startswith("http")

    # 1순위: gallery images
    for b in blocks:
        if b is exclude or not isinstance(b, dict):
            continue
        imgs = (b.get("data") or {}).get("images")
        if isinstance(imgs, list):
            for u in imgs:
                if _ok(u):
                    return u.strip()
    # 2순위: 블록 썸네일/이미지, 그룹링크 항목 썸네일
    for b in blocks:
        if b is exclude or not isinstance(b, dict):
            continue
        d = b.get("data") or {}
        for k in ("thumbnail_url", "image_url"):
            if _ok(d.get(k)):
                return d[k].strip()
        links = d.get("links")
        if isinstance(links, list):
            for ln in links:
                if isinstance(ln, dict) and _ok(ln.get("thumbnail_url")):
                    return ln["thumbnail_url"].strip()
    return ""


def _fix_empty_hero(blocks: list, report: dict) -> None:
    """profile 이 cover/cover_bg 인데 cover_image_url 이 비면 빈 회색 히어로가 렌더된다.

    → 본문의 실제 이미지를 히어로로 승격하고, 마땅한 이미지가 없으면 레이아웃을 center 로 낮춰
    빈 커버 플레이스홀더를 없앤다. (텍스트-only 컨셉에서 모델이 cover_bg 만 고르고 이미지를 안
    채우는 흔한 케이스 방어.)
    """
    profile = next((b for b in blocks if isinstance(b, dict) and _is_profile(b)), None)
    if profile is None:
        return
    d = profile.get("data")
    if not isinstance(d, dict):
        return
    layout = d.get("profile_layout") or d.get("layout")
    if layout not in ("cover", "cover_bg"):
        return
    if (d.get("cover_image_url") or "").strip():
        return  # 이미 커버 있음

    cand = _first_body_image(blocks, exclude=profile)
    if cand:
        d["cover_image_url"] = cand
        report["hero_image_promoted"] += 1
    else:
        d["profile_layout"] = "center"
        report["hero_downgraded"] += 1


def _pin_palette(ds: dict, palette: dict, report: dict) -> None:
    """컨셉 이미지 팔레트를 design_settings 에 강제 스냅(모델의 임의 변경 되돌림)."""
    bg = (palette.get("background") or "").strip()
    if C.is_hex(bg):
        if (ds.get("backgroundColor") or "").strip().lower() != bg.lower():
            report["palette_pinned"] += 1
        ds["backgroundColor"] = bg
        ds["frameBackgroundColor"] = bg
    surface = (palette.get("surface") or "").strip()
    if C.is_hex(surface):
        ds["blockBgColor"] = surface
    accent = (palette.get("accent") or "").strip()
    if C.is_hex(accent):
        ds["buttonColor"] = accent


def _guard_design_settings(ds: dict, palette: dict | None, report: dict) -> None:
    bg = (ds.get("backgroundColor") or "").strip()
    if not C.is_hex(bg):
        bg = C.DEFAULT_BG
        ds["backgroundColor"] = bg

    # 1) 슬롭 보라 buttonColor 교체 (모델이 안 정했거나 기본 보라면)
    btn = (ds.get("buttonColor") or "").strip()
    if not C.is_hex(btn) or btn.lower() == C.SLOP_PURPLE.lower():
        accent = (palette or {}).get("accent")
        ds["buttonColor"] = accent if C.is_hex(accent or "") else "#111827"
        report["slop_color_replaced"] += 1

    # 2) 배경 중간톤 → 자동 페이지 텍스트 대비 보정
    fixed_bg = _fix_bg_text_contrast(bg)
    if fixed_bg != bg:
        ds["backgroundColor"] = fixed_bg
        bg = fixed_bg
        report["bg_text_fixed"] += 1

    # 3) frameBackgroundColor 비었으면 backgroundColor 와 동일하게
    frame = (ds.get("frameBackgroundColor") or "").strip()
    if not C.is_hex(frame):
        ds["frameBackgroundColor"] = bg
        report["frame_bg_filled"] += 1

    # 4) 배경↔카드 명도차 부족(muddy) → 카드 명도 조정
    card = (ds.get("blockBgColor") or "").strip()
    if C.is_hex(card):
        spread = abs(C.lightness(card) - C.lightness(bg))
        if spread < MIN_BG_CARD_SPREAD:
            is_dark = C.contrast_text(bg) == "#FFFFFF"
            # 라이트 배경이면 카드를 더 밝게(흰쪽), 다크면 더 밝게(떠 보이게)
            ds["blockBgColor"] = C.adjust_lightness(card, 0.07 if not is_dark else 0.06)
            report["bg_card_spread_fixed"] += 1


def _guard_block_colors(block: dict, ds: dict, report: dict) -> None:
    data = block.get("data")
    if not isinstance(data, dict):
        return

    default_card_bg, default_card_text = _card_defaults(ds)

    cb = (data.get("custom_bg_color") or "").strip()
    ct = (data.get("custom_text_color") or "").strip()

    # 블록 custom 카드색이 페이지 배경에 동화(명도차 부족)되면 카드 경계가 사라져
    # 단조롭다(사용자 피드백: promo 메뉴 블록색=배경색). 명도를 한쪽으로 밀어 분리.
    page_bg = (ds.get("backgroundColor") or "").strip()
    if C.is_hex(cb) and C.is_hex(page_bg):
        if abs(C.lightness(cb) - C.lightness(page_bg)) < MIN_BG_CARD_SPREAD:
            is_dark = C.contrast_text(page_bg) == "#FFFFFF"
            cb = C.adjust_lightness(cb, 0.07 if is_dark else -0.07)
            data["custom_bg_color"] = cb
            report["block_bg_spread_fixed"] += 1

    # plain 텍스트 블록은 카드 없이 **페이지 배경 위에 직접** 렌더된다 — 대비 기준이
    # 카드가 아니라 page backgroundColor 다(베이지 배경 + 흰 글씨 사고의 원인).
    # 미지정 text_layout 의 프론트 기본값은 default(boxed) — **명시적 plain 만** 해당.
    is_plain_text = data.get("_type") == "text" and data.get("text_layout") == "plain"
    if is_plain_text and C.is_hex(page_bg):
        eff_bg = page_bg
    else:
        eff_bg = cb if C.is_hex(cb) else default_card_bg
    eff_text = ct if C.is_hex(ct) else default_card_text

    # 순수 검정 글씨는 부드럽게
    if C.is_hex(ct) and ct.lower() in ("#000", "#000000"):
        data["custom_text_color"] = C.SOFT_BLACK
        ct = C.SOFT_BLACK
        eff_text = C.SOFT_BLACK
        report["pure_black_softened"] += 1

    # 글씨 대비 미달 → 글씨색을 실제 배경 대비로 강제(가장 안전).
    # 단 plain 텍스트가 custom 색 없이 자동 색을 쓰는 경우는 렌더러가 알아서 잡으므로 패스.
    if C.wcag_contrast(eff_bg, eff_text) < MIN_CONTRAST and (not is_plain_text or C.is_hex(ct)):
        data["custom_text_color"] = C.contrast_text(eff_bg)
        report["block_contrast_fixed"] += 1

    # 보조 텍스트색(custom_sub_text_color)도 같은 배경 위 — 함께 검사.
    sub_ct = (data.get("custom_sub_text_color") or "").strip()
    if C.is_hex(sub_ct) and C.wcag_contrast(eff_bg, sub_ct) < LARGE_MIN_CONTRAST:
        data["custom_sub_text_color"] = C.contrast_text(eff_bg)
        report["block_contrast_fixed"] += 1

    # 버튼색 대비는 렌더러가 자동(contrast_text(buttonColor))이라 안전 — 손대지 않음.
