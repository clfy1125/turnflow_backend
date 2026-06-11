"""color_utils 테스트 — hex 파싱 / 명도·대비 / 프론트 색 로직 포팅 / 팔레트 추출."""

from __future__ import annotations

import io

from . import services as _  # noqa: F401  (앱 로드 보장)
from .services import color_utils as C


class TestHex:
    def test_parse_and_to_hex_roundtrip(self):
        assert C.parse_hex("#ffffff") == (255, 255, 255)
        assert C.parse_hex("#000") == (0, 0, 0)
        assert C.parse_hex("#1A2B3C") == (26, 43, 60)
        assert C.to_hex((26, 43, 60)) == "#1a2b3c"

    def test_invalid_hex(self):
        assert C.parse_hex("nope") is None
        assert C.parse_hex("") is None
        assert not C.is_hex("rgb(0,0,0)")
        assert C.is_hex("#abc")


class TestContrast:
    def test_contrast_text_matches_frontend_luma(self):
        # 밝은 배경 → 검정, 어두운 배경 → 흰색 (프론트 getContrastText 와 동일)
        assert C.contrast_text("#ffffff") == "#000000"
        assert C.contrast_text("#000000") == "#FFFFFF"
        assert C.contrast_text("#0b0b14") == "#FFFFFF"
        assert C.contrast_text("#fdfbf7") == "#000000"

    def test_wcag_contrast_known_pairs(self):
        # 흑백 = 21:1
        assert round(C.wcag_contrast("#000000", "#ffffff"), 1) == 21.0
        # 같은 색 = 1:1
        assert round(C.wcag_contrast("#777777", "#777777"), 2) == 1.0
        # 저대비 회색쌍은 4.5 미만
        assert C.wcag_contrast("#888888", "#777777") < 4.5


class TestLightness:
    def test_with_lightness_changes_only_l(self):
        light = C.with_lightness("#3b82f6", 0.95)
        assert C.lightness(light) > 0.9
        # hue 유지 — 여전히 푸른 계열(대략)
        r, g, b = C.parse_hex(light)
        assert b >= r

    def test_adjust_lightness_clamps(self):
        # 이미 매우 밝은 색을 더 밝게 → 흰색 근처, 깨지지 않음
        out = C.adjust_lightness("#fafafa", 0.5)
        assert C.is_hex(out)

    def test_near_gray_detection(self):
        assert C.is_near_gray("#808080")
        assert not C.is_near_gray("#3b82f6")


class TestPaletteExtraction:
    def _solid(self, rgb, size=64):
        from PIL import Image

        im = Image.new("RGB", (size, size), rgb)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()

    def _two_tone(self, top, bottom, size=64):
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (size, size), top)
        d = ImageDraw.Draw(im)
        d.rectangle((0, size // 2, size, size), fill=bottom)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()

    def test_extract_dominant_solid(self):
        dom = C.extract_dominant(self._solid((201, 105, 122)), k=4)
        assert dom
        top_hex, frac = dom[0]
        assert C.is_hex(top_hex)
        # 단색이면 첫 색이 압도적
        assert frac > 0.8

    def test_build_palette_roles_present(self):
        dom = C.extract_dominant(self._two_tone((253, 251, 247), (166, 124, 82)), k=5)
        pal = C.build_palette(dom)
        for key in ("background", "surface", "text", "accent", "brightness"):
            assert key in pal
        assert C.is_hex(pal["background"])
        assert C.is_hex(pal["accent"])
        # 밝은 베이지 위주 → light
        assert pal["brightness"] == "light"
        # 배경은 매우 밝게 정규화
        assert C.lightness(pal["background"]) > 0.85

    def test_extract_dominant_bad_bytes(self):
        assert C.extract_dominant(b"not an image") == []


class TestReconcilePalette:
    def test_mockup_misclassification_fixed(self):
        # 2026-06-11 실사고: 시안 스크린샷에서 k-means 가 흰 카드를 background 로 오분류.
        # VLM 은 역할을 맞게 봤지만 hex 가 drift → VLM 역할 + 픽셀 최근접 스냅으로 보정.
        from .services.color_utils import reconcile_palette

        det = {
            "background": "#f5f5f7",  # 오분류(흰 카드)
            "accent": "#3b18e0",
            "brightness": "light",
            "dominant_colors": ["#3b18e0", "#2b159e", "#b0bb80", "#6b718d", "#553daf"],
        }
        vlm = {
            "background": "#4a2fd9",  # VLM: 배경은 딥 바이올렛 (hex 는 근사치)
            "accent": "#e8ff2a",  # VLM: 네온 옐로 포인트
            "brightness": "dark",
        }
        out = reconcile_palette(vlm, det)
        # 배경: VLM 역할 판단 → 픽셀 풀 최근접(#3b18e0 또는 #553daf 계열 보라)으로 스냅
        assert out["background"] in ("#3b18e0", "#553daf", "#2b159e")
        assert out["brightness"] == "dark"
        # 노랑은 풀에 없음(면적 작아 클러스터 미포함) → VLM hex 유지
        assert out["accent"] == "#e8ff2a"

    def test_empty_vlm_falls_back_to_det(self):
        from .services.color_utils import reconcile_palette

        det = {"background": "#111111", "dominant_colors": ["#111111"]}
        assert reconcile_palette({}, det) == det

    def test_empty_det_uses_vlm_as_is(self):
        from .services.color_utils import reconcile_palette

        out = reconcile_palette({"background": "#0b132b", "brightness": "dark"}, {})
        assert out["background"] == "#0b132b" and out["brightness"] == "dark"
