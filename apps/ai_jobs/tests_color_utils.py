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
