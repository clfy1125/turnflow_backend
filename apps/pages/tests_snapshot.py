"""Playwright 캡쳐 Celery 태스크 테스트.

실제 Playwright/Chromium 을 띄우지 않기 위해 ``capture_page_snapshot`` 자체를
stub 으로 교체. 통합 테스트는 별도 환경에서 실행.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from PIL import Image

from .models import Page
from .services.snapshot import SnapshotError, SnapshotResult

User = get_user_model()


def _fake_webp_bytes() -> bytes:
    im = Image.new("RGB", (390, 844), (240, 240, 240))
    buf = BytesIO()
    im.save(buf, format="WEBP", quality=82)
    return buf.getvalue()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="snap@example.com", password="Pass1234!")


@pytest.fixture
def public_page(db, user):
    return Page.objects.create(
        user=user, slug="snap-target", title="대상", is_public=True
    )


@pytest.fixture
def private_page(db, user):
    return Page.objects.create(
        user=user, slug="snap-private", title="비공개", is_public=False
    )


@pytest.fixture
def fake_capture_success(monkeypatch):
    """``capture_page_snapshot`` 를 항상 성공으로 만드는 stub."""
    webp = _fake_webp_bytes()

    def _fake(slug: str):
        return SnapshotResult(
            content_file=ContentFile(webp, name=f"snapshot_{slug}.webp"),
            suggested_name=f"snapshot_{slug}_test.webp",
            width=390,
            height=844,
            elapsed_seconds=1.23,
        )
    monkeypatch.setattr(
        "apps.pages.services.snapshot.capture_page_snapshot", _fake
    )
    return _fake


@pytest.fixture
def fake_capture_failure(monkeypatch):
    def _fake(slug: str):
        raise SnapshotError("테스트용 의도된 실패")
    monkeypatch.setattr(
        "apps.pages.services.snapshot.capture_page_snapshot", _fake
    )
    return _fake


class TestCaptureReferenceSnapshot:
    def test_success_updates_page(self, public_page, fake_capture_success):
        from .tasks import capture_reference_snapshot

        # apply (Celery eager) — 동기 실행
        result = capture_reference_snapshot.apply(args=[public_page.id]).get()

        public_page.refresh_from_db()
        assert public_page.reference_snapshot_status == "succeeded"
        assert public_page.reference_snapshot_updated_at is not None
        assert public_page.reference_snapshot  # FieldFile 가 존재
        assert public_page.reference_snapshot_error == ""
        assert result["status"] == "succeeded"

    def test_failure_records_error(self, public_page, fake_capture_failure):
        from .tasks import capture_reference_snapshot

        result = capture_reference_snapshot.apply(args=[public_page.id]).get()

        public_page.refresh_from_db()
        assert public_page.reference_snapshot_status == "failed"
        assert "테스트용 의도된 실패" in public_page.reference_snapshot_error
        assert result["status"] == "failed"

    def test_private_page_marked_failed(self, private_page, fake_capture_success):
        from .tasks import capture_reference_snapshot

        result = capture_reference_snapshot.apply(args=[private_page.id]).get()

        private_page.refresh_from_db()
        assert private_page.reference_snapshot_status == "failed"
        assert "비공개" in private_page.reference_snapshot_error
        assert not private_page.reference_snapshot
        assert result["status"] == "failed"

    def test_missing_page_returns_failed(self, db, fake_capture_success):
        from .tasks import capture_reference_snapshot

        result = capture_reference_snapshot.apply(args=[999_999]).get()
        assert result["status"] == "failed"
        assert result["error"] == "page_not_found"
