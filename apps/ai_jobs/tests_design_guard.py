"""design_guard 테스트 — 슬롭색 교체 · 대비 보정 · muddy 방지 · 블록 색 교정."""

from __future__ import annotations

from . import services as _  # noqa: F401
from .services import color_utils as C
from .services.design_guard import enforce_compact_links, enforce_design_quality


class TestCompactLinks:
    def _link(self, layout, label="버튼", url="https://x.com"):
        return {
            "type": "single_link",
            "data": {"_type": "single_link", "layout": layout, "label": label, "url": url},
        }

    def test_primary_cta_promoted_to_standard(self):
        # 주요 전환 CTA(카톡 상담)는 첫 1개가 medium(스탠다드)로 — large 면 강등, small 이면 승격.
        r = {"blocks": [self._link("large", label="카카오톡 상담", url="https://pf.kakao.com/_x")]}
        out = enforce_compact_links(r)
        assert out["blocks"][0]["data"]["layout"] == "medium"

    def test_secondary_contact_forced_small(self):
        # 첫 전환 CTA 이후의 연락/예약 류 보조 버튼은 small.
        r = {
            "blocks": [
                self._link("medium", label="무료체험 시작", url="https://x.com"),
                self._link("medium", label="네이버 예약하기", url="https://naver.me/x"),
            ]
        }
        out = enforce_compact_links(r)
        assert out["blocks"][0]["data"]["layout"] == "medium"  # 첫 CTA = 스탠다드
        assert out["blocks"][1]["data"]["layout"] == "small"  # 보조 = 컴팩트

    def test_only_one_showcase_kept(self):
        r = {
            "blocks": [
                self._link("large", label="대표 상품 A"),
                self._link("large", label="상품 B"),
                self._link("medium", label="상품 C"),
            ]
        }
        out = enforce_compact_links(r)
        layouts = [b["data"]["layout"] for b in out["blocks"]]
        assert layouts[0] == "large"  # 첫 쇼케이스 유지
        assert layouts[1] == "small" and layouts[2] == "small"  # 나머지 강등

    def test_group_link_untouched(self):
        r = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {"_type": "group_link", "group_layout": "grid-2", "links": []},
                }
            ]
        }
        out = enforce_compact_links(r)
        assert out["blocks"][0]["data"]["group_layout"] == "grid-2"

    def test_small_links_untouched(self):
        r = {"blocks": [self._link("small", label="상품 A"), self._link("small", label="상품 B")]}
        out = enforce_compact_links(r)
        assert all(b["data"]["layout"] == "small" for b in out["blocks"])


def _ds(result):
    return result["data"]["design_settings"]


class TestSlopColor:
    def test_slop_purple_replaced_with_palette_accent(self):
        r = {"data": {"design_settings": {"backgroundColor": "#ffffff", "buttonColor": "#8c25f4"}}}
        out = enforce_design_quality(r, palette={"accent": "#c9697a"})
        assert _ds(out)["buttonColor"] == "#c9697a"

    def test_missing_button_color_filled(self):
        r = {"data": {"design_settings": {"backgroundColor": "#ffffff"}}}
        out = enforce_design_quality(r, palette={"accent": "#2563eb"})
        assert _ds(out)["buttonColor"] == "#2563eb"

    def test_valid_button_color_preserved(self):
        r = {"data": {"design_settings": {"backgroundColor": "#fff", "buttonColor": "#0a7d4b"}}}
        out = enforce_design_quality(r, palette={"accent": "#c9697a"})
        assert _ds(out)["buttonColor"] == "#0a7d4b"


class TestFrameAndSpread:
    def test_frame_bg_filled_from_background(self):
        r = {"data": {"design_settings": {"backgroundColor": "#101018", "buttonColor": "#3b82f6"}}}
        out = enforce_design_quality(r)
        assert _ds(out)["frameBackgroundColor"] == "#101018"

    def test_muddy_bg_card_spread_fixed(self):
        # 배경과 카드가 거의 같은 명도 → 카드 명도 조정으로 분리
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#f3f1ec",
                    "blockBgColor": "#f2f0eb",  # 거의 동일
                    "buttonColor": "#a67c52",
                }
            }
        }
        out = enforce_design_quality(r)
        bg = _ds(out)["backgroundColor"]
        card = _ds(out)["blockBgColor"]
        assert abs(C.lightness(card) - C.lightness(bg)) >= 0.05 - 1e-6 or card != "#f2f0eb"


