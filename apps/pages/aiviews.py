"""
apps/pages/aiviews.py

AI 도구 전용 페이지 편집 API.

■ 슬러그로 페이지 복사
  POST /api/v1/pages/ai/clone-from-slug/   → 특정 slug 페이지를 내 새 페이지로 복사

■ 페이지 전체 편집 (AI 1-shot)
  POST /api/v1/pages/ai/@{slug}/           → 내 페이지 메타 + 블록 전체를 한 번에 덮어쓰기
"""

from django.db import transaction
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Block, Page
from .models import _generate_unique_slug
from .serializers import BlockSerializer, PageSerializer
from .validators import validate_block_data

_AI_TAG = "AI 도구"


# ─────────────────────────────────────────────────────────────
# Serializers (aiviews 전용)
# ─────────────────────────────────────────────────────────────

class AiCloneFromSlugRequestSerializer(serializers.Serializer):
    slug = serializers.SlugField(
        help_text="복사할 원본 페이지의 slug.",
    )
    title = serializers.CharField(
        max_length=255,
        required=False,
        default="",
        allow_blank=True,
        help_text="새 페이지의 제목. 생략 시 원본 제목 그대로 사용.",
    )


class AiBlockItemSerializer(serializers.Serializer):
    """AiPageEditView 요청 내 블록 하나."""

    type = serializers.ChoiceField(
        choices=Block.BlockType.choices,
        help_text="블록 타입 (profile | contact | single_link)",
    )
    order = serializers.IntegerField(
        min_value=1,
        required=False,
        default=None,
        allow_null=True,
        help_text="표시 순서 (1~). 생략 시 배열 인덱스+1 순서로 자동 부여.",
    )
    is_enabled = serializers.BooleanField(
        default=True,
        required=False,
        help_text="false이면 공개 페이지에서 숨김.",
    )
    data = serializers.JSONField(
        default=dict,
        required=False,
        help_text="블록 콘텐츠 데이터.",
    )
    custom_css = serializers.CharField(
        default="",
        allow_blank=True,
        required=False,
        help_text="블록 커스텀 CSS.",
    )
    schedule_enabled = serializers.BooleanField(default=False, required=False)
    publish_at = serializers.DateTimeField(allow_null=True, required=False, default=None)
    hide_at = serializers.DateTimeField(allow_null=True, required=False, default=None)


class AiPageEditRequestSerializer(serializers.Serializer):
    """POST /api/v1/pages/ai/@{slug}/ 요청 바디."""

    title = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text="페이지 제목.",
    )
    is_public = serializers.BooleanField(
        required=False,
        help_text="공개 여부.",
    )
    data = serializers.JSONField(
        required=False,
        help_text="페이지 설정 데이터 (테마 등). 전체 덮어쓰기.",
    )
    custom_css = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="페이지 커스텀 CSS.",
    )
    blocks = AiBlockItemSerializer(
        many=True,
        required=False,
        help_text=(
            "블록 배열. 전송하면 기존 블록 전체 삭제 후 재생성. "
            "생략하면 블록 변경 없음."
        ),
    )

    def validate_blocks(self, blocks):
        for i, block in enumerate(blocks):
            btype = block.get("type")
            bdata = block.get("data") or {}
            if btype:
                validate_block_data(btype, bdata)
            if block.get("schedule_enabled"):
                pub = block.get("publish_at")
                hide = block.get("hide_at")
                if pub is None and hide is None:
                    raise serializers.ValidationError(
                        f"blocks[{i}]: schedule_enabled=true일 때 publish_at 또는 hide_at이 필요합니다."
                    )
                if pub and hide and pub >= hide:
                    raise serializers.ValidationError(
                        f"blocks[{i}]: hide_at은 publish_at보다 나중이어야 합니다."
                    )
        return blocks


# ─────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────

class AiCloneFromSlugView(APIView):
    """POST /api/v1/pages/ai/clone-from-slug/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_AI_TAG],
        summary="슬러그로 페이지 복사 (AI용)",
        description="""
