"""image_resolver 의 {{user_image:N}} 치환 + 기존 {{image:}} 경로 보존 테스트."""

from __future__ import annotations

from .services.image_resolver import resolve_images


class TestUserImageResolve:
    def test_substitutes_user_image_placeholder(self):
        data = {
            "blocks": [
                {"data": {"image_url": "{{user_image:1}}"}},
                {"data": {"image_url": "{{user_image:2}}"}},
            ]
        }
        out = resolve_images(
            data,
            user_image_urls={"1": "https://cdn/a.jpg", "2": "https://cdn/b.jpg"},
        )
        assert out["blocks"][0]["data"]["image_url"] == "https://cdn/a.jpg"
        assert out["blocks"][1]["data"]["image_url"] == "https://cdn/b.jpg"

    def test_unknown_index_becomes_empty(self):
        data = {"blocks": [{"data": {"image_url": "{{user_image:9}}"}}]}
        out = resolve_images(data, user_image_urls={"1": "https://cdn/a.jpg"})
        assert out["blocks"][0]["data"]["image_url"] == ""

    def test_no_user_map_leaves_no_user_placeholder(self):
        # user_image_urls 미전달 → 매핑 없음 → 빈 문자열로 제거 (플레이스홀더 잔존 X)
        data = {"blocks": [{"data": {"image_url": "{{user_image:1}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["image_url"] == ""

    def test_no_placeholders_returns_data_untouched(self):
        data = {"blocks": [{"data": {"image_url": "https://already/real.jpg"}}]}
        out = resolve_images(data, user_image_urls={"1": "https://cdn/a.jpg"})
        assert out == data

    def test_pixabay_keyword_left_for_existing_path(self, monkeypatch):
        # {{image:키워드}} 는 기존 경로(_resolve_one)로 위임 — 여기선 키워드가 그대로 검출되는지만 확인.
        import apps.ai_jobs.services.image_resolver as ir

        monkeypatch.setattr(ir, "_resolve_one", lambda kw: f"RESOLVED::{kw}")
        data = {
            "blocks": [{"data": {"image_url": "{{user_image:1}}", "bg": "{{image:cafe interior}}"}}]
        }
        out = resolve_images(data, user_image_urls={"1": "https://cdn/a.jpg"})
        assert out["blocks"][0]["data"]["image_url"] == "https://cdn/a.jpg"
        assert out["blocks"][0]["data"]["bg"] == "RESOLVED::cafe interior"
