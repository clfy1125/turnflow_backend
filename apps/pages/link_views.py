"""
apps/pages/link_views.py

POST /api/v1/link/fetch-meta/ — 외부 상품/콘텐츠 URL 메타 조회.

페이지 빌더에서 사용자가 쿠팡·오늘의집 등 링크를 붙여넣으면, 프론트가 이 엔드포인트로
title/thumbnail/price/original_price 를 받아 링크 블록 필드를 자동 채운다.
실제 추출 로직은 ``apps.pages.services.link_meta.fetch_meta`` 참조.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.pages.services.link_meta import fetch_meta

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Serializers
# ─────────────────────────────────────────────────────────────


class LinkMetaRequestSerializer(serializers.Serializer):
    """메타 조회 요청."""

    url = serializers.URLField(
        required=True,
        max_length=2048,
        help_text="메타 정보를 조회할 상품/콘텐츠 URL (http/https). 예: 쿠팡·오늘의집 상품 링크",
    )


class LinkMetaResponseSerializer(serializers.Serializer):
    """메타 조회 응답 (flat). 모든 필드 optional — 못 찾은 키는 응답에서 생략됨."""

    title = serializers.CharField(required=False, help_text="제목 (og:title → <title>)")
    thumbnail = serializers.URLField(required=False, help_text="대표 이미지 절대 URL (og:image)")
    price = serializers.CharField(
        required=False, help_text='현재가 — 콤마 없는 숫자 문자열 (예: "29900")'
    )
    original_price = serializers.CharField(
        required=False, help_text="정가 — 콤마 없는 숫자 문자열 (할인 전, 있을 때만)"
    )


# ─────────────────────────────────────────────────────────────
# View
# ─────────────────────────────────────────────────────────────


class LinkMetaView(APIView):
    """외부 URL → flat 메타(title/thumbnail/price/original_price) 조회."""

    permission_classes = [IsAuthenticated]
    # 사용자당 호출 제한. rate 는 settings.REST_FRAMEWORK.DEFAULT_THROTTLE_RATES.link_meta 참조.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "link_meta"

    @extend_schema(
        summary="외부 링크 메타 조회 (제목/이미지/가격)",
        description="""
        ## 목적
        사용자가 페이지 빌더에서 쿠팡·오늘의집 등 외부 상품/콘텐츠 URL 을 붙여넣으면,
        백엔드가 해당 페이지의 메타 정보(제목/대표이미지/가격/정가)를 추출해 돌려준다.
        프론트는 이 값으로 링크 블록(single_link/group_link)의 필드를 자동 채운다.

        ## 인증
        - Bearer JWT 필수
        - 사용자당 분당 호출 제한(throttle). 초과 시 429.

        ## 동작
        1. **쿠팡 도메인**(`*.coupang.com`) → 쿠팡 파트너스 Open API 로 조회(가격/이미지/상품명).
           (쿠팡은 Akamai 가 서버 직접 fetch 를 차단 → 공식 API 만 가능. 키 미설정/mock 이면 `{}`)
        2. **그 외 사이트** → 서버가 HTML 을 직접 받아 파싱:
           - 제목: `og:title` → `twitter:title` → `<title>`
           - 이미지: `og:image`(secure) → `twitter:image` (절대 URL 로 정규화)
           - 가격: `meta(product:price 등)` → `JSON-LD offers` → 사이트별 셀렉터 순 폴백
        3. **봇 차단 사이트**(오늘의집 등, 직접 fetch 가 403): 외부 anti-bot 스크랩 서비스가
           설정돼 있으면 그쪽으로 폴백해 HTML 을 받아 동일하게 파싱. 미설정이면 `{}`.
           ⚠️ 스크랩 폴백은 **응답이 느릴 수 있음**(렌더링/우회로 최대 ~20초) — 프론트는
           이 엔드포인트 타임아웃을 25초 이상으로 두고 로딩 표시 권장.
        4. 같은 URL 은 캐싱(성공 1시간 / 빈 결과 5분).

        ## 응답 (200) — flat, 모든 필드 optional
        - 못 찾은 필드는 **응답에서 생략**된다 (항상 모든 키가 오는 게 아님).
        - `price` / `original_price` 는 **콤마 없는 숫자 문자열** (예: `"29900"`).
        - `thumbnail` 은 **절대 http(s) URL**.
        - **에러/차단 페이지**(403/404/"Just a moment" 등)나 비-HTML 응답, SSRF 차단,
          타임아웃 등은 **빈 객체 `{}`** 로 응답한다 (항상 HTTP 200).

        ```json
        { "title": "상품명", "thumbnail": "https://.../img.jpg", "price": "29900", "original_price": "49900" }
        ```

        ## 보안 / 제약
        - SSRF 방어: 사설/루프백/링크로컬/예약 IP 로 향하는 URL 은 차단(리다이렉트 hop 포함).
        - http/https scheme 만 허용.
        - 전체 처리는 15초 안에 종료(connect/read 타임아웃 + 본문 크기 상한).

        ## 에러
        - **400** — `url` 누락 / URL 형식 오류 (DRF 검증)
        - **401** — 인증 실패
        - **429** — Throttle 한도 초과
        - (외부 사이트 오류/차단/타임아웃은 에러가 아니라 200 + `{}` 로 응답)
        """,
        request=LinkMetaRequestSerializer,
        responses={
            200: LinkMetaResponseSerializer,
            400: OpenApiResponse(description="url 누락 / URL 형식 오류"),
            401: OpenApiResponse(description="인증 실패"),
            429: OpenApiResponse(description="Throttle 초과"),
        },
        examples=[
            OpenApiExample(
                name="요청 (JavaScript fetch)",
                value=(
                    "fetch('/api/v1/link/fetch-meta/', {\n"
                    "  method: 'POST',\n"
                    "  headers: {\n"
                    "    'Authorization': 'Bearer ' + token,\n"
                    "    'Content-Type': 'application/json'\n"
                    "  },\n"
                    "  body: JSON.stringify({url: 'https://ohou.se/productions/123456/selling'})\n"
                    "})"
                ),
                request_only=True,
            ),
            OpenApiExample(
                name="응답 (성공)",
                value={
                    "title": "베이직 우드 4인 식탁",
                    "thumbnail": "https://image.ohou.se/i/abc.jpg",
                    "price": "129000",
                    "original_price": "189000",
                },
                response_only=True,
            ),
            OpenApiExample(
                name="응답 (메타 없음/차단 페이지)",
                value={},
                response_only=True,
            ),
        ],
        tags=["Pages — Link Meta"],
    )
    def post(self, request):
        req = LinkMetaRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        url = req.validated_data["url"]

        try:
            data = fetch_meta(url)
        except Exception as e:  # noqa: BLE001 — 어떤 경우에도 200 + {} 로 graceful
            logger.warning(
                "link-meta unexpected error url=%s err=%s",
                url[:80],
                e,
                extra={"request_id": getattr(request, "id", None)},
            )
            data = {}

        return Response(data, status=status.HTTP_200_OK)
