"""POST /api/v1/track/visit/ 통합 테스트 (APIClient, 무인증).

캐시 의존 로직(방문자별 캡·burst dedup·스로틀)은 공유 dev Redis 를 오염시키지 않도록
locmem 캐시로 격리한다. DB 는 재사용될 수 있으므로 단언은 델타 기반.
"""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from apps.analytics.channels import CH_IG_ORGANIC, CH_META_ADS
from apps.analytics.models import LandingVisit, UAClass

URL = "/api/v1/track/visit/"

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def locmem_cache(settings):
    """테스트 전용 locmem 캐시 — dev Redis(/1) 를 건드리지 않는다."""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "analytics-track-visit-tests",
        }
    }
    from django.core.cache import cache

    cache.clear()  # LocMem 은 프로세스 내 잔존 — 테스트 간 격리
    return cache


def _payload(**overrides):
    payload = {"visitor_id": str(uuid.uuid4())}
    payload.update(overrides)
    return payload


@pytest.mark.django_db
class TestTrackVisitWrite:
    def test_valid_payload_creates_row_with_derived_channel(self, client, locmem_cache):
        vid = str(uuid.uuid4())
        before = LandingVisit.objects.count()
        res = client.post(
            URL,
            _payload(visitor_id=vid, utm_source="meta", utm_medium="cpc", landing_path="/pricing"),
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204
        assert res.content == b""
        assert LandingVisit.objects.count() == before + 1

        row = LandingVisit.objects.filter(visitor_id=vid).latest("created_at")
        assert row.channel == CH_META_ADS
        assert row.utm_source == "meta"
        assert row.landing_path == "/pricing"
        assert row.ua_class == UAClass.DESKTOP

    def test_ip_hash_is_sha256_hex_not_raw_ip(self, client, locmem_cache):
        vid = str(uuid.uuid4())
        client.post(URL, _payload(visitor_id=vid), format="json", HTTP_USER_AGENT=DESKTOP_UA)
        row = LandingVisit.objects.filter(visitor_id=vid).latest("created_at")
        assert len(row.ip_hash) == 64
        assert all(c in "0123456789abcdef" for c in row.ip_hash)
        assert "127.0.0.1" not in row.ip_hash

    def test_referrer_only_visit_gets_organic_channel(self, client, locmem_cache):
        vid = str(uuid.uuid4())
        client.post(
            URL,
            _payload(visitor_id=vid, referrer="https://l.instagram.com/"),
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        row = LandingVisit.objects.filter(visitor_id=vid).latest("created_at")
        assert row.channel == CH_IG_ORGANIC

    def test_blank_landing_path_defaults_to_root(self, client, locmem_cache):
        vid = str(uuid.uuid4())
        client.post(
            URL,
            _payload(visitor_id=vid, landing_path=""),
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        row = LandingVisit.objects.filter(visitor_id=vid).latest("created_at")
        assert row.landing_path == "/"

    def test_cf_ipcountry_header_stored(self, client, locmem_cache):
        vid = str(uuid.uuid4())
        client.post(
            URL,
            _payload(visitor_id=vid),
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
            HTTP_CF_IPCOUNTRY="KR",
        )
        row = LandingVisit.objects.filter(visitor_id=vid).latest("created_at")
        assert row.country == "KR"


@pytest.mark.django_db
class TestTrackVisitSilentSkip:
    """모든 실패 경로가 204 + 0행 (silent-204)."""

    def test_missing_visitor_id(self, client, locmem_cache):
        before = LandingVisit.objects.count()
        res = client.post(URL, {"utm_source": "meta"}, format="json", HTTP_USER_AGENT=DESKTOP_UA)
        assert res.status_code == 204
        assert LandingVisit.objects.count() == before

    def test_non_uuid_visitor_id(self, client, locmem_cache):
        before = LandingVisit.objects.count()
        res = client.post(
            URL,
            {"visitor_id": "not-a-uuid", "utm_source": "meta"},
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204
        assert LandingVisit.objects.count() == before

    def test_oversize_utm_source(self, client, locmem_cache):
        before = LandingVisit.objects.count()
        res = client.post(
            URL,
            _payload(utm_source="x" * 10_240),  # 봇의 10KB utm — 페이로드 전체 무효
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204
        assert LandingVisit.objects.count() == before

    def test_bot_ua_writes_nothing(self, client, locmem_cache):
        before = LandingVisit.objects.count()
        res = client.post(URL, _payload(utm_source="meta"), format="json", HTTP_USER_AGENT=BOT_UA)
        assert res.status_code == 204
        assert LandingVisit.objects.count() == before


@pytest.mark.django_db
class TestTrackVisitAbuseGuards:
    def test_burst_dedup_identical_payload_writes_once(self, client, locmem_cache):
        payload = _payload(utm_source="meta", landing_path="/")
        before = LandingVisit.objects.count()
        for _ in range(2):
            res = client.post(URL, payload, format="json", HTTP_USER_AGENT=DESKTOP_UA)
            assert res.status_code == 204
        assert LandingVisit.objects.count() == before + 1

    def test_per_visitor_hourly_cap_default_six(self, client, locmem_cache):
        vid = str(uuid.uuid4())
        before = LandingVisit.objects.count()
        for i in range(7):  # landing_path 를 바꿔 dedup 은 통과, 캡만 검증
            res = client.post(
                URL,
                _payload(visitor_id=vid, landing_path=f"/p{i}"),
                format="json",
                HTTP_USER_AGENT=DESKTOP_UA,
            )
            assert res.status_code == 204
        assert LandingVisit.objects.count() == before + 6


@pytest.mark.django_db
class TestTrackVisitThrottle:
    def test_ip_throttle_returns_429(self, client, locmem_cache, monkeypatch):
        # local.py 가 track_visit rate 를 None 으로 눌러두므로 여기서만 개별 활성화.
        # ⚠️ DRF 3.14 는 SimpleRateThrottle.THROTTLE_RATES 가 최초 import 시점의
        # rates *dict 객체* 를 클래스 속성으로 캡처한다 — settings.REST_FRAMEWORK 를
        # 통째로 갈아끼우는 override 는 이미 import 된 프로세스에선 무효.
        # 클래스가 실제로 들고 있는 dict 를 직접 mutate 한다 (teardown 자동 복원).
        from rest_framework.throttling import ScopedRateThrottle

        monkeypatch.setitem(ScopedRateThrottle.THROTTLE_RATES, "track_visit", "3/min")

        for _ in range(3):
            res = client.post(URL, _payload(), format="json", HTTP_USER_AGENT=DESKTOP_UA)
            assert res.status_code == 204
        res = client.post(URL, _payload(), format="json", HTTP_USER_AGENT=DESKTOP_UA)
        assert res.status_code == 429
        # 표준 에러 포맷 (custom_exception_handler)
        assert res.json()["success"] is False
        assert res.json()["error"]["code"] == 429
