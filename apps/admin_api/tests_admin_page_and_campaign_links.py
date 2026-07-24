"""어드민 링크 노출 — 페이지 public_url(J-2) / DM 캠페인 media_permalink(J-3) 테스트.

대상:
- GET /api/v1/admin/pages/ · /api/v1/admin/pages/{slug}/  (public_url)
- GET /api/v1/admin/auto-dm/campaigns/{id}/               (media_permalink)
- integrations 서비스/태스크: get_media_permalink / backfill_campaign_media_permalink

주의: 파일명(tests_*.py)이 pytest 자동 수집 패턴과 달라 **경로 명시 실행** 필요.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection
from apps.integrations.tasks import backfill_campaign_media_permalink
from apps.pages.models import Page
from apps.workspace.models import Workspace

User = get_user_model()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com", password="Pass1234!", is_staff=True
    )


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


def _mk_conn(username="brand_official"):
    owner = User.objects.create_user(
        email=f"owner-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    return IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=username,
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    )


def _mk_campaign(conn, **kwargs):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "name": "camp",
        "message_template": "hi",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


# ─── J-2: 페이지 public_url ────────────────────────────────────────────


class TestPagePublicUrl:
    """@override_settings 클래스 데코레이터는 pytest 클래스에서 깨지므로 settings 픽스처 사용."""

    @pytest.fixture(autouse=True)
    def _base_url(self, settings):
        settings.SNAPSHOT_BASE_URL = "https://turnflow.link"

    def test_detail_public_url(self, staff_client):
        user = User.objects.create_user(email=f"u-{uuid.uuid4().hex[:8]}@t.com", password="P1234!")
        page = Page.objects.create(user=user, slug="my-brand", is_public=True)
        res = staff_client.get(f"/api/v1/admin/pages/{page.slug}/")
        assert res.status_code == 200
        assert res.data["public_url"] == "https://turnflow.link/@my-brand"

    def test_list_public_url(self, staff_client):
        user = User.objects.create_user(email=f"u-{uuid.uuid4().hex[:8]}@t.com", password="P1234!")
        Page.objects.create(user=user, slug=f"p-{uuid.uuid4().hex[:6]}", is_public=True)
        res = staff_client.get("/api/v1/admin/pages/")
        assert res.status_code == 200
        assert all(
            r["public_url"].startswith("https://turnflow.link/@") for r in res.data["results"]
        )

    def test_blocked_page_still_has_url(self, staff_client):
        # 차단(is_active=false)이어도 canonical URL 은 항상 반환 (접근 가부는 프론트 판단).
        user = User.objects.create_user(email=f"u-{uuid.uuid4().hex[:8]}@t.com", password="P1234!")
        page = Page.objects.create(user=user, slug="blocked-one", is_public=True, is_active=False)
        res = staff_client.get(f"/api/v1/admin/pages/{page.slug}/")
        assert res.data["is_active"] is False
        assert res.data["public_url"] == "https://turnflow.link/@blocked-one"


# ─── J-3: 캠페인 media_permalink ───────────────────────────────────────


class TestCampaignMediaPermalink:
    def test_serializer_returns_permalink_when_ig_url(self, staff_client):
        conn = _mk_conn()
        camp = _mk_campaign(conn, media_id="123", media_url="https://www.instagram.com/p/ABC123/")
        res = staff_client.get(f"/api/v1/admin/auto-dm/campaigns/{camp.id}/")
        assert res.status_code == 200
        assert res.data["media_permalink"] == "https://www.instagram.com/p/ABC123/"

    def test_serializer_blank_when_non_permalink(self, staff_client):
        conn = _mk_conn()
        # CDN 이미지 URL (permalink 아님) → media_permalink 는 빈 문자열
        camp = _mk_campaign(
            conn, media_id="123", media_url="https://scontent.cdninstagram.com/x.jpg"
        )
        res = staff_client.get(f"/api/v1/admin/auto-dm/campaigns/{camp.id}/")
        assert res.data["media_permalink"] == ""

    def test_serializer_blank_when_empty(self, staff_client):
        conn = _mk_conn()
        camp = _mk_campaign(conn, media_id="123", media_url=None)
        res = staff_client.get(f"/api/v1/admin/auto-dm/campaigns/{camp.id}/")
        assert res.data["media_permalink"] == ""


class TestBackfillPermalinkTask:
    # is_mock_mode() = DEBUG and INSTAGRAM_MOCK_MODE — pytest 는 DEBUG=False 라 둘 다 켜야 함.
    @pytest.fixture(autouse=True)
    def _mock_mode(self, settings):
        settings.DEBUG = True
        settings.INSTAGRAM_MOCK_MODE = True

    def test_backfill_fills_media_url_in_mock(self, db):
        conn = _mk_conn()
        camp = _mk_campaign(conn, media_id="17900000000000000", media_url=None)
        result = backfill_campaign_media_permalink(str(camp.id))
        assert result["status"] == "ok"
        camp.refresh_from_db()
        assert camp.media_url == "https://www.instagram.com/p/17900000000000000/"

    def test_backfill_skips_when_already_permalink(self, db):
        conn = _mk_conn()
        camp = _mk_campaign(conn, media_id="123", media_url="https://www.instagram.com/reel/XYZ/")
        result = backfill_campaign_media_permalink(str(camp.id))
        assert result["status"] == "skipped"
        camp.refresh_from_db()
        assert camp.media_url == "https://www.instagram.com/reel/XYZ/"

    def test_backfill_skips_without_media_id(self, db):
        conn = _mk_conn()
        camp = _mk_campaign(
            conn, trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA, media_id="", media_url=None
        )
        result = backfill_campaign_media_permalink(str(camp.id))
        assert result["status"] == "skipped"


class TestGetMediaPermalinkService:
    @pytest.fixture(autouse=True)
    def _mock_mode(self, settings):
        settings.DEBUG = True
        settings.INSTAGRAM_MOCK_MODE = True

    def test_mock_synthesizes_permalink(self, db):
        from apps.integrations.services import InstagramMediaService

        url = InstagramMediaService.get_media_permalink("999", "tok")
        assert url == "https://www.instagram.com/p/999/"

    def test_empty_media_id_returns_none(self, db):
        from apps.integrations.services import InstagramMediaService

        assert InstagramMediaService.get_media_permalink("", "tok") is None
