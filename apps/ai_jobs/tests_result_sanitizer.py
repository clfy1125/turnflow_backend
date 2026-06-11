"""result_sanitizer 테스트.

- 가짜/플레이스홀더 URL("#" 등) → 빈 문자열
- 유효 URL 보존 / 스킴 없는 도메인 자동 보정
- social 핸들·색상·ID 등 비-URL 필드는 건드리지 않음
- gallery images 배열에서 잘못된 URL 제거
- 썸네일 없는 그룹링크 grid/carousel → list 강등
"""

from __future__ import annotations

from .services.result_sanitizer import _LINK_URL_PLACEHOLDER, sanitize_result_json


class TestUrlCleaning:
    def test_hash_url_on_single_link_filled_with_placeholder(self):
        # single_link 의 "#"은 정화로 ""가 되지만, 빈 url single_link 는 프론트가 렌더를
        # 스킵하므로 placeholder 로 채워 최소한 보이게 한다.
        data = {"blocks": [{"type": "single_link", "data": {"_type": "single_link", "url": "#"}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == _LINK_URL_PLACEHOLDER

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
        # _type 없는(순수 single_link 아님) 블록은 채우지 않음.
        data = {"blocks": [{"data": {"url": ""}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == ""

    def test_none_url_untouched(self):
        data = {"blocks": [{"data": {"url": None}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] is None


class TestSingleLinkUrlFill:
    def test_empty_single_link_url_filled(self):
        data = {"blocks": [{"type": "single_link", "data": {"_type": "single_link", "url": ""}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == _LINK_URL_PLACEHOLDER

    def test_missing_single_link_url_filled(self):
        data = {"blocks": [{"type": "single_link", "data": {"_type": "single_link"}}]}
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == _LINK_URL_PLACEHOLDER

    def test_valid_single_link_url_preserved(self):
        data = {
            "blocks": [
                {"type": "single_link", "data": {"_type": "single_link", "url": "https://ok.com"}}
            ]
        }
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["url"] == "https://ok.com"

    def test_non_single_link_subtypes_not_filled(self):
        # social/video/text 등은 url 을 다르게 쓰므로 채우지 않는다.
        # (gallery 는 빈 이미지면 블록째 제거되므로 별도 테스트에서 다룬다.)
        for sub in ("social", "video", "text", "spacer", "map"):
            data = {"blocks": [{"type": "single_link", "data": {"_type": sub}}]}
            out = sanitize_result_json(data)
            assert "url" not in out["blocks"][0]["data"] or not out["blocks"][0]["data"].get("url")


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


class TestTextCap:
    def _text(self, content, headline="", layout="plain"):
        d = {"_type": "text", "content": content, "text_layout": layout}
        if headline:
            d["headline"] = headline
        return {"blocks": [{"type": "single_link", "data": d}]}

    def test_long_text_with_headline_collapsed_to_toggle(self):
        out = sanitize_result_json(self._text("가" * 200, headline="안내"))
        assert out["blocks"][0]["data"]["text_layout"] == "toggle"
        # 내용은 보존(접기만)
        assert len(out["blocks"][0]["data"]["content"]) == 200

    def test_long_text_without_headline_trimmed(self):
        out = sanitize_result_json(self._text("가" * 200))
        c = out["blocks"][0]["data"]["content"]
        assert len(c) < 200 and c.endswith("…")

    def test_short_text_untouched(self):
        out = sanitize_result_json(self._text("짧은 한 줄 소개입니다.", headline="소개"))
        assert out["blocks"][0]["data"]["text_layout"] == "plain"
        assert out["blocks"][0]["data"]["content"] == "짧은 한 줄 소개입니다."

    def test_long_text_ok_preserves_layout(self):
        # 청첩장/커미션: 긴 문단 허용 → 접지도 자르지도 않음.
        out = sanitize_result_json(self._text("가" * 300, headline="인사말"), long_text_ok=True)
        assert out["blocks"][0]["data"]["text_layout"] == "plain"
        assert len(out["blocks"][0]["data"]["content"]) == 300

    def test_subline_capped(self):
        data = {"blocks": [{"type": "profile", "data": {"subline": "소" * 80, "headline": "이름"}}]}
        out = sanitize_result_json(data)
        assert len(out["blocks"][0]["data"]["subline"]) <= 46

    def test_hard_max_even_when_long_ok(self):
        out = sanitize_result_json(self._text("가" * 900, headline="x"), long_text_ok=True)
        assert len(out["blocks"][0]["data"]["content"]) <= 801


class TestVideoDrop:
    def test_video_kept_as_scaffold_when_flag_set(self):
        # 정책 변경(2026-06-11): 새-페이지에서도 video 는 스캐폴드로 유지(URL 정리만).
        data = {
            "blocks": [
                {"type": "single_link", "data": {"_type": "text", "content": "hi"}},
                {
                    "type": "single_link",
                    "data": {
                        "_type": "video",
                        "video_urls": ["https://youtube.com/watch?v=example"],
                    },
                },
            ]
        }
        out = sanitize_result_json(data, drop_fabricated_video=True)
        subs = [(b.get("data") or {}).get("_type") for b in out["blocks"]]
        assert "video" in subs
        assert "text" in subs

    def test_video_kept_by_default(self):
        data = {
            "blocks": [{"type": "single_link", "data": {"_type": "video", "video_urls": ["x"]}}]
        }
        out = sanitize_result_json(data)
        assert (out["blocks"][0]["data"]).get("_type") == "video"


class TestLayoutNormalize:
    def test_gallery_carousel_to_thumbnail(self):
        data = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "gallery",
                        "gallery_layout": "carousel",
                        "images": ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"],
                    },
                }
            ]
        }
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["gallery_layout"] == "thumbnail"

    def test_single_image_gallery_kept(self):
        data = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "gallery",
                        "gallery_layout": "single",
                        "images": ["https://a/1.jpg"],
                    },
                }
            ]
        }
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["gallery_layout"] == "single"

    def test_group_carousel_with_thumbs_to_grid2(self):
        data = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "group_link",
                        "group_layout": "carousel-1",
                        "links": [
                            {
                                "url": "https://a",
                                "thumbnail_url": "https://a/t.jpg",
                                "is_enabled": True,
                            },
                            {
                                "url": "https://b",
                                "thumbnail_url": "https://b/t.jpg",
                                "is_enabled": True,
                            },
                        ],
                    },
                }
            ]
        }
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["group_layout"] == "grid-2"

    def test_group_carousel_without_thumbs_to_list(self):
        data = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "group_link",
                        "group_layout": "carousel-2",
                        "links": [
                            {"url": "https://a", "is_enabled": True},
                        ],
                    },
                }
            ]
        }
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["group_layout"] == "list"

    def test_empty_gallery_dropped(self):
        data = {
            "blocks": [
                {"type": "single_link", "data": {"_type": "text", "content": "x"}},
                {"type": "single_link", "data": {"_type": "gallery", "images": ["", "#"]}},
            ]
        }
        out = sanitize_result_json(data)
        subs = [(b.get("data") or {}).get("_type") for b in out["blocks"]]
        assert "gallery" not in subs and "text" in subs

    def test_gallery_with_valid_image_kept(self):
        data = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {"_type": "gallery", "images": ["https://a/1.jpg", ""]},
                }
            ]
        }
        out = sanitize_result_json(data)
        assert (out["blocks"][0]["data"]).get("_type") == "gallery"

    def test_imageless_large_card_demoted_to_small(self):
        data = {
            "blocks": [
                {
                    "type": "single_link",
                    "data": {
                        "_type": "single_link",
                        "layout": "large",
                        "thumbnail_url": "",
                        "url": "https://x.com",
                    },
                }
            ]
        }
        out = sanitize_result_json(data)
        assert out["blocks"][0]["data"]["layout"] == "small"


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
        # 순수 single_link 두 개는 placeholder 로 채워져 렌더 가능해진다.
        assert out["blocks"][0]["data"]["url"] == _LINK_URL_PLACEHOLDER
        assert out["blocks"][1]["data"]["url"] == _LINK_URL_PLACEHOLDER
        # group_link 내부 링크는 채우지 않음(빈 항목은 렌더 시 자연 처리) → 그대로 "".
        assert out["blocks"][2]["data"]["links"][0]["url"] == ""
        assert out["blocks"][2]["data"]["group_layout"] == "list"


