"""UTM 표준화(한국어 UTM 대응) 테스트 — 순수 단위 + 방문 비콘 통합.

배경: 어드민 마케팅 대시보드는 UTM 4-튜플 **완전일치**로 유입을 저장 링크에 붙인다.
한글은 저장 경로에 따라 NFC/NFD 가 섞이고 공백류도 달라지는데 두 값이 화면상 똑같아
보이므로, 표준형을 한 곳(apps/analytics/utm.py)에서 강제하는 것이 유일한 방어다.
"""

from __future__ import annotations

import unicodedata
import uuid

import pytest
from rest_framework.test import APIClient

from apps.analytics.attribution import capture_signup_attribution
from apps.analytics.channels import CH_META_ADS, derive_channel
from apps.analytics.models import LandingVisit, SignupAttribution
from apps.analytics.utm import UTM_MAX_LENGTH, normalize_utm

URL = "/api/v1/track/visit/"
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

NFC_KO = "테스트 캠페인"
NFD_KO = unicodedata.normalize("NFD", NFC_KO)


class TestNormalizeUtm:
    def test_nfd_becomes_nfc(self):
        assert NFD_KO != NFC_KO  # 전제: 화면상 동일하지만 다른 문자열
        assert normalize_utm(NFD_KO) == NFC_KO

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  여름 세일  ", "여름 세일"),
            ("여름 세일", "여름 세일"),  # NBSP (엑셀/슬랙 복붙)
            ("여름　세일", "여름 세일"),  # 전각 공백
            ("여름  세일", "여름 세일"),  # 연속 공백
            ("여름\t세일", "여름 세일"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_whitespace_variants_collapse(self, raw, expected):
        assert normalize_utm(raw) == expected

    def test_case_is_preserved(self):
        """대소문자는 표시값이라 보존 — 매칭 시에만 호출부가 lower() 한다."""
        assert normalize_utm(" Summer_Sale ") == "Summer_Sale"


class TestKoreanChannelAliases:
    @pytest.mark.parametrize("source", ["메타", "페이스북", "인스타그램", "인스타"])
    def test_korean_source_maps_to_meta_ads(self, source):
        assert derive_channel(source, "cpc", "") == CH_META_ADS

    def test_nfd_korean_source_also_maps(self):
        """NFD 로 온 '메타'도 별칭 매칭 — derive_channel 이 normalize_utm 을 통과시킨다."""
        assert derive_channel(unicodedata.normalize("NFD", "메타"), "", "") == CH_META_ADS

    def test_korean_paid_medium(self):
        """미매핑 소스 + 한글 유료 매체 → paid_other (other_campaign 으로 새지 않는다)."""
        assert derive_channel("어딘가", "광고", "") == "paid_other"

    def test_korean_influencer_medium_wins_over_source(self):
        assert derive_channel("인스타그램", "인플루언서", "") == "influencer"


@pytest.mark.django_db
class TestTrackVisitNormalizes:
    @pytest.fixture(autouse=True)
    def _locmem(self, settings):
        """dev Redis(/1) 오염 방지 — 방문 캡·dedup 가 캐시를 쓴다."""
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "analytics-utm-normalization-tests",
            }
        }
        from django.core.cache import cache

        cache.clear()

    def test_nfd_korean_utm_is_stored_as_nfc(self):
        vid = str(uuid.uuid4())
        res = APIClient().post(
            URL,
            {"visitor_id": vid, "utm_source": "메타", "utm_medium": "cpc", "utm_campaign": NFD_KO},
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204
        visit = LandingVisit.objects.get(visitor_id=vid)
        assert visit.utm_campaign == NFC_KO
        assert visit.channel == CH_META_ADS

    def test_long_nfd_korean_is_not_dropped(self):
        """NFD 한글은 글자수가 3배 → 정규화가 길이 검증 **뒤**면 silent-204 로 사라진다.

        (검증 실패 = 기록 없음이라 이 회귀는 화면상 '유입이 아예 없음'으로만 보인다.)
        """
        vid = str(uuid.uuid4())
        campaign = "가나다라마" * 20  # NFC 100자 → NFD 300자 (구 상한 150 초과)
        res = APIClient().post(
            URL,
            {"visitor_id": vid, "utm_campaign": unicodedata.normalize("NFD", campaign)},
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204
        assert LandingVisit.objects.get(visitor_id=vid).utm_campaign == campaign

    def test_over_limit_is_still_skipped(self):
        """상한(200자)을 진짜로 넘는 값은 기존대로 조용히 스킵 — 봇 쓰레기 방어 유지."""
        vid = str(uuid.uuid4())
        res = APIClient().post(
            URL,
            {"visitor_id": vid, "utm_campaign": "가" * (UTM_MAX_LENGTH + 1)},
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204
        assert not LandingVisit.objects.filter(visitor_id=vid).exists()


@pytest.mark.django_db
class TestSignupAttributionNormalizes:
    def test_nfd_korean_utm_is_stored_as_nfc(self, django_user_model):
        """가입 귀속도 방문과 **같은 표준형** — 아니면 방문/가입이 다른 행으로 갈린다."""
        user = django_user_model.objects.create_user(
            email=f"attr-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
        )
        capture_signup_attribution(
            user,
            {"utm_source": "메타", "utm_medium": "cpc", "utm_campaign": NFD_KO},
            "email",
        )
        attr = SignupAttribution.objects.get(user=user)
        assert attr.utm_campaign == NFC_KO
        assert attr.channel == CH_META_ADS
