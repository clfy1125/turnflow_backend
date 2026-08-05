"""캠페인 게시물 썸네일 (IG CDN → 우리 스토리지 재호스팅) 테스트.

배경: `media_url` 은 2026-08-03 부터 **게시물 permalink** 가 채워지는데, 썸네일이 그 값의
미러였다 → 프론트가 HTML 페이지 URL 을 <img src> 에 넣어 전부 깨졌다(prod 77건 중 68건).
그래서 썸네일을 별 컬럼으로 분리하고, 만료되는 CDN URL 대신 **사본**을 보관하도록 바꿨다.

커버리지:
  - 소스 선택: IMAGE / VIDEO(릴스) / CAROUSEL_ALBUM — 릴스에서 .mp4 를 고르지 않는지
  - 재호스팅: 다운로드→정제→저장, 콘텐츠 해시 dedup
  - 태스크: 성공/이미 있음/게시물 삭제(실패 카운터)/비활성 연결
  - 스위퍼: 영구 실패 상한 초과 건 제외
  - API 계약: 상세·목록 응답에 thumbnail_url, permalink 를 절대 썸네일로 주지 않음
  - 목록 응답이 Graph 를 **동기 호출하지 않음** (예전 N×5초 경로 회귀 방지)
  - 게시물 변경 시 옛 썸네일 초기화
"""

import itertools
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection
from apps.integrations.services import InstagramMediaService
from apps.workspace.models import Membership, Workspace

LIST_URL = "/api/v1/integrations/auto-dm-campaigns/"

_seq = itertools.count()


def _png_bytes(size=(1200, 1200), color=(200, 40, 40)):
    import io

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _make_ws_user():
    n = next(_seq)
    User = get_user_model()
    user = User.objects.create_user(
        email=f"thumb{n}-{timezone.now().timestamp()}@t.dev", password="pw12345!", full_name="t"
    )
    ws = Workspace.objects.create(
        name=f"thumb{n}", slug=f"thumb-{n}-{int(timezone.now().timestamp()*1000)}", owner=user
    )
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


def _make_conn(ws):
    n = next(_seq)
    c = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig{n}",
        username=f"acct{n}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    c.access_token = "tok"
    c.save()
    return c


def _make_campaign(conn, **kw):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": f"media{next(_seq)}",
        "name": "캠페인",
        "message_template": "hi",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def local_storage(tmp_path):
    """썸네일 저장 대상을 임시 로컬 디렉터리로 교체하고 그 스토리지를 돌려준다.

    ⚠️ 이 저장소의 dev 환경은 **prod 와 같은 R2 버킷**을 쓴다(USE_R2=True, 같은 버킷명) —
    교체 없이 저장 테스트를 돌리면 운영 버킷에 테스트 파일이 쌓인다.

    ``settings.STORAGES`` 오버라이드로는 **안 먹는다**(실측): 모듈이 임포트 시점에 잡은
    ``default_storage`` 프록시가 이미 S3Storage 로 고정돼 있어 그대로 R2 로 나갔다.
    그래서 모듈 심볼을 직접 패치한다 — 각 테스트가 `startswith("/media-test/")` 로
    교체가 실제로 먹었는지 단언하므로, 이 픽스처가 조용히 무력화되면 테스트가 깨진다.
    """
    from django.core.files.storage import FileSystemStorage

    from apps.integrations import media_thumbnail

    storage = FileSystemStorage(location=str(tmp_path), base_url="/media-test/")
    with patch.object(media_thumbnail, "default_storage", storage):
        yield storage


# ── 1. Graph 페이로드 → 표시용 이미지 URL 선택 ────────────────────────────────


