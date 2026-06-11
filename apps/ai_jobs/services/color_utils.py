"""색 유틸 — 결정적 팔레트 추출 + 대비(WCAG) 계산 + 프론트 렌더 색 로직 포팅.

설계 배경
  - VLM 은 이미지에서 **정확한 #hex 를 읽지 못한다**(지각 근사로 drift). 그래서 색은
    여기서 코드로 **결정적 추출**(k-means/median-cut)하고, LLM 은 역할 배정만 한다.
  - 공개 페이지 렌더러(TurnflowLink ``PublicLinkPage.tsx`` / ``useBlockColors.ts``)는
    **텍스트색을 backgroundColor 대비로 자동 결정**하고, 카드 텍스트색은 buttonColor 대비에
    커플링돼 있다. 이 모듈은 그 로직을 그대로 포팅해 "렌더 후 실제로 보일 색"을 예측하고
    대비 미달을 코드로 교정한다(``design_guard``).

순수 함수 only — Django/IO 의존 없음(팔레트 추출만 Pillow 사용). 테스트 용이.
"""

from __future__ import annotations

import colorsys
import io
import re

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# 프론트 기본값 — 이 값들과 동일하게 맞춰 "렌더 후 색"을 예측한다.
DEFAULT_BG = "#F5F5F8"
SLOP_PURPLE = "#8c25f4"  # 프론트 buttonColor 폴백 = 'AI 슬롭' 보라. 모델이 이걸 그대로 두면 교체.
SOFT_BLACK = "#1a1a1a"  # 순수 #000 대신 부드러운 검정(눈 피로 저감).

# ─────────────────────────────────────────────────────────────
# hex <-> rgb
# ─────────────────────────────────────────────────────────────


def is_hex(s: object) -> bool:
    return isinstance(s, str) and bool(_HEX_RE.match(s.strip()))


def parse_hex(s: str) -> tuple[int, int, int] | None:
    """'#rgb' / '#rrggbb' → (r,g,b). 실패 시 None."""
    if not is_hex(s):
        return None
    c = s.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


# ─────────────────────────────────────────────────────────────
# 명도 / 대비
# ─────────────────────────────────────────────────────────────


def luma(rgb: tuple[int, int, int]) -> float:
    """프론트 getContrastText 와 동일 공식 (0~1). 0.5 초과면 '밝음'."""
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def contrast_text(hex_color: str) -> str:
    """프론트 ``getContrastText`` 포팅 — 배경 hex 위에 깔릴 자동 텍스트색."""
    rgb = parse_hex(hex_color)
    if rgb is None:
        return "#000000"
    return "#000000" if luma(rgb) > 0.5 else "#FFFFFF"


