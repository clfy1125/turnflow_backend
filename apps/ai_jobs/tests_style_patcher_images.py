"""style_patcher — 리메이크 ``_new`` 블록의 이미지 placeholder 통과 테스트.

리뉴얼에 첨부한 사용자 이미지({{user_image:N}})와 검색 placeholder({{image:키워드}})는
새 블록의 이미지 슬롯으로 통과시키되, 환각 raw URL 은 여전히 차단한다.
"""

from __future__ import annotations

from .services.style_patcher import merge_full_restyle

_META = {"title": "t", "is_public": True, "data": {}, "custom_css": ""}


def _merge(new_block_data: dict, subtype: str = "gallery") -> dict:
    res = merge_full_restyle(
        existing_page_meta=_META,
        existing_blocks=[],
        llm_response={
            "page": {},
            "blocks": [{"_new": True, "_type": subtype, "order": 1, "data": new_block_data}],
        },
        preserve_content=False,
    )
    blocks = res.get("blocks") or []
    return (blocks[0].get("data") or {}) if blocks else {}


class TestNewBlockImagePlaceholders:
    def test_user_image_placeholder_passes_in_gallery(self):
        d = _merge(
            {
                "_type": "gallery",
                "images": ["{{user_image:1}}", "{{user_image:2}}"],
                "gallery_layout": "thumbnail",
            }
        )
        assert d.get("images") == ["{{user_image:1}}", "{{user_image:2}}"]
        assert d.get("gallery_layout") == "thumbnail"

    def test_pixabay_placeholder_passes(self):
        d = _merge({"_type": "gallery", "images": ["{{image:cafe interior}}"]})
        assert d.get("images") == ["{{image:cafe interior}}"]

    def test_raw_url_blocked_in_new_gallery(self):
        # 환각 raw URL 은 placeholder 가 아니므로 떨어진다.
        d = _merge({"_type": "gallery", "images": ["https://hallucinated.example/x.jpg"]})
        assert "images" not in d or d["images"] == []

    def test_mixed_list_keeps_only_placeholders(self):
        d = _merge(
            {"_type": "gallery", "images": ["{{user_image:1}}", "https://evil.example/a.jpg"]}
        )
        assert d.get("images") == ["{{user_image:1}}"]

    def test_thumbnail_placeholder_passes_on_new_text_fallback(self):
        # _new single_link 는 url 이 없으면 text 로 폴백되지만, 라벨이 있는 경우다.
        # 여기선 notice 처럼 image_url 을 가진 서브타입으로 검증.
        d = _merge(
            {"_type": "notice", "title": "공지", "image_url": "{{user_image:3}}"},
            subtype="notice",
        )
        assert d.get("image_url") == "{{user_image:3}}"

    def test_raw_thumbnail_url_blocked(self):
        d = _merge(
            {"_type": "notice", "title": "공지", "image_url": "https://x.example/a.png"},
            subtype="notice",
        )
        assert "image_url" not in d