class TestPickThumbnailSource:
    def test_image_uses_media_url(self):
        got = InstagramMediaService.pick_thumbnail_source(
            {"media_type": "IMAGE", "media_url": "https://cdn/img.jpg"}
        )
        assert got == "https://cdn/img.jpg"

    def test_video_uses_thumbnail_not_mp4(self):
        """릴스/동영상의 media_url 은 .mp4 다 → <img> 에 넣으면 안 된다."""
        got = InstagramMediaService.pick_thumbnail_source(
            {
                "media_type": "VIDEO",
                "media_url": "https://cdn/reel.mp4",
                "thumbnail_url": "https://cdn/cover.jpg",
            }
        )
        assert got == "https://cdn/cover.jpg"

    def test_video_without_thumbnail_returns_empty(self):
        got = InstagramMediaService.pick_thumbnail_source(
            {"media_type": "VIDEO", "media_url": "https://cdn/reel.mp4"}
        )
        assert got == ""

    def test_carousel_uses_first_child_cover(self):
        """캐러셀 부모엔 media_url 이 없다 → 첫 슬라이드(커버)를 쓴다."""
        got = InstagramMediaService.pick_thumbnail_source(
            {
                "media_type": "CAROUSEL_ALBUM",
                "children": {
                    "data": [
                        {"media_type": "IMAGE", "media_url": "https://cdn/1.jpg"},
                        {"media_type": "IMAGE", "media_url": "https://cdn/2.jpg"},
                    ]
                },
            }
        )
        assert got == "https://cdn/1.jpg"

    def test_carousel_video_cover_uses_child_thumbnail(self):
        got = InstagramMediaService.pick_thumbnail_source(
            {
                "media_type": "CAROUSEL_ALBUM",
                "children": {
                    "data": [
                        {
                            "media_type": "VIDEO",
                            "media_url": "https://cdn/1.mp4",
                            "thumbnail_url": "https://cdn/1.jpg",
                        }
                    ]
                },
            }
        )
        assert got == "https://cdn/1.jpg"

    def test_empty_payload(self):
        assert InstagramMediaService.pick_thumbnail_source({}) == ""


# ── 2. 재호스팅 (다운로드 → 정제 → 저장) ──────────────────────────────────────


@pytest.mark.django_db
class TestFetchAndStore:
    def test_stores_downscaled_copy_and_returns_our_url(self, local_storage):
        from apps.integrations import media_thumbnail

        raw = _png_bytes(size=(1500, 1500))
        with patch.object(media_thumbnail, "_download", return_value=raw):
            url = media_thumbnail.fetch_and_store_campaign_thumbnail("https://cdn/x.jpg", "ig1")

        assert "ig_thumbnails/ig1/" in url
        # 우리 도메인/스토리지 URL 이어야 한다 (IG CDN URL 을 그대로 돌려주면 만료된다)
        assert "cdninstagram" not in url
        # 픽스처가 실제로 먹었는지 (안 먹으면 운영 R2 버킷에 쓴다 → 반드시 단언)
        assert url.startswith("/media-test/"), url

        key = url.split("/ig_thumbnails/")[1]
        assert local_storage.exists(f"ig_thumbnails/{key}")

        # 카드용이라 640px 로 축소됐는지 (원본 1500px)
        import io

        with local_storage.open(f"ig_thumbnails/{key}") as fh:
            img = Image.open(io.BytesIO(fh.read()))
        assert max(img.size) == media_thumbnail.THUMBNAIL_MAX_EDGE

    def test_same_bytes_dedup_to_same_key(self, local_storage):
        from apps.integrations import media_thumbnail

        raw = _png_bytes(color=(1, 2, 3))
        with patch.object(media_thumbnail, "_download", return_value=raw):
            a = media_thumbnail.fetch_and_store_campaign_thumbnail("https://cdn/a.jpg", "igdup")
            b = media_thumbnail.fetch_and_store_campaign_thumbnail("https://cdn/b.jpg", "igdup")
        assert a == b

    def test_download_failure_raises_typed_error(self, local_storage):
        from apps.integrations import media_thumbnail

        with patch.object(media_thumbnail, "_download", side_effect=OSError("boom")):
            with pytest.raises(media_thumbnail.ThumbnailFetchError):
                media_thumbnail.fetch_and_store_campaign_thumbnail("https://cdn/x.jpg", "ig1")

    def test_non_image_bytes_rejected(self, local_storage):
        from apps.integrations import media_thumbnail

        with patch.object(media_thumbnail, "_download", return_value=b"not-an-image"):
            with pytest.raises(media_thumbnail.ThumbnailFetchError):
                media_thumbnail.fetch_and_store_campaign_thumbnail("https://cdn/x.mp4", "ig1")