class TestPageTextContrast:
    def test_mid_tone_background_pushed_for_contrast(self):
        # 중간톤 회색 배경은 자동 텍스트와 대비가 약함 → 명도를 밀어 보정
        r = {"data": {"design_settings": {"backgroundColor": "#7a7a7a", "buttonColor": "#222"}}}
        out = enforce_design_quality(r)
        bg = _ds(out)["backgroundColor"]
        txt = "#FFFFFF" if C.contrast_text(bg) == "#FFFFFF" else "#0f172a"
        assert C.wcag_contrast(bg, txt) >= 4.5


class TestBlockContrast:
    def test_dark_custom_card_with_inherited_dark_text_fixed(self):
        # blockBgColor 라이트(→기본 카드글씨 어두움)인데 한 블록만 custom_bg 다크 +
        # custom_text 미지정 → 어두운 카드에 어두운 글씨가 상속됨. 가드가 글씨를 흰색으로.
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#ffffff",
                    "blockBgColor": "#ffffff",
                    "buttonColor": "#111827",
                }
            },
            "blocks": [
                {"type": "single_link", "data": {"_type": "text", "custom_bg_color": "#1f2430"}},
            ],
        }
        out = enforce_design_quality(r)
        bd = out["blocks"][0]["data"]
        # 글씨색이 카드(#1f2430) 대비 충분해야 함
        eff_text = bd.get("custom_text_color") or "#111827"
        assert C.wcag_contrast("#1f2430", eff_text) >= 4.5

    def test_pure_black_text_softened(self):
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#ffffff",
                    "blockBgColor": "#ffffff",
                    "buttonColor": "#111827",
                }
            },
            "blocks": [
                {"type": "single_link", "data": {"_type": "text", "custom_text_color": "#000000"}},
            ],
        }
        out = enforce_design_quality(r)
        assert out["blocks"][0]["data"]["custom_text_color"].lower() == C.SOFT_BLACK

    def test_good_block_colors_untouched(self):
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#ffffff",
                    "blockBgColor": "#ffffff",
                    "buttonColor": "#2563eb",
                }
            },
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "single_link",
                        "custom_bg_color": "#ffffff",
                        "custom_text_color": "#1a1a1a",
                    },
                },
            ],
        }
        out = enforce_design_quality(r)
        assert out["blocks"][0]["data"]["custom_text_color"] == "#1a1a1a"


class TestHeroFix:
    def _page(self, profile_data, extra_blocks):
        return {
            "data": {"design_settings": {"backgroundColor": "#ffffff", "buttonColor": "#2563eb"}},
            "blocks": [{"type": "profile", "data": profile_data}, *extra_blocks],
        }

    def test_empty_cover_promotes_gallery_image(self):
        r = self._page(
            {"profile_layout": "cover_bg", "cover_image_url": "", "headline": "X"},
            [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "gallery",
                        "images": ["https://r2/g1.jpg", "https://r2/g2.jpg"],
                    },
                }
            ],
        )
        out = enforce_design_quality(r)
        assert out["blocks"][0]["data"]["cover_image_url"] == "https://r2/g1.jpg"
        assert out["blocks"][0]["data"]["profile_layout"] == "cover_bg"

    def test_empty_cover_promotes_thumbnail(self):
        r = self._page(
            {"profile_layout": "cover", "cover_image_url": "", "headline": "X"},
            [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "single_link",
                        "thumbnail_url": "https://r2/t.jpg",
                        "url": "https://ok.com",
                    },
                }
            ],
        )
        out = enforce_design_quality(r)
        assert out["blocks"][0]["data"]["cover_image_url"] == "https://r2/t.jpg"

    def test_no_image_downgrades_to_center(self):
        r = self._page(
            {"profile_layout": "cover_bg", "cover_image_url": "", "headline": "X"},
            [{"type": "single_link", "data": {"_type": "text", "content": "hi"}}],
        )
        out = enforce_design_quality(r)
        assert out["blocks"][0]["data"]["profile_layout"] == "center"

    def test_existing_cover_untouched(self):
        r = self._page(
            {"profile_layout": "cover_bg", "cover_image_url": "https://r2/keep.jpg"},
            [{"type": "single_link", "data": {"_type": "gallery", "images": ["https://r2/g.jpg"]}}],
        )
        out = enforce_design_quality(r)
        assert out["blocks"][0]["data"]["cover_image_url"] == "https://r2/keep.jpg"

    def test_non_cover_layout_ignored(self):
        r = self._page(
            {"profile_layout": "center", "cover_image_url": ""},
            [{"type": "single_link", "data": {"_type": "gallery", "images": ["https://r2/g.jpg"]}}],
        )
        out = enforce_design_quality(r)
        assert not out["blocks"][0]["data"].get("cover_image_url")


