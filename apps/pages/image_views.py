"""
apps/pages/image_views.py

미디어 파일(이미지) 업로드·목록·삭제·재편집 API

■ 사용 흐름 (이미지 편집 포함)
  1) POST  /api/pages/me/media/        → 완성본(file) + 원본(original_file) + 크롭 파라미터(crop_data) 업로드
  2) PATCH /api/pages/me/blocks/{id}/  → { data: { thumbnail_url: "<반환된 url>" } }
  3) 재편집 시:
     GET   /api/pages/me/media/{id}/   → original_url + crop_data 로 편집기 복원
     PATCH /api/pages/me/media/{id}/   → 새 완성본(file) + 새 crop_data 전송
  4) DELETE /api/pages/me/media/{id}/  → 파일 교체 시 이전 파일 삭제

■ crop_data 기본 정책
  - crop_data가 빈 객체({})이면 → 프론트: 전체 영역(최대 크롭)으로 간주
  - locked 미지정 시 → false 로 간주

■ block.data 사용 예
  single_link  → data.thumbnail_url
  profile      → data.avatar_url
  (그 외 필드도 URL 문자열로 자유롭게 사용 가능)
"""

import json

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .image_pipeline import ImageValidationError, process_upload, sanitize_original
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

# ── Swagger UI 업로드 UI 노출용 스키마 (multipart/form-data) ─────
# OpenApiTypes.BINARY 는 application/octet-stream 이 되어
# multipart 필드(`file` 등)를 잡지 못하므로 아래 스키마를 사용.
_MEDIA_UPLOAD_REQUEST = {
    "multipart/form-data": {
        "type": "object",
        "required": ["file"],
        "properties": {
            "file": {
                "type": "string",
                "format": "binary",
                "description": "편집(크롭) 완료된 최종 이미지. 필수.",
            },
            "original_file": {
                "type": "string",
                "format": "binary",
                "description": "편집 전 원본 이미지. 재편집 시 편집기 로드용. 선택.",
            },
            "crop_data": {
                "type": "string",
                "description": "크롭 파라미터 JSON 문자열. 예: `{\"x\":0,\"y\":0,...}`. 선택.",
            },
        },
    }
}
_MEDIA_PATCH_REQUEST = {
    "multipart/form-data": {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "format": "binary",
                "description": "재편집(재크롭) 완료된 새 최종 이미지. 선택.",
            },
            "crop_data": {
                "type": "string",
                "description": "새 크롭 파라미터 JSON 문자열. 선택.",
            },
        },
    }
}


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
        summary="미디어 파일 업로드 (원본 + 크롭 파라미터 포함)",
        description="""
## 개요
블록에서 사용할 **이미지 파일을 서버에 업로드**합니다.  
**이미지 편집(크롭) 기능**을 지원하기 위해 완성본, 원본 이미지, 크롭 파라미터를 함께 저장합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 형식
`Content-Type: multipart/form-data` 필수 (JSON이 아님)

| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `file` | ✅ | File | **편집(크롭) 완료된 최종 이미지**. 블록 렌더링에 사용되는 파일 |
| `original_file` | ❌ | File | **편집 전 원본 이미지**. 재편집 시 편집기에서 이 파일을 로드 |
| `crop_data` | ❌ | string(JSON) | **크롭 파라미터 JSON 문자열**. 재편집 시 편집기 상태 복원용 |

## crop_data 구조 예시
```json
{
  "x": 120,
  "y": 80,
  "width": 400,
  "height": 300,
  "aspect_ratio": "4:3",
  "locked": true,
  "rotation": 0,
  "original_width": 1200,
  "original_height": 900
}
```

## crop_data 기본 정책
| 상황 | 프론트엔드 동작 |
|------|----------------|
| `crop_data`가 `{}`(빈 객체) | 전체 영역(최대 크롭)으로 간주 |
| `locked` 미지정 | `false`로 간주 (비율 고정 안 함) |
| `original_url`이 빈 문자열 | `url`(완성본)을 원본으로 사용하여 편집기 로드 |

## 파일 제한
| 항목 | 제한 |
|------|------|
| 최대 크기 | **10 MB** (file, original_file 각각) |
| 허용 MIME | `image/jpeg` `image/png` `image/gif` `image/webp` `image/svg+xml` `image/bmp` `image/tiff` |

## 전체 흐름 예시
```typescript
// Step 1: 사용자가 이미지 선택 후 편집기에서 크롭
const formData = new FormData();
formData.append('file', croppedBlob);           // 크롭 완료된 최종 이미지
formData.append('original_file', originalFile);  // 편집 전 원본 이미지
formData.append('crop_data', JSON.stringify({
  x: 120, y: 80, width: 400, height: 300,
  aspect_ratio: '4:3', locked: true,
  original_width: 1200, original_height: 900,
}));

const { data: media } = await api.post('/api/pages/me/media/', formData, {
  headers: { 'Content-Type': 'multipart/form-data' },
});

// Step 2: 반환된 URL을 블록에 저장
await api.patch(`/api/pages/me/blocks/${blockId}/`, {
  data: { ...block.data, thumbnail_url: media.url },
});
```

## 응답 필드
| 필드 | 설명 |
|------|------|
| `url` | 완성(크롭) 이미지 URL → 블록 렌더링에 사용 |
| `original_url` | 원본 이미지 URL → 재편집 시 편집기 로드용 |
| `crop_data` | 크롭 파라미터 → 재편집 시 편집기 상태 복원용 |

## 에러
| 코드 | 원인 | 메시지 예시 |
|------|------|------------|
| 400 | 파일 미첨부 | `"파일을 첨부해 주세요."` |
| 400 | 허용되지 않는 MIME | `"지원하지 않는 파일 형식입니다."` |
| 400 | 파일 크기 초과 | `"파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."` |
| 400 | crop_data JSON 파싱 실패 | `"crop_data는 유효한 JSON이어야 합니다."` |
| 401 | 토큰 없음/만료 | — |
""",
        request=_MEDIA_UPLOAD_REQUEST,
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
                            "original_url": "https://yourdomain.com/media/pages/originals/2026/03/product-thumb.jpg",
                            "crop_data": {
                                "x": 120, "y": 80, "width": 400, "height": 300,
                                "aspect_ratio": "4:3", "locked": True,
                                "original_width": 1200, "original_height": 900,
                            },
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
                    OpenApiExample(
                        "crop_data 파싱 오류",
                        value={"crop_data": ["crop_data는 유효한 JSON이어야 합니다."]},
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

        # 원본 파일 검증 + EXIF 제거 + 해상도 상한 (선택)
        original_file = request.FILES.get("original_file")
        original_processed = None
        if original_file:
            orig_mime = original_file.content_type or ""
            if orig_mime not in ALLOWED_MIME_TYPES:
                return Response(
                    {"original_file": ["지원하지 않는 파일 형식입니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if original_file.size > MAX_FILE_SIZE_BYTES:
                return Response(
                    {"original_file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # EXIF 제거 + 해상도 상한(4096) + 포맷 유지 정제
            try:
                original_processed = sanitize_original(original_file)
            except ImageValidationError as e:
                return Response(
                    {"original_file": [str(e)]},
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

        # 완성본은 정제·압축·EXIF 제거 파이프라인 통과
        try:
            processed = process_upload(file)
        except ImageValidationError as e:
            return Response(
                {"file": [str(e)]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        media = PageMedia.objects.create(
            page=page,
            file=ContentFile(processed.content, name=processed.suggest_filename(file.name)),
            original_file=(
                ContentFile(
                    original_processed.content,
                    name=original_processed.suggest_filename(original_file.name),
                )
                if original_processed
                else None
            ),
            crop_data=crop_data,
            original_name=file.name,
            mime_type=processed.mime_type,
            size=processed.size,
        )
        return Response(
            PageMediaSerializer(media, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# ─────────────────────────────────────────────────────────────
# 미디어 상세 — 삭제
# ─────────────────────────────────────────────────────────────

class PageMediaDetailView(APIView):
    """
    GET    /api/pages/me/media/{id}/ — 미디어 상세 조회 (재편집 시 original_url + crop_data 확인용)
    PATCH  /api/pages/me/media/{id}/ — 재편집 후 완성본 + crop_data 업데이트
    DELETE /api/pages/me/media/{id}/ — 미디어 파일 삭제 (스토리지 + DB 동시)
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        tags=["미디어"],
        summary="미디어 파일 상세 조회 (재편집용)",
        description="""
## 개요
특정 미디어 파일의 상세 정보를 반환합니다.  
**이미지 재편집** 시 이 API를 호출하여 `original_url`과 `crop_data`를 가져온 뒤  
편집기의 이전 상태를 복원합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 재편집 흐름
```typescript
// 1) 사용자가 이미지 클릭 → 미디어 상세 조회
const { data: media } = await api.get(`/api/pages/me/media/${mediaId}/`);

// 2) 편집기 복원
//    - original_url이 있으면 → 원본으로 편집기 열기
//    - 없으면 → url(완성본)으로 편집기 열기
const editUrl = media.original_url || media.url;

// 3) crop_data로 편집기 상태 복원
//    - crop_data가 {} 이면 → 전체 영역(최대 크롭) 적용
//    - locked가 없으면 → false로 간주
openEditor(editUrl, {
  ...media.crop_data,
  locked: media.crop_data.locked ?? false,
});
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 404 | 파일 없음 또는 본인 페이지의 파일이 아님 |
""",
        parameters=[
            OpenApiParameter(
                name="pk",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.PATH,
                description="미디어 파일 ID",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=PageMediaSerializer,
                description="미디어 파일 상세 정보",
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
                            "original_url": "https://yourdomain.com/media/pages/originals/2026/03/product-thumb.jpg",
                            "crop_data": {
                                "x": 120, "y": 80, "width": 400, "height": 300,
                                "aspect_ratio": "4:3", "locked": True,
                                "original_width": 1200, "original_height": 900,
                            },
                            "created_at": "2026-03-11T10:00:00Z",
                        },
                    ),
                    OpenApiExample(
                        "기존 이미지 (crop_data 없음 → 전체 크롭)",
                        value={
                            "id": 2,
                            "original_name": "avatar.png",
                            "mime_type": "image/png",
                            "size": 51200,
                            "size_display": "50.0 KB",
                            "url": "https://yourdomain.com/media/pages/2026/03/avatar.png",
                            "original_url": "",
                            "crop_data": {},
                            "created_at": "2026-03-10T09:00:00Z",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="파일 없음 또는 접근 권한 없음"),
        },
    )
    def get(self, request, pk):
        page, _ = Page.get_or_create_for_user(request.user)
        media = PageMedia.objects.filter(pk=pk, page=page).first()
        if not media:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(PageMediaSerializer(media, context={"request": request}).data)

    @extend_schema(
        tags=["미디어"],
        summary="미디어 파일 재편집 (완성본 + 크롭 파라미터 업데이트)",
        description="""
## 개요
이미지 **재편집(재크롭) 완료 후** 완성본 파일과 크롭 파라미터를 업데이트합니다.  
원본 이미지(`original_file`)는 변경되지 않습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 형식
`Content-Type: multipart/form-data` 필수

| 필드 | 필수 | 타입 | 설명 |
|------|:------:|------|------|
| `file` | ❌ | File | 재편집(재크롭) 완료된 새 최종 이미지 |
| `crop_data` | ❌ | string(JSON) | 새 크롭 파라미터 |

> 전송한 필드만 업데이트됩니다. `file`만 보내면 `crop_data`는 유지됩니다.

## 재편집 전체 흐름
```typescript
// 1) 미디어 상세 조회
const { data: media } = await api.get(`/api/pages/me/media/${mediaId}/`);

// 2) 편집기 복원 → 사용자 재편집 → 새 크롭 완료
const editUrl = media.original_url || media.url;
const { croppedBlob, newCropData } = await openEditor(editUrl, media.crop_data);

// 3) 재편집 결과 업데이트
const formData = new FormData();
formData.append('file', croppedBlob);
formData.append('crop_data', JSON.stringify(newCropData));
const { data: updated } = await api.patch(
  `/api/pages/me/media/${mediaId}/`,
  formData,
  { headers: { 'Content-Type': 'multipart/form-data' } }
);
// updated.url → 블록에서 참조하던 URL이 자동 갱신됨 (동일 media_id)
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 파일 형식/크기 오류, crop_data JSON 파싱 실패 |
| 401 | 토큰 없음/만료 |
| 404 | 파일 없음 또는 본인 페이지의 파일이 아님 |
""",
        parameters=[
            OpenApiParameter(
                name="pk",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.PATH,
                description="수정할 미디어 파일 ID",
            ),
        ],
        request=_MEDIA_PATCH_REQUEST,
        responses={
            200: OpenApiResponse(
                response=PageMediaSerializer,
                description="업데이트된 미디어 파일 정보",
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="파일 없음 또는 접근 권한 없음"),
        },
    )
    def patch(self, request, pk):
        page, _ = Page.get_or_create_for_user(request.user)
        media = PageMedia.objects.filter(pk=pk, page=page).first()
        if not media:
            return Response(status=status.HTTP_404_NOT_FOUND)

        # 새 완성본 파일 (선택)
        new_file = request.FILES.get("file")
        if new_file:
            mime_type = new_file.content_type or ""
            if mime_type not in ALLOWED_MIME_TYPES:
                return Response(
                    {"file": ["지원하지 않는 파일 형식입니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if new_file.size > MAX_FILE_SIZE_BYTES:
                return Response(
                    {"file": ["파일 크기가 너무 큽니다. 최대 10MB까지 업로드 가능합니다."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # 정제·압축 파이프라인
            try:
                processed = process_upload(new_file)
            except ImageValidationError as e:
                return Response(
                    {"file": [str(e)]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # 기존 완성본 파일 스토리지 삭제
            if media.file:
                media.file.delete(save=False)
            media.file.save(
                processed.suggest_filename(new_file.name),
                ContentFile(processed.content),
                save=False,
            )
            media.mime_type = processed.mime_type
            media.size = processed.size

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