# ── 3. 동기화 태스크 ─────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestSyncTask:
    def _run(self, campaign, source=("https://cdn/x.jpg", "IMAGE"), **kw):
        from apps.integrations import media_thumbnail, tasks

        with (
            patch.object(InstagramMediaService, "get_media_thumbnail_source", return_value=source),
            patch.object(media_thumbnail, "_download", return_value=_png_bytes()),
        ):
            return tasks.sync_campaign_thumbnail(str(campaign.id), **kw)

    def test_success_records_hosted_url_and_resets_counters(self, local_storage):
        ws, _ = _make_ws_user()
        c = _make_campaign(_make_conn(ws), thumbnail_sync_attempts=3)
        res = self._run(c)

        assert res["status"] == "ok"
        c.refresh_from_db()
        assert c.thumbnail_url.startswith("/media-test/"), c.thumbnail_url
        assert "ig_thumbnails/" in c.thumbnail_url
        assert c.thumbnail_source_url == "https://cdn/x.jpg"
        assert c.thumbnail_synced_at is not None
        assert c.thumbnail_sync_attempts == 0

    def test_already_synced_is_noop(self):
        ws, _ = _make_ws_user()
        c = _make_campaign(_make_conn(ws), thumbnail_url="https://ours/x.jpg")
        res = self._run(c)
        assert res["status"] == "unchanged"

    def test_force_resyncs(self, local_storage):
        ws, _ = _make_ws_user()
        c = _make_campaign(_make_conn(ws), thumbnail_url="https://ours/old.jpg")
        res = self._run(c, force=True)
        assert res["status"] == "ok"

    def test_deleted_media_increments_attempts(self):
        """게시물이 삭제되면 Graph 가 소스를 안 준다 → 실패 카운터만 올린다."""
        ws, _ = _make_ws_user()
        c = _make_campaign(_make_conn(ws))
        res = self._run(c, source=("", ""))

        assert res["status"] == "failed"
        c.refresh_from_db()
        assert c.thumbnail_sync_attempts == 1
        assert c.thumbnail_url == ""
        assert c.thumbnail_sync_error == "no_source_url"

    def test_no_media_id_skipped(self):
        ws, _ = _make_ws_user()
        c = _make_campaign(
            _make_conn(ws), media_id="", trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA
        )
        res = self._run(c)
        assert res == {"status": "skipped", "reason": "no_media_id"}

    def test_inactive_connection_skipped(self):
        ws, _ = _make_ws_user()
        conn = _make_conn(ws)
        conn.status = IGAccountConnection.Status.EXPIRED
        conn.save(update_fields=["status"])
        c = _make_campaign(conn)
        res = self._run(c)
        assert res["reason"] == "ig_connection_not_active"


@pytest.mark.django_db
class TestSweeper:
    def test_skips_permanently_failing_campaigns(self):
        from apps.integrations import tasks

        ws, _ = _make_ws_user()
        conn = _make_conn(ws)
        fresh = _make_campaign(conn, name="신규")
        exhausted = _make_campaign(
            conn,
            name="영구실패",
            thumbnail_sync_attempts=AutoDMCampaign.THUMBNAIL_MAX_SYNC_ATTEMPTS,
        )

        seen = []

        def _fake(cid, force=False):
            seen.append(cid)
            return {"status": "ok"}

        # 테스트 DB 는 다른 테스트가 만든 캠페인도 들고 있어(공용 dev DB) 전체 건수로는 단언할 수
        # 없다 → 이 테스트가 만든 두 건의 포함/제외만 본다. limit 은 넉넉히.
        with patch.object(tasks, "sync_campaign_thumbnail", side_effect=_fake):
            tasks.sweep_missing_campaign_thumbnails(limit=500)

        assert str(fresh.id) in seen
        assert str(exhausted.id) not in seen


