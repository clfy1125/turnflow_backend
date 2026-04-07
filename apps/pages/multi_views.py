"""
apps/pages/multi_views.py

다중 페이지(Multi-Page) 관리 API.
기존 /api/v1/pages/me/* API는 그대로 유지하고,
이 파일은 /api/v1/pages/multipages/* 엔드포인트만 담당합니다.

■ 페이지 관리
  GET    /api/v1/pages/multipages/                          → 내 모든 페이지 목록
  POST   /api/v1/pages/multipages/                          → 새 페이지 생성
  GET    /api/v1/pages/multipages/{id}/                     → 특정 페이지 조회
  PATCH  /api/v1/pages/multipages/{id}/                     → 특정 페이지 수정
  DELETE /api/v1/pages/multipages/{id}/                     → 특정 페이지 삭제
  PATCH  /api/v1/pages/multipages/{id}/slug/                → slug 변경

■ 블록 관리
  GET    /api/v1/pages/multipages/{id}/blocks/              → 블록 목록
  POST   /api/v1/pages/multipages/{id}/blocks/              → 블록 생성
  POST   /api/v1/pages/multipages/{id}/blocks/reorder/      → 블록 순서 재정렬
  PATCH  /api/v1/pages/multipages/{id}/blocks/{block_id}/   → 블록 수정
  DELETE /api/v1/pages/multipages/{id}/blocks/{block_id}/   → 블록 삭제

■ 통계
  GET    /api/v1/pages/multipages/{id}/stats/               → 통계 요약
  GET    /api/v1/pages/multipages/{id}/stats/chart/         → 날짜별 차트
  GET    /api/v1/pages/multipages/{id}/stats/blocks/        → 블록별 통계

■ 문의 관리
  GET    /api/v1/pages/multipages/{id}/inquiries/           → 문의 목록
  PATCH  /api/v1/pages/multipages/{id}/inquiries/{pk}/      → 문의 메모 수정
  DELETE /api/v1/pages/multipages/{id}/inquiries/{pk}/      → 문의 삭제

■ 구독자 관리
  GET    /api/v1/pages/multipages/{id}/subscriptions/       → 구독자 목록
  PATCH  /api/v1/pages/multipages/{id}/subscriptions/{pk}/  → 구독자 메모 수정
  DELETE /api/v1/pages/multipages/{id}/subscriptions/{pk}/  → 구독자 삭제

■ 미디어
  GET    /api/v1/pages/multipages/{id}/media/               → 미디어 파일 목록
  POST   /api/v1/pages/multipages/{id}/media/               → 미디어 파일 업로드
  GET    /api/v1/pages/multipages/{id}/media/{media_id}/    → 미디어 파일 상세
  DELETE /api/v1/pages/multipages/{id}/media/{media_id}/    → 미디어 파일 삭제
"""

from datetime import timedelta

import json

from django.db import transaction
from django.db.models import Case, IntegerField, Q, When
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Block, ContactInquiry, Page, PageMedia, PageSubscription
from .multi_serializers import (
    MultiPageCreateSerializer,
    MultiPageSerializer,
    MultiPageSlugChangeSerializer,
)
from .serializers import (
    BlockSerializer,
    BlockStatsSerializer,
    ChartDataSerializer,
    ContactInquiryMemoSerializer,
    ContactInquirySerializer,
    CustomCssSerializer,
    LinkClicksStatsSerializer,
    PageMediaSerializer,
    PageSubscriptionMemoSerializer,
    PageSubscriptionSerializer,
    ReorderSerializer,
    StatsSummarySerializer,
)
from .stats import get_block_stats, get_chart_data, get_link_stats, get_stats_summary, resolve_period

_MULTIPAGE_TAG = "다중 페이지 서비스"

# ── 업로드 제한 (image_views.py 와 동일)
_ALLOWED_MIME_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif",
    "image/webp", "image/svg+xml", "image/bmp", "image/tiff",
})
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

_PERIOD_MAP = {"all": None, "6m": 180, "1m": 30, "7d": 7}


def _get_owned_page(request, page_id: int) -> Page | None:
    """요청한 사용자가 소유한 Page를 반환. 없거나 권한 없으면 None."""
    return Page.objects.filter(pk=page_id, user=request.user).first()


# ═════════════════════════════════════════════════════════════
# 페이지 목록 / 생성
# ═════════════════════════════════════════════════════════════

class MultiPageListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="내 페이지 목록 조회",
        description="""
## 개요
로그인한 사용자가 소유한 **모든 페이지 목록**을 생성일 오름차순으로 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 페이지 고유 ID. 블록·통계·문의 등 하위 API에서 `{id}`로 사용 |
| `slug` | string | 공개 URL 식별자 (`@slug` 형태). **읽기 전용** |
| `title` | string | 페이지 제목 |
| `is_public` | bool | `true`이면 누구나 열람 가능 |
| `data` | object | 프론트엔드 전용 설정 저장소 (테마, 배경색 등) |
| `created_at` | datetime | ISO 8601 |
| `updated_at` | datetime | ISO 8601 |

## 프론트엔드 통합 패턴
```typescript
// 내 페이지 목록 조회 → 대시보드 페이지 선택 UI
const { data: pages } = await api.get('/api/v1/pages/multipages/');
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
        """,
        responses={
            200: OpenApiResponse(
                response=MultiPageSerializer(many=True),
                description="내 페이지 목록 (생성일 오름차순)",
                examples=[
                    OpenApiExample(
                        "Success",
                        value=[
                            {
                                "id": 1,
                                "slug": "hong-gildong",
                                "title": "메인 링크 페이지",
                                "is_public": True,
                                "data": {"theme": "dark"},
                                "created_at": "2026-03-01T00:00:00Z",
                                "updated_at": "2026-03-01T00:00:00Z",
                            },
                            {
                                "id": 3,
                                "slug": "my-product-page",
                                "title": "상품 소개 페이지",
                                "is_public": False,
                                "data": {},
                                "created_at": "2026-03-10T12:00:00Z",
                                "updated_at": "2026-03-10T12:00:00Z",
                            },
                        ],
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        pages = Page.objects.filter(user=request.user).order_by("created_at")
        return Response(MultiPageSerializer(pages, many=True).data)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="새 페이지 생성",
        description="""
## 개요
새 블록형 링크 페이지를 생성합니다.  
계정당 페이지 수에 제한은 없습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `slug` | 선택 | string | 공개 URL 식별자. **생략 시 자동 생성** (username 기반 + 숫자 suffix) |
| `title` | 선택 | string | 페이지 제목. 기본값 빈 문자열 |
| `is_public` | 선택 | bool | 공개 여부. 기본값 `false` |
| `data` | 선택 | object | 프론트엔드 전용 설정 저장소 |

## slug 자동 생성 정책
- `slug` 미지정 시: `{username}-2`, `{username}-3` ... 형태로 자동 부여
- 충돌 시 숫자 suffix를 계속 증가시켜 고유 slug 확보

## slug 형식 규칙
- 영문 소문자(a-z), 숫자(0-9), 하이픈(-) 만 허용
- 첫글자/끝에 하이픈 불가
- 2자 이상 120자 이하

## 에러
| 코드 | 원인 |
|------|------|
| 400 | slug 형식 오류 또는 이미 사용 중인 slug |
| 401 | 토큰 없음/만료 |
        """,
        request=MultiPageCreateSerializer,
        responses={
            201: OpenApiResponse(
                response=MultiPageSerializer,
                description="생성된 페이지",
                examples=[
                    OpenApiExample(
                        "slug 자동 생성",
                        value={
                            "id": 3,
                            "slug": "hong-gildong-2",
                            "title": "두 번째 링크 페이지",
                            "is_public": False,
                            "data": {},
                            "created_at": "2026-03-10T12:00:00Z",
                            "updated_at": "2026-03-10T12:00:00Z",
                        },
                    ),
                    OpenApiExample(
                        "slug 직접 지정",
                        value={
                            "id": 4,
                            "slug": "my-product-page",
                            "title": "상품 소개 페이지",
                            "is_public": True,
                            "data": {"theme": "light"},
                            "created_at": "2026-03-11T09:00:00Z",
                            "updated_at": "2026-03-11T09:00:00Z",
                        },
                    ),
                ],
            ),
            400: OpenApiResponse(
                description="유효성 검증 실패",
                examples=[
                    OpenApiExample(
                        "slug 중복",
                        value={"slug": ["이미 사용 중인 slug입니다."]},
                    ),
                    OpenApiExample(
                        "slug 형식 오류",
                        value={"slug": ["Enter a valid 'slug' consisting of letters, numbers, underscores or hyphens."]},
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        serializer = MultiPageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vd = serializer.validated_data

        slug = vd.get("slug") or None
        if not slug:
            from .models import _generate_unique_slug
            # username 기반으로 생성하되 이미 slug가 있으면 suffix 증가
            base = request.user.username
            from django.utils.text import slugify
            base_slug = slugify(base) or "page"
            candidate = base_slug
            counter = 2
            # 이미 사용 중이면 숫자 suffix
            while Page.objects.filter(slug=candidate).exists():
                candidate = f"{base_slug}-{counter}"
                counter += 1
            slug = candidate

        page = Page.objects.create(
            user=request.user,
            slug=slug,
            title=vd.get("title", ""),
            is_public=vd.get("is_public", False),
            data=vd.get("data", {}),
        )
        return Response(MultiPageSerializer(page).data, status=status.HTTP_201_CREATED)


# ═════════════════════════════════════════════════════════════
# 페이지 상세 / 수정 / 삭제
# ═════════════════════════════════════════════════════════════

class MultiPageDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 조회",
        description="""
## 개요
페이지 ID로 특정 페이지 정보를 조회합니다.  
**본인 소유 페이지만** 조회 가능합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID (`GET /api/v1/pages/multipages/` 응답의 `id` 필드) |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        parameters=[
            OpenApiParameter(
                name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH,
                description="조회할 페이지 ID",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=MultiPageSerializer,
                description="페이지 정보",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 3,
                            "slug": "my-product-page",
                            "title": "상품 소개 페이지",
                            "is_public": True,
                            "data": {"theme": "light", "background_color": "#ffffff"},
                            "created_at": "2026-03-10T12:00:00Z",
                            "updated_at": "2026-03-10T12:00:00Z",
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        return Response(MultiPageSerializer(page).data)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 수정",
        description="""
## 개요
특정 페이지의 메타 정보를 수정합니다. **PATCH** 방식이므로 변경할 필드만 전송하면 됩니다.  
**본인 소유 페이지만** 수정 가능합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 수정 가능 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `title` | string | 페이지 상단 제목. 빈 문자열 허용 |
| `is_public` | bool | `true` → 즉시 전체 공개. `false` → 비공개 전환 |
| `data` | object | 프론트엔드 전용 설정 저장소. 전송한 값으로 **전체 덮어쓰기** |

> **`slug`는 이 API로 변경 불가합니다.**  
> slug 변경은 `PATCH /api/v1/pages/multipages/{id}/slug/` 를 사용하세요.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 필드 타입 오류 |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        request=MultiPageSerializer,
        responses={
            200: OpenApiResponse(
                response=MultiPageSerializer,
                description="수정된 페이지",
                examples=[
                    OpenApiExample(
                        "성공 응답",
                        value={
                            "id": 3,
                            "slug": "my-product-page",
                            "title": "업데이트된 제목",
                            "is_public": True,
                            "data": {"theme": "dark"},
                            "created_at": "2026-03-10T12:00:00Z",
                            "updated_at": "2026-03-15T09:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def patch(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MultiPageSerializer(page, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 삭제",
        description="""
## 개요
특정 페이지를 **영구 삭제**합니다.  
페이지에 속한 블록, 통계, 문의, 구독자, 미디어 파일이 모두 함께 삭제됩니다.

> ⚠️ **삭제 후 복구 불가**  
> 공개 중인 페이지를 삭제하면 방문자는 즉시 404를 받게 됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 삭제할 페이지 ID |

## 응답
성공 시 **204 No Content** — 바디 없음.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        parameters=[
            OpenApiParameter(
                name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH,
                description="삭제할 페이지 ID",
            ),
        ],
        responses={
            204: OpenApiResponse(description="삭제 완료 — 바디 없음"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def delete(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        page.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ═════════════════════════════════════════════════════════════
# 페이지 slug 변경
# ═════════════════════════════════════════════════════════════

class MultiPageSlugChangeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 slug 변경",
        description="""
## 개요
특정 페이지의 공개 URL slug를 변경합니다.  
**변경 즉시 기존 slug는 사용 불가** — 기존 URL로 접속하는 방문자는 새 slug로 안내해주세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 요청 필드
| 필드 | 필수 | 설명 |
|------|------|------|
| `slug` | ✅ | 새 slug. 영문 소문자/숫자/하이픈만 허용, 2~120자 |

## slug 형식 규칙
- 영문 소문자(a-z), 숫자(0-9), 하이픈(-) 만 허용
- 첫글자/끝에 하이픈 불가
- 2자 이상 120자 이하
- 대소문자 입력 시 소문자로 자동 변환

## 권장 흐름
```
1. GET /api/v1/pages/check-slug/?slug=new-name  → available: true 확인
2. PATCH /api/v1/pages/multipages/{id}/slug/  { slug: "new-name" }  → 변경 완료
3. 프론트 저장된 공개 URL을 새 slug로 갱신
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | slug 형식 오류 또는 이미 사용 중인 slug |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        request=MultiPageSlugChangeSerializer,
        responses={
            200: OpenApiResponse(
                response=MultiPageSerializer,
                description="slug가 변경된 페이지 정보",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 3,
                            "slug": "new-slug-name",
                            "title": "상품 소개 페이지",
                            "is_public": True,
                            "data": {},
                            "created_at": "2026-03-10T12:00:00Z",
                            "updated_at": "2026-03-15T10:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="유효성 검증 실패",
                examples=[
                    OpenApiExample("slug 중복", value={"slug": ["이미 사용 중인 slug입니다."]}),
                    OpenApiExample("형식 오류", value={"slug": ["slug는 2자 이상이어야 합니다."]}),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def patch(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MultiPageSlugChangeSerializer(
            data=request.data, context={"page_id": page_id}
        )
        serializer.is_valid(raise_exception=True)
        page.slug = serializer.validated_data["slug"]
        page.save(update_fields=["slug", "updated_at"])
        return Response(MultiPageSerializer(page).data)


# ═════════════════════════════════════════════════════════════
# 커스텀 CSS 수정
# ═════════════════════════════════════════════════════════════

class MultiPageCustomCssView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["커스텀 CSS"],
        summary="특정 페이지 커스텀 CSS 조회",
        description="""
## 개요
특정 페이지에 적용된 **커스텀 CSS**를 조회합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 응답
| 필드 | 타입 | 설명 |
|------|------|------|
| `custom_css` | string | 현재 저장된 CSS 문자열. 빈 문자열이면 커스텀 CSS 미설정 |
        """,
        parameters=[
            OpenApiParameter(
                name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH,
                description="페이지 ID",
            ),
        ],
        responses={
            200: OpenApiResponse(response=CustomCssSerializer, description="현재 커스텀 CSS"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"custom_css": page.custom_css})

    @extend_schema(
        tags=["커스텀 CSS"],
        summary="특정 페이지 커스텀 CSS 수정",
        description="""
## 개요
특정 페이지에 적용할 **커스텀 CSS**를 저장합니다.  
프론트엔드에서 공개 페이지 렌더링 시 `<style>` 태그로 주입하여 사용합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `custom_css` | ✅ | string | 적용할 CSS 문자열. 빈 문자열 `""` 전송 시 초기화 |

## 프론트엔드 통합 패턴
```typescript
// CSS 저장
await api.patch(`/api/v1/pages/multipages/${pageId}/css/`, {
  custom_css: `.page-container { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
.block-link { border-radius: 16px; backdrop-filter: blur(10px); }`
});

// CSS 초기화
await api.patch(`/api/v1/pages/multipages/${pageId}/css/`, { custom_css: '' });
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | `custom_css` 필드 누락 |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        parameters=[
            OpenApiParameter(
                name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH,
                description="페이지 ID",
            ),
        ],
        request=CustomCssSerializer,
        responses={
            200: OpenApiResponse(response=MultiPageSerializer, description="수정된 페이지 전체 정보"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def patch(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = CustomCssSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        page.custom_css = serializer.validated_data["custom_css"]
        page.save(update_fields=["custom_css", "updated_at"])
        return Response(MultiPageSerializer(page).data)


# ═════════════════════════════════════════════════════════════
# 블록 커스텀 CSS 수정
# ═════════════════════════════════════════════════════════════

class MultiBlockCustomCssView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["커스텀 CSS"],
        summary="블록 커스텀 CSS 조회 (다중 페이지)",
        description="""
## 개요
특정 페이지의 특정 블록에 적용된 **커스텀 CSS**를 조회합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `block_id` | int | 블록 ID |
        """,
        parameters=[
            OpenApiParameter(name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH, description="페이지 ID"),
            OpenApiParameter(name="block_id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH, description="블록 ID"),
        ],
        responses={
            200: OpenApiResponse(response=CustomCssSerializer, description="현재 커스텀 CSS"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지/블록 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int, block_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        block = Block.objects.filter(pk=block_id, page=page).first()
        if not block:
            return Response({"detail": "블록을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"custom_css": block.custom_css})

    @extend_schema(
        tags=["커스텀 CSS"],
        summary="블록 커스텀 CSS 수정 (다중 페이지)",
        description="""
## 개요
특정 페이지의 특정 블록에 적용할 **커스텀 CSS**를 저장합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `block_id` | int | 블록 ID |

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `custom_css` | ✅ | string | 적용할 CSS 문자열. 빈 문자열 `""` 전송 시 초기화 |

## 프론트엔드 통합 패턴
```typescript
await api.patch(`/api/v1/pages/multipages/${pageId}/blocks/${blockId}/css/`, {
  custom_css: `.block-wrapper { box-shadow: 0 4px 20px rgba(0,0,0,0.1); }`
});
```
        """,
        parameters=[
            OpenApiParameter(name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH, description="페이지 ID"),
            OpenApiParameter(name="block_id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH, description="블록 ID"),
        ],
        request=CustomCssSerializer,
        responses={
            200: OpenApiResponse(response=BlockSerializer, description="수정된 블록 전체 정보"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지/블록 없음 또는 접근 권한 없음"),
        },
    )
    def patch(self, request, page_id: int, block_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        block = Block.objects.filter(pk=block_id, page=page).first()
        if not block:
            return Response({"detail": "블록을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = CustomCssSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        block.custom_css = serializer.validated_data["custom_css"]
        block.save(update_fields=["custom_css", "updated_at"])
        return Response(BlockSerializer(block).data)


# ═════════════════════════════════════════════════════════════
# 블록 목록 / 생성
# ═════════════════════════════════════════════════════════════

class MultiBlockListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 블록 목록 조회",
        description="""
## 개요
특정 페이지의 **전체 블록**을 `order` 오름차순으로 반환합니다.  
`is_enabled: false`인 비활성 블록도 포함됩니다 (편집 화면에서 표시 여부 토글 가능).

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 블록 고유 ID. PATCH/DELETE 시 URL에 사용 |
| `type` | string | `profile` \| `contact` \| `single_link` |
| `order` | int | 표시 순서 (1부터 시작) |
| `is_enabled` | bool | `false`면 공개 페이지에서 숨김 |
| `data` | object | 타입별 콘텐츠 |
| `schedule_enabled` | bool | 예약 설정 활성화 여부 |
| `publish_at` | datetime\|null | 공개 시작 일시 |
| `hide_at` | datetime\|null | 숨김 시작 일시 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        responses={
            200: OpenApiResponse(response=BlockSerializer(many=True), description="블록 목록"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        blocks = page.blocks.order_by("order")
        return Response(BlockSerializer(blocks, many=True).data)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지에 블록 생성",
        description="""
## 개요
특정 페이지에 새 블록을 추가합니다. **`type`은 생성 후 변경 불가**이므로 신중하게 선택하세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 블록을 추가할 페이지 ID |

## 요청 필드
| 필드 | 필수 | 설명 |
|------|------|------|
| `type` | ✅ | 블록 종류 (`profile` / `contact` / `single_link`) |
| `data` | ✅ | 타입별 콘텐츠 객체 |
| `order` | 선택 | 미지정 시 현재 마지막 블록 다음 순번 자동 부여 |
| `is_enabled` | 선택 | 기본값 `true` |
| `schedule_enabled` | 선택 | `true`로 설정하면 publish_at/hide_at 기준으로 자동 공개/숨김 |
| `publish_at` | 조건부 | 공개 시작 시각 (ISO 8601). `schedule_enabled=true`일 때 필요 |
| `hide_at` | 조건부 | 숨김 시작 시각 (ISO 8601). `schedule_enabled=true`일 때 필요 |

## 타입별 `data` 스키마

### `profile` — 프로필 소개 블록
```json
{
  "headline": "독일 면도기 전문",   // 필수
  "subline": "방수 / 저소음",       // 선택
  "avatar_url": "https://..."      // 선택
}
```

### `contact` — 연락처 블록
```json
{
  "country_code": "+82",           // 필수
  "phone": "01012345678",          // 필수
  "whatsapp": true                 // 선택
}
```

### `single_link` — 단일 링크 버튼 블록
```json
{
  "url": "https://naver.me/abc",   // 필수
  "label": "쿠팡 추천 링크",        // 필수
  "description": "오늘만 할인",    // 선택
  "layout": "small",              // 선택. 'small'(기본) | 'large'
  "thumbnail_url": "https://..."  // 선택
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 필수 data 필드 누락, URL 형식 오류, order 중복 |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        request=BlockSerializer,
        responses={
            201: OpenApiResponse(response=BlockSerializer, description="생성된 블록"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def post(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = BlockSerializer(data=request.data, context={"page": page})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


# ═════════════════════════════════════════════════════════════
# 블록 상세 (수정 / 삭제)
# ═════════════════════════════════════════════════════════════

class MultiBlockDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_block(self, request, page_id: int, block_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return None, None
        block = Block.objects.filter(pk=block_id, page=page).first()
        return page, block

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 블록 수정",
        description="""
## 개요
블록의 콘텐츠(`data`), 표시 여부(`is_enabled`), 순서(`order`)를 수정합니다.  
**PATCH** 방식이므로 변경할 필드만 전송하면 됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수 (소유자만 가능)

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `block_id` | int | 수정할 블록 ID |

## 수정 가능 필드
| 필드 | 설명 |
|------|------|
| `data` | 타입별 콘텐츠 객체 전체를 교체 |
| `is_enabled` | `false`로 변경 시 공개 페이지에서 즉시 숨김 |
| `order` | 직접 순서 변경. 중복 금지. 다수 블록 재정렬은 reorder API 사용 권장 |
| `schedule_enabled` | `true`로 설정하면 예약 조건에 따라 자동 공개/숨김 |
| `publish_at` | 공개 시각 (ISO 8601, 타임존 포함) |
| `hide_at` | 숨김 시각 (ISO 8601, 타임존 포함) |

## 제약
- **`type` 변경 불가** → 타입 변경이 필요하면 삭제 후 재생성
- `data` 부분 수정 불가 → 전체 object를 새로 전송

## 에러
| 코드 | 원인 |
|------|------|
| 400 | type 변경 시도 / data 필수 필드 누락 |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 블록 없음 |
        """,
        request=BlockSerializer,
        responses={
            200: OpenApiResponse(response=BlockSerializer, description="수정된 블록"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 블록 없음"),
        },
    )
    def patch(self, request, page_id: int, block_id: int):
        page, block = self._get_block(request, page_id, block_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        if not block:
            return Response({"detail": "블록을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = BlockSerializer(block, data=request.data, partial=True, context={"page": page})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 블록 삭제",
        description="""
## 개요
블록을 영구 삭제합니다. **복구 불가**하므로 UI에서 `is_enabled: false`로 숨기는 것을 먼저 고려하세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수 (소유자만 가능)

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `block_id` | int | 삭제할 블록 ID |

## 동작
- 해당 블록의 `order` 값이 비워지지만, 나머지 블록의 order는 재정렬되지 않습니다.
- 필요 시 삭제 후 reorder API로 순번 정리를 권장합니다.

## 응답
성공 시 **204 No Content** — 바디 없음.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 블록 없음 |
        """,
        responses={
            204: OpenApiResponse(description="삭제 완료"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 블록 없음"),
        },
    )
    def delete(self, request, page_id: int, block_id: int):
        page, block = self._get_block(request, page_id, block_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        if not block:
            return Response({"detail": "블록을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        block.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ═════════════════════════════════════════════════════════════
# 블록 순서 재정렬
# ═════════════════════════════════════════════════════════════

class MultiBlockReorderView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 블록 순서 재정렬",
        description="""
## 개요
특정 페이지의 여러 블록 `order`를 **하나의 트랜잭션**으로 원자적으로 변경합니다.  
드래그 앤 드롭 정렬 완료 후 호출하는 것을 권장합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 요청 형식
```json
{
  "orders": [
    { "id": 10, "order": 1 },
    { "id": 11, "order": 2 },
    { "id": 12, "order": 3 }
  ]
}
```

## 동작 방식
- 포함된 id의 block만 order 변경됨 (전체 블록 포함 불필요)
- 내부적으로 `CASE/WHEN` 단일 UPDATE 쿼리로 처리 — 원자적 순서 교환 보장
- 실패 시 전체 롤백

## 제약
| 조건 | 결과 |
|------|------|
| `orders` 배열이 비어 있음 | 400 |
| `order` 값 중복 | 400 |
| `id` 값 중복 | 400 |
| 다른 페이지의 블록 id 포함 | 400 |

## 드래그 드롭 통합 예시
```typescript
const handleDragEnd = async (reorderedBlocks: Block[]) => {
  const orders = reorderedBlocks.map((b, i) => ({ id: b.id, order: i + 1 }));
  await api.post(`/api/v1/pages/multipages/${pageId}/blocks/reorder/`, { orders });
};
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 유효성 검증 실패 또는 권한 없는 블록 포함 |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        request=ReorderSerializer,
        responses={
            200: OpenApiResponse(response=BlockSerializer(many=True), description="재정렬된 블록 목록"),
            400: OpenApiResponse(description="유효성 검증 실패 또는 권한 없는 블록 포함"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def post(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orders = serializer.validated_data["orders"]

        requested_ids = [item["id"] for item in orders]

        if Block.objects.filter(pk__in=requested_ids, page=page).count() != len(requested_ids):
            return Response(
                {"detail": "요청한 블록 중 이 페이지에 속하지 않거나 존재하지 않는 블록이 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        id_to_order = {item["id"]: item["order"] for item in orders}
        with transaction.atomic():
            cases = [When(pk=pk, then=order) for pk, order in id_to_order.items()]
            Block.objects.filter(pk__in=id_to_order.keys(), page=page).update(
                order=Case(*cases, output_field=IntegerField())
            )

        updated = page.blocks.order_by("order")
        return Response(BlockSerializer(updated, many=True).data)


# ═════════════════════════════════════════════════════════════
# 통계
# ═════════════════════════════════════════════════════════════

class MultiPageStatsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 통계 요약",
        description="""
## 개요
특정 페이지의 기간별 조회수·클릭수·클릭율과 유입 채널/국가 Top5를 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 |
|---------|--------|--------|
| `period` | `7d` | `7d` `30d` `90d` |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `period` | string | 조회 기간 |
| `total_views` | int | 기간 내 총 조회수 |
| `total_clicks` | int | 기간 내 총 클릭수 |
| `click_rate` | float | 클릭율 = 클릭수/조회수 × 100 (%) |
| `referers` | array | 유입 채널 Top5 (`source`, `count`, `percentage`) |
| `countries` | array | 유입 국가 Top5 (`code`, `name`, `count`, `percentage`) |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        parameters=[
            OpenApiParameter(
                name="period", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                description="조회 기간. `7d`=7일, `30d`=30일, `90d`=90일",
                required=False, enum=["7d", "30d", "90d"],
            ),
        ],
        responses={
            200: OpenApiResponse(response=StatsSummarySerializer, description="통계 요약"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        period_key, days = resolve_period(request.query_params.get("period", "7d"))
        data = get_stats_summary(page, days)
        data["period"] = period_key
        return Response(StatsSummarySerializer(data).data)


class MultiStatsChartView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 통계 차트 데이터",
        description="""
## 개요
특정 페이지의 날짜별 조회수·클릭수 배열을 반환합니다 (라인 차트용).

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 |
|---------|--------|--------|
| `period` | `7d` | `7d` `30d` `90d` |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `period` | string | 조회 기간 |
| `labels` | string[] | 날짜 배열 (`YYYY-MM-DD`) |
| `views` | int[] | labels와 같은 길이의 일별 조회수 배열 |
| `clicks` | int[] | labels와 같은 길이의 일별 클릭수 배열 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        parameters=[
            OpenApiParameter(
                name="period", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                description="조회 기간. `7d`=7일, `30d`=30일, `90d`=90일",
                required=False, enum=["7d", "30d", "90d"],
            ),
        ],
        responses={
            200: OpenApiResponse(response=ChartDataSerializer, description="날짜별 차트 데이터"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        period_key, days = resolve_period(request.query_params.get("period", "7d"))
        data = get_chart_data(page, days)
        data["period"] = period_key
        return Response(ChartDataSerializer(data).data)


class MultiStatsBlocksView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 블록별 클릭 통계",
        description="""
## 개요
특정 페이지의 각 블록의 기간 내 클릭수와 클릭율을 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 |
|---------|--------|--------|
| `period` | `7d` | `7d` `30d` `90d` |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `period` | string | 조회 기간 |
| `blocks` | array | 블록별 클릭 통계 배열 |
| `blocks[].block_id` | int | 블록 ID |
| `blocks[].type` | string | 블록 타입 |
| `blocks[].label` | string | 블록 표시명 |
| `blocks[].clicks` | int | 기간 내 클릭수 |
| `blocks[].click_rate` | float | 클릭율 (%) |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        parameters=[
            OpenApiParameter(
                name="period", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                description="조회 기간. `7d`=7일, `30d`=30일, `90d`=90일",
                required=False, enum=["7d", "30d", "90d"],
            ),
        ],
        responses={
            200: OpenApiResponse(response=BlockStatsSerializer, description="블록별 클릭 통계"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        period_key, days = resolve_period(request.query_params.get("period", "7d"))
        blocks = get_block_stats(page, days)
        data = {"period": period_key, "blocks": blocks}
        return Response(BlockStatsSerializer(data).data)


class MultiStatsLinksView(APIView):
    """GET multipages/{id}/stats/links/ — 서브링크별 클릭수 (link_clicks) 반환."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지 서브링크별 클릭수 (link_clicks)",
        description="""
## 개요
특정 페이지의 기간 내 **서브링크별 클릭수**를 `link_clicks` 배열로 반환합니다.  
`link_id`가 있는 클릭(social, group_link 등)은 서브링크 단위로 분리되고,
`link_id`가 없는 클릭(single_link 등)은 블록 단위로 합산됩니다.

## 사용 시나리오
- social 블록: instagram 3회, youtube 5회처럼 플랫폼별 분리 통계
- group_link 블록: 그룹 내 개별 링크별 클릭수 분리
- single_link / gallery 블록: 기존처럼 블록 단위 합산

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 |
|---------|--------|--------|
| `period` | `7d` | `7d` `30d` `90d` |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `period` | string | 조회 기간 |
| `total_clicks` | int | 해당 기간 페이지 전체 클릭수 |
| `link_clicks` | array | 서브링크별 클릭 통계 |
| `link_clicks[].block_id` | int | 블록 ID (같은 block_id가 여러 entry로 올 수 있음) |
| `link_clicks[].link_id` | string | 서브링크 ID (빈 문자열이면 블록 단위) |
| `link_clicks[].type` | string | 블록 타입 (social / group_link / single_link 등) |
| `link_clicks[].label` | string | 서브링크 표시명 |
| `link_clicks[].is_enabled` | bool | 노출 여부 |
| `link_clicks[].clicks` | int | 기간 내 클릭수 |
| `link_clicks[].click_rate` | float | 클릭율 (%) |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        parameters=[
            OpenApiParameter(
                name="period", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                description="조회 기간. `7d`=7일, `30d`=30일, `90d`=90일",
                required=False, enum=["7d", "30d", "90d"],
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=LinkClicksStatsSerializer,
                description="서브링크별 클릭수 (link_clicks)",
                examples=[
                    OpenApiExample(
                        "서브링크 분리 예시",
                        value={
                            "period": "7d",
                            "total_clicks": 18,
                            "link_clicks": [
                                {
                                    "block_id": 17,
                                    "link_id": "youtube",
                                    "type": "social",
                                    "label": "youtube",
                                    "is_enabled": True,
                                    "clicks": 5,
                                    "click_rate": 12.5,
                                },
                                {
                                    "block_id": 20,
                                    "link_id": "1773124482018",
                                    "type": "group_link",
                                    "label": "쿠팡 링크",
                                    "is_enabled": True,
                                    "clicks": 4,
                                    "click_rate": 10.0,
                                },
                                {
                                    "block_id": 17,
                                    "link_id": "instagram",
                                    "type": "social",
                                    "label": "instagram",
                                    "is_enabled": True,
                                    "clicks": 3,
                                    "click_rate": 7.5,
                                },
                                {
                                    "block_id": 20,
                                    "link_id": "1773124499001",
                                    "type": "group_link",
                                    "label": "네이버 링크",
                                    "is_enabled": True,
                                    "clicks": 3,
                                    "click_rate": 7.5,
                                },
                                {
                                    "block_id": 5,
                                    "link_id": "",
                                    "type": "single_link",
                                    "label": "블로그",
                                    "is_enabled": True,
                                    "clicks": 3,
                                    "click_rate": 7.5,
                                },
                            ],
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        period_key, days = resolve_period(request.query_params.get("period", "7d"))
        link_clicks = get_link_stats(page, days)
        total_clicks = sum(b["clicks"] for b in link_clicks)
        data = {"period": period_key, "total_clicks": total_clicks, "link_clicks": link_clicks}
        return Response(LinkClicksStatsSerializer(data).data)


# ═════════════════════════════════════════════════════════════
# 문의 관리
# ═════════════════════════════════════════════════════════════

class MultiInquiryListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 문의 목록 조회 (관리자)",
        description="""
## 개요
특정 페이지에 들어온 문의 목록을 최신순으로 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 | 설명 |
|---------|--------|--------|------|
| `period` | `all` | `all` `6m` `1m` `7d` | 조회 기간 |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 문의 ID |
| `name` | string | 보낸 사람 |
| `category` | string | 분류 코드 (`general` `business` `support` `other`) |
| `category_display` | string | 한글 분류명 |
| `email` | string | 이메일 |
| `phone` | string | 휴대폰번호 |
| `subject` | string | 문의 제목 |
| `content` | string | 문의 내용 |
| `agreed_to_terms` | boolean | 동의 여부 |
| `memo` | string | 관리자 메모 (없으면 빈 문자열) |
| `created_at` | datetime | 문의 일시 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        parameters=[
            OpenApiParameter(
                name="period", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                description="조회 기간. `all`=전체, `6m`=6개월, `1m`=1개월, `7d`=7일",
                required=False, enum=["all", "6m", "1m", "7d"],
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=ContactInquirySerializer(many=True),
                description="문의 목록",
                examples=[
                    OpenApiExample(
                        "Success",
                        value=[
                            {
                                "id": 5,
                                "name": "방문자",
                                "category": "business",
                                "category_display": "비즈니스 협업",
                                "email": "visitor@example.com",
                                "phone": "010-9876-5432",
                                "subject": "협업 문의",
                                "content": "안녕하세요, 협업 관련 문의드립니다.",
                                "agreed_to_terms": True,
                                "memo": "",
                                "created_at": "2026-03-12T10:00:00Z",
                                "updated_at": "2026-03-12T10:00:00Z",
                            }
                        ],
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        qs = ContactInquiry.objects.filter(page=page)
        period = request.query_params.get("period", "all")
        days = _PERIOD_MAP.get(period)
        if days is not None:
            since = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=since)

        return Response(ContactInquirySerializer(qs, many=True).data)


class MultiInquiryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_inquiry(self, request, page_id: int, pk: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return None, None
        return page, ContactInquiry.objects.filter(pk=pk, page=page).first()

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 문의 삭제 (관리자)",
        description="""
## 개요
특정 페이지의 문의 1건을 영구 삭제합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `pk` | int | 삭제할 문의 ID |

## 응답
성공 시 **204 No Content** — 바디 없음.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 문의 없음 |
        """,
        responses={
            204: OpenApiResponse(description="삭제 완료"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 문의 없음"),
        },
    )
    def delete(self, request, page_id: int, pk: int):
        page, inquiry = self._get_inquiry(request, page_id, pk)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        if not inquiry:
            return Response({"detail": "문의를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        inquiry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 문의 메모 수정 (관리자)",
        description="""
## 개요
특정 페이지의 문의에 **관리자 메모**를 작성하거나 수정합니다.  
메모는 관리자만 볼 수 있으며 문의자에게 전달되지 않습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `pk` | int | 메모를 수정할 문의 ID |

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `memo` | ✅ | string | 메모 내용. 빈 문자열로 메모 삭제 가능 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 문의 없음 |
        """,
        request=ContactInquiryMemoSerializer,
        examples=[
            OpenApiExample("메모 작성", request_only=True, value={"memo": "확인완료. 다음 주에 답변 예정"}),
            OpenApiExample("메모 삭제", request_only=True, value={"memo": ""}),
        ],
        responses={
            200: OpenApiResponse(
                response=ContactInquiryMemoSerializer,
                description="수정된 메모",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={"id": 5, "memo": "확인완료. 다음 주에 답변 예정", "updated_at": "2026-03-12T15:00:00Z"},
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 문의 없음"),
        },
    )
    def patch(self, request, page_id: int, pk: int):
        page, inquiry = self._get_inquiry(request, page_id, pk)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        if not inquiry:
            return Response({"detail": "문의를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = ContactInquiryMemoSerializer(inquiry, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ═════════════════════════════════════════════════════════════
# 구독자 관리
# ═════════════════════════════════════════════════════════════

class MultiSubscriptionListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 구독자 목록 조회 (관리자)",
        description="""
## 개요
특정 페이지에 등록된 구독자 목록을 최신순으로 반환합니다.  
기간 필터와 키워드 검색을 지원합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 | 설명 |
|---------|--------|--------|------|
| `period` | `all` | `all` `6m` `1m` `7d` | 조회 기간 |
| `q` | - | 문자열 | 이름·이메일·휴대폰번호 통합 검색 |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 구독자 ID |
| `name` | string | 이름 |
| `category` | string | 분류 코드 (`page_subscribe` `newsletter` `event` `other`) |
| `category_display` | string | 한글 분류명 |
| `email` | string | 이메일 |
| `phone` | string | 휴대폰번호 |
| `agreed_to_terms` | boolean | 개인정보 수집 동의 여부 |
| `memo` | string | 관리자 메모 (없으면 빈 문자열) |
| `created_at` | datetime | 구독 일시 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        parameters=[
            OpenApiParameter(
                name="period", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                description="조회 기간. `all`=전체, `6m`=6개월, `1m`=1개월, `7d`=7일",
                required=False, enum=["all", "6m", "1m", "7d"],
            ),
            OpenApiParameter(
                name="q", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY,
                description="키워드 검색 — 이름, 이메일, 휴대폰번호를 통합 검색합니다.",
                required=False,
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=PageSubscriptionSerializer(many=True),
                description="구독자 목록",
                examples=[
                    OpenApiExample(
                        "Success",
                        value=[
                            {
                                "id": 3,
                                "name": "홍길동",
                                "category": "newsletter",
                                "category_display": "뉴스레터",
                                "email": "gildong@example.com",
                                "phone": "010-1234-5678",
                                "agreed_to_terms": True,
                                "memo": "",
                                "created_at": "2026-03-12T08:00:00Z",
                                "updated_at": "2026-03-12T08:00:00Z",
                            }
                        ],
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        qs = PageSubscription.objects.filter(page=page)

        period = request.query_params.get("period", "all")
        days = _PERIOD_MAP.get(period)
        if days is not None:
            since = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=since)

        q = request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q)
            )

        return Response(PageSubscriptionSerializer(qs, many=True).data)


class MultiSubscriptionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_subscription(self, request, page_id: int, pk: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return None, None
        return page, PageSubscription.objects.filter(pk=pk, page=page).first()

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 구독자 삭제 (관리자)",
        description="""
## 개요
특정 페이지의 구독자 1건을 영구 삭제합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `pk` | int | 삭제할 구독자 ID |

## 응답
성공 시 **204 No Content** — 바디 없음.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 구독자 없음 |
        """,
        responses={
            204: OpenApiResponse(description="삭제 완료"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 구독자 없음"),
        },
    )
    def delete(self, request, page_id: int, pk: int):
        page, subscription = self._get_subscription(request, page_id, pk)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        if not subscription:
            return Response({"detail": "구독자를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        subscription.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 구독자 메모 수정 (관리자)",
        description="""
## 개요
특정 페이지의 구독자에 **관리자 메모**를 작성하거나 수정합니다.  
메모는 관리자만 볼 수 있으며 구독자에게 노출되지 않습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `pk` | int | 메모를 수정할 구독자 ID |

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `memo` | ✅ | string | 메모 내용. 빈 문자열로 메모 삭제 가능 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 구독자 없음 |
        """,
        request=PageSubscriptionMemoSerializer,
        examples=[
            OpenApiExample("메모 작성", request_only=True, value={"memo": "VIP 구독자 — 이메일 발송 우선"}),
            OpenApiExample("메모 삭제", request_only=True, value={"memo": ""}),
        ],
        responses={
            200: OpenApiResponse(
                response=PageSubscriptionMemoSerializer,
                description="수정된 메모",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={"id": 3, "memo": "VIP 구독자 — 이메일 발송 우선", "updated_at": "2026-03-12T16:00:00Z"},
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 구독자 없음"),
        },
    )
    def patch(self, request, page_id: int, pk: int):
        page, subscription = self._get_subscription(request, page_id, pk)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        if not subscription:
            return Response({"detail": "구독자를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        serializer = PageSubscriptionMemoSerializer(subscription, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ═════════════════════════════════════════════════════════════
# 미디어 파일 관리
# ═════════════════════════════════════════════════════════════

class MultiPageMediaView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 미디어 파일 목록 조회",
        description="""
## 개요
특정 페이지에 업로드된 **이미지 파일 목록**을 최신순으로 반환합니다.  
블록 편집 화면에서 기존 업로드된 이미지를 재사용할 때 호출합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 미디어 고유 ID. 삭제 시 사용 |
| `original_name` | string | 업로드 당시 파일 이름 |
| `mime_type` | string | MIME 타입 (예: `image/jpeg`) |
| `size` | int | 파일 크기 (bytes) |
| `size_display` | string | 사람이 읽기 좋은 크기 (예: `1.2 MB`) |
| `url` | string | **블록에 저장할 이미지 URL** |
| `created_at` | datetime | 업로드 일시 (ISO 8601) |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        responses={
            200: OpenApiResponse(
                response=PageMediaSerializer(many=True),
                description="미디어 파일 목록 (최신순)",
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        qs = PageMedia.objects.filter(page=page)
        return Response(PageMediaSerializer(qs, many=True, context={"request": request}).data)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지에 미디어 파일 업로드 (원본 + 크롭 파라미터 포함)",
        description="""
## 개요
특정 페이지에 블록에서 사용할 **이미지 파일을 서버에 업로드**합니다.  
**이미지 편집(크롭) 기능**을 지원하기 위해 완성본, 원본 이미지, 크롭 파라미터를 함께 저장합니다.  
업로드 완료 후 반환된 `url`을 `block.data` 의 URL 필드에 저장하는 **2단계 방식**입니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |

## 요청 형식
`Content-Type: multipart/form-data` 필수

| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `file` | ✅ | File | **편집(크롭) 완료된 최종 이미지** |
| `original_file` | ❌ | File | **편집 전 원본 이미지**. 재편집 시 사용 |
| `crop_data` | ❌ | string(JSON) | **크롭 파라미터**. 재편집 시 편집기 상태 복원용 |

## crop_data 기본 정책
| 상황 | 프론트엔드 동작 |
|------|----------------|
| `crop_data`가 `{}`(빈 객체) | 전체 영역(최대 크롭)으로 간주 |
| `locked` 미지정 | `false`로 간주 |
| `original_url`이 빈 문자열 | `url`(완성본)을 원본으로 간주 |

## 파일 제한
| 항목 | 제한 |
|------|------|
| 최대 크기 | **10 MB** (file, original_file 각각) |
| 허용 MIME | `image/jpeg` `image/png` `image/gif` `image/webp` `image/svg+xml` `image/bmp` `image/tiff` |

## 전체 흐름 예시
```typescript
const formData = new FormData();
formData.append('file', croppedBlob);
formData.append('original_file', originalFile);
formData.append('crop_data', JSON.stringify({
  x: 120, y: 80, width: 400, height: 300,
  aspect_ratio: '4:3', locked: true,
  original_width: 1200, original_height: 900,
}));
const { data: media } = await api.post(
  `/api/v1/pages/multipages/${pageId}/media/`,
  formData,
  { headers: { 'Content-Type': 'multipart/form-data' } }
);
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 파일 미첨부 / 허용되지 않는 MIME / 파일 크기 초과 / crop_data JSON 오류 |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 없음 또는 접근 권한 없음 |
        """,
        request=OpenApiTypes.BINARY,
        responses={
            201: OpenApiResponse(response=PageMediaSerializer, description="업로드 성공"),
            400: OpenApiResponse(
                description="유효성 검증 실패",
                examples=[
                    OpenApiExample("파일 미첨부", value={"file": ["파일을 첨부해 주세요."]}),
                    OpenApiExample("MIME 타입 오류", value={"file": ["지원하지 않는 파일 형식입니다."]}),
                    OpenApiExample("파일 크기 초과", value={"file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]}),
                    OpenApiExample("crop_data 오류", value={"crop_data": ["crop_data는 유효한 JSON이어야 합니다."]}),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
    )
    def post(self, request, page_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        file = request.FILES.get("file")
        if not file:
            return Response({"file": ["파일을 첨부해 주세요."]}, status=status.HTTP_400_BAD_REQUEST)

        mime_type = file.content_type or ""
        if mime_type not in _ALLOWED_MIME_TYPES:
            return Response(
                {"file": ["지원하지 않는 파일 형식입니다. 허용 타입: jpeg, png, gif, webp, svg, bmp, tiff"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if file.size > _MAX_FILE_SIZE_BYTES:
            return Response(
                {"file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 원본 파일 검증 (선택)
        original_file = request.FILES.get("original_file")
        if original_file:
            orig_mime = original_file.content_type or ""
            if orig_mime not in _ALLOWED_MIME_TYPES:
                return Response(
                    {"original_file": ["지원하지 않는 파일 형식입니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if original_file.size > _MAX_FILE_SIZE_BYTES:
                return Response(
                    {"original_file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # crop_data 파싱 (선택)
        crop_data = {}
        raw_crop = request.data.get("crop_data")
        if raw_crop:
            if isinstance(raw_crop, str):
                try:
                    crop_data = json.loads(raw_crop)
                except (json.JSONDecodeError, ValueError):
                    return Response(
                        {"crop_data": ["crop_data는 유효한 JSON이어야 합니다."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            elif isinstance(raw_crop, dict):
                crop_data = raw_crop

        media = PageMedia.objects.create(
            page=page,
            file=file,
            original_file=original_file,
            crop_data=crop_data,
            original_name=file.name,
            mime_type=mime_type,
            size=file.size,
        )
        return Response(
            PageMediaSerializer(media, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class MultiPageMediaDetailView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 미디어 파일 상세 조회 (재편집용)",
        description="""
## 개요
특정 페이지에 업로드된 미디어 파일 1건의 정보를 반환합니다.  
**이미지 재편집** 시 이 API를 호출하여 `original_url`과 `crop_data`를 가져온 뒤
편집기의 이전 상태를 복원합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `media_id` | int | 미디어 파일 ID |

## 재편집 흐름
```typescript
const { data: media } = await api.get(
  `/api/v1/pages/multipages/${pageId}/media/${mediaId}/`
);
const editUrl = media.original_url || media.url;
openEditor(editUrl, {
  ...media.crop_data,
  locked: media.crop_data.locked ?? false,
});
```

## crop_data 기본 정책
| 상황 | 프론트엔드 동작 |
|------|----------------|
| `crop_data`가 `{}`(빈) | 전체 영역(최대 크롭)으로 간주 |
| `locked` 미지정 | `false`로 간주 |
| `original_url`이 빈 문자열 | `url`(완성본)을 원본으로 사용 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 파일 없음 |
        """,
        responses={
            200: OpenApiResponse(response=PageMediaSerializer, description="미디어 파일 정보"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 파일 없음"),
        },
    )
    def get(self, request, page_id: int, media_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        media = PageMedia.objects.filter(pk=media_id, page=page).first()
        if not media:
            return Response({"detail": "파일을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        return Response(PageMediaSerializer(media, context={"request": request}).data)

    @extend_schema(
        tags=[_MULTIPAGE_TAG],
        summary="특정 페이지의 미디어 파일 재편집",
        description="""
## 개요
이미지 **재편집(재크롭) 완료 후** 완성본 파일과 크롭 파라미터를 업데이트합니다.  
원본 이미지(`original_file`)는 변경되지 않습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `media_id` | int | 수정할 미디어 파일 ID |

## 요청 형식
`Content-Type: multipart/form-data` 필수

| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `file` | ❌ | File | 재편집(재크롭) 완료된 새 최종 이미지 |
| `crop_data` | ❌ | string(JSON) | 새 크롭 파라미터 |

> 전송한 필드만 업데이트됩니다.

## 재편집 전체 흐름
```typescript
const { data: media } = await api.get(
  `/api/v1/pages/multipages/${pageId}/media/${mediaId}/`
);
const editUrl = media.original_url || media.url;
const { croppedBlob, newCropData } = await openEditor(editUrl, media.crop_data);

const formData = new FormData();
formData.append('file', croppedBlob);
formData.append('crop_data', JSON.stringify(newCropData));
await api.patch(
  `/api/v1/pages/multipages/${pageId}/media/${mediaId}/`,
  formData,
  { headers: { 'Content-Type': 'multipart/form-data' } }
);
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 파일 형식/크기 오류, crop_data JSON 파싱 실패 |
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 파일 없음 |
        """,
        request=OpenApiTypes.BINARY,
        responses={
            200: OpenApiResponse(response=PageMediaSerializer, description="업데이트된 미디어 파일 정보"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 파일 없음"),
        },
    )
    def patch(self, request, page_id: int, media_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        media = PageMedia.objects.filter(pk=media_id, page=page).first()
        if not media:
            return Response({"detail": "파일을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        # 새 완성본 파일 (선택)
        new_file = request.FILES.get("file")
        if new_file:
            mime_type = new_file.content_type or ""
            if mime_type not in _ALLOWED_MIME_TYPES:
                return Response(
                    {"file": ["지원하지 않는 파일 형식입니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if new_file.size > _MAX_FILE_SIZE_BYTES:
                return Response(
                    {"file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if media.file:
                media.file.delete(save=False)
            media.file = new_file
            media.mime_type = mime_type
            media.size = new_file.size

        # crop_data 업데이트 (선택)
        raw_crop = request.data.get("crop_data")
        if raw_crop is not None:
            if isinstance(raw_crop, str):
                try:
                    media.crop_data = json.loads(raw_crop)
                except (json.JSONDecodeError, ValueError):
                    return Response(
                        {"crop_data": ["crop_data는 유효한 JSON이어야 합니다."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            elif isinstance(raw_crop, dict):
                media.crop_data = raw_crop

        media.save()
        return Response(PageMediaSerializer(media, context={"request": request}).data)

    @extend_schema(
        description="""
## 개요
특정 페이지에 업로드된 미디어 파일 1건을 **영구 삭제**합니다.  
스토리지의 실제 파일과 DB 레코드가 동시에 제거됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `id` | int | 페이지 ID |
| `media_id` | int | 삭제할 미디어 파일 ID |

## 주의사항
> ⚠️ **삭제 후 복구 불가**  
> 삭제된 파일 URL이 이미 블록의 `data`에 저장되어 있다면,  
> 해당 블록의 이미지는 **깨진 링크**가 됩니다.  
> 새 이미지를 업로드한 뒤 블록의 URL도 업데이트하세요.

## 응답
성공 시 **204 No Content** — 바디 없음.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 페이지 또는 파일 없음 |
        """,
        responses={
            204: OpenApiResponse(description="삭제 완료 — 바디 없음"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 또는 파일 없음"),
        },
    )
    def delete(self, request, page_id: int, media_id: int):
        page = _get_owned_page(request, page_id)
        if not page:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        media = PageMedia.objects.filter(pk=media_id, page=page).first()
        if not media:
            return Response({"detail": "파일을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        media.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