class TestRobustness:
    def test_no_design_settings_no_crash(self):
        assert enforce_design_quality({"blocks": []}) == {"blocks": []}
        assert enforce_design_quality(None) is None


class TestPlainTextPageBgContrast:
    def test_plain_text_white_on_light_page_bg_fixed(self):
        # 리메이크 1회차 실사고: 베이지 페이지 배경 + plain 텍스트 흰 글씨 → 안 보임.
        # plain 은 카드 없이 페이지 배경 위에 직접 렌더되므로 page bg 기준으로 보정해야 한다.
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#f5f0eb",
                    "blockBgColor": "#ffffff",
                    "buttonColor": "#8b5e3c",
                }
            },
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "text",
                        "text_layout": "plain",
                        "custom_text_color": "#ffffff",
                        "content": "본문",
                    },
                }
            ],
        }
        out = enforce_design_quality(r)
        fixed = out["blocks"][0]["data"]["custom_text_color"]
        assert C.wcag_contrast("#f5f0eb", fixed) >= 4.5

    def test_boxed_text_still_checked_against_card(self):
        # 명시 plain 이 아니면(default boxed) 기존처럼 카드 배경 기준.
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#ffffff",
                    "blockBgColor": "#ffffff",
                    "buttonColor": "#111827",
                }
            },
            "blocks": [
                {"type": "single_link", "data": {"_type": "text", "custom_bg_color": "#1f2430"}}
            ],
        }
        out = enforce_design_quality(r)
        bd = out["blocks"][0]["data"]
        eff_text = bd.get("custom_text_color") or "#111827"
        assert C.wcag_contrast("#1f2430", eff_text) >= 4.5


class TestPalettePin:
    def test_model_drift_snapped_back(self):
        # 시안 로열바이올렛(#3b18e0)인데 모델이 검은 네이비로 민 사고(2026-06-11) — 핀으로 복원.
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#1A1A2E",
                    "blockBgColor": "#4f2ee9",
                    "buttonColor": "#FFD700",
                }
            },
            "blocks": [],
        }
        pal = {"background": "#3b18e0", "surface": "#3b18e0", "accent": "#FFD700"}
        out = enforce_design_quality(r, palette=pal, pin_palette=True)
        ds = out["data"]["design_settings"]
        assert ds["backgroundColor"] == "#3b18e0"
        assert ds["frameBackgroundColor"] == "#3b18e0"
        assert ds["buttonColor"] == "#FFD700"

    def test_pin_off_keeps_model_colors(self):
        r = {
            "data": {
                "design_settings": {
                    "backgroundColor": "#1A1A2E",
                    "buttonColor": "#FFD700",
                }
            },
            "blocks": [],
        }
        out = enforce_design_quality(r, palette={"background": "#3b18e0"}, pin_palette=False)
        assert out["data"]["design_settings"]["backgroundColor"] == "#1A1A2E"