# ── 4. API 계약 ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestApiContract:
    def test_detail_includes_thumbnail_url(self):
        """상세 응답에 thumbnail_url 이 **있어야** 한다 (예전엔 키 자체가 없었다)."""
        ws, user = _make_ws_user()
        c = _make_campaign(_make_conn(ws), thumbnail_url="https://media.example/t.jpg")

        r = _client(user).get(f"{LIST_URL}{c.id}/")
        assert r.status_code == 200
        assert r.data["thumbnail_url"] == "https://media.example/t.jpg"
        # 상세도 목록과 같은 통계 필드를 갖는다
        for key in ("delivered_count", "delivery_rate", "needs_attention_count", "last_sent_at"):
            assert key in r.data

    def test_detail_thumbnail_null_when_unsynced(self):
        ws, user = _make_ws_user()
        c = _make_campaign(_make_conn(ws))
        r = _client(user).get(f"{LIST_URL}{c.id}/")
        assert r.data["thumbnail_url"] is None

    def test_permalink_is_never_served_as_thumbnail(self):
        """media_url 에 permalink 가 있어도 thumbnail_url 로 새어나가면 안 된다 (핵심 회귀)."""
        ws, user = _make_ws_user()
        c = _make_campaign(_make_conn(ws), media_url="https://www.instagram.com/p/ABC123/")

        detail = _client(user).get(f"{LIST_URL}{c.id}/").data
        assert detail["media_url"] == "https://www.instagram.com/p/ABC123/"
        assert detail["thumbnail_url"] is None

        items = _client(user).get(LIST_URL).data
        item = next(i for i in items if i["id"] == str(c.id))
        assert item["thumbnail_url"] is None

    def test_list_does_not_call_graph_synchronously(self):
        """목록 응답 경로에서 Graph 동기 호출이 사라졌는지 (항목당 최대 5초 × N 회귀 방지)."""
        ws, user = _make_ws_user()
        conn = _make_conn(ws)
        for _ in range(3):
            _make_campaign(conn)

        with (
            patch("apps.integrations.views.requests.get") as graph_get,
            patch("apps.integrations.tasks.sync_campaign_thumbnail.delay") as delayed,
        ):
            r = _client(user).get(LIST_URL)

        assert r.status_code == 200
        assert graph_get.call_count == 0
        # 대신 비동기로 예약된다
        assert delayed.call_count == 3

    def test_list_enqueue_is_throttled_across_calls(self):
        ws, user = _make_ws_user()
        _make_campaign(_make_conn(ws))
        client = _client(user)

        with patch("apps.integrations.tasks.sync_campaign_thumbnail.delay") as delayed:
            client.get(LIST_URL)
            client.get(LIST_URL)
        assert delayed.call_count == 1

    def test_synced_campaign_is_not_reenqueued(self):
        ws, user = _make_ws_user()
        _make_campaign(_make_conn(ws), thumbnail_url="https://media.example/t.jpg")

        with patch("apps.integrations.tasks.sync_campaign_thumbnail.delay") as delayed:
            _client(user).get(LIST_URL)
        assert delayed.call_count == 0


@pytest.mark.django_db
class TestMediaChangeResetsThumbnail:
    def test_patch_media_id_clears_old_thumbnail(self):
        ws, user = _make_ws_user()
        c = _make_campaign(
            _make_conn(ws),
            media_id="old_media",
            media_url="https://www.instagram.com/p/OLD/",
            thumbnail_url="https://media.example/old.jpg",
        )

        with (
            patch("apps.integrations.tasks.backfill_campaign_media_permalink.delay"),
            patch("apps.integrations.tasks.sync_campaign_thumbnail.delay"),
        ):
            r = _client(user).patch(f"{LIST_URL}{c.id}/", {"media_id": "new_media"}, format="json")

        assert r.status_code == 200
        c.refresh_from_db()
        assert c.media_id == "new_media"
        # 옛 게시물 썸네일/링크가 남아 다른 게시물을 가리키면 안 된다
        assert c.thumbnail_url == ""
        assert c.media_url == ""
        assert c.thumbnail_synced_at is None