## 개요
공개된 특정 페이지(또는 본인 페이지)의 **slug를 입력받아 동일한 구조의 새 페이지**를 생성합니다.  
AI 도구가 참고 페이지를 불러와 복제하거나 편집의 시작점으로 활용할 때 사용합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 동작 순서
1. `slug`로 페이지를 조회합니다.
   - 본인 소유이면 비공개 페이지도 허용됩니다.
   - 타인 소유이면 **공개(`is_public: true`) 페이지만** 허용됩니다.
2. 원본 페이지의 `title`, `data`, `custom_css`, 블록 전체를 복사합니다.
3. 새 slug는 `{요청자_username}-{n}` 형태로 자동 생성됩니다.
4. 새 페이지의 `is_public`은 항상 `false`로 생성됩니다.

## 요청 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `slug` | ✅ | 복사할 원본 페이지의 slug |
| `title` | ❌ | 새 페이지 제목. 생략 시 원본 제목 사용 |

## 에러
| 코드 | 원인 |
|------|------|
| 400 | slug 형식 오류 |
| 401 | 인증 실패 |
| 404 | 존재하지 않거나 비공개 타인 페이지 |
        """,
        request=AiCloneFromSlugRequestSerializer,
        responses={
            201: OpenApiResponse(
                response=PageSerializer,
                description="복사하여 생성된 새 페이지",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 10,
                            "slug": "myuser-3",
                            "title": "복사된 페이지",
                            "is_public": False,
                            "data": {"theme": "dark"},
                            "custom_css": "",
                            "created_at": "2026-04-06T00:00:00Z",
                            "updated_at": "2026-04-06T00:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 비공개 타인 페이지"),
        },
        examples=[
            OpenApiExample(
                "기본 복사",
                summary="slug만 전송, 제목은 원본 그대로",
                value={"slug": "hong-gildong"},
                request_only=True,
            ),
            OpenApiExample(
                "제목 변경 후 복사",
                summary="slug + 새 제목 지정",
                value={"slug": "hong-gildong", "title": "내 버전 페이지"},
                request_only=True,
            ),
        ],
    )
    @transaction.atomic
    def post(self, request):
        ser = AiCloneFromSlugRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        slug = ser.validated_data["slug"]
        title_override = ser.validated_data.get("title", "")

        # 본인 소유이면 비공개도 허용, 타인이면 공개만 허용
        qs = Page.objects.filter(slug=slug)
        page = qs.filter(user=request.user).first() or qs.filter(is_public=True).first()
        if page is None:
            return Response(
                {"detail": "페이지를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        new_slug = _generate_unique_slug(request.user.username)
        new_page = Page.objects.create(
            user=request.user,
            slug=new_slug,
            title=title_override if title_override else page.title,
            is_public=False,
            data=page.data,
            custom_css=page.custom_css,
        )

        # 블록 복사
        source_blocks = list(page.blocks.order_by("order"))
        if source_blocks:
            Block.objects.bulk_create([
                Block(
                    page=new_page,
                    type=b.type,
                    order=b.order,
                    is_enabled=b.is_enabled,
                    data=b.data,
                    custom_css=b.custom_css,
                    schedule_enabled=b.schedule_enabled,
                    publish_at=b.publish_at,
                    hide_at=b.hide_at,
                )
                for b in source_blocks
            ])

        return Response(PageSerializer(new_page).data, status=status.HTTP_201_CREATED)


class AiPageEditView(APIView):
    """POST /api/v1/pages/ai/@{slug}/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[_AI_TAG],
        summary="페이지 전체 편집 (AI 1-shot)",
        description="""
## 개요
AI 도구가 페이지 메타데이터와 블록 전체를 **한 번의 요청으로 덮어씌울 수 있는** 엔드포인트입니다.  
기존 편집 API들(블록 개별 PATCH, 재정렬 등)을 여러 번 호출하는 대신  
전체 페이지 상태를 한 번에 서버로 전달해 원자적으로 업데이트합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수  
**본인 소유 페이지만** 편집 가능합니다.

## 경로 파라미터
| 파라미터 | 설명 |
|----------|------|
| `slug` | 편집할 페이지의 slug |

## 요청 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `title` | ❌ | 페이지 제목 |
| `is_public` | ❌ | 공개 여부 |
| `data` | ❌ | 페이지 설정 (테마 등). 전체 덮어쓰기 |
| `custom_css` | ❌ | 페이지 커스텀 CSS |
| `blocks` | ❌ | 블록 배열. **전송 시 기존 블록 전체 삭제 후 재생성** |

## `blocks` 동작 방식
- 생략하면 블록이 **변경되지 않습니다**.
- 전송하면 해당 페이지의 **기존 블록이 모두 삭제**되고 배열 순서대로 새로 생성됩니다.
- 각 블록의 `order`를 생략하면 배열 인덱스+1이 자동 부여됩니다.

## 블록 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `type` | ✅ | `profile` \| `contact` \| `single_link` |
| `order` | ❌ | 표시 순서. 생략 시 자동 부여 |
| `is_enabled` | ❌ | false이면 공개 페이지에서 숨김 (기본 true) |
| `data` | ❌ | 타입별 콘텐츠 |
| `custom_css` | ❌ | 블록 커스텀 CSS |
| `schedule_enabled` | ❌ | 예약 설정 활성화 |
| `publish_at` | ❌ | 공개 시작 일시 |
| `hide_at` | ❌ | 숨김 시작 일시 |

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 유효성 검증 실패 |
| 401 | 인증 실패 |
| 404 | 페이지 없음 또는 다른 사용자의 페이지 |
        """,
        parameters=[
            OpenApiParameter(
                name="slug",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="편집할 페이지의 slug",
            ),
        ],
        request=AiPageEditRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=PageSerializer,
                description="수정된 페이지 정보",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "id": 3,
                            "slug": "my-page",
                            "title": "AI가 수정한 페이지",
                            "is_public": True,
                            "data": {"theme": "light"},
                            "custom_css": "",
                            "created_at": "2026-04-01T00:00:00Z",
                            "updated_at": "2026-04-06T10:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="페이지 없음 또는 접근 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "전체 편집 예시",
                summary="제목·공개 여부·블록 전체 교체",
                value={
                    "title": "AI가 만든 페이지",
                    "is_public": True,
                    "data": {"theme": "dark"},
                    "blocks": [
                        {
                            "type": "profile",
                            "data": {"headline": "안녕하세요"},
                        },
                        {
                            "type": "single_link",
                            "data": {"url": "https://example.com", "label": "내 사이트"},
                        },
                        {
                            "type": "contact",
                            "data": {"country_code": "+82", "phone": "01012345678"},
                        },
                    ],
                },
                request_only=True,
            ),
            OpenApiExample(
                "메타만 수정 (블록 유지)",
                summary="blocks 생략 시 블록 변경 없음",
                value={
                    "title": "제목만 바꾸기",
                    "is_public": False,
                },
                request_only=True,
            ),
        ],
    )
    @transaction.atomic
    def post(self, request, slug: str):
        page = Page.objects.filter(slug=slug, user=request.user).first()
        if page is None:
            return Response(
                {"detail": "페이지를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        ser = AiPageEditRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        # 페이지 메타 업데이트 (전송된 필드만)
        updated = False
        for field in ("title", "is_public", "data", "custom_css"):
            if field in vd:
                setattr(page, field, vd[field])
                updated = True
        if updated:
            page.save()

        # 블록 교체 (전송된 경우에만)
        if "blocks" in vd:
            page.blocks.all().delete()
            new_blocks = []
            for i, b in enumerate(vd["blocks"]):
                new_blocks.append(Block(
                    page=page,
                    type=b["type"],
                    order=b["order"] if b.get("order") else (i + 1),
                    is_enabled=b.get("is_enabled", True),
                    data=b.get("data") or {},
                    custom_css=b.get("custom_css", ""),
                    schedule_enabled=b.get("schedule_enabled", False),
                    publish_at=b.get("publish_at"),
                    hide_at=b.get("hide_at"),
                ))
            if new_blocks:
                Block.objects.bulk_create(new_blocks)

        return Response(PageSerializer(page).data, status=status.HTTP_200_OK)
