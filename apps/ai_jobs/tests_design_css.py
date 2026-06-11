"""design_css 테스트 — 디자인 킷 생성 + page custom_css 병합."""

from __future__ import annotations

from . import services as _  # noqa: F401
from .services import category_profiles as CP
from .services.design_css import _MARKER, build_design_css, enhance_page_css


class TestBuildCss:
    def test_contains_marker_and_selectors(self):
        css = build_design_css(accent="#FF6B35", background="#FFF7F0", category=CP.GROUPBUY)
        assert _MARKER in css
        assert '.block-link[data-block-type="single_link"]' in css
        assert '.block-link[data-block-type="group_link"]' in css
        assert "@keyframes tfUp" in css
        assert "#FF6B35" in css  # accent 좌측 바

    def test_variant_radius_differs(self):
        soft = build_design_css(accent="#c9a96e", background="#fff", category=CP.INVITATION)
        editorial = build_design_css(accent="#111", background="#fff", category=CP.PORTFOLIO)
        assert "border-radius:22px" in soft
        assert "border-radius:8px" in editorial

    def test_dark_background_uses_glow(self):
        css = build_design_css(accent="#c9a96e", background="#0a0a0a", category=CP.PORTFOLIO)
        assert "rgba(0,0,0,.55)" in css  # 다크용 글로우
        assert "rgba(255,255,255,.08)" in css  # 밝은 헤어라인

    def test_invalid_accent_falls_back(self):
        css = build_design_css(accent="", background="#fff", category=CP.BIZCARD)
        assert "#111827" in css


class TestEnhance:
    def _r(self, css=""):
        return {
            "data": {"design_settings": {"buttonColor": "#FF6B35", "backgroundColor": "#FFF7F0"}},
            "custom_css": css,
        }

    def test_appends_kit_and_preserves_body(self):
        r = self._r("body{background:#FFF7F0;}")
        out = enhance_page_css(r, CP.GROUPBUY)
        css = out["data"]["custom_css"]
        assert "body{background:#FFF7F0;}" in css
        assert _MARKER in css
        assert out["custom_css"] == css

    def test_idempotent(self):
        r = self._r("body{background:#fff;}")
        once = enhance_page_css(r, CP.GROUPBUY)["data"]["custom_css"]
        twice = enhance_page_css(r, CP.GROUPBUY)["data"]["custom_css"]
        assert once == twice
        assert twice.count(_MARKER) == 1

    def test_empty_existing_css(self):
        r = {"data": {"design_settings": {"buttonColor": "#2563EB", "backgroundColor": "#fff"}}}
        out = enhance_page_css(r, CP.LANDING)
        assert _MARKER in out["custom_css"]

    def test_non_dict_safe(self):
        assert enhance_page_css(None, CP.GROUPBUY) is None
