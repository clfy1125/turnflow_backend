"""
링크 메타 조회 서비스(apps.pages.services.link_meta) 테스트.

외부 HTTP 는 `_safe_get` / CoupangPartnersService 를 mock 으로 차단하고,
SSRF 가드·HTML 파싱·가격 정규화·에러 title 판별·분기 로직만 검증한다.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache

from apps.pages.services import link_meta as lm


@pytest.fixture
def locmem_cache(settings):
    """fetch_meta 의 캐시 의존을 끊기 위해 테스트 동안 격리된 locmem 캐시 사용."""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "linkmeta-test",
        }
    }
    cache.clear()
    yield
    cache.clear()


# ─────────────────────────────────────────────────────────────
# SSRF 가드
# ─────────────────────────────────────────────────────────────


class TestAssertPublicHttpUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/x",
            "file:///etc/passwd",
            "gopher://example.com",
            "//example.com/no-scheme",
        ],
    )
    def test_rejects_non_http_scheme(self, url):
        with pytest.raises(lm.LinkMetaFetchError):
            lm._assert_public_http_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost:8000/",
            "http://10.0.0.5/meta",
            "http://192.168.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # 클라우드 메타데이터
            "http://[::1]/",
            "http://0.0.0.0/",
        ],
    )
    def test_blocks_private_and_reserved(self, url):
        with pytest.raises(lm.LinkMetaFetchError):
            lm._assert_public_http_url(url)

    def test_allows_public_ip_literal(self):
        # 공인 IP 리터럴은 DNS 없이 통과해야 함
        lm._assert_public_http_url("https://1.1.1.1/")


# ─────────────────────────────────────────────────────────────
# 가격 정규화
# ─────────────────────────────────────────────────────────────


class TestCleanPrice:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("29,900", "29900"),
            ("₩ 129,000원", "129000"),
            (29900, "29900"),
            ("49900.00", "49900"),
            ("12.50", "12.50"),
            ("0", None),
            ("0.00", None),
            ("", None),
            (None, None),
            ("무료", None),
            (True, None),
        ],
    )
    def test_clean_price(self, raw, expected):
        assert lm._clean_price(raw) == expected


# ─────────────────────────────────────────────────────────────
# 에러 title 판별
# ─────────────────────────────────────────────────────────────


class TestErrorTitle:
    @pytest.mark.parametrize(
        "title",
        [
            "Just a moment...",
            "Attention Required! | Cloudflare",
            "Access Denied",
            "404 Not Found",
            "403 Forbidden",
            "Page Not Found",
            "Error",
            "페이지를 찾을 수 없습니다",
            "접근이 거부되었습니다",
        ],
    )
    def test_detects_error_titles(self, title):
        assert lm._looks_like_error_title(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "베이직 우드 4인 식탁 - 오늘의집",
            "Nike Air Force 1 화이트",
            "삼성 비스포크 냉장고 4도어",
            "",
        ],
    )
    def test_keeps_real_titles(self, title):
        assert lm._looks_like_error_title(title) is False

    def test_long_title_with_weak_word_not_error(self):
        # weak 키워드('error')가 들어가도 긴 정상 제목은 오탐하지 않음
        long_title = "Trial and Error 시즌2 한정판 굿즈 박스 - 공식 스토어 단독 판매 상품"
        assert lm._looks_like_error_title(long_title) is False


# ─────────────────────────────────────────────────────────────
# 메타/이미지/제목 파싱
# ─────────────────────────────────────────────────────────────


class TestHtmlParsing:
    SAMPLE = """
    <html><head>
      <meta charset="utf-8">
      <meta property="og:title" content="베이직 우드 4인 식탁">
      <meta property="og:image" content="//image.ohou.se/i/abc.jpg">
      <meta name="twitter:image" content="https://image.ohou.se/i/fallback.jpg">
      <title>오늘의집</title>
    </head><body>...</body></html>
    """

    def test_extract_title_prefers_og(self):
        metas = lm._extract_metas(self.SAMPLE)
        assert lm._extract_title(self.SAMPLE, metas) == "베이직 우드 4인 식탁"

    def test_extract_image_protocol_relative_made_absolute(self):
        metas = lm._extract_metas(self.SAMPLE)
        img = lm._extract_image(metas, "https://ohou.se/productions/1/selling")
        assert img == "https://image.ohou.se/i/abc.jpg"

    def test_title_fallback_to_title_tag(self):
        html = "<html><head><title>  순수   타이틀  </title></head></html>"
        metas = lm._extract_metas(html)
        assert lm._extract_title(html, metas) == "순수 타이틀"

    def test_meta_content_html_entities_unescaped(self):
        html = '<meta property="og:title" content="A &amp; B &lt;세트&gt;">'
        metas = lm._extract_metas(html)
        assert metas["og:title"] == "A & B <세트>"

    def test_relative_image_resolved_against_base(self):
        assert lm._absolutize("/img/x.png", "https://shop.com/p/1") == "https://shop.com/img/x.png"

    def test_non_http_image_rejected(self):
        assert lm._absolutize("data:image/png;base64,xxxx", "https://shop.com") is None


# ─────────────────────────────────────────────────────────────
# 가격 추출 — meta / JSON-LD / 사이트별
# ─────────────────────────────────────────────────────────────


class TestPriceExtraction:
    def test_price_from_meta(self):
        metas = {
            "product:price:amount": "29,900",
            "product:original_price:amount": "49900",
        }
        assert lm._price_from_meta(metas) == ("29900", "49900")

    def test_price_from_jsonld_offers_dict(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Product","name":"x","offers":{"@type":"Offer","price":"15000","priceCurrency":"KRW"}}
        </script>
        """
        assert lm._price_from_jsonld(html) == ("15000", None)

    def test_price_from_jsonld_offers_list_and_graph(self):
        html = """
        <script type="application/ld+json">
        {"@graph":[{"@type":"WebSite"},{"@type":"Product","offers":[{"price":"8900"},{"price":"9900"}]}]}
        </script>
        """
        price, _ = lm._price_from_jsonld(html)
        assert price == "8900"

    def test_price_from_jsonld_high_price_as_original(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Product","offers":{"lowPrice":"10000","highPrice":"20000"}}
        </script>
        """
        assert lm._price_from_jsonld(html) == ("10000", "20000")

    def test_invalid_jsonld_ignored(self):
        html = '<script type="application/ld+json">{not valid json,,}</script>'
        assert lm._price_from_jsonld(html) == (None, None)

    def test_site_specific_ohou(self):
        html = '{"product":{"salePrice":129000,"originalPrice":189000,"name":"식탁"}}'
        assert lm._price_from_site_specific("ohou.se", html) == ("129000", "189000")

    def test_site_specific_unknown_host(self):
        assert lm._price_from_site_specific("example.com", "{}") == (None, None)


# ─────────────────────────────────────────────────────────────
# fetch_meta 통합 — _safe_get / 쿠팡 서비스 mock
# ─────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("locmem_cache")
class TestFetchMetaGeneric:
    def test_full_generic_page(self):
        html = """
        <html><head>
          <meta property="og:title" content="모던 소파">
          <meta property="og:image" content="https://img.test/sofa.jpg">
          <meta property="product:price:amount" content="259,000">
          <meta property="product:original_price:amount" content="399000">
        </head></html>
        """
        with patch.object(lm, "_safe_get", return_value=(html, "https://ohou.se/p/1")):
            out = lm.fetch_meta("https://ohou.se/p/1")
        assert out == {
            "title": "모던 소파",
            "thumbnail": "https://img.test/sofa.jpg",
            "price": "259000",
            "original_price": "399000",
        }

    def test_error_page_title_returns_empty(self):
        html = (
            "<html><head><title>Just a moment...</title>"
            '<meta property="og:image" content="https://img/x.jpg"></head></html>'
        )
        with patch.object(lm, "_safe_get", return_value=(html, "https://blocked.test/")):
            out = lm.fetch_meta("https://blocked.test/")
        assert out == {}

    def test_blocked_fetch_returns_empty(self):
        with patch.object(lm, "_safe_get", side_effect=lm.LinkMetaFetchError("private IP")):
            out = lm.fetch_meta("http://10.0.0.1/")
        assert out == {}

    def test_non_html_returns_empty(self):
        with patch.object(lm, "_safe_get", return_value=(None, "https://x.test/a.pdf")):
            assert lm.fetch_meta("https://x.test/a.pdf") == {}

    def test_only_present_keys_included(self):
        html = '<html><head><meta property="og:title" content="제목만"></head></html>'
        with patch.object(lm, "_safe_get", return_value=(html, "https://x.test/")):
            out = lm.fetch_meta("https://x.test/")
        assert out == {"title": "제목만"}

    def test_empty_url_returns_empty(self):
        assert lm.fetch_meta("") == {}


@pytest.mark.usefixtures("locmem_cache")
class TestFetchMetaCoupang:
    @pytest.fixture(autouse=True)
    def _real_keys(self, settings):
        # mock 가드를 우회해 실제 Partners API 경로(패치됨)를 타도록
        settings.COUPANG_MOCK_MODE = False
        settings.COUPANG_PARTNERS_ACCESS_KEY = "test-access"
        settings.COUPANG_PARTNERS_SECRET_KEY = "test-secret"

    def test_mock_mode_returns_empty(self, settings):
        # mock 모드(키 미발급)면 가짜 데이터 대신 빈 dict — lookup 호출도 안 함
        settings.COUPANG_MOCK_MODE = True
        with patch("apps.pages.services.link_meta.CoupangPartnersService.lookup_by_url") as mocked:
            out = lm.fetch_meta("https://link.coupang.com/a/abc")
        mocked.assert_not_called()
        assert out == {}

    def test_coupang_branch_maps_flat(self):
        fake = {
            "product_name": "쿠팡 상품",
            "image_url": "https://image.coupang.com/x.jpg",
            "price": 29900,
            "original_price": 49900,
        }
        with patch(
            "apps.pages.services.link_meta.CoupangPartnersService.lookup_by_url",
            return_value=fake,
        ) as mocked:
            out = lm.fetch_meta("https://www.coupang.com/vp/products/123")
        mocked.assert_called_once()
        assert out == {
            "title": "쿠팡 상품",
            "thumbnail": "https://image.coupang.com/x.jpg",
            "price": "29900",
            "original_price": "49900",
        }

    def test_coupang_error_returns_empty(self):
        with patch(
            "apps.pages.services.link_meta.CoupangPartnersService.lookup_by_url",
            side_effect=lm.CoupangError("boom"),
        ):
            out = lm.fetch_meta("https://link.coupang.com/abc")
        assert out == {}

    def test_coupang_no_original_when_missing(self):
        fake = {"product_name": "X", "image_url": "", "price": 1000, "original_price": None}
        with patch(
            "apps.pages.services.link_meta.CoupangPartnersService.lookup_by_url",
            return_value=fake,
        ):
            out = lm.fetch_meta("https://m.coupang.com/vp/products/9")
        assert out == {"title": "X", "price": "1000"}


# ─────────────────────────────────────────────────────────────
# 외부 스크랩 폴백 (anti-bot 사이트)
# ─────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.mark.usefixtures("locmem_cache")
class TestScrapeFallback:
    @pytest.fixture
    def scraper_on(self, settings):
        settings.LINK_SCRAPER_PROVIDER = "scraperapi"
        settings.LINK_SCRAPER_API_KEY = "k-123"
        settings.LINK_SCRAPER_RENDER_JS = True
        settings.LINK_SCRAPER_COUNTRY = "kr"
        settings.LINK_SCRAPER_TIMEOUT = 20
        settings.LINK_SCRAPER_EXTRA_PARAMS = "ultra_premium=true"

    def test_not_configured_returns_none(self, settings):
        settings.LINK_SCRAPER_PROVIDER = ""
        settings.LINK_SCRAPER_API_KEY = ""
        assert lm._scrape_get("https://ohou.se/x") is None

    def test_scrape_get_builds_request(self, scraper_on):
        resp = _FakeResp(200, "<html><head><meta property='og:title' content='OK'></head></html>")
        with patch.object(lm.requests, "get", return_value=resp) as g:
            html = lm._scrape_get("https://store.ohou.se/goods/1")
        assert html and "OK" in html
        _, kwargs = g.call_args
        params = kwargs["params"]
        assert g.call_args[0][0] == "https://api.scraperapi.com/"
        assert params["api_key"] == "k-123"
        assert params["url"] == "https://store.ohou.se/goods/1"
        assert params["render"] == "true"
        assert params["country_code"] == "kr"
        assert params["ultra_premium"] == "true"  # EXTRA_PARAMS 병합

    def test_scrape_get_private_url_skipped(self, scraper_on):
        with patch.object(lm.requests, "get") as g:
            assert lm._scrape_get("ftp://x/y") is None
            g.assert_not_called()

    def test_scrape_get_non_200_returns_none(self, scraper_on):
        with patch.object(lm.requests, "get", return_value=_FakeResp(500, "err")):
            assert lm._scrape_get("https://store.ohou.se/goods/1") is None

    def test_fallback_on_blocked_direct(self, scraper_on):
        html = (
            "<html><head><meta property='og:title' content='소파'>"
            "<meta property='og:image' content='https://img/s.jpg'></head></html>"
        )
        with (
            patch.object(
                lm, "_safe_get", side_effect=lm.LinkMetaFetchError("HTTP 403", scrapable=True)
            ),
            patch.object(lm, "_scrape_get", return_value=html) as sg,
        ):
            out = lm.fetch_meta("https://www.furnitureshop.test/p/1")
        sg.assert_called_once()
        assert out == {"title": "소파", "thumbnail": "https://img/s.jpg"}

    def test_no_fallback_on_non_scrapable(self, scraper_on):
        with (
            patch.object(
                lm, "_safe_get", side_effect=lm.LinkMetaFetchError("scheme", scrapable=False)
            ),
            patch.object(lm, "_scrape_get") as sg,
        ):
            out = lm.fetch_meta("https://x.test/")
        sg.assert_not_called()
        assert out == {}

    def test_scrape_first_host_skips_direct(self, scraper_on):
        html = "<html><head><meta property='og:title' content='오집상품'></head></html>"
        with (
            patch.object(lm, "_safe_get") as direct,
            patch.object(lm, "_scrape_get", return_value=html) as sg,
        ):
            out = lm.fetch_meta("https://store.ohou.se/goods/3893988")
        direct.assert_not_called()
        sg.assert_called_once()
        assert out == {"title": "오집상품"}

    def test_scrape_first_no_fallback_to_direct_when_scrape_empty(self, scraper_on):
        # 스크랩-직행 호스트인데 스크랩이 비면 막힌 직접 fetch 로 헛수고하지 않고 {} 반환
        with (
            patch.object(lm, "_safe_get") as direct,
            patch.object(lm, "_scrape_get", return_value=None),
        ):
            out = lm.fetch_meta("https://ohou.se/productions/1/selling")
        direct.assert_not_called()
        assert out == {}
