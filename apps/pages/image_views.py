"""
apps/pages/image_views.py

미디어 파일(이미지) 업로드·목록·삭제 API

■ 사용 흐름
  1) POST  /api/pages/me/media/        → 파일 업로드  → { id, url, ... } 반환
  2) PATCH /api/pages/me/blocks/{id}/  → { data: { thumbnail_url: "<반환된 url>" } }
  3) DELETE /api/pages/me/media/{id}/  → 파일 교체 시 이전 파일 삭제

■ block.data 사용 예
  single_link  → data.thumbnail_url
  profile      → data.avatar_url
  (그 외 필드도 URL 문자열로 자유롭게 사용 가능)
"""

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

from .models import Page, PageMedia
from .serializers import PageMediaSerializer

# ── 업로드 제한 설정 ───────────────────────────────────────────────
ALLOWED_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "image/bmp",
        "image/tiff",
    }
)
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


# ─────────────────────────────────────────────────────────────
# 미디어 목록 + 업로드
# ─────────────────────────────────────────────────────────────

class PageMediaView(APIView):
    """
    GET  /api/pages/me/media/  — 업로드된 미디어 파일 목록 조회
    POST /api/pages/me/media/  — 새 이미지 파일 업로드
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        tags=["미디어"],
        summary="미디어 파일 목록 조회",
        description="""
## 개요
내 페이지에 업로드된 **이미지 파일 목록**을 최신순으로 반환합니다.  
블록 편집 화면에서 기존 업로드된 이미지를 재사용할 때 호출합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | int | 미디어 고유 ID. 삭제 시 사용 |
| `original_name` | string | 업로드 당시 파일 이름 |
| `mime_type` | string | MIME 타입 (예: `image/jpeg`) |
| `size` | int | 파일 크기 (bytes) |
| `size_display` | string | 사람이 읽기 좋은 크기 (예: `1.2 MB`) |
| `url` | string | **블록에 저장할 이미지 URL**. `block.data.thumbnail_url` 등에 직접 사용 |
| `created_at` | datetime | 업로드 일시 (ISO 8601) |

## 프론트엔드 통합 패턴
```typescript
// 미디어 라이브러리 모달 로딩
const { data: mediaList } = await api.get('/api/pages/me/media/');
// mediaList[0].url → block.data.thumbnail_url 에 바로 사용 가능
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
""",
        responses={
            200: OpenApiResponse(
                response=PageMediaSerializer(many=True),
                description="미디어 파일 목록 (최신순)",
                examples=[
                    OpenApiExample(
                        "Success",
                        value=[
                            {
                                "id": 3,
                                "original_name": "product-thumb.jpg",
                                "mime_type": "image/jpeg",
                                "size": 245760,
                                "size_display": "240.0 KB",
                                "url": "https://yourdomain.com/media/pages/2026/03/product-thumb.jpg",
                                "created_at": "2026-03-11T10:00:00Z",
                            },
                            {
                                "id": 2,
                                "original_name": "avatar.png",
                                "mime_type": "image/png",
                                "size": 51200,
                                "size_display": "50.0 KB",
                                "url": "https://yourdomain.com/media/pages/2026/03/avatar.png",
                                "created_at": "2026-03-10T09:00:00Z",
                            },
                        ],
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        page, _ = Page.get_or_create_for_user(request.user)
        qs = PageMedia.objects.filter(page=page)
        return Response(
            PageMediaSerializer(qs, many=True, context={"request": request}).data
        )

    @extend_schema(
        tags=["미디어"],
        summary="미디어 파일 업로드",
        description="""
## 개요
블록에서 사용할 **이미지 파일을 서버에 업로드**합니다.  
업로드 완료 후 반환된 `url`을 `block.data` 의 URL 필드에 저장하는 **2단계 방식**입니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 형식
`Content-Type: multipart/form-data` 필수 (JSON이 아님)

| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `file` | ✅ | File | 업로드할 이미지 파일 |

## 파일 제한
| 항목 | 제한 |
|------|------|
| 최대 크기 | **10 MB** |
| 허용 MIME | `image/jpeg` `image/png` `image/gif` `image/webp` `image/svg+xml` `image/bmp` `image/tiff` |

## 응답
성공 시 업로드된 파일 정보와 **`url`** 을 반환합니다.  
이 `url`을 블록 편집 시 `block.data` 에 저장하세요.

## 전체 흐름 예시
```typescript
// Step 1: 파일 업로드
const formData = new FormData();
formData.append('file', selectedFile);  // <input type="file"> 에서 선택된 파일

const { data: media } = await api.post('/api/pages/me/media/', formData, {
  headers: { 'Content-Type': 'multipart/form-data' },
});
// media.url → "https://yourdomain.com/media/pages/2026/03/product-thumb.jpg"

