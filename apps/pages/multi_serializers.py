"""
apps/pages/multi_serializers.py

다중 페이지(Multi-Page) 전용 시리얼라이저.
Block / Stats / Inquiry / Subscription / Media 관련 시리얼라이저는
기존 serializers.py 것을 그대로 재사용합니다.
"""

from drf_spectacular.utils import OpenApiExample, extend_schema_serializer
from rest_framework import serializers

from .models import Page


# ─────────────────────────────────────────────────────────────
# 페이지 목록 / 상세
# ─────────────────────────────────────────────────────────────

@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "페이지 예시",
            value={
                "id": 3,
                "slug": "my-second-page",
                "title": "두 번째 링크 페이지",
                "is_public": True,
                "data": {
                    "theme": "light",
                    "background_color": "#ffffff",
                    "font_family": "Pretendard",
                },
                "created_at": "2026-03-10T12:00:00Z",
                "updated_at": "2026-03-12T09:30:00Z",
            },
        )
    ]
)
class MultiPageSerializer(serializers.ModelSerializer):
    """다중 페이지 목록/상세 조회 응답."""

    class Meta:
        model = Page
        fields = ["id", "slug", "title", "is_public", "data", "custom_css", "created_at", "updated_at"]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]


# ─────────────────────────────────────────────────────────────
# 페이지 생성
# ─────────────────────────────────────────────────────────────

@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "slug 미지정 — 자동 생성",
            summary="slug 생략 시 자동 할당",
            value={
                "title": "두 번째 링크 페이지",
                "is_public": False,
                "data": {"theme": "light"},
            },
            request_only=True,
        ),
        OpenApiExample(
            "slug 직접 지정",
            summary="원하는 slug를 직접 지정",
            value={
                "slug": "my-product-page",
                "title": "상품 소개 페이지",
                "is_public": True,
                "data": {},
            },
            request_only=True,
        ),
    ]
)
class MultiPageCreateSerializer(serializers.Serializer):
    """
    POST /api/v1/pages/multipages/ 전용 생성 시리얼라이저.
    slug는 선택 입력 — 미지정 시 username 기반으로 자동 생성됩니다.
    """

    slug = serializers.SlugField(
        max_length=120,
        required=False,
        allow_null=True,
        help_text="영문 소문자·숫자·하이픈만 허용. 2~120자. 생략 시 자동 생성.",
    )
    title = serializers.CharField(
        max_length=255,
        required=False,
        default="",
        allow_blank=True,
        help_text="페이지 제목. 빈 문자열 허용.",
    )
    is_public = serializers.BooleanField(
        required=False,
        default=False,
        help_text="true이면 즉시 공개. 기본값 false.",
    )
    data = serializers.JSONField(
        required=False,
        default=dict,
        help_text="프론트엔드 전용 설정 저장소 (테마, 배경색 등). 서버는 내용을 파싱하지 않습니다.",
    )
    custom_css = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        help_text="페이지에 적용할 커스텀 CSS. 빈 문자열 허용.",
    )

    def validate_slug(self, value: str) -> str:
        if value is None:
            return value
        value = value.lower().strip("-")
        if len(value) < 2:
            raise serializers.ValidationError("slug는 2자 이상이어야 합니다.")
        if Page.objects.filter(slug=value).exists():
            raise serializers.ValidationError("이미 사용 중인 slug입니다.")
        return value

    def validate_data(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("data 필드는 JSON object여야 합니다.")
        return value


# ─────────────────────────────────────────────────────────────
# 페이지 slug 변경 (다중 페이지용)
# ─────────────────────────────────────────────────────────────

class MultiPageSlugChangeSerializer(serializers.Serializer):
    """
    PATCH /api/v1/pages/multipages/{id}/slug/ 전용.
    현재 페이지 소유자는 context['page_id']로 전달.
    """

    slug = serializers.SlugField(
        max_length=120,
        help_text="영문 소문자, 숫자, 하이픈(앞뒤 불가)만 허용. 2~120자.",
    )

    def validate_slug(self, value: str) -> str:
        value = value.lower().strip("-")
        if len(value) < 2:
            raise serializers.ValidationError("slug는 2자 이상이어야 합니다.")
        page_id = self.context.get("page_id")
        qs = Page.objects.filter(slug=value)
        if page_id:
            qs = qs.exclude(pk=page_id)
        if qs.exists():
            raise serializers.ValidationError("이미 사용 중인 slug입니다.")
        return value