class TestVideoScaffold:
    """video 블록은 스캐폴드로 **유지**한다(2026-06-11 정책: 유저가 자기 영상으로 교체)."""

    def _video(self, urls):
        return {"type": "single_link", "data": {"_type": "video", "video_urls": urls}}

    def test_concept_video_url_promoted_first(self):
        from .services.result_sanitizer import extract_video_urls

        concept = "내 채널 https://youtube.com/watch?v=abc123 영상을 보여줘"
        allowed = extract_video_urls(concept)
        data = {"blocks": [self._video(["https://youtube.com/watch?v=fake99"])]}
        out = sanitize_result_json(data, drop_fabricated_video=True, allowed_video_urls=allowed)
        urls = out["blocks"][0]["data"]["video_urls"]
        # 컨셉의 진짜 URL 이 맨 앞, 모델 placeholder 는 뒤에 보존(교체용 자리).
        assert urls[0] == "https://youtube.com/watch?v=abc123"
        assert "https://youtube.com/watch?v=fake99" in urls

    def test_fabricated_video_kept_as_scaffold(self):
        data = {"blocks": [self._video(["https://youtube.com/watch?v=hallucinated"])]}
        out = sanitize_result_json(data, drop_fabricated_video=True, allowed_video_urls=set())
        assert len(out["blocks"]) == 1  # 더 이상 드롭하지 않는다

    def test_invalid_scheme_url_removed(self):
        data = {"blocks": [self._video(["#", "javascript:x", "https://youtu.be/ok1"])]}
        out = sanitize_result_json(data, drop_fabricated_video=True, allowed_video_urls=set())
        assert out["blocks"][0]["data"]["video_urls"] == ["https://youtu.be/ok1"]

    def test_video_with_no_valid_urls_dropped(self):
        data = {"blocks": [self._video(["#"])]}
        out = sanitize_result_json(data, drop_fabricated_video=True, allowed_video_urls=set())
        assert out["blocks"] == []

    def test_remake_videos_untouched(self):
        data = {"blocks": [self._video(["https://youtu.be/user-real"])]}
        out = sanitize_result_json(data, drop_fabricated_video=False)
        assert len(out["blocks"]) == 1
