"""이미지 기반 새-페이지 생성: 업로드 API + 프롬프트 카탈로그/캐시 프리픽스 테스트."""

from __future__ import annotations

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from .models import AiSourceImage
from .services.prompt_builder import build_prompts

User = get_user_model()


def _jpeg(name="t.jpg", color="red"):
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


@pytest.fixture
def user(db):
    return User.objects.create_user(email="img@example.com", password="Pass1234!")


@pytest.fixture
def client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


# ── 업로드 API ────────────────────────────────────────────────


class TestSourceImageUpload:
    URL = "/api/v1/ai/source-images/"

    def test_upload_two_images(self, client, user):
        resp = client.post(
            self.URL, {"files": [_jpeg("a.jpg"), _jpeg("b.jpg", "blue")]}, format="multipart"
        )
        assert resp.status_code == 201, resp.content
        assert len(resp.data) == 2
        assert AiSourceImage.objects.filter(user=user, job__isnull=True).count() == 2
        for item in resp.data:
            assert item["url"]
            assert item["mime_type"] == "image/jpeg"

    def test_no_file_400(self, client):
        resp = client.post(self.URL, {}, format="multipart")
        assert resp.status_code == 400

    def test_over_limit_400(self, client, user):
        files = [_jpeg(f"{i}.jpg") for i in range(11)]
        resp = client.post(self.URL, {"files": files}, format="multipart")
        assert resp.status_code == 400
        # 한 장도 저장되지 않아야 함
        assert AiSourceImage.objects.filter(user=user).count() == 0

    def test_bad_mime_400_no_rows(self, client, user):
        bad = SimpleUploadedFile("x.txt", b"not an image", content_type="text/plain")
        resp = client.post(self.URL, {"files": [bad]}, format="multipart")
        assert resp.status_code == 400
        assert AiSourceImage.objects.filter(user=user).count() == 0

    def test_requires_auth(self, db):
        resp = APIClient().post(self.URL, {"files": [_jpeg()]}, format="multipart")
        assert resp.status_code in (401, 403)


# ── 프롬프트 카탈로그 / 캐시 프리픽스 ───────────────────────────

_PREFIX_MARKER = "### [출력 형식]"


def _cache_prefix(user_prompt: str) -> str:
    """캐시되는 고정 프리픽스 영역(가변부 직전까지) 추출."""
    return user_prompt[: user_prompt.index(_PREFIX_MARKER)]


class TestPromptCatalog:
    def _catalog(self, n=1):
        usable = [
            {"n": i, "url": f"https://cdn/{i}.jpg", "summary": f"요약{i}", "suggested_use": "hero"}
            for i in range(1, n + 1)
        ]
        return {"usable": usable, "mood_notes": "파스텔 무드", "url_by_n": {}}

    def test_catalog_rendered_in_variable_part(self, db):
        _, user_p = build_prompts(
            "bio_remake",
            {"concept": "디저트 카페", "image_catalog": self._catalog(2)},
            mode="",
        )
        assert "반드시 활용" in user_p  # 카탈로그 섹션 헤더 (규칙 텍스트와 구분되는 마커)
        assert "{{user_image:1}}" in user_p
        assert "{{user_image:2}}" in user_p
        assert "요약1" in user_p
        assert "파스텔 무드" in user_p
        # 카탈로그는 캐시 프리픽스 밖(가변부)에 있어야 함
        assert "{{user_image:1}}" not in _cache_prefix(user_p)

    def test_user_image_rule_in_cached_prefix(self, db):
        _, user_p = build_prompts("bio_remake", {"concept": "x"}, mode="")
        # 규칙(상수)은 캐시 프리픽스에 포함되어야 함
        assert "[사용자 업로드 이미지 규칙]" in _cache_prefix(user_p)

    def test_no_catalog_no_usable_section(self, db):
        _, user_p = build_prompts("bio_remake", {"concept": "x"}, mode="")
        assert "반드시 활용" not in user_p

    def test_cache_prefix_stable_across_catalogs(self, db):
        """카탈로그가 달라도 캐시 프리픽스는 바이트 동일해야 한다(DeepSeek 캐시 안전)."""
        _, p1 = build_prompts(
            "bio_remake", {"concept": "동일", "image_catalog": self._catalog(1)}, mode=""
        )
        _, p2 = build_prompts(
            "bio_remake", {"concept": "동일", "image_catalog": self._catalog(3)}, mode=""
        )
        assert _cache_prefix(p1) == _cache_prefix(p2)