def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 상대 휘도."""
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def wcag_contrast(hex1: str, hex2: str) -> float:
    """두 색의 WCAG 대비비 (1.0 ~ 21.0). 파싱 실패 시 1.0(최악)."""
    a, b = parse_hex(hex1), parse_hex(hex2)
    if a is None or b is None:
        return 1.0
    l1, l2 = relative_luminance(a), relative_luminance(b)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ─────────────────────────────────────────────────────────────
# HLS 기반 명도/채도 조정 (sRGB 라운드트립 → 항상 게멋 안)
# ─────────────────────────────────────────────────────────────


def _to_hls(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (v / 255.0 for v in rgb)
    h, lt, s = colorsys.rgb_to_hls(r, g, b)
    return h, lt, s


def _from_hls(h: float, lt: float, s: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, lt)), max(0.0, min(1.0, s)))
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def with_lightness(hex_color: str, lightness: float) -> str:
    """색의 hue/sat 유지하고 명도(L)만 0~1 로 설정."""
    rgb = parse_hex(hex_color)
    if rgb is None:
        return hex_color
    h, _, s = _to_hls(rgb)
    return to_hex(_from_hls(h, lightness, s))


def adjust_lightness(hex_color: str, delta: float) -> str:
    """명도를 delta(+밝게/-어둡게) 만큼 이동."""
    rgb = parse_hex(hex_color)
    if rgb is None:
        return hex_color
    h, lt, s = _to_hls(rgb)
    return to_hex(_from_hls(h, lt + delta, s))


def saturation(hex_color: str) -> float:
    rgb = parse_hex(hex_color)
    if rgb is None:
        return 0.0
    return _to_hls(rgb)[2]


def lightness(hex_color: str) -> float:
    rgb = parse_hex(hex_color)
    if rgb is None:
        return 0.0
    return _to_hls(rgb)[1]


def is_near_gray(hex_color: str, sat_threshold: float = 0.12) -> bool:
    return saturation(hex_color) < sat_threshold


# ─────────────────────────────────────────────────────────────
# 결정적 팔레트 추출 (이미지 바이트 → 후보 색 + 역할 추천)
# ─────────────────────────────────────────────────────────────


def extract_dominant(image_bytes: bytes, k: int = 6) -> list[tuple[str, float]]:
    """이미지에서 지배색 후보를 (hex, 비율) 리스트로. 비율 내림차순.

    Pillow median-cut 양자화 → 썸네일에서 색 빈도 집계. 실패 시 빈 리스트.
    """
    try:
        from PIL import Image
    except ImportError:
        return []
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            im = im.convert("RGB")
            im.thumbnail((96, 96))
            q = im.quantize(colors=max(2, k), method=Image.Quantize.MEDIANCUT)
            palette = q.getpalette() or []
            counts = q.getcolors() or []  # [(count, index), ...]
            total = sum(c for c, _ in counts) or 1
            out: list[tuple[str, float]] = []
            for count, idx in sorted(counts, key=lambda x: -x[0]):
                base = idx * 3
                rgb = (palette[base], palette[base + 1], palette[base + 2])
                out.append((to_hex(rgb), count / total))
            return out
    except Exception:  # noqa: BLE001 — 이미지 깨짐 등은 비치명적
        return []


def build_palette(dominant: list[tuple[str, float]]) -> dict:
    """지배색 후보 → design_settings 역할 색 추천 (결정적).

    반환::
        {"background", "surface", "text", "accent", "brightness",
         "dominant_colors": [...]}

    원칙(연구 기반): 60-30-10 — 밝은 중립 배경(60), 살짝 틴트된 카드(30),
    채도 있는 강조 1개(10). 색 정확도는 dominant 에서만 가져오고 명도/채도는
    HLS 로 안전하게 정규화한다.
    """
    if not dominant:
        return {}
    hexes = [h for h, _ in dominant]

    # 전체 밝기 — 가중 평균 luma
    avg = sum(luma(parse_hex(h) or (0, 0, 0)) * frac for h, frac in dominant)
    brightness = "light" if avg >= 0.5 else "dark"

    # accent: 채도 가장 높은 비-회색 (없으면 가장 지배적인 색)
    colored = [h for h in hexes if not is_near_gray(h)]
    accent = max(colored, key=saturation) if colored else hexes[0]

    # background/surface: accent hue 기반 중립 틴트로 합성 (muddy 방지: 채도 낮게)
    h, _, s = _to_hls(parse_hex(accent) or (0, 0, 0))
    if brightness == "light":
        background = to_hex(_from_hls(h, 0.965, min(s, 0.10)))  # 거의 흰 + 미세 틴트
        surface = to_hex(_from_hls(h, 0.90, min(s, 0.14)))  # 카드: 살짝 어둡게
    else:
        background = to_hex(_from_hls(h, 0.10, min(s, 0.18)))
        surface = to_hex(_from_hls(h, 0.16, min(s, 0.20)))

    # accent 는 너무 어둡/밝으면 버튼 가독성↓ → 중간 명도로 보정, 채도 확보
    al = lightness(accent)
    if al < 0.30:
        accent = with_lightness(accent, 0.42)
    elif al > 0.72:
        accent = with_lightness(accent, 0.55)
    if saturation(accent) < 0.35 and colored:
        ah, _, _ = _to_hls(parse_hex(accent) or (0, 0, 0))
        accent = to_hex(_from_hls(ah, lightness(accent), 0.55))

    text = SOFT_BLACK if brightness == "light" else "#f5f5f5"

    return {
        "background": background,
        "surface": surface,
        "text": text,
        "accent": accent,
        "brightness": brightness,
        "dominant_colors": hexes[:5],
    }


def merge_palettes(palettes: list[dict]) -> dict:
    """여러 이미지의 dominant 를 합쳐 하나의 팔레트 추천으로.

    각 palette 는 build_palette 결과(또는 {'dominant_colors': [...]}). concept 이미지가
    있으면 그 dominant 를 앞에 둬 가중. 가장 많은 후보를 모아 build_palette 재실행.
    """
    agg: list[tuple[str, float]] = []
    for p in palettes:
        for hx in p.get("dominant_colors", []):
            agg.append((hx, 1.0))
    if not agg:
        return {}
    # 중복 hue 근접 제거(간단): 같은 hex 만 dedup
    seen: dict[str, float] = {}
    for hx, w in agg:
        seen[hx] = seen.get(hx, 0.0) + w
    ordered = sorted(seen.items(), key=lambda x: -x[1])
    return build_palette(ordered)
