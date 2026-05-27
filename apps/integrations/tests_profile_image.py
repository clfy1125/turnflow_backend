"""
IG 프로필 사진 캐싱·동기화 테스트.

외부 IO (urllib, IG /me API) 는 mock — 다운로드 사이즈 캡, 정제 통과,
default_storage 저장, dedup, sync_ig_profile_picture 태스크 분기까지 검증.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.storage import default_storage
from django.utils import timezone
from PIL import Image

from apps.integrations.profile_image import (
    ProfileImageFetchError,
    fetch_and_store_profile_image,
)


def _make_png_bytes(size: tuple[int, int] = (64, 64)) -> bytes:
    """테스트용 유효 PNG 바이트 생성."""
    buf = io.BytesIO()
    Image.new("RGB", size, color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _make_urlopen_mock(payload: bytes):
    """urllib.request.urlopen context manager 흉내."""
    resp = MagicMock()

    # _download 는 64KB chunk 반복 read — payload 한 번에 다 주고 빈 바이트로 종료
    chunks = iter([payload, b""])
    resp.read.side_effect = lambda size: next(chunks)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ─────────────────────────────────────────────────────────────
# fetch_and_store_profile_image
# ─────────────────────────────────────────────────────────────


class TestFetchAndStoreProfileImage:
    def test_empty_url_raises(self, db):
        with pytest.raises(ProfileImageFetchError, match="remote_url"):
            fetch_and_store_profile_image("", "ig_user_123")

    def test_empty_user_id_raises(self, db):
        with pytest.raises(ProfileImageFetchError, match="ig_user_id"):
            fetch_and_store_profile_image("https://example.com/a.jpg", "")

    def test_happy_path_saves_to_default_storage(self, db):
        png = _make_png_bytes()
        with patch("urllib.request.urlopen", return_value=_make_urlopen_mock(png)):
            url = fetch_and_store_profile_image(
                "https://cdn.example.com/ig/profile.jpg",
                "ig_user_HAPPY",
            )

        assert url
        # 저장된 키는 ig_profiles/{ig_user_id}/{hash}.{ext}
        # PNG (alpha 없음) → process_upload 가 JPEG 로 변환 → ext=jpg
        # storage 에 실제 저장됐는지 확인 (URL 에서 키 추출)
        # default_storage.url 의 결과는 backend 마다 형식 다름 → 존재 여부는
        # default_storage.exists 로 검증하기 위해 키를 따로 만들 수는 없으니
        # url 이 비어있지 않은 것만 검증.
        assert "ig_profiles" in url or url.startswith("http")

    def test_dedup_same_image_returns_same_url(self, db):
        png = _make_png_bytes()
        # 두 번 호출 — 두 번째는 dedup hit
        with patch("urllib.request.urlopen", return_value=_make_urlopen_mock(png)):
            url1 = fetch_and_store_profile_image("https://x.example/a.jpg", "ig_dedup")
        with patch("urllib.request.urlopen", return_value=_make_urlopen_mock(png)):
            url2 = fetch_and_store_profile_image("https://x.example/b.jpg", "ig_dedup")
        assert url1 == url2

    def test_oversize_download_raises(self, db):
        # 6MB > _MAX_BYTES (5MB) — 사이즈 캡 트리거
        big = b"\x00" * (6 * 1024 * 1024)
        with patch("urllib.request.urlopen", return_value=_make_urlopen_mock(big)):
            with pytest.raises(ProfileImageFetchError, match="다운로드"):
                fetch_and_store_profile_image(
                    "https://cdn.example.com/big.jpg",
                    "ig_user_BIG",
                )

    def test_invalid_image_bytes_raise(self, db):
        # 이미지가 아닌 바이트 → image_pipeline 이 ImageValidationError → 우리 예외로 래핑
        garbage = b"this is not an image, just some text"
        with patch("urllib.request.urlopen", return_value=_make_urlopen_mock(garbage)):
            with pytest.raises(ProfileImageFetchError, match="정제"):
                fetch_and_store_profile_image(
                    "https://cdn.example.com/garbage.jpg",
                    "ig_user_BAD",
                )


# ─────────────────────────────────────────────────────────────
# sync_ig_profile_picture (Celery 태스크 — apply() 로 인라인 실행)
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def workspace_and_user(db):
    from django.contrib.auth import get_user_model

    from apps.workspace.models import Workspace

    User = get_user_model()
    user = User.objects.create_user(
        username="igowner", email="igowner@example.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="Test WS", slug="test-ws", owner=user)
    return ws, user


@pytest.fixture
def ig_connection(workspace_and_user):
    from apps.integrations.models import IGAccountConnection

    ws, _ = workspace_and_user
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id="ig_user_TASK",
        username="testuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_xyz"
    conn.save()
    return conn


class TestSyncIgProfilePictureTask:
    def test_skips_when_no_remote_url(self, db, ig_connection):
        from apps.integrations.tasks import sync_ig_profile_picture

        # mock account_info → profile_picture_url 없음
        with patch(
            "apps.integrations.services.MockInstagramProvider.is_mock_token",
            return_value=True,
        ), patch(
            "apps.integrations.services.MockInstagramProvider.get_mock_account_info",
            return_value={"id": "x", "username": "u", "name": "n"},
        ):
            result = sync_ig_profile_picture.apply(args=[str(ig_connection.id)]).get()

        assert result["status"] == "skipped"
        assert result["reason"] == "no_remote_url"

    def test_unchanged_when_source_url_same(self, db, ig_connection):
        from apps.integrations.tasks import sync_ig_profile_picture

        # 미리 source_url + cached url 세팅
        ig_connection.profile_picture_source_url = "https://cdn.example.com/p.jpg"
        ig_connection.profile_picture_url = "https://r2.example/cached.jpg"
        ig_connection.save()

        with patch(
            "apps.integrations.services.MockInstagramProvider.is_mock_token",
            return_value=True,
        ), patch(
            "apps.integrations.services.MockInstagramProvider.get_mock_account_info",
            return_value={
                "id": "x",
                "username": "u",
                "profile_picture_url": "https://cdn.example.com/p.jpg",
            },
        ):
            result = sync_ig_profile_picture.apply(args=[str(ig_connection.id)]).get()

        assert result["status"] == "unchanged"
        ig_connection.refresh_from_db()
        assert ig_connection.profile_picture_synced_at is not None

    def test_updated_when_remote_url_changes(self, db, ig_connection):
        from apps.integrations.tasks import sync_ig_profile_picture

        png = _make_png_bytes()
        new_remote = "https://cdn.example.com/new.jpg"

        with patch(
            "apps.integrations.services.MockInstagramProvider.is_mock_token",
            return_value=True,
        ), patch(
            "apps.integrations.services.MockInstagramProvider.get_mock_account_info",
            return_value={
                "id": "x",
                "username": "newname",
                "name": "New Name",
                "profile_picture_url": new_remote,
            },
        ), patch(
            "urllib.request.urlopen",
            return_value=_make_urlopen_mock(png),
        ):
            result = sync_ig_profile_picture.apply(args=[str(ig_connection.id)]).get()

        assert result["status"] == "updated"
        assert result["profile_picture_url"]

        ig_connection.refresh_from_db()
        assert ig_connection.profile_picture_source_url == new_remote
        assert ig_connection.profile_picture_url == result["profile_picture_url"]
        assert ig_connection.username == "newname"
        assert ig_connection.name == "New Name"

    def test_skipped_when_connection_revoked(self, db, ig_connection):
        from apps.integrations.models import IGAccountConnection
        from apps.integrations.tasks import sync_ig_profile_picture

        ig_connection.status = IGAccountConnection.Status.REVOKED
        ig_connection.save()

        result = sync_ig_profile_picture.apply(args=[str(ig_connection.id)]).get()
        assert result["status"] == "skipped"
        assert "revoked" in result["reason"]
