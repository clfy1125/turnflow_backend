"""
apps/pages/services/coupang.py

쿠팡 파트너스(어필리에이트) Open API 클라이언트.

■ 동기
  프론트가 쿠팡 상품 URL 을 입력했을 때 백엔드에서 가격/이미지/딥링크를
  조회해서 single_link/group_link 블록 데이터에 자동 채워넣기 위함.

■ 인증 (HMAC-SHA256)
  쿠팡 파트너스 Open API 는 모든 요청에 다음 헤더를 요구:
    Authorization: CEA algorithm=HmacSHA256, access-key=..., signed-date=..., signature=...
  signed-date 는 'yymmddTHHMMSSZ' (UTC) 포맷.
  signature = HMAC-SHA256(secret, signed_date + method + path + query).hex

■ Mock 모드
  settings.COUPANG_MOCK_MODE=True 면 외부 호출 없이 더미 데이터 반환.
  로컬 개발 / CI / 키 발급 전 단계용.

■ 캐싱
  같은 URL 반복 조회 시 Redis 1시간 TTL — 쿠팡 API rate limit (분당 10회) 보호.

■ 참고
  - 공식 문서: https://partners.coupang.com/#affiliate/openapi/guide
  - 단축 URL (link.coupang.com/...) 은 HEAD 요청으로 redirect 따라가서 최종 URL 파싱.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone as dt_timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────

_HOST = "https://api-gateway.coupang.com"
_TIMEOUT = 10.0
_HEAD_TIMEOUT = 5.0
_CACHE_TTL = 3600  # 1h
_CACHE_PREFIX = "coupang:lookup:"

# 쿠팡 productId 추출 — vp/products/{id} 또는 /products/{id}
_PRODUCT_ID_RE = re.compile(r"/(?:vp/)?products/(\d+)")

# 쿠팡 도메인 화이트리스트
_COUPANG_HOSTS = {
    "coupang.com",
    "www.coupang.com",
    "m.coupang.com",
    "link.coupang.com",
}


# ─────────────────────────────────────────────────────────────
# 예외
# ─────────────────────────────────────────────────────────────


class CoupangError(Exception):
    """쿠팡 API 호출 베이스 예외."""


class CoupangBadURLError(CoupangError):
    """입력 URL 이 쿠팡 도메인이 아니거나 product id 추출 실패."""


class CoupangProductNotFound(CoupangError):
    """쿠팡 API 가 해당 productId 에 매칭되는 상품을 못 찾음."""


class CoupangAPIError(CoupangError):
    """쿠팡 API 4xx/5xx 응답."""


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


class CoupangPartnersService:
    """쿠팡 파트너스 Open API 클라이언트."""

    @classmethod
    def lookup_by_url(cls, url: str) -> dict:
        """쿠팡 상품 URL → 가격/이미지/딥링크 통합 조회.

        Returns:
            {
                "source_url": str,
                "product_id": str,
                "product_name": str,
                "price": int | None,        # 현재 판매가
                "original_price": int | None,  # 정가 (없으면 None)
                "discount_rate": int | None,   # 0~100 (없으면 None)
                "image_url": str,
                "deep_link": str,           # 어필리에이트 트래킹 URL
                "is_rocket": bool,          # 로켓배송 여부
                "category_name": str,
                "fetched_at": str,          # ISO8601
            }

        Raises:
            CoupangBadURLError: URL 파싱 실패
            CoupangProductNotFound: 상품 못찾음
            CoupangAPIError: 쿠팡 API 4xx/5xx
        """
        normalized_url = cls._normalize_url(url)

        # 캐시 hit 시 즉시 반환
        cache_key = _CACHE_PREFIX + hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        cached = cache.get(cache_key)
        if cached is not None:
            logger.debug("coupang lookup cache hit url=%s", normalized_url[:80])
            return cached

        # Mock 모드
        if getattr(settings, "COUPANG_MOCK_MODE", True):
            data = cls._mock_lookup(normalized_url)
            cache.set(cache_key, data, _CACHE_TTL)
            return data

        # 실제 호출 — productId 추출 후 검색 API 로 매칭
        product_id = cls._extract_product_id(normalized_url)
        if not product_id:
            raise CoupangBadURLError(f"쿠팡 상품 URL 에서 productId 추출 실패: {url}")

        # 검색 — productId 자체를 keyword 로 검색하면 매칭됨 (쿠팡 검색 동작)
        results = cls.search_products(keyword=product_id, limit=10)
        matched = cls._match_by_product_id(results, product_id)
        if not matched:
            raise CoupangProductNotFound(f"productId={product_id} 에 매칭되는 상품 없음")

        # 딥링크 생성
        try:
            deep_links = cls.create_deep_link([normalized_url])
            deep_link = (deep_links[0].get("shortenUrl") if deep_links else "") or normalized_url
        except Exception as e:  # noqa: BLE001
            logger.warning("coupang deep link failed url=%s err=%s", normalized_url, e)
            deep_link = normalized_url

        data = cls._normalize_product_data(
            matched,
            source_url=normalized_url,
            deep_link=deep_link,
        )
        cache.set(cache_key, data, _CACHE_TTL)
        return data

    @classmethod
    def search_products(cls, keyword: str, limit: int = 5) -> list[dict]:
        """상품 검색.

        GET /v2/providers/affiliate_open_api/apis/openapi/v1/products/search
            ?keyword=...&limit=...
        """
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
        query = f"keyword={keyword}&limit={limit}"
        headers = cls._build_auth_headers("GET", path, query)

        try:
            resp = requests.get(
                f"{_HOST}{path}?{query}",
                headers=headers,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as e:
            raise CoupangAPIError(f"쿠팡 search 요청 실패: {e}") from e

        if resp.status_code >= 400:
            raise CoupangAPIError(
                f"쿠팡 search HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise CoupangAPIError(f"쿠팡 search 응답 JSON 파싱 실패: {e}") from e

        return (payload.get("data") or {}).get("productData") or []

    @classmethod
    def create_deep_link(cls, urls: list[str]) -> list[dict]:
        """일반 쿠팡 URL → 어필리에이트 트래킹(딥링크) URL 생성.

        POST /v2/providers/affiliate_open_api/apis/openapi/v1/deeplink
            body: {"coupangUrls": ["..."]}
        """
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
        headers = cls._build_auth_headers("POST", path, query="")
        headers["Content-Type"] = "application/json"

        try:
            resp = requests.post(
                f"{_HOST}{path}",
                headers=headers,
                json={"coupangUrls": urls},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as e:
            raise CoupangAPIError(f"쿠팡 deeplink 요청 실패: {e}") from e

        if resp.status_code >= 400:
            raise CoupangAPIError(
                f"쿠팡 deeplink HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise CoupangAPIError(f"쿠팡 deeplink 응답 JSON 파싱 실패: {e}") from e

        return payload.get("data") or []

    # ─────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────

    @classmethod
    def _build_auth_headers(cls, method: str, path: str, query: str) -> dict:
        """HMAC-SHA256 인증 헤더 생성.

        Authorization: CEA algorithm=HmacSHA256, access-key=..., signed-date=..., signature=...
        """
        access_key = getattr(settings, "COUPANG_PARTNERS_ACCESS_KEY", "")
        secret_key = getattr(settings, "COUPANG_PARTNERS_SECRET_KEY", "")
        if not access_key or not secret_key:
            raise CoupangAPIError(
                "COUPANG_PARTNERS_ACCESS_KEY / SECRET_KEY 가 설정되지 않음 "
                "(COUPANG_MOCK_MODE=True 로 mock 모드 사용 가능)"
            )

        signed_date = datetime.now(dt_timezone.utc).strftime("%y%m%dT%H%M%SZ")
        message = signed_date + method.upper() + path + query
        signature = hmac.new(
            secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        authorization = (
            f"CEA algorithm=HmacSHA256, "
            f"access-key={access_key}, "
            f"signed-date={signed_date}, "
            f"signature={signature}"
        )
        return {"Authorization": authorization}

    @classmethod
    def _normalize_url(cls, url: str) -> str:
        """단축 URL 펼치기 + 호스트 검증.

        link.coupang.com/... → 최종 coupang.com URL 로 redirect 따라가기 (1회).
        """
        url = (url or "").strip()
        if not url:
            raise CoupangBadURLError("URL 이 비어 있음")

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in _COUPANG_HOSTS:
            raise CoupangBadURLError(f"쿠팡 도메인이 아님: {host}")

        # 단축 URL 펼치기
        if host == "link.coupang.com":
            try:
                resp = requests.head(url, allow_redirects=True, timeout=_HEAD_TIMEOUT)
                final_url = resp.url
                final_host = (urlparse(final_url).hostname or "").lower()
                if final_host in _COUPANG_HOSTS:
                    return final_url
            except requests.RequestException as e:
                logger.warning("coupang shortened URL expand failed: %s", e)
                # 펼치기 실패해도 원본 URL 그대로 사용
                return url

        return url

    @classmethod
    def _extract_product_id(cls, url: str) -> Optional[str]:
        """쿠팡 URL 에서 productId 추출. 실패 시 None."""
        m = _PRODUCT_ID_RE.search(url)
        return m.group(1) if m else None

    @classmethod
    def _match_by_product_id(
        cls, results: list[dict], product_id: str
    ) -> Optional[dict]:
        """검색 결과 중 productId 일치하는 항목 우선 선택."""
        if not results:
            return None
        for item in results:
            if str(item.get("productId", "")) == product_id:
                return item
        # 못 찾으면 첫 번째 결과를 fallback 으로 사용 (검색 매칭이 정확하지 않을 때)
        return results[0]

    @classmethod
    def _normalize_product_data(
        cls, item: dict, *, source_url: str, deep_link: str
    ) -> dict:
        """쿠팡 search API 응답 → 우리 응답 포맷.

        쿠팡 응답 키 (https://partners.coupang.com/#affiliate/openapi/guide#product 참고):
            productId, productName, productPrice, productImage, productUrl,
            isRocket, isFreeShipping, categoryName
        """
        price = item.get("productPrice")
        try:
            price_int = int(price) if price is not None else None
        except (TypeError, ValueError):
            price_int = None

        return {
            "source_url": source_url,
            "product_id": str(item.get("productId", "")),
            "product_name": item.get("productName", ""),
            "price": price_int,
            "original_price": None,  # search API 는 정가 미제공
            "discount_rate": None,
            "image_url": item.get("productImage", ""),
            "deep_link": deep_link,
            "is_rocket": bool(item.get("isRocket", False)),
            "category_name": item.get("categoryName", ""),
            "fetched_at": datetime.now(dt_timezone.utc).isoformat(),
        }

    @classmethod
    def _mock_lookup(cls, url: str) -> dict:
        """Mock 모드 응답 — 외부 호출 없이 더미 데이터."""
        product_id = cls._extract_product_id(url) or "mock_product"
        return {
            "source_url": url,
            "product_id": product_id,
            "product_name": f"[Mock] 쿠팡 더미 상품 {product_id}",
            "price": 29900,
            "original_price": 49900,
            "discount_rate": 40,
            "image_url": f"https://placehold.co/400x400/png?text=Coupang+{product_id}",
            "deep_link": f"https://link.coupang.com/mock/{product_id}",
            "is_rocket": True,
            "category_name": "기타",
            "fetched_at": datetime.now(dt_timezone.utc).isoformat(),
        }