// Step 2: 반환된 URL을 블록에 저장
await api.patch(`/api/pages/me/blocks/${blockId}/`, {
  data: {
    url: 'https://naver.me/abc',
    label: '상품 링크',
    thumbnail_url: media.url,   // ← 여기에 그대로 사용
  },
});
```

## React Hook 통합 패턴
```typescript
const uploadImage = async (file: File): Promise<string> => {
  const formData = new FormData();
  formData.append('file', file);
  const res = await api.post('/api/pages/me/media/', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return res.data.url;  // URL만 꺼내서 사용
};

// 사용
const imageUrl = await uploadImage(e.target.files[0]);
setBlockData(prev => ({ ...prev, thumbnail_url: imageUrl }));
```

## 에러
| 코드 | 원인 | 메시지 예시 |
|------|------|------------|
| 400 | 파일 미첨부 | `"파일을 첨부해 주세요."` |
| 400 | 허용되지 않는 MIME | `"지원하지 않는 파일 형식입니다."` |
| 400 | 파일 크기 초과 | `"파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."` |
| 401 | 토큰 없음/만료 | — |
""",
        request=OpenApiTypes.BINARY,
        responses={
            201: OpenApiResponse(
                response=PageMediaSerializer,
                description="업로드 성공 — 반환된 url을 block.data에 저장하세요",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 5,
                            "original_name": "product-thumb.jpg",
                            "mime_type": "image/jpeg",
                            "size": 245760,
                            "size_display": "240.0 KB",
                            "url": "https://yourdomain.com/media/pages/2026/03/product-thumb.jpg",
                            "created_at": "2026-03-11T10:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="유효성 검증 실패",
                examples=[
                    OpenApiExample(
                        "파일 미첨부",
                        value={"file": ["파일을 첨부해 주세요."]},
                    ),
                    OpenApiExample(
                        "MIME 타입 오류",
                        value={"file": ["지원하지 않는 파일 형식입니다. 허용 타입: jpeg, png, gif, webp, svg, bmp, tiff"]},
                    ),
                    OpenApiExample(
                        "파일 크기 초과",
                        value={"file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]},
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        page, _ = Page.get_or_create_for_user(request.user)

        file = request.FILES.get("file")
        if not file:
            return Response(
                {"file": ["파일을 첨부해 주세요."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mime_type = file.content_type or ""
        if mime_type not in ALLOWED_MIME_TYPES:
            return Response(
                {"file": ["지원하지 않는 파일 형식입니다. 허용 타입: jpeg, png, gif, webp, svg, bmp, tiff"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if file.size > MAX_FILE_SIZE_BYTES:
            return Response(
                {"file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        media = PageMedia.objects.create(
            page=page,
            file=file,
            original_name=file.name,
            mime_type=mime_type,
            size=file.size,
        )
        return Response(
            PageMediaSerializer(media, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# ─────────────────────────────────────────────────────────────
# 미디어 상세 — 삭제
# ─────────────────────────────────────────────────────────────

class PageMediaDetailView(APIView):
    """DELETE /api/pages/me/media/{id}/ — 미디어 파일 삭제 (스토리지 + DB 동시)"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["미디어"],
        summary="미디어 파일 삭제",
        description="""
## 개요
업로드된 미디어 파일 1건을 **영구 삭제**합니다.  
스토리지의 실제 파일과 DB 레코드가 동시에 제거됩니다.

## 삭제해야 하는 상황
- 블록의 이미지를 **다른 이미지로 교체**할 때 (이전 파일 정리)
- 더 이상 사용하지 않는 파일 정리 (서버 용량 절약)

## 인증
`Authorization: Bearer <access_token>` 헤더 필수  
**본인 페이지에 속한 파일만** 삭제 가능합니다.

## 주의사항
> **⚠️ 삭제 후 복구 불가**  
> 삭제된 파일 URL이 이미 어떤 블록의 `data`에 저장되어 있다면,  
> 해당 블록의 이미지는 **깨진 링크(broken image)**가 됩니다.  
> 새 이미지를 업로드한 뒤, 블록의 URL도 업데이트하세요.

## 권장 이미지 교체 흐름
```typescript
// 1) 새 이미지 먼저 업로드
const formData = new FormData();
formData.append('file', newFile);
const { data: newMedia } = await api.post('/api/pages/me/media/', formData, {
  headers: { 'Content-Type': 'multipart/form-data' },
});

// 2) 블록의 이미지 URL 업데이트
await api.patch(`/api/pages/me/blocks/${blockId}/`, {
  data: { ...block.data, thumbnail_url: newMedia.url },
});

// 3) 이전 파일 삭제 (oldMediaId는 교체 전 media.id)
await api.delete(`/api/pages/me/media/${oldMediaId}/`);
```

## 응답
성공 시 **204 No Content** — 바디 없음.

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 파일 없음 또는 본인 페이지의 파일이 아님 |
""",
        parameters=[
            OpenApiParameter(
                name="id",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.PATH,
                description="삭제할 미디어 파일 ID (`GET /api/pages/me/media/` 응답의 `id` 필드)",
            ),
        ],
        responses={
            204: OpenApiResponse(description="삭제 완료 — 바디 없음"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="파일 없음 또는 접근 권한 없음"),
        },
    )
    def delete(self, request, pk):
        page, _ = Page.get_or_create_for_user(request.user)
        media = PageMedia.objects.filter(pk=pk, page=page).first()
        if not media:
            return Response(status=status.HTTP_404_NOT_FOUND)
        media.delete()  # PageMedia.delete() 오버라이드에서 스토리지 파일도 삭제
        return Response(status=status.HTTP_204_NO_CONTENT)
