"""image_guard 테스트 — 빈 이미지 슬롯에 {{image:}} 주입 + 히어로 레이아웃 다양성."""

from __future__ import annotations

from . import services as _  # noqa: F401
from .services.image_guard import ensure_image_placeholders


def _profile(layout, **kw):
    return {"type": "profile", "data": {"profile_layout": layout, **kw}}


def _is_ph(v) -> bool:
    return isinstance(v, str) and v.startswith("{{image:") and v.endswith("}}")


class TestProfileHero:
    def test_cover_category_fills_empty_cover(self):
        # rental = cover 전략
        r = {"blocks": [_profile("cover_bg", cover_image_url="")]}
        out = ensure_image_placeholders(r, "rental")
        d = out["blocks"][0]["data"]
        assert d["profile_layout"] == "cover_bg"
        assert _is_ph(d["cover_image_url"])

    def test_cover_category_forces_cover_from_center(self):
        r = {"blocks": [_profile("center", cover_image_url="")]}
        out = ensure_image_placeholders(r, "portfolio")
        d = out["blocks"][0]["data"]
        assert d["profile_layout"] == "cover_bg"
        assert _is_ph(d["cover_image_url"])

    def test_avatar_category_downgrades_cover_to_center(self):
        # bizcard = avatar 전략 → cover_bg 남발 방지
        r = {"blocks": [_profile("cover_bg", cover_image_url="", avatar_url="")]}
        out = ensure_image_placeholders(r, "bizcard")
        d = out["blocks"][0]["data"]
        assert d["profile_layout"] == "center"
        assert "cover_image_url" not in d
        assert _is_ph(d["avatar_url"])

    def test_avatar_category_keeps_left_layout(self):
        r = {"blocks": [_profile("left", avatar_url="")]}
        out = ensure_image_placeholders(r, "profile")
        d = out["blocks"][0]["data"]
        assert d["profile_layout"] == "left"
        assert _is_ph(d["avatar_url"])

    def test_existing_cover_image_preserved(self):
        r = {"blocks": [_profile("cover_bg", cover_image_url="https://r2/keep.jpg")]}
        out = ensure_image_placeholders(r, "rental")
        assert out["blocks"][0]["data"]["cover_image_url"] == "https://r2/keep.jpg"


class TestBlockImages:
    def test_empty_gallery_filled(self):
        r = {"blocks": [{"type": "single_link", "data": {"_type": "gallery", "images": []}}]}
        out = ensure_image_placeholders(r, "portfolio")
        imgs = out["blocks"][0]["data"]["images"]
        assert len(imgs) == 4 and all(_is_ph(x) for x in imgs)
        # 키워드가 서로 달라야 중복 이미지를 피함
        assert len(set(imgs)) == 4

    def test_gallery_with_some_real_images_topped_up(self):
        r = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {"_type": "gallery", "images": ["https://r2/a.jpg"]},
                }
            ]
        }
        out = ensure_image_placeholders(r, "portfolio")
        imgs = out["blocks"][0]["data"]["images"]
        assert imgs[0] == "https://r2/a.jpg"
        assert len(imgs) == 4

    def test_grid_group_link_empty_thumbs_filled(self):
        r = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "group_link",
                        "group_layout": "grid-2",
                        "links": [
                            {"url": "https://a.com", "is_enabled": True},
                            {"url": "https://b.com", "thumbnail_url": "", "is_enabled": True},
                        ],
                    },
                }
            ]
        }
        out = ensure_image_placeholders(r, "groupbuy")
        links = out["blocks"][0]["data"]["links"]
        assert _is_ph(links[0]["thumbnail_url"])
        assert _is_ph(links[1]["thumbnail_url"])

    def test_list_group_link_filled(self):
        # list 레이아웃도 좌측 48px 썸네일을 렌더하므로 채운다(사용자 피드백: 사진 빠짐=심각).
        r = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "group_link",
                        "group_layout": "list",
                        "links": [{"url": "https://a.com", "is_enabled": True}],
                    },
                }
            ]
        }
        out = ensure_image_placeholders(r, "bizcard")
        assert _is_ph(out["blocks"][0]["data"]["links"][0]["thumbnail_url"])

    def test_review_group_link_not_filled(self):
        # 후기 리스트(제목에 별점)는 텍스트가 자연스럽다 — 썸네일 채우지 않음.
        r = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "group_link",
                        "group_layout": "list",
                        "links": [
                            {"url": "https://a.com", "title": "김OO ★★★★★", "is_enabled": True},
                            {"url": "https://b.com", "title": "이OO ★★★★☆", "is_enabled": True},
                        ],
                    },
                }
            ]
        }
        out = ensure_image_placeholders(r, "groupbuy")
        for ln in out["blocks"][0]["data"]["links"]:
            assert "thumbnail_url" not in ln

    def test_showcase_single_link_thumb_filled(self):
        r = {
            "blocks": [{"type": "single_link", "data": {"_type": "single_link", "layout": "large"}}]
        }
        out = ensure_image_placeholders(r, "groupbuy")
        assert _is_ph(out["blocks"][0]["data"]["thumbnail_url"])

    def test_small_single_link_thumb_not_filled(self):
        r = {
            "blocks": [{"type": "single_link", "data": {"_type": "single_link", "layout": "small"}}]
        }
        out = ensure_image_placeholders(r, "groupbuy")
        assert "thumbnail_url" not in out["blocks"][0]["data"]


class TestRobustness:
    def test_non_dict_safe(self):
        assert ensure_image_placeholders(None, "rental") is None
        assert ensure_image_placeholders({"blocks": "x"}, "rental") == {"blocks": "x"}

    def test_unknown_category_uses_generic(self):
        r = {"blocks": [_profile("cover_bg", cover_image_url="")]}
        out = ensure_image_placeholders(r, "nonexistent")
        # generic = avatar 전략 → center 로
        assert out["blocks"][0]["data"]["profile_layout"] == "center"
