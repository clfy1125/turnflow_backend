"""
쿠팡 파트너스 서비스 테스트.

외부 호출(requests)은 mock 으로 차단 — 시그너처 생성·URL 파싱·응답 매핑·
mock 모드 동작·캐싱 같은 순수 로직만 검증.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.pages.services.coupang import (
    CoupangAPIError,
    CoupangBadURLError,
    CoupangPartnersService,
)


# ─────────────────────────────────────────────────────────────
# URL 파싱
# ─────────────────────────────────────────────────────────────


class TestExtractProductId:
    def test_vp_products_url(self):
        url = "https://www.coupang.com/vp/products/1234567?itemId=abc&vendorItemId=xyz"
        assert CoupangPartnersService._extract_product_id(url) == "1234567"

    def test_products_only(self):
        url = "https://www.coupang.com/products/9876543"
        assert CoupangPartnersService._extract_product_id(url) == "9876543"

    def test_no_product_id_returns_none(self):
        url = "https://www.coupang.com/np/categories/123"
        assert CoupangPartnersService._extract_product_id(url) is None


# ─────────────────────────────────────────────────────────────
# 시그너처 생성 (HMAC-SHA256)
# ─────────────────────────────────────────────────────────────


@override_settings(
    COUPANG_PARTNERS_ACCESS_KEY="test_access_key",
    COUPANG_PARTNERS_SECRET_KEY="test_secret_key",
)
class TestBuildAuthHeaders:
    def test_returns_authorization_header(self):
        headers = CoupangPartnersService._build_auth_headers(
            "GET",
            "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search",
            "keyword=test&limit=5",
        )
        assert "Authorization" in headers
        auth = headers["Authorization"]
        # 필수 컴포넌트 모두 포함
        assert "CEA algorithm=HmacSHA256" in auth
        assert "access-key=test_access_key" in auth
        assert "signed-date=" in auth
        assert "signature=" in auth

    def test_signature_is_hex(self):
        headers = CoupangPartnersService._build_auth_headers("GET", "/path", "q=1")
        auth = headers["Authorization"]
        # signature 는 64자 hex 문자열 (SHA256)
        sig_part = auth.split("signature=")[-1]
        assert len(sig_part) == 64
        assert all(c in "0123456789abcdef" for c in sig_part)

    @override_settings(
        COUPANG_PARTNERS_ACCESS_KEY="",
        COUPANG_PARTNERS_SECRET_KEY="",
    )
    def test_missing_keys_raises(self):
        with pytest.raises(CoupangAPIError, match="ACCESS_KEY"):
            CoupangPartnersService._build_auth_headers("GET", "/path", "")


# ─────────────────────────────────────────────────────────────
# Mock 모드 lookup
# ─────────────────────────────────────────────────────────────


@override_settings(COUPANG_MOCK_MODE=True)
class TestMockLookup:
    def setup_method(self):
        # 캐시 격리
        cache.clear()

    def test_mock_returns_full_payload(self):
        url = "https://www.coupang.com/vp/products/1234567"
        data = CoupangPartnersService.lookup_by_url(url)

        assert data["product_id"] == "1234567"
        assert data["product_name"].startswith("[Mock]")
        assert isinstance(data["price"], int) and data["price"] > 0
        assert data["original_price"] is not None
        assert data["discount_rate"] == 40
        assert data["image_url"].startswith("https://")
        assert data["deep_link"].startswith("https://link.coupang.com/")
        assert data["is_rocket"] is True

    def test_mock_bad_domain_rejected(self):
        with pytest.raises(CoupangBadURLError):
            CoupangPartnersService.lookup_by_url("https://example.com/foo")

    def test_mock_cached_returns_same_object(self):
        url = "https://www.coupang.com/vp/products/9999"
        first = CoupangPartnersService.lookup_by_url(url)
        with patch.object(CoupangPartnersService, "_mock_lookup") as mocked:
            # 캐시 hit 이면 _mock_lookup 이 다시 호출되면 안 됨
            second = CoupangPartnersService.lookup_by_url(url)
            mocked.assert_not_called()
        assert first == second


# ─────────────────────────────────────────────────────────────
# 응답 정규화
# ─────────────────────────────────────────────────────────────


class TestNormalizeProductData:
    def test_full_mapping(self):
        raw = {
            "productId": 1234567,
            "productName": "테스트 상품",
            "productPrice": 29900,
            "productImage": "https://image.coupang.com/abc.jpg",
            "isRocket": True,
            "categoryName": "전자제품",
        }
        data = CoupangPartnersService._normalize_product_data(
            raw,
            source_url="https://www.coupang.com/vp/products/1234567",
            deep_link="https://link.coupang.com/aff/xyz",
        )
        assert data["product_id"] == "1234567"
        assert data["product_name"] == "테스트 상품"
        assert data["price"] == 29900
        assert data["is_rocket"] is True
        assert data["category_name"] == "전자제품"
        assert data["deep_link"] == "https://link.coupang.com/aff/xyz"

    def test_missing_price_becomes_none(self):
        raw = {"productId": 1, "productName": "x"}
        data = CoupangPartnersService._normalize_product_data(
            raw, source_url="u", deep_link="d"
        )
        assert data["price"] is None


# ─────────────────────────────────────────────────────────────
# 매칭 로직
# ─────────────────────────────────────────────────────────────


class TestMatchByProductId:
    def test_exact_match_preferred(self):
        results = [
            {"productId": 111, "productName": "다른 상품"},
            {"productId": 222, "productName": "정확한 매칭"},
        ]
        matched = CoupangPartnersService._match_by_product_id(results, "222")
        assert matched["productName"] == "정확한 매칭"

    def test_fallback_to_first_when_no_exact(self):
        results = [
            {"productId": 111, "productName": "유사 상품"},
            {"productId": 222, "productName": "다른 상품"},
        ]
        matched = CoupangPartnersService._match_by_product_id(results, "999")
        # fallback: 첫 번째
        assert matched["productId"] == 111

    def test_empty_returns_none(self):
        assert CoupangPartnersService._match_by_product_id([], "1") is None
