"""apps/admin_api/serializers/pages.py — 어드민 페이지 관리/모더레이션 시리얼라이저.

라우팅은 ``/api/v1/admin/pages/`` 아래에서 ``IsAdminUser``(is_staff=True) 권한으로만 접근.
크로스 워크스페이스 전역 스코프이며, 페이지 소유자(``page.user``)는 최소 정보(id/email)만 노출한다.
민감 정보(IG 토큰 등)는 절대 직렬화하지 않는다 — 이 도메인은 페이지/통계만 다룬다.

일반 유저용 시리얼라이저는 ``apps.pages.serializers`` 참고.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from apps.pages.models import ContactInquiry, Page, PageSubscription


class _OwnerSerializer(serializers.Serializer):
    """페이지 소유자(User) 최소 정보. ``source=page.user`` 로 주입."""

    id = serializers.IntegerField(read_only=True, help_text="소유자 User PK")
    email = serializers.EmailField(read_only=True, help_text="소유자 이메일 (로그인 ID)")

    class Meta:
        ref_name = "AdminPageOwner"


class _BlockSummarySerializer(serializers.Serializer):
    """상세 응답에 포함되는 블록 요약. (page.blocks, order 정렬)"""

    id = serializers.IntegerField(read_only=True, help_text="블록 PK")
    type = serializers.CharField(
        read_only=True, help_text="블록 타입 (profile/contact/single_link 등)"
    )
    order = serializers.IntegerField(read_only=True, help_text="표시 순서 (ASC)")
    is_enabled = serializers.BooleanField(read_only=True, help_text="노출 여부")

    class Meta:
        ref_name = "AdminPageBlockSummary"


class _ReferenceCategoryBriefSerializer(serializers.Serializer):
    """상세 응답의 reference_category 축약 표현 (slug + name)."""

    slug = serializers.CharField(read_only=True, help_text="카테고리 영문 슬러그")
    name = serializers.CharField(read_only=True, help_text="카테고리 한글명")

    class Meta:
        ref_name = "AdminPageReferenceCategoryBrief"


def _views_in_last_30d(page: Page) -> int:
    """최근 30일 PageView 카운트. (annotate 가 없을 때 per-row fallback)"""
    since = timezone.now() - timedelta(days=30)
    return page.views.filter(viewed_at__gte=since).count()


class AdminPageListSerializer(serializers.ModelSerializer):
    """어드민 페이지 목록 항목. 소유자/공개여부/임포트출처/최근30일 조회수 요약."""

    owner = _OwnerSerializer(source="user", read_only=True, help_text="페이지 소유자 (id/email)")
    views_30d = serializers.SerializerMethodField(
        help_text="최근 30일간 PageView(공개 페이지 조회) 수."
    )

    class Meta:
        model = Page
        fields = [
            "slug",
            "title",
            "owner",
            "is_public",
            "is_active",
            "import_source",
            "is_reference",
            "views_30d",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_views_30d(self, obj: Page) -> int:
        # 뷰에서 annotate(views_30d=...) 한 경우 그 값을 우선 사용해 N+1 회피.
        annotated = getattr(obj, "views_30d", None)
        if isinstance(annotated, int):
            return annotated
        return _views_in_last_30d(obj)


class AdminPageDetailSerializer(serializers.ModelSerializer):
    """어드민 페이지 상세. 목록 필드 + 블록 목록 + 누적/최근 통계 + 레퍼런스 카테고리."""

    owner = _OwnerSerializer(source="user", read_only=True, help_text="페이지 소유자 (id/email)")
    views_30d = serializers.SerializerMethodField(help_text="최근 30일간 PageView 수.")
    blocks = serializers.SerializerMethodField(
        help_text="페이지에 속한 블록 요약 목록 (order ASC)."
    )
    stats = serializers.SerializerMethodField(
        help_text="누적 통계 {views_total, clicks_total, views_30d}."
    )
    reference_category = serializers.SerializerMethodField(
        help_text="AI 레퍼런스 카테고리 {slug, name} 또는 null."
    )

    class Meta:
        model = Page
        fields = [
            "slug",
            "title",
            "owner",
            "is_public",
            "is_active",
            "import_source",
            "is_reference",
            "views_30d",
            "created_at",
            "updated_at",
            "blocks",
            "stats",
            "reference_category",
        ]
        read_only_fields = fields

    def get_views_30d(self, obj: Page) -> int:
        return _views_in_last_30d(obj)

    def get_blocks(self, obj: Page) -> list[dict]:
        blocks = obj.blocks.all().order_by("order")
        return _BlockSummarySerializer(blocks, many=True).data

    def get_stats(self, obj: Page) -> dict:
        return {
            "views_total": obj.views.count(),
            "clicks_total": obj.clicks.count(),
            "views_30d": _views_in_last_30d(obj),
        }

    def get_reference_category(self, obj: Page) -> dict | None:
        if obj.reference_category_id is None:
            return None
        return _ReferenceCategoryBriefSerializer(obj.reference_category).data


class AdminPageUpdateSerializer(serializers.ModelSerializer):
    """어드민 모더레이션 PATCH 바디. is_active(차단) / is_public(강제 비공개) 만 변경 가능."""

    class Meta:
        model = Page
        fields = ["is_active", "is_public"]
        extra_kwargs = {
            "is_active": {
                "required": False,
                "help_text": "False 로 설정 시 페이지 차단 (공개 URL 접근 불가).",
            },
            "is_public": {
                "required": False,
                "help_text": "False 로 강제 시 비공개 전환 (정책 위반 페이지 모더레이션).",
            },
        }


class AdminPageInquirySerializer(serializers.ModelSerializer):
    """페이지 방문자 문의(ContactInquiry) 읽기 전용 시리얼라이저."""

    class Meta:
        model = ContactInquiry
        fields = [
            "id",
            "page",
            "name",
            "category",
            "email",
            "phone",
            "subject",
            "content",
            "agreed_to_terms",
            "memo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class AdminPageSubscriptionSerializer(serializers.ModelSerializer):
    """페이지 구독자(PageSubscription) 읽기 전용 시리얼라이저."""

    class Meta:
        model = PageSubscription
        fields = [
            "id",
            "page",
            "name",
            "category",
            "email",
            "phone",
            "agreed_to_terms",
            "memo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
