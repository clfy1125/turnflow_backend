"""어드민 링크 노출 — 페이지 public_url(J-2) / DM 캠페인 media_permalink(J-3) 테스트.

대상:
- GET /api/v1/admin/pages/ · /api/v1/admin/pages/{slug}/  (public_url)
- GET /api/v1/admin/auto-dm/campaigns/{id}/               (media_permalink)
- integrations 서비스/태스크: get_media_permalink / backfill_campaign_media_permalink

주의: 파일명(tests_*.py)이 pytest 자동 수집 패턴과 달라 **경로 명시 실행** 필요.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
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


class TestCampaignDetailUserSettings:
    """C-1 — 회원이 유저 콘솔에서 설정한 값은 **전부** 어드민 상세에 보여야 문의 대응이 된다."""

    # 유저 콘솔에서 설정 가능한데 어드민 상세에 빠져 있던 7개 (C-1)
    C1_FIELDS = (
        "opening_message_templates",
        "follow_gate_prompt_templates",
        "recovery_reply_enabled",
        "recovery_reply_templates",
        "recovery_ttl_seconds",
        "scheduled_start_at",
        "scheduled_end_at",
    )

    def test_all_seven_fields_present(self, staff_client):
        camp = _mk_campaign(_mk_conn())
        res = staff_client.get(f"/api/v1/admin/auto-dm/campaigns/{camp.id}/")
        assert res.status_code == 200
        missing = [f for f in self.C1_FIELDS if f not in res.data]
        assert not missing, f"어드민 상세에 빠진 설정값: {missing}"

    def test_values_round_trip(self, staff_client):
        """기본값이 아니라 **회원이 설정한 값**이 그대로 보여야 한다."""
        now = timezone.now()
        camp = _mk_campaign(
            _mk_conn(),
            opening_message_templates=["안녕하세요 A", "안녕하세요 B"],
            follow_gate_prompt_templates=["팔로우 부탁 A"],
            recovery_reply_enabled=True,
            recovery_reply_templates=["숨김함 확인 후 다시 댓글 주세요"],
            recovery_ttl_seconds=7200,
            scheduled_start_at=now,
            scheduled_end_at=now + timedelta(days=3),
        )
        res = staff_client.get(f"/api/v1/admin/auto-dm/campaigns/{camp.id}/")
        assert res.data["opening_message_templates"] == ["안녕하세요 A", "안녕하세요 B"]
        assert res.data["follow_gate_prompt_templates"] == ["팔로우 부탁 A"]
        assert res.data["recovery_reply_enabled"] is True
        assert res.data["recovery_reply_templates"] == ["숨김함 확인 후 다시 댓글 주세요"]
        assert res.data["recovery_ttl_seconds"] == 7200
        assert res.data["scheduled_start_at"] is not None
        assert res.data["scheduled_end_at"] is not None


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


# ─── 2026-08-03: 목록에도 링크 노출 + 백필 훅 전 경로 배선 ─────────────


class TestCampaignListMediaPermalink:
    """어드민 캠페인 **목록**에도 게시물 링크가 실려야 한다 (상세에만 있어서 목록에 안 떴다)."""

    def _row(self, res, camp):
        rows = res.data["results"] if isinstance(res.data, dict) else res.data
        return next(r for r in rows if str(r["id"]) == str(camp.id))

    def test_list_exposes_media_id_and_permalink(self, staff_client):
        conn = _mk_conn()
        camp = _mk_campaign(
            conn, media_id="18023500352890471", media_url="https://www.instagram.com/reel/DbX2I7/"
        )
        res = staff_client.get("/api/v1/admin/auto-dm/campaigns/")
        assert res.status_code == 200
        row = self._row(res, camp)
        assert row["media_id"] == "18023500352890471"
        assert row["media_permalink"] == "https://www.instagram.com/reel/DbX2I7/"

    def test_list_blank_permalink_when_media_url_empty(self, staff_client):
        conn = _mk_conn()
        camp = _mk_campaign(conn, media_id="123", media_url=None)
        res = staff_client.get("/api/v1/admin/auto-dm/campaigns/")
        row = self._row(res, camp)
        assert row["media_permalink"] == ""  # 프론트는 이때 링크를 숨긴다
        assert row["media_id"] == "123"

    def test_list_blank_permalink_for_cdn_url(self, staff_client):
        conn = _mk_conn()
        camp = _mk_campaign(
            conn, media_id="123", media_url="https://scontent.cdninstagram.com/x.jpg"
        )
        res = staff_client.get("/api/v1/admin/auto-dm/campaigns/")
        assert self._row(res, camp)["media_permalink"] == ""


class TestPermalinkBackfillHookWiring:
    """백필 enqueue 가 생성/부착 **모든** 경로에 걸려 있는지 (직접 생성만 있었다)."""

    @pytest.fixture
    def enqueued(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "apps.integrations.tasks.backfill_campaign_media_permalink.delay",
            lambda cid: calls.append(cid),
        )
        return calls

    def test_helper_enqueues_on_commit_only(self, db, enqueued, django_capture_on_commit_callbacks):
        from apps.integrations.tasks import enqueue_media_permalink_backfill

        camp = _mk_campaign(_mk_conn(), media_id="123", media_url=None)
        with django_capture_on_commit_callbacks(execute=True):
            assert enqueue_media_permalink_backfill(camp) is True
            assert enqueued == []  # 커밋 전에는 발행 금지 (워커가 옛 행을 읽는다)
        assert enqueued == [str(camp.id)]

    def test_helper_skips_non_specific_media(self, db, enqueued):
        from apps.integrations.tasks import enqueue_media_permalink_backfill

        camp = _mk_campaign(
            _mk_conn(), trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA, media_id=""
        )
        assert enqueue_media_permalink_backfill(camp) is False
        assert enqueued == []

    def test_copy_path_enqueues(self, db, enqueued, django_capture_on_commit_callbacks):
        """복사본은 media_id 를 물려받지만 media_url 은 빈 채였다 → 링크 미노출."""
        source = _mk_campaign(_mk_conn(), media_id="777", media_url=None)
        with django_capture_on_commit_callbacks(execute=True):
            copied = source.copy(new_name="복사본")
            from apps.integrations.tasks import enqueue_media_permalink_backfill

            enqueue_media_permalink_backfill(copied)
        assert enqueued == [str(copied.id)]

    def test_next_media_attach_enqueues(self, db, enqueued, django_capture_on_commit_callbacks):
        """next_media → specific_media 전환 시점도 '게시물이 정해진' 순간이다."""
        conn = _mk_conn()
        camp = _mk_campaign(
            conn,
            trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
            media_id="",
            media_url=None,
        )
        with django_capture_on_commit_callbacks(execute=True):
            res = AutoDMCampaign.attach_next_media_single_active(
                ig_connection_id=conn.id,
                candidate_ids=[camp.id],
                media_id="new_post_1",
            )
        assert res["attached"] == [camp.id]
        camp.refresh_from_db()
        assert camp.trigger_type == AutoDMCampaign.TriggerType.SPECIFIC_MEDIA
        assert enqueued == [str(camp.id)]


class TestPermalinkSweeper:
    @pytest.fixture(autouse=True)
    def _mock_mode(self, settings):
        settings.DEBUG = True
        settings.INSTAGRAM_MOCK_MODE = True

    def test_sweeper_fills_only_blank_media_url(self, db):
        """빈값만 채운다 — 사용자가 넣은 참고 URL 은 덮어쓰면 소실된다."""
        from apps.integrations.tasks import sweep_missing_media_permalinks

        conn = _mk_conn()
        blank = _mk_campaign(conn, media_id="17900000000000001", media_url=None)
        user_url = _mk_campaign(
            conn, media_id="17900000000000002", media_url="https://images.unsplash.com/photo-1"
        )
        already = _mk_campaign(
            conn, media_id="17900000000000003", media_url="https://www.instagram.com/p/KEEP/"
        )

        res = sweep_missing_media_permalinks(limit=50)

        blank.refresh_from_db()
        user_url.refresh_from_db()
        already.refresh_from_db()
        assert blank.media_url == "https://www.instagram.com/p/17900000000000001/"
        assert user_url.media_url == "https://images.unsplash.com/photo-1"  # 보존
        assert already.media_url == "https://www.instagram.com/p/KEEP/"
        assert res["ok"] >= 1
