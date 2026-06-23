"""design_css 테스트 — 디자인 킷 생성 + page custom_css 병합."""

from __future__ import annotations

from . import services as _  # noqa: F401
from .services import category_profiles as CP
from .services.design_css import (
    _MARKER,
    _resolve_variant,
    _signature_background_css,
    build_design_css,
    enhance_page_css,
)


class TestBuildCss:
    def test_contains_marker_and_selectors(self):
        css = build_design_css(accent="#FF6B35", background="#FFF7F0", category=CP.GROUPBUY)
        assert _MARKER in css
        assert '.block-link[data-block-type="single_link"]' in css
        assert '.block-link[data-block-type="group_link"]' in css
        assert "@keyframes tfUp" in css
        assert "#FF6B35" in css  # accent 좌측 바

    def test_variant_radius_differs(self):
        import re

        def card_radius(css: str) -> int:
            m = re.search(
                r'data-block-type="single_link"\] > a,[^{]*\{\s*border-radius:(\d+)px', css
            )
            return int(m.group(1)) if m else -1

        # 같은 시드에서 invitation=soft(라운드 큼) > portfolio=editorial(라운드 작음).
        # (시드 지터로 절대값은 변하지만 variant 간 순서는 유지된다.)
        soft = build_design_css(accent="#c9a96e", background="#fff", category=CP.INVITATION, seed=0)
        editorial = build_design_css(
            accent="#111", background="#fff", category=CP.PORTFOLIO, seed=0
        )
        assert card_radius(soft) > card_radius(editorial)

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


class TestSeededVariety:
    """같은 카테고리도 시드에 따라 variant/장식/카드 기하가 달라진다(품질 풀 안에서)."""

    def test_seed_changes_output(self):
        a = build_design_css(accent="#2563EB", background="#fff", category=CP.PROFILE, seed=0)
        b = build_design_css(accent="#2563EB", background="#fff", category=CP.PROFILE, seed=1)
        assert a != b  # 시드만 달라도 CSS 가 달라짐
        assert _MARKER in a and _MARKER in b
        assert "#2563EB" in a and "#2563EB" in b  # accent 는 유지(가드 색)

    def test_commission_always_outline(self):
        # outline 싱글톤 — 모든 시드에서 잉크 보더 정체성 유지.
        for s in range(20):
            assert _resolve_variant(CP.COMMISSION, s) == "outline"
            css = build_design_css(accent="#111", background="#fff", category=CP.COMMISSION, seed=s)
            assert "3px 3px 0" in css  # 카툰 오프셋 하드섀도

    def test_invitation_variant_pool_bounded(self):
        # 청첩장은 우아한 soft/editorial 만 — bold/outline 로 새지 않는다.
        for s in range(20):
            assert _resolve_variant(CP.INVITATION, s) in ("soft", "editorial")

    def test_card_radius_clamped(self):
        import re

        for s in range(30):
            css = build_design_css(accent="#111", background="#fff", category=CP.PROFILE, seed=s)
            for m in re.finditer(r"border-radius:(\d+)px", css):
                assert 4 <= int(m.group(1)) <= 26

    def test_seeded_idempotent(self):
        r = {
            "data": {"design_settings": {"buttonColor": "#FF6B35", "backgroundColor": "#FFF7F0"}},
            "custom_css": "body{background:#FFF7F0;}",
        }
        once = enhance_page_css(r, CP.GROUPBUY, seed=7)["data"]["custom_css"]
        twice = enhance_page_css(r, CP.GROUPBUY, seed=7)["data"]["custom_css"]
        assert once == twice
        assert twice.count(_MARKER) == 1

    def test_font_fallback_when_empty(self):
        r = {"data": {"design_settings": {"buttonColor": "#111", "backgroundColor": "#fff"}}}
        out = enhance_page_css(r, CP.INVITATION, seed=0)
        font = out["data"]["design_settings"].get("fontFamily")
        assert font in CP._FONT_WHITELIST

    def test_font_not_overridden_when_set(self):
        r = {
            "data": {
                "design_settings": {
                    "buttonColor": "#111",
                    "backgroundColor": "#fff",
                    "fontFamily": "Pretendard",
                }
            }
        }
        out = enhance_page_css(r, CP.INVITATION, seed=0)
        assert out["data"]["design_settings"]["fontFamily"] == "Pretendard"


class TestSignatureModules:
    """플레이북 하이브리드: 배경 시그니처(메시/스타필드) + bold 하드섀도.

    검증된 훅(.page-container / [data-block-type])만 쓰고, 플레이북의 미검증 훅
    (.mt-6.space-y-3 / div:first-of-type h2)은 새지 않는다.
    """

    def test_signature_helper_light_dark_and_exclusions(self):
        # 라이트 → 메시 그라데(.page-container 배경) + 콘텐츠 z-index 가드
        light = _signature_background_css("soft", False, "#7C5CFF", "#F6F3FF", 0)
        assert ".page-container{ background:radial-gradient" in light
        assert ".page-container > *{ position:relative; z-index:1; }" in light
        assert "tf-tw" not in light
        # 다크 → 스타필드
        dark = _signature_background_css("clean", True, "#3B82F6", "#0B0F1A", 0)
        assert "tf-tw" in dark and ".page-container::after" in dark
        # editorial(미니멀)·outline(카툰)은 제외 → 빈 문자열
        assert _signature_background_css("editorial", False, "#C9A24B", "#FBF3EC", 0) == ""
        assert _signature_background_css("outline", False, "#A8552A", "#EBE3D4", 0) == ""

    def test_bold_variant_hard_shadow(self):
        assert _resolve_variant(CP.GROUPBUY, 0) == "bold"
        css = build_design_css(accent="#16E07A", background="#EFEFE7", category=CP.GROUPBUY, seed=0)
        assert "6px 6px 0" in css  # 하드 오프셋 섀도
        assert "translate(-2px,-2px)" in css  # hover

    def test_no_unverified_playbook_hooks_leak(self):
        # 다크 랜딩(스타필드 포함)에도 미검증 훅이 새지 않아야 한다.
        css = build_design_css(accent="#3B82F6", background="#0B0F1A", category=CP.LANDING, seed=0)
        assert ".mt-6.space-y-3" not in css
        assert "div:first-of-type h2" not in css

    def test_signature_idempotent_via_enhance(self):
        r = {"data": {"design_settings": {"buttonColor": "#7C5CFF", "backgroundColor": "#F6F3FF"}}}
        once = enhance_page_css(r, CP.PROFILE, seed=1)["custom_css"]
        twice = enhance_page_css(r, CP.PROFILE, seed=1)["custom_css"]
        assert once == twice
        assert twice.count(_MARKER) == 1
