from django.db import transaction
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet

from .models import Block, BlockClick, Page, PageView
from .permissions import IsPageOwner, IsPublicPageOrOwner
from .serializers import (
    BlockSerializer,
    BlockStatSerializer,
    BlockStatsSerializer,
    ChartDataSerializer,
    PagePublicSerializer,
    PageSerializer,
    RecordClickSerializer,
    RecordViewSerializer,
    ReorderSerializer,
    SlugChangeSerializer,
    SlugCheckSerializer,
    StatsSummarySerializer,
)
from .stats import (
    get_block_stats,
    get_chart_data,
    get_country,
    get_stats_summary,
    hash_ip,
    parse_referer,
    resolve_period,
)


# ─────────────────────────────────────────────────────────────
# 내 페이지 (GET / PATCH)
# ─────────────────────────────────────────────────────────────

class MyPageView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["페이지 서비스"],
        summary="내 페이지 조회 (없으면 자동 생성)",
        description="""
## 개요
로그인한 사용자의 블록형 링크 페이지를 반환합니다.  
**페이지가 없으면 최초 호출 시 자동 생성**되므로 별도의 생성 API 호출은 불필요합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 응답 필드 설명
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 페이지 고유 ID |
| `slug` | string | 공개 URL 식별자 (`@slug` 형태로 사용). **읽기 전용** |
| `title` | string | 페이지 상단에 표시되는 제목 (기본값: 빈 문자열) |
| `is_public` | bool | `true`이면 누구나 열람 가능. 기본값 `false` |
| `created_at` | datetime | ISO 8601 |
| `updated_at` | datetime | ISO 8601 |

## 자동 생성 정책
- 최초 호출 시 `username` 기반 slug(`hong-gildong` 등)로 페이지 자동 생성
- slug 충돌 시 `-2`, `-3` … 접미사 자동 부여
- **`is_public` 기본값: `false`** → 공개 전까지 외부 접근 차단됨

## 프론트엔드 통합 패턴
```typescript
// 앱 초기화 시 페이지 정보 fetch → 없으면 자동 생성되므로 분기 불필요
const { data: page } = await api.get('/api/pages/me/');
const publicUrl = `https://yourdomain.com/@${page.slug}`;
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
        """,
        responses={
            200: OpenApiResponse(
                response=PageSerializer,
                description="내 페이지 정보",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 1,
                            "slug": "hong-gildong",
                            "title": "",
                            "is_public": False,
                            "created_at": "2026-03-01T00:00:00Z",
                            "updated_at": "2026-03-01T00:00:00Z",
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        page, _ = Page.get_or_create_for_user(request.user)
        return Response(PageSerializer(page).data)

    @extend_schema(tags=["페이지 서비스"],
        summary="내 페이지 수정",
        description="""
## 개요
페이지의 메타 정보를 수정합니다. **PATCH** 방식이므로 변경할 필드만 전송하면 됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 수정 가능 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `title` | string | 페이지 상단 제목. 빈 문자열 허용 |
| `is_public` | bool | `true` → 즉시 전체 공개. `false` → 비공개 전환 |

> **`slug`는 이 API로 변경 불가합니실.**
> slug 변경은 `PATCH /api/pages/me/slug/` 를 사용하세요.
> 변경 전 `GET /api/pages/check-slug/?slug=xxx` 로 중복 확인을 권장합니다.

## 공개 전환 플로우
```
페이지 편집 완료 → PATCH { is_public: true } → 사용자에게 공개 URL 노출
```

## Request 예시
```typescript
// 제목 변경 + 공개 전환
await api.patch('/api/pages/me/', { title: '내 링크 페이지', is_public: true });

// 비공개로 전환만
await api.patch('/api/pages/me/', { is_public: false });
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 필드 타입 오류 |
| 401 | 토큰 없음/만료 |
        """,
        request=PageSerializer,
        responses={
            200: OpenApiResponse(response=PageSerializer, description="수정된 페이지"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def patch(self, request):
        page, _ = Page.get_or_create_for_user(request.user)
        serializer = PageSerializer(page, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ─────────────────────────────────────────────────────────────
# 공개 페이지 조회 (slug 기반)
# ─────────────────────────────────────────────────────────────

class PublicPageView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(tags=["페이지 서비스"],
        summary="공개 페이지 조회 (slug 기반)",
        description="""
## 개요
`@slug` 식별자로 공개 링크 페이지와 활성화된 블록 목록을 반환합니다.  
**인증 불필요** — 랜딩 페이지 렌더링용 public endpoint입니다.

## 인증
불필요. 단, 비공개 페이지는 소유자만 조회 가능합니다.

## 접근 규칙
| 상태 | 비로그인 | 타 사용자 | 소유자 |
|------|----------|-----------|--------|
| `is_public: true` | ✅ 200 | ✅ 200 | ✅ 200 |
| `is_public: false` | ❌ 404 | ❌ 404 | ✅ 200 |

> 비공개 페이지에는 404를 반환합니다 (403이 아님 — 존재 자체를 노출하지 않음)

## 응답 구조
`blocks` 배열에는 **`is_enabled: true`이면서 예약 조건을 통과한 블록만** 포함됩니다.

예약 노출 조건:
| schedule_enabled | 결과 |
|------|------|
| `false` | `is_enabled` 값만 적용 |
| `true`, `publish_at`만 지정 | `publish_at` 도래 후 영구 노출 |
| `true`, `hide_at`만 지정 | 지금부터 `hide_at` 전까지 노출 |
| `true`, 둘 다 지정 | `publish_at` ~ `hide_at` 구간만 노출 |

## Request 예시
```typescript
// 슬러그로 페이지 렌더링 (Next.js 예시)
export async function getServerSideProps({ params }) {
  const res = await fetch(`${API_URL}/api/pages/@${params.slug}/`);
  if (!res.ok) return { notFound: true };
  return { props: { page: await res.json() } };
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 404 | 존재하지 않는 slug, 또는 비공개 페이지에 비소유자 접근 |
        """,
        responses={
            200: OpenApiResponse(
                response=PagePublicSerializer,
                description="공개 페이지 + 블록 목록",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "slug": "clfy",
                            "title": "CLFY",
                            "is_public": True,
                            "blocks": [
                                {
                                    "id": 1,
                                    "type": "profile",
                                    "order": 1,
                                    "data": {"headline": "독일 면도기"},
                                }
                            ],
                        },
                    )
                ],
            ),
            404: OpenApiResponse(description="페이지 없음 또는 비공개"),
        },
    )
    def get(self, request, slug: str):
        try:
            page = Page.objects.get(slug=slug)
        except Page.DoesNotExist:
            return Response({"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        # 비공개면 소유자만 허용
        if not page.is_public:
            if not request.user.is_authenticated or page.user != request.user:
                return Response(
                    {"detail": "페이지를 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND
                )

        return Response(PagePublicSerializer(page).data)


# ─────────────────────────────────────────────────────────────
# 내 블록 목록 / 생성
# ─────────────────────────────────────────────────────────────

class BlockListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_page(self, user):
        page, _ = Page.get_or_create_for_user(user)
        return page

    @extend_schema(tags=["페이지 서비스"],
        summary="내 블록 목록 조회",
        description="""
## 개요
내 페이지의 **전체 블록**을 `order` 오름차순으로 반환합니다.  
`is_enabled: false`인 비활성 블록도 포함됩니다 (편집 화면에서 표시 여부 토글 가능).

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 공개 페이지와의 차이
| API | is_enabled 필터 | 용도 |
|-----|----------------|------|
| `GET /api/pages/me/blocks/` | 없음 (전체) | 편집 화면 |
| `GET /api/pages/@slug/` | is_enabled=true만 | 공개 렌더링 |

## 응답 필드 설명
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 블록 고유 ID. PATCH/DELETE 시 URL에 사용 |
| `type` | string | `profile` \| `contact` \| `single_link` |
| `order` | int | 표시 순서 (1부터 시작). 재정렬 API로 변경 |
| `is_enabled` | bool | `false`면 공개 페이지에서 숨김 |
| `data` | object | 타입별 콘텐츠 (아래 블록 생성 API 참조) |
        """,
        responses={200: BlockSerializer(many=True)},
    )
    def get(self, request):
        page = self._get_page(request.user)
        blocks = page.blocks.order_by("order")
        return Response(BlockSerializer(blocks, many=True).data)

    @extend_schema(tags=["페이지 서비스"],
        summary="블록 생성",
        description="""
## 개요
새 블록을 내 페이지에 추가합니다. **`type`은 생성 후 변경 불가**이므로 신중하게 선택하세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 필드
| 필드 | 필수 | 설명 |
|------|------|------|
| `type` | ✅ | 블록 종류 (`profile` / `contact` / `single_link`) |
| `data` | ✅ | 타입별 콘텐츠 객체 (아래 스키마 참조) |
| `order` | 선택 | 미지정 시 현재 마지막 블록 다음 순번 자동 부여 |
| `is_enabled` | 선택 | 기본값 `true`. `false`로 생성하면 공개 페이지에서 즉시 숨김 |
| `schedule_enabled` | 선택 | `true`로 설정하면 아래 시각에 따라 자동 공개/숨김 |
| `publish_at` | 조건부 | 공개 시작 시각 (ISO 8601). `schedule_enabled=true`일 때 필요 |
| `hide_at` | 조건부 | 숨김 시작 시각 (ISO 8601). `schedule_enabled=true`일 때 필요 |

## 예약 설정 규칙

| 설정 | 동작 |
|------|------|
| `schedule_enabled: false` | 예약 무시. `is_enabled`만으로 노출 여부 결정 |
| `publish_at`만 지정 | 해당 시각 이후 영구 볈 |
| `hide_at`만 지정 | 즉시 공개, 해당 시각에 숨김 |
| 둘 다 지정 | `publish_at` ~ `hide_at` 구간만 볈 |

> `publish_at >= hide_at` 시 400 에러 반환

## 타입별 `data` 스키마

### `profile` — 프로필 소개 블록
```json
{
  "headline": "독일 면도기 전문",   // 필수. 메인 한 줄 소개
  "subline": "방수 / 저소음",       // 선택. 부제목
  "avatar_url": "https://..."      // 선택. 프로필 이미지 URL
}
```

### `contact` — 연락처 블록
```json
{
  "country_code": "+82",           // 필수. 국가 코드 (+82 형식)
  "phone": "01012345678",          // 필수. 하이픈 없이
  "whatsapp": true                 // 선택. WhatsApp 링크 사용 여부
}
```

### `single_link` — 단일 링크 버튼 블록
```json
{
  "url": "https://naver.me/abc",   // 필수. 유효한 http(s) URL
  "label": "쿠팡 추천 링크",        // 필수. 버튼 표시 텍스트
  "description": "오늘만 할인",    // 선택. 버튼 하단 설명
  "layout": "small",              // 선택. 'small'(기본) | 'large'
  "thumbnail_url": "https://..."  // 선택. 썸네일 이미지 URL
}
```

## 에러
| 코드 | 원인 | 예시 메시지 |
|------|------|------------|
| 400 | 필수 data 필드 누락 | `"headline 필드는 필수입니다."` |
| 400 | URL 형식 오류 | `"유효한 URL을 입력하세요."` |
| 400 | order 중복 | `"이미 사용 중인 order 값입니다."` |
| 401 | 토큰 없음/만료 | — |
        """,
        request=BlockSerializer,
        responses={
            201: OpenApiResponse(response=BlockSerializer, description="생성된 블록"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        page = self._get_page(request.user)
        serializer = BlockSerializer(data=request.data, context={"page": page})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────
# 내 블록 상세 (PATCH / DELETE)
# ─────────────────────────────────────────────────────────────

class BlockDetailView(APIView):
    permission_classes = [IsAuthenticated, IsPageOwner]

    def _get_block(self, request, pk):
        try:
            block = Block.objects.select_related("page__user").get(pk=pk)
        except Block.DoesNotExist:
            return None
        self.check_object_permissions(request, block)
        return block

    @extend_schema(tags=["페이지 서비스"],
        summary="블록 수정",
        description="""
## 개요
블록의 콘텐츠(`data`), 표시 여부(`is_enabled`), 순서(`order`)를 수정합니다.  
**PATCH** 방식이므로 변경할 필드만 전송하면 됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수 (소유자만 가능)

## 수정 가능 필드
| 필드 | 설명 |
|------|------|
| `data` | 타입별 콘텐츠 객체 전체를 교체. 타입별 필수값 검증 동일 적용 |
| `is_enabled` | `false`로 변경 시 공개 페이지에서 즉시 숨김 |
| `order` | 직접 순서 변경. 중복 금지. 다수 블록 순서 변경은 reorder API 사용 권장 |
| `schedule_enabled` | `true`로 설정하면 예약 조건에 따라 자동 공개/숨김. `false`로 돌리면 즉시 비활성화 |
| `publish_at` | 공개 시각 (ISO 8601, 타임존 포함). `schedule_enabled=true`일 때 츜 |
| `hide_at` | 숨김 시각 (ISO 8601, 타임존 포함). `schedule_enabled=true`일 때 츜 |

## 예약 설정 예시
```typescript
// 공개 후 숨김 외에도 신뢰제 플래시셀
await api.patch(`/api/pages/me/blocks/${blockId}/`, {
  schedule_enabled: true,
  publish_at: '2026-03-10T10:00:00+09:00',
  hide_at: '2026-03-17T23:59:00+09:00',
});

// 예약 조건 해제 (다시 is_enabled 정적 제어로)
await api.patch(`/api/pages/me/blocks/${blockId}/`, {
  schedule_enabled: false,
});
```

## 제약
- **`type` 변경 불가** → 타입 변경이 필요하면 삭제 후 재생성
- `data` 부분 수정 불가 → 전체 object를 새로 전송해야 함

## Request 예시
```typescript
// 블록 숨기기
await api.patch(`/api/pages/me/blocks/${blockId}/`, { is_enabled: false });

// 링크 텍스트 변경
await api.patch(`/api/pages/me/blocks/${blockId}/`, {
  data: { url: 'https://new-url.com', label: '새 링크명' }
});
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | type 변경 시도 / data 필수 필드 누락 |
| 403 | 다른 사용자의 블록 |
| 404 | 블록 ID 없음 |
        """,
        request=BlockSerializer,
        responses={
            200: OpenApiResponse(response=BlockSerializer, description="수정된 블록"),
            400: OpenApiResponse(description="유효성 검증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="블록 없음"),
        },
    )
    def patch(self, request, pk):
        block = self._get_block(request, pk)
        if not block:
            return Response({"detail": "블록을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        page, _ = Page.get_or_create_for_user(request.user)
        serializer = BlockSerializer(block, data=request.data, partial=True, context={"page": page})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(tags=["페이지 서비스"],
        summary="블록 삭제",
        description="""
## 개요
블록을 영구 삭제합니다. **복구 불가**하므로 UI에서 `is_enabled: false`로 숨기는 것을 먼저 고려하세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수 (소유자만 가능)

## 동작
- 해당 블록의 `order` 값이 비워지지만, 나머지 블록의 order는 재정렬되지 않습니다.
- 필요 시 삭제 후 reorder API로 순번 정리를 권장합니다.

## 에러
| 코드 | 원인 |
|------|------|
| 403 | 다른 사용자의 블록 |
| 404 | 블록 ID 없음 |
        """,
        responses={
            204: OpenApiResponse(description="삭제 성공"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="블록 없음"),
        },
    )
    def delete(self, request, pk):
        block = self._get_block(request, pk)
        if not block:
            return Response({"detail": "블록을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)
        block.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────
# Reorder
# ─────────────────────────────────────────────────────────────

class BlockReorderView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["페이지 서비스"],
        summary="블록 순서 재정렬",
        description="""
## 개요
여러 블록의 `order`를 **하나의 트랜잭션**으로 원자적으로 변경합니다.  
드래그 앤 드롭 정렬 완료 후 호출하는 것을 권장합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

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
- 페이지의 **전체 블록을 모두 포함할 필요는 없습니다** — 포함된 id의 block만 order 변경됨
- 내부적으로 `unique_together(page, order)` 충돌을 피하기 위해 음수 임시값으로 2-pass 저장
- 실패 시 전체 롤백 (원자성 보장)

## 제약
| 조건 | 결과 |
|------|------|
| `orders` 배열이 비어 있음 | 400 |
| `order` 값 중복 | 400 |
| `id` 값 중복 | 400 |
| 타 사용자 페이지의 블록 id 포함 | 400 (`"이 페이지에 속하지 않는 블록"`) |

## 드래그 드롭 통합 예시
```typescript
// DnD 완료 핸들러 (React DnD / dnd-kit 등 공통 패턴)
const handleDragEnd = async (reorderedBlocks: Block[]) => {
  const orders = reorderedBlocks.map((b, i) => ({ id: b.id, order: i + 1 }));
  await api.post('/api/pages/me/blocks/reorder/', { orders });
};
```
        """,
        request=ReorderSerializer,
        responses={
            200: OpenApiResponse(response=BlockSerializer(many=True), description="재정렬된 블록 목록"),
            400: OpenApiResponse(description="유효성 검증 실패 or 권한 없는 블록 포함"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        page, _ = Page.get_or_create_for_user(request.user)

        serializer = ReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orders = serializer.validated_data["orders"]

        requested_ids = [item["id"] for item in orders]
        blocks = Block.objects.filter(pk__in=requested_ids, page=page)

        # 다른 페이지 블록 포함 여부 검증
        if blocks.count() != len(requested_ids):
            return Response(
                {"detail": "요청한 블록 중 이 페이지에 속하지 않거나 존재하지 않는 블록이 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        id_to_order = {item["id"]: item["order"] for item in orders}

        with transaction.atomic():
            # unique_together 충돌 방지를 위해 임시로 음수 order로 먼저 저장
            for block in blocks:
                block.order = -(id_to_order[block.pk])
                block.save(update_fields=["order"])
            for block in blocks:
                block.order = id_to_order[block.pk]
                block.save(update_fields=["order"])

        updated = page.blocks.order_by("order")
        return Response(BlockSerializer(updated, many=True).data)


# ─────────────────────────────────────────────────────────────
# slug 중복 확인 / slug 변경
# ─────────────────────────────────────────────────────────────

class SlugCheckView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["페이지 서비스"],
        summary="slug 사용 가능 여부 확인",
        description="""
## 개요
slug 변경 **전**에 해당 slug가 이미 사용 중인지 미리 확인하는 API입니다.  
UI에서 입력 시 debounce + 이 API 호출로 실시간 중복 표시를 구현하세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## Query Parameter
| 파라미터 | 필수 | 설명 |
|----------|------|------|
| `slug` | ✅ | 확인할 slug 문자열 |

## 응답
- `available: true` → 사용 가능 (PATCH /api/pages/me/slug/ 협출 허용)
- `available: false` → 이미 사용 중 (오류 표시)

## 프론트엔드 통합 패턴
```typescript
// 입력 시 debounce 적용
const checkSlug = useDebouncedCallback(async (value: string) => {
  const res = await api.get(`/api/pages/check-slug/?slug=${value}`);
  setSlugAvailable(res.data.available);
}, 400);
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | slug 파라미터 누락 |
| 401 | 토큰 없음/만료 |
        """,
        responses={
            200: OpenApiResponse(
                response=SlugCheckSerializer,
                description="사용 가능 여부",
                examples=[
                    OpenApiExample(
                        "Available",
                        value={"slug": "my-brand", "available": True, "message": "사용 가능한 slug입니다."},
                    ),
                    OpenApiExample(
                        "Taken",
                        value={"slug": "clfy", "available": False, "message": "이미 사용 중인 slug입니다."},
                    ),
                ],
            ),
            400: OpenApiResponse(description="slug 파라미터 누락"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        slug = request.query_params.get("slug", "").lower().strip("-")
        if not slug:
            return Response(
                {"detail": "slug 파라미터가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        taken = Page.objects.filter(slug=slug).exclude(user=request.user).exists()
        if taken:
            return Response({"slug": slug, "available": False, "message": "이미 사용 중인 slug입니다."})
        return Response({"slug": slug, "available": True, "message": "사용 가능한 slug입니다."})


class SlugChangeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["페이지 서비스"],
        summary="내 페이지 slug 변경",
        description="""
## 개요
공개 URL의 slug(주소)를 변경합니다.  
**변경 즉시 기존 slug는 사용 불가** — 기존 URL로 접속하는 방문자는 새 slug로 안내해주세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 권장 흐름
```
1. GET /api/pages/check-slug/?slug=new-name  → available: true 확인
2. PATCH /api/pages/me/slug/  { slug: "new-name" }  → 변경 완료
3. 프론트 저장된 공개 URL을 새 slug로 갱신
```

## 요청 필드
| 필드 | 필수 | 설명 |
|------|------|------|
| `slug` | ✅ | 주소로 사용할 새 slug. 영문 소문자/숫자/하이픈만 허용, 2~120자 |

## slug 형식 규칙
- 영문 소문자(a-z), 숫자(0-9), 하이픈(-) 만 허용  
- 첫글자/끝에 하이픈 불가  
- 2자 이상 120자 이하  
- 대소문자 입력 시 소문자로 자동 변환

## Request 예시
```typescript
// 새 slug로 변경
const res = await api.patch('/api/pages/me/slug/', { slug: 'my-brand-2026' });
console.log(res.data.slug); // 'my-brand-2026'
// 저장한 공개 URL도 갱신
const newPublicUrl = `https://yourdomain.com/@${res.data.slug}`;
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | slug 형식 오류 |
| 400 | 이미 사용 중인 slug |
| 401 | 토큰 없음/만료 |
        """,
        request=SlugChangeSerializer,
        responses={
            200: OpenApiResponse(
                response=PageSerializer,
                description="slug가 변경된 페이지 정보",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 1,
                            "slug": "my-brand-2026",
                            "title": "내 링크 페이지",
                            "is_public": True,
                            "created_at": "2026-03-01T00:00:00Z",
                            "updated_at": "2026-03-09T12:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="유효성 검증 실패",
                examples=[
                    OpenApiExample(
                        "Taken",
                        value={"slug": ["이미 사용 중인 slug입니다."]},
                    ),
                    OpenApiExample(
                        "Format Error",
                        value={"slug": ["Enter a valid \u2018slug\u2019 consisting of letters, numbers, underscores or hyphens."]},
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def patch(self, request):
        page, _ = Page.get_or_create_for_user(request.user)
        serializer = SlugChangeSerializer(
            data=request.data, context={"user": request.user}
        )
        serializer.is_valid(raise_exception=True)
        page.slug = serializer.validated_data["slug"]
        page.save(update_fields=["slug", "updated_at"])
        return Response(PageSerializer(page).data)


# ─────────────────────────────────────────────────────────────
# 페이지 조회 기록 (공개 — 프론트가 렌더링 시 호출)
# ─────────────────────────────────────────────────────────────

class PageViewRecordView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []  # JWT 토큰 파싱 불필요

    @extend_schema(
        tags=["통계"],
        summary="페이지 조회 기록",
        description="""
## 개요
공개 페이지(`@slug`)가 화면에 렌더링될 때 **프론트엔드가 자동 호출**해야 하는 엔드포인트입니다.  
조회 이벤트(IP 해시·유입 채널·국가)를 서버에 기록하며, 이 데이터가 통계 대시보드의 **조회수** 수치로 집계됩니다.

## 인증
**불필요** — JWT 토큰 없이 누구나 호출 가능합니다.

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `slug` | string | 공개 페이지의 slug. `@hong-gildong` → slug는 `hong-gildong` |

## 요청 바디 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `referer` | 선택 | string | 방문자 브라우저의 `document.referrer` 값. 어디서 왔는지 파악하는 유입 채널 데이터입니다. 없으면 빈 문자열 `""` 전송 |

> **`referer`가 뭔가요?**  
> 브라우저 내장 값인 `document.referrer`입니다.  
> 예를 들어 인스타그램 프로필 링크를 클릭해서 들어왔으면 `"https://l.instagram.com/..."` 이 담깁니다.  
> 이 값을 서버가 파싱해서 **"인스타그램", "네이버", "카카오", "직접 방문"** 등으로 분류합니다.

## 응답
성공 시 **204 No Content** — 바디 없음.

## 프론트엔드 통합 패턴
```typescript
// pages/@[slug]/page.tsx (Next.js)

useEffect(() => {
  // 페이지가 마운트되자마자 1회 호출
  fetch(`/api/pages/@${slug}/view/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      referer: document.referrer,   // 브라우저에서 자동으로 채워지는 값
    }),
  });
}, [slug]);
```

## 에러
| 상태코드 | 원인 |
|----------|------|
| 404 | `slug`에 해당하는 공개 페이지가 없거나 비공개(`is_public: false`) 상태 |
""",
        parameters=[
            OpenApiParameter(
                name="slug",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="공개 페이지의 slug (예: `hong-gildong`)",
            )
        ],
        request=RecordViewSerializer,
        examples=[
            OpenApiExample(
                "기본 호출 (referer 있음)",
                value={"referer": "https://l.instagram.com/"},
                request_only=True,
            ),
            OpenApiExample(
                "직접 방문 (referer 없음)",
                value={"referer": ""},
                request_only=True,
            ),
        ],
        responses={
            204: OpenApiResponse(description="기록 완료 — 바디 없음"),
            404: OpenApiResponse(
                description="페이지 없음",
                examples=[
                    OpenApiExample(
                        "Not Found",
                        value={"detail": "Not found."},
                    )
                ],
            ),
        },
    )
    def post(self, request, slug):
        page = Page.objects.filter(slug=slug, is_public=True).first()
        if not page:
            return Response(status=status.HTTP_404_NOT_FOUND)
        referer_url = request.data.get("referer", "") or request.META.get("HTTP_REFERER", "")
        PageView.objects.create(
            page=page,
            ip_hash=hash_ip(request),
            referer=parse_referer(referer_url),
            country=get_country(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────
# 블록 클릭 기록 (공개)
# ─────────────────────────────────────────────────────────────

class BlockClickRecordView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["통계"],
        summary="블록 클릭 기록",
        description="""
## 개요
방문자가 공개 페이지의 **블록(링크)을 클릭할 때** 프론트엔드가 호출해야 하는 엔드포인트입니다.  
클릭 이벤트(IP 해시·유입 채널·국가)를 기록하며, 통계 대시보드의 **클릭수·클릭율** 수치로 집계됩니다.

## 인증
**불필요** — JWT 토큰 없이 누구나 호출 가능합니다.

## 경로 파라미터
| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `slug` | string | 공개 페이지의 slug (예: `hong-gildong`) |
| `block_id` | integer | 클릭된 블록의 `id` (블록 목록 조회 시 반환되는 `id` 필드값) |

## 요청 바디 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `referer` | 선택 | string | 방문자 브라우저의 `document.referrer` 값. 없으면 빈 문자열 `""` 전송 |

## 응답
성공 시 **204 No Content** — 바디 없음.

## 프론트엔드 통합 패턴
```typescript
// 블록 컴포넌트에서 클릭 핸들러
const handleBlockClick = async (block: Block) => {
  // 1) 클릭 기록 (fire-and-forget — 실패해도 사용자 경험에 영향 없음)
  fetch(`/api/pages/@${slug}/blocks/${block.id}/click/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      referer: document.referrer,
    }),
  }).catch(() => {});  // 통계 실패가 페이지 동작을 막으면 안 됨

  // 2) 실제 링크 이동
  window.open(block.data.url, '_blank');
};
```

## 에러
| 상태코드 | 원인 |
|----------|------|
| 404 | `slug`에 해당하는 공개 페이지가 없거나 비공개 상태 |
| 404 | `block_id`가 해당 페이지에 속하지 않음 |
""",
        parameters=[
            OpenApiParameter(
                name="slug",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="공개 페이지의 slug (예: `hong-gildong`)",
            ),
            OpenApiParameter(
                name="block_id",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.PATH,
                description="클릭된 블록의 ID (`GET /api/pages/me/blocks/` 응답의 `id` 필드)",
            ),
        ],
        request=RecordClickSerializer,
        examples=[
            OpenApiExample(
                "기본 호출 (referer 있음)",
                value={"referer": "https://l.instagram.com/"},
                request_only=True,
            ),
            OpenApiExample(
                "직접 방문 (referer 없음)",
                value={"referer": ""},
                request_only=True,
            ),
        ],
        responses={
            204: OpenApiResponse(description="기록 완료 — 바디 없음"),
            404: OpenApiResponse(
                description="페이지 또는 블록 없음",
                examples=[
                    OpenApiExample(
                        "Not Found",
                        value={"detail": "Not found."},
                    )
                ],
            ),
        },
    )
    def post(self, request, slug, block_id):
        page = Page.objects.filter(slug=slug, is_public=True).first()
        if not page:
            return Response(status=status.HTTP_404_NOT_FOUND)
        block = Block.objects.filter(pk=block_id, page=page).first()
        if not block:
            return Response(status=status.HTTP_404_NOT_FOUND)
        referer_url = request.data.get("referer", "") or request.META.get("HTTP_REFERER", "")
        BlockClick.objects.create(
            block=block,
            page=page,
            ip_hash=hash_ip(request),
            referer=parse_referer(referer_url),
            country=get_country(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────
# 통계 요약 (인증 필수 — 페이지 소유자)
# ─────────────────────────────────────────────────────────────

class PageStatsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["통계"],
        summary="페이지 통계 요약",
        description="""## 개요
기간별 페이지 조회수·클릭수·클릭율과 유입 채널/국가 Top5를 반환합니다.

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 |
|---------|--------|--------|
| `period` | `7d` | `7d` `30d` `90d` |
""",
        responses={200: StatsSummarySerializer, 401: OpenApiResponse(description="인증 실패")},
    )
    def get(self, request):
        period_key, days = resolve_period(request.query_params.get("period", "7d"))
        page, _ = Page.get_or_create_for_user(request.user)
        data = get_stats_summary(page, days)
        data["period"] = period_key
        return Response(StatsSummarySerializer(data).data)


# ─────────────────────────────────────────────────────────────
# 차트 데이터 (인증 필수)
# ─────────────────────────────────────────────────────────────

class StatsChartView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["통계"],
        summary="페이지 통계 차트 데이터",
        description="""## 개요
날짜별 조회수·클릭수 배열을 반환합니다 (라인 차트용).

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 |
|---------|--------|--------|
| `period` | `7d` | `7d` `30d` `90d` |
""",
        responses={200: ChartDataSerializer, 401: OpenApiResponse(description="인증 실패")},
    )
    def get(self, request):
        period_key, days = resolve_period(request.query_params.get("period", "7d"))
        page, _ = Page.get_or_create_for_user(request.user)
        data = get_chart_data(page, days)
        data["period"] = period_key
        return Response(ChartDataSerializer(data).data)


# ─────────────────────────────────────────────────────────────
# 블록별 통계 (인증 필수)
# ─────────────────────────────────────────────────────────────

class StatsBlocksView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["통계"],
        summary="블록별 클릭 통계",
        description="""## 개요
각 블록의 기간 내 클릭수와 클릭율을 반환합니다.

## 쿼리 파라미터
| 파라미터 | 기본값 | 허용값 |
|---------|--------|--------|
| `period` | `7d` | `7d` `30d` `90d` |
""",
        responses={200: BlockStatsSerializer, 401: OpenApiResponse(description="인증 실패")},
    )
    def get(self, request):
        period_key, days = resolve_period(request.query_params.get("period", "7d"))
        page, _ = Page.get_or_create_for_user(request.user)
        blocks = get_block_stats(page, days)
        data = {"period": period_key, "blocks": blocks}
        return Response(BlockStatsSerializer(data).data)
