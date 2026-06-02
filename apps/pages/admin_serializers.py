"""apps/pages/admin_serializers.py — 어드민 전용 AI 레퍼런스 시리얼라이저.

라우팅은 ``/api/v1/admin/`` 아래에서 ``IsAdminUser`` 권한으로만 접근 가능.
일반 유저용 시리얼라이저는 ``apps.ai_jobs.serializers`` 참고.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import Page, ReferenceCategory


class AdminReferenceCategorySerializer(serializers.ModelSerializer):
    """어드민용 카테고리 CRUD 시리얼라이저."""

    reference_page_count = serializers.SerializerMethodField(
        help_text="이 카테고리에 매핑된 Page 수 (is_reference=True 만 카운트)."
    )

    class Meta:
        model = ReferenceCategory
        fields = [
            "id",
            "slug",
            "name",
            "description",
            "icon_emoji",
            "icon_url",
            "sort_order",
            "is_active",
            "reference_page_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "reference_page_count", "created_at", "updated_at"]

    def get_reference_page_count(self, obj: ReferenceCategory) -> int:
        # 어드민 화면에선 비공개도 함께 보고 싶을 수 있으므로 is_public 필터 미적용.
        return obj.reference_pages.filter(is_reference=True).count()


class AdminReferencePageSerializer(serializers.ModelSerializer):
    """어드민용 레퍼런스 후보 페이지 응답 시리얼라이저 (읽기 전용 메타 + 토글 필드)."""

    reference_category_slug = serializers.SerializerMethodField()
    reference_category_name = serializers.SerializerMethodField()
    reference_snapshot_url = serializers.SerializerMethodField()
    user_email = serializers.CharField(source="user.email", read_only=True)
    effective_title = serializers.SerializerMethodField(
        help_text="reference_title 이 있으면 그것, 없으면 page.title."
    )

    class Meta:
        model = Page
        fields = [
            "slug",
            "user_email",
            "title",
            "effective_title",
            "is_public",
            "is_active",
            "is_reference",
            "reference_category",
            "reference_category_slug",
            "reference_category_name",
            "reference_order",
            "reference_title",
            "reference_description",
            "reference_snapshot_url",
            "reference_snapshot_status",
            "reference_snapshot_updated_at",
            "reference_snapshot_job_id",
            "reference_snapshot_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields  # 응답 전용. 업데이트는 별도 시리얼라이저 사용.

    def get_reference_category_slug(self, obj: Page) -> str:
        return obj.reference_category.slug if obj.reference_category_id else ""

    def get_reference_category_name(self, obj: Page) -> str:
        return obj.reference_category.name if obj.reference_category_id else ""

    def get_reference_snapshot_url(self, obj: Page):
        if not obj.reference_snapshot:
            return None
        request = self.context.get("request")
        url = obj.reference_snapshot.url
        if request is not None and url.startswith("/"):
            return request.build_absolute_uri(url)
        return url

    def get_effective_title(self, obj: Page) -> str:
        return (obj.reference_title or "").strip() or obj.title


class AdminPageReferenceUpdateSerializer(serializers.Serializer):
    """`PATCH /api/v1/admin/pages/{slug}/reference/` 요청 바디.

    모든 필드 optional — 보낸 키만 적용 (partial update).
    """

    is_reference = serializers.BooleanField(required=False)
    reference_category_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="ReferenceCategory PK. null 을 보내면 카테고리 해제.",
    )
    reference_order = serializers.IntegerField(
        required=False, min_value=0, max_value=10000
    )
    reference_title = serializers.CharField(
        required=False, allow_blank=True, max_length=120
    )
    reference_description = serializers.CharField(
        required=False, allow_blank=True
    )

    def validate_reference_category_id(self, value):
        if value is None:
            return value
        if not ReferenceCategory.objects.filter(id=value).exists():
            raise serializers.ValidationError("존재하지 않는 카테고리 ID 입니다.")
        return value


class AdminSnapshotTriggerResponseSerializer(serializers.Serializer):
    """`POST .../reference/snapshot/` 응답 — Celery job id + 초기 상태."""

    job_id = serializers.CharField()
    status = serializers.ChoiceField(
        choices=["pending", "running", "succeeded", "failed"]
    )


class AdminSnapshotStatusSerializer(serializers.Serializer):
    """`GET .../reference/snapshot/status/` 응답 — 폴링용."""

    job_id = serializers.CharField(allow_blank=True)
    status = serializers.CharField(allow_blank=True)
    error = serializers.CharField(allow_blank=True)
    snapshot_url = serializers.URLField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)
