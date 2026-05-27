"""
apps/pages/coupang_views.py

쿠팡 상품 URL → 가격/이미지/딥링크 조회 API.
프론트가 사용자에게 쿠팡 링크 입력 받으면 이 엔드포인트로 메타데이터를 조회하여
single_link/group_link 블록의 price/image_url/url 필드를 자동 채움.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.pages.services.coupang import (
    CoupangAPIError,
    CoupangBadURLError,
    CoupangPartnersService,
    CoupangProductNotFound,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Serializers
# ─────────────────────────────────────────────────────────────


class CoupangLookupRequestSerializer(serializers.Serializer):
    """쿠팡 상품 URL 조회 요청."""

    url = serializers.URLField(
        required=True,
        max_length=2048,
        help_text="쿠팡 상품 URL (https://www.coupang.com/vp/products/... 또는 https://link.coupang.com/...)",
    )


class CoupangLookupDataSerializer(serializers.Serializer):
    """쿠팡 상품 조회 응답 데이터."""

    source_url = serializers.URLField(help_text="입력된 원본 URL (단축이면 펼쳐진 URL)")
    product_id = serializers.CharField(help_text="쿠팡 productId")
    product_name = serializers.CharField(help_text="상품명")
    price = serializers.IntegerField(
        allow_null=True, help_text="현재 판매가 (원). null 이면 미제공."
    )
    original_price = serializers.IntegerField(
        allow_null=True, help_text="정가 (원). search API 응답에는 보통 미제공 → null"
    )
    discount_rate = serializers.IntegerField(
        allow_null=True, help_text="할인율 (0~100). 미제공 시 null"
    )
    image_url = serializers.URLField(help_text="대표 이미지 URL")
    deep_link = serializers.URLField(help_text="어필리에이트 딥링크 (link.coupang.com 단축 URL)")
    is_rocket = serializers.BooleanField(help_text="로켓배송 여부")
    category_name = serializers.CharField(allow_blank=True, help_text="카테고리명")
    fetched_at = serializers.CharField(help_text="조회 시각 (ISO8601 UTC)")


class CoupangLookupResponseSerializer(serializers.Serializer):
    """쿠팡 상품 조회 응답 (성공)."""

    success = serializers.BooleanField()
    data = CoupangLookupDataSerializer()


class CoupangLookupRateThrottle(UserRateThrottle):
    """사용자별 분당 30회 — 쿠팡 API rate limit + 어뷰즈 방어."""

    rate = "30/min"


# ─────────────────────────────────────────────────────────────
# View
# ─────────────────────────────────────────────────────────────


class CoupangLookupView(APIView):
    """쿠팡 상품 URL → 가격/이미지/딥링크 조회."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [CoupangLookupRateThrottle]

    @extend_schema(
        summary="쿠팡 상품 URL → 가격/메타 조회",
        description="""
        ## 목적
        프론트가 사용자에게 쿠팡 상품 URL 을 입력받으면, 이 엔드포인트로 백엔드가
        쿠팡 파트너스 Open API 를 호출하여 상품명/가격/이미지/딥링크를 가져옵니다.
        프론트는 받은 정보로 single_link/group_link 블록의 필드를 자동 채워서
        사용자가 가격을 직접 입력하지 않게 할 수 있습니다.

        ## 인증
        - Bearer JWT 필수
        - 사용자당 분당 30회 throttle (쿠팡 API rate limit + 어뷰즈 방어)

        ## 동작
        1. URL 정규화 — `link.coupang.com/...` 단축 URL 이면 HEAD 로 펼쳐서 최종 URL 획득
        2. 쿠팡 도메인 검증 (`coupang.com`, `m.coupang.com`, `link.coupang.com`)
        3. URL 에서 productId 추출 — `/vp/products/{id}` 또는 `/products/{id}` 패턴
        4. 쿠팡 search API 로 productId 키워드 검색 → 매칭 항목 선택
        5. 쿠팡 deeplink API 로 어필리에이트 트래킹 URL 생성
        6. 동일 URL 재조회는 1시간 캐싱 (Redis)

        ## Mock 모드
        `COUPANG_MOCK_MODE=True` 또는 키 미설정 시 외부 호출 없이 더미 응답 반환.
        로컬 개발 / 키 발급 전 단계용.

        ## 응답 예시 (200)
        ```json
        {
          "success": true,
          "data": {
            "source_url": "https://www.coupang.com/vp/products/1234567",
            "product_id": "1234567",
            "product_name": "[Mock] 쿠팡 더미 상품 1234567",
            "price": 29900,
            "original_price": 49900,
            "discount_rate": 40,
            "image_url": "https://placehold.co/400x400/png?text=Coupang+1234567",
            "deep_link": "https://link.coupang.com/mock/1234567",
            "is_rocket": true,
            "category_name": "기타",
            "fetched_at": "2026-05-26T12:34:56.789+00:00"
          }
        }
        ```

        ## 에러
        - **400 BAD_URL** — 쿠팡 도메인이 아니거나 productId 추출 실패
        - **401** — 인증 실패
        - **404 PRODUCT_NOT_FOUND** — 쿠팡 API 가 상품을 못 찾음
        - **429** — Throttle 한도 초과
        - **502 COUPANG_API_ERROR** — 쿠팡 API 4xx/5xx 응답 또는 네트워크 오류
        """,
        request=CoupangLookupRequestSerializer,
        responses={
            200: CoupangLookupResponseSerializer,
            400: OpenApiResponse(description="URL 형식 오류 / 쿠팡 도메인 아님"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="쿠팡 API 가 상품을 못 찾음"),
            429: OpenApiResponse(description="Throttle 초과 (분당 30회)"),
            502: OpenApiResponse(description="쿠팡 API 호출 실패"),
        },
        examples=[
            OpenApiExample(
                name="JavaScript fetch 예시",
                value=(
                    "fetch('/api/v1/pages/products/coupang/lookup/', {\n"
                    "  method: 'POST',\n"
                    "  headers: {\n"
                    "    'Authorization': 'Bearer ' + token,\n"
                    "    'Content-Type': 'application/json'\n"
                    "  },\n"
                    "  body: JSON.stringify({url: 'https://www.coupang.com/vp/products/12345'})\n"
                    "})"
                ),
                request_only=True,
            ),
        ],
        tags=["Pages — Coupang"],
    )
    def post(self, request):
        req = CoupangLookupRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        url = req.validated_data["url"]

        try:
            data = CoupangPartnersService.lookup_by_url(url)
        except CoupangBadURLError as e:
            return Response(
                {
                    "success": False,
                    "error": {"code": "BAD_URL", "message": str(e)},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except CoupangProductNotFound as e:
            return Response(
                {
                    "success": False,
                    "error": {"code": "PRODUCT_NOT_FOUND", "message": str(e)},
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except CoupangAPIError as e:
            logger.warning("coupang lookup api error url=%s err=%s", url, e)
            return Response(
                {
                    "success": False,
                    "error": {"code": "COUPANG_API_ERROR", "message": str(e)},
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"success": True, "data": data}, status=status.HTTP_200_OK)
