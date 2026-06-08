"""result_sanitizer 테스트.

- 가짜/플레이스홀더 URL("#" 등) → 빈 문자열
- 유효 URL 보존 / 스킴 없는 도메인 자동 보정
- social 핸들·색상·ID 등 비-URL 필드는 건드리지 않음
- gallery images 배열에서 잘못된 URL 제거
- 썸네일 없는 그룹링크 grid/carousel → list 강등
"""

from __future__ import annotations

from .services.result_sanitizer import sanitize_result_json


class TestUrlCleaning:
    def test_hash_url_becomes_empty(self):
        data = {"blocks": [{"type": "single_link", "data": {"_type": "single_link", "url": "#"}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == ""

    def test_valid_https_url_preserved(self):
        data = {"blocks": [{"data": {"url": "https://example.com/x"}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == "https://example.com/x"

    def test_bare_domain_gets_https(self):
        data = {"blocks": [{"data": {"url": "example.com/x"}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == "https://example.com/x"

    def test_non_http_scheme_becomes_empty(self):
        data = {"blocks": [{"data": {"url": "javascript:alert(1)"}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == ""

    def test_thumbnail_url_cleaned(self):
        data = {"blocks": [{"data": {"thumbnail_url": "#"}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["thumbnail_url"] == ""

    def test_nested_links_urls_cleaned(self):
        data = {
            "blocks": [
                {
                    "data": {
                        "_type": "group_link",
                        "group_layout": "list",
                        "links": [
                            {"url": "#", "thumbnail_url": "#", "title": "a", "is_enabled": True},
                            {"url": "https://ok.com", "title": "b", "is_enabled": True},
                        ],
                    }
                }
            ]
        }
        out = sanitize_result_json(data)
        links = out["blocks"][0]["data"]["links"]
        assert links[0]["url"] == ""
        assert links[0]["thumbnail_url"] == ""
        assert links[1]["url"] == "https://ok.com"

    def test_empty_string_stays_empty(self):
        data = {"blocks": [{"data": {"url": ""}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == ""

    def test_none_url_untouched(self):
        data = {"blocks": [{"data": {"url": None}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] is None


class TestNonUrlFieldsUntouched:
    def test_social_handles_preserved(self):
        data = {
            "blocks": [
                {
                    "data": {
                        "_type": "social",
                        "instagram": "@myhandle",
                        "phone": "010-1234-5678",
                        "email": "user@example.com",
                    }
                }
            ]
        }
        out = sanitize_result_json(data)
        d = out["blocks"][0]["data"]
        assert d["instagram"] == "@myhandle"
        assert d["phone"] == "010-1234-5678"
        assert d["email"] == "user@example.com"

    def test_colors_and_ids_preserved(self):
        data = {
            "blocks": [
                {
                    "data": {
                        "custom_bg_color": "#F0F9FF",
                        "custom_text_color": "#1F2937",
                        "child_block_ids": [1, 2, 3],
                    }
                }
            ]
        }
        out = sanitize_result_json(data)
        d = out["blocks"][0]["data"]
        assert d["custom_bg_color"] == "#F0F9FF"
        assert d["custom_text_color"] == "#1F2937"
        assert d["child_block_ids"] == [1, 2, 3]


class TestGalleryImages:
    def test_invalid_images_dropped_valid_kept(self):
        data = {
            "blocks": [
                {
                    "data": {
                        "_type": "gallery",
                        "images": ["https://a.com/1.jpg", "#", "", "b.com/2.jpg"],
                    }
                }
            ]
        }
        out = sanitize_result_json(data)
        imgs = out["blocks"][0]["data"]["images"]
        assert imgs == ["https://a.com/1.jpg", "https://b.com/2.jpg"]


class TestGroupLayoutDowngrade:
    def _group(self, layout, links):
        return {
            "blocks": [{"data": {"_type": "group_link", "group_layout": layout, "links": links}}]
        }

    def test_grid3_without_thumbnails_downgrades_to_list(self):
        data = self._group(
            "grid-3",
            [{"url": "https://a.com", "title": "a", "is_enabled": True}],
        )
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["group_layout"] == "list"

    def test_grid3_with_all_thumbnails_stays_grid3(self):
        data = self._group(
            "grid-3",
            [
                {
                    "url": "https://a.com",
                    "thumbnail_url": "https://a.com/t.jpg",
                    "is_enabled": True,
                },
                {
                    "url": "https://b.com",
                    "thumbnail_url": "https://b.com/t.jpg",
                    "is_enabled": True,
                },
            ],
        )
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["group_layout"] == "grid-3"

    def test_carousel_partial_thumbnails_downgrades(self):
        data = self._group(
            "carousel-1",
            [
                {
                    "url": "https://a.com",
                    "thumbnail_url": "https://a.com/t.jpg",
                    "is_enabled": True,
                },
                {"url": "https://b.com", "is_enabled": True},  # 썸네일 없음
            ],
        )
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["group_layout"] == "list"

    def test_list_layout_untouched(self):
        data = self._group("list", [{"url": "https://a.com", "is_enabled": True}])
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["group_layout"] == "list"

    def test_disabled_links_ignored_for_thumb_check(self):
        # 비활성 링크는 렌더되지 않으므로 썸네일 검사에서 제외 → grid 유지.
        data = self._group(
            "grid-2",
            [
                {
                    "url": "https://a.com",
                    "thumbnail_url": "https://a.com/t.jpg",
                    "is_enabled": True,
                },
                {"url": "https://b.com", "is_enabled": False},  # 비활성, 썸네일 없음
            ],
        )
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["group_layout"] == "grid-2"

    def test_hash_thumbnail_triggers_downgrade(self):
        # "#" 썸네일은 정화 후 ""가 되어 → 썸네일 없음 → list 강등.
        data = self._group(
            "grid-3",
            [{"url": "https://a.com", "thumbnail_url": "#", "is_enabled": True}],
        )
        out = sanitize_result_json(data)
        d = out["blocks"][0]["data"]
        assert d["links"][0]["thumbnail_url"] == ""
        assert d["group_layout"] == "list"


class TestRobustness:
    def test_non_dict_returned_as_is(self):
        assert sanitize_result_json(None) is None
        assert sanitize_result_json("x") == "x"

    def test_full_5a430828_repro_all_hash_urls(self):
        # 실제 실패 작업 재현: 모든 링크 url 이 "#" → 모두 "" 가 되어 검증 통과 가능 형태.
        data = {
            "title": "t",
            "blocks": [
                {"type": "single_link", "data": {"_type": "single_link", "url": "#"}},
                {"type": "single_link", "data": {"_type": "single_link", "url": "#"}},
                {
                    "type": "single_link",
                    "data": {
                        "_type": "group_link",
                        "group_layout": "grid-3",
                        "links": [{"url": "#", "title": "c", "is_enabled": True}],
                    },
                },
            ],
        }
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == ""
        assert out["blocks"][1]["data"]["url"] == ""
        assert out["blocks"][2]["data"]["links"][0]["url"] == ""
        assert out["blocks"][2]["data"]["group_layout"] == "list"
