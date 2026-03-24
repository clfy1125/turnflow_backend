from drf_spectacular.utils import OpenApiExample, extend_schema_field, extend_schema_serializer
from rest_framework import serializers
from django.db.models import Q
from django.utils import timezone

from .models import Block, ContactInquiry, Page, PageMedia, PageSubscription
from .validators import validate_block_data


@extend_schema_field(
    field={
        "type": "object",
        "properties": {
            "url":           {"type": "string", "format": "uri",    "description": "[single_link 필수] 이동할 URL"},
            "label":         {"type": "string",                      "description": "[single_link 필수] 버튼 표시 텍스트"},
            "description":   {"type": "string",                      "description": "[single_link 선택] 버튼 하단 설명"},
            "layout":        {"type": "string", "enum": ["small", "large"], "description": "[single_link 선택] 버튼 크기 (기본: small)"},
            "thumbnail_url": {"type": "string", "format": "uri",    "description": "[single_link 선택] 썸네일 이미지 URL"},
            "headline":      {"type": "string",                      "description": "[profile 필수] 한 줄 소개"},
            "subline":       {"type": "string",                      "description": "[profile 선택] 부제목"},
            "avatar_url":    {"type": "string", "format": "uri",    "description": "[profile 선택] 프로필 이미지 URL"},
            "country_code":  {"type": "string",                      "description": "[contact 필수] 국가 코드 (+82 형식)"},
            "phone":         {"type": "string",                      "description": "[contact 필수] 전화번호 (하이픈 없이)"},
            "whatsapp":      {"type": "boolean",                     "description": "[contact 선택] WhatsApp 링크 사용 여부"},
        },
        "example": {
            "url": "https://naver.me/abc",
            "label": "쿠팡 추천 링크",
            "description": "오늘만 할인",
            "layout": "large",
        },
    }
)
class BlockDataField(serializers.JSONField):
    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "single_link — 링크 버튼",
            summary="단일 링크 버튼 블록",
            value={
                "type": "single_link",
                "data": {
                    "url": "https://naver.me/abc",
                    "label": "쿠팡 추천 링크",
                    "description": "오늘만 할인",
                    "layout": "large",
                    "thumbnail_url": "https://example.com/thumb.jpg",
                },
            },
            request_only=True,
        ),
        OpenApiExample(
            "single_link — 예약 공개/숨김",
            summary="링크 블록 + 예약 설정 (공개다음 숨김)",
            value={
                "type": "single_link",
                "data": {
                    "url": "https://naver.me/abc",
                    "label": "시즈널 플래시셀",
                },
                "schedule_enabled": True,
                "publish_at": "2026-03-10T10:00:00+09:00",
                "hide_at": "2026-03-17T23:59:00+09:00",
            },
            request_only=True,
        ),
        OpenApiExample(
            "profile — 프로필 소개",
            summary="프로필 소개 블록",
            value={
                "type": "profile",
                "data": {
                    "headline": "독일 면도기 전문",
                    "subline": "방수 / 저소음",
                    "avatar_url": "https://example.com/avatar.jpg",
                },
            },
            request_only=True,
        ),
        OpenApiExample(
            "contact — 연락처",
            summary="연락처 블록",
            value={
                "type": "contact",
                "data": {
                    "country_code": "+82",
                    "phone": "01012345678",
                    "whatsapp": True,
                },
            },
            request_only=True,
        ),
    ]
)
class BlockSerializer(serializers.ModelSerializer):
    data = BlockDataField(default=dict)

    class Meta:
        model = Block
        fields = [
            "id", "type", "order", "is_enabled", "data",
            "schedule_enabled", "publish_at", "hide_at",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        # type 변경 금지 (PATCH 시 instance 존재)
        if self.instance is not None:
            if "type" in attrs and attrs["type"] != self.instance.type:
                raise serializers.ValidationError(
                    {"type": "블록 타입은 변경할 수 없습니다."}
                )
            block_type = self.instance.type
        else:
            block_type = attrs.get("type")

        # data 검증
        data_value = attrs.get("data", self.instance.data if self.instance else {})
        if block_type:
            validate_block_data(block_type, data_value)

        # 예약 설정 검증
        schedule_enabled = attrs.get(
            "schedule_enabled",
            self.instance.schedule_enabled if self.instance else False,
        )
        publish_at = attrs.get("publish_at", self.instance.publish_at if self.instance else None)
        hide_at = attrs.get("hide_at", self.instance.hide_at if self.instance else None)

        if schedule_enabled:
            if publish_at is None and hide_at is None:
                raise serializers.ValidationError(
                    {"schedule": "schedule_enabled=true일 때 publish_at 또는 hide_at 중 하나는 필수입니다."}
                )
            if publish_at and hide_at and publish_at >= hide_at:
                raise serializers.ValidationError(
                    {"hide_at": "hide_at은 publish_at보다 나중이어야 합니다."}
                )

        return attrs

    def create(self, validated_data):
        page = self.context["page"]

        # order 미지정 시 맨 뒤에 추가
        if "order" not in validated_data or validated_data.get("order") is None:
            last = Block.objects.filter(page=page).order_by("-order").first()
            validated_data["order"] = (last.order + 1) if last else 1

        validated_data["page"] = page
        return super().create(validated_data)


class BlockPublicSerializer(serializers.ModelSerializer):
    """공개 페이지 조회 시 is_enabled=True 블록만 반환용."""

    class Meta:
        model = Block
        fields = ["id", "type", "order", "data"]


class PageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Page
        fields = ["id", "slug", "title", "is_public", "data", "created_at", "updated_at"]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]


class SlugChangeSerializer(serializers.Serializer):
    """
    PATCH /api/pages/me/slug/ 전용.
    slug 형식 검증 + 중복 검증.
    """
    slug = serializers.SlugField(
        max_length=120,
        help_text="영문 소문자, 숫자, 하이픈(헤더/톨미 불가)만 허용. 2~120자.",
    )

    def validate_slug(self, value: str) -> str:
        value = value.lower().strip("-")
        if len(value) < 2:
            raise serializers.ValidationError("slug는 2자 이상이어야 합니다.")
        # 소유자 자신의 변경 전제외 중복 체크
        user = self.context.get("user")
        qs = Page.objects.filter(slug=value)
        if user:
            qs = qs.exclude(user=user)
        if qs.exists():
            raise serializers.ValidationError("이미 사용 중인 slug입니다.")
        return value


class SlugCheckSerializer(serializers.Serializer):
    """GET /api/pages/check-slug/?slug=xxx 전용 응답 스키마."""
    slug = serializers.SlugField()
    available = serializers.BooleanField()
    message = serializers.CharField()


class PagePublicSerializer(serializers.ModelSerializer):
    """공개 페이지 조회 - is_enabled=True 블록 포함."""

    blocks = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = ["slug", "title", "is_public", "data", "blocks"]

    def get_blocks(self, obj):
        now = timezone.now()
        qs = obj.blocks.filter(
            is_enabled=True
        ).filter(
            # schedule_enabled=False → 시간 제한 없이 표시
            Q(schedule_enabled=False)
            # schedule_enabled=True → publish_at 이후 and hide_at 이전
            | Q(
                schedule_enabled=True,
                publish_at__isnull=False,
                publish_at__lte=now,
            ) & (Q(hide_at__isnull=True) | Q(hide_at__gt=now))
            # publish_at 없이 hide_at만 있는 경우 (hide_at 이전에만 표시)
            | Q(
                schedule_enabled=True,
                publish_at__isnull=True,
                hide_at__isnull=False,
                hide_at__gt=now,
            )
        ).order_by("order")
        return BlockPublicSerializer(qs, many=True).data


class ReorderItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    order = serializers.IntegerField(min_value=1)


class ReorderSerializer(serializers.Serializer):
    orders = ReorderItemSerializer(many=True)

    def validate_orders(self, value):
        if not value:
            raise serializers.ValidationError("orders 목록이 비어 있습니다.")
        # 중복 order 금지
        orders = [item["order"] for item in value]
        if len(orders) != len(set(orders)):
            raise serializers.ValidationError("order 값에 중복이 있습니다.")
        # 중복 id 금지
        ids = [item["id"] for item in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("id 값에 중복이 있습니다.")
        return value


# ─── 통계 시리얼라이저 ────────────────────────────────────────

class RefererStatSerializer(serializers.Serializer):
    source = serializers.CharField(help_text="유입 채널명 (Instagram, 직접 방문 등)")
    count = serializers.IntegerField(help_text="해당 채널 유입 횟수")
    percentage = serializers.FloatField(help_text="전체 조회수 대비 비율 (%)")


class CountryStatSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="ISO 3166-1 alpha-2 국가 코드 (KR, US …)")
    name = serializers.CharField(help_text="국가명 (한국어)")
    count = serializers.IntegerField(help_text="해당 국가 유입 횟수")
    percentage = serializers.FloatField(help_text="전체 조회수 대비 비율 (%)")


class StatsSummarySerializer(serializers.Serializer):
    period = serializers.CharField(help_text="조회 기간 (7d / 30d / 90d)")
    total_views = serializers.IntegerField(help_text="기간 내 페이지 총 조회수")
    total_clicks = serializers.IntegerField(help_text="기간 내 블록 총 클릭수")
    click_rate = serializers.FloatField(help_text="클릭율 = 클릭수 / 조회수 × 100 (%)")
    referers = RefererStatSerializer(many=True, help_text="유입 채널 Top5")
    countries = CountryStatSerializer(many=True, help_text="유입 국가 Top5")


class ChartDataSerializer(serializers.Serializer):
    period = serializers.CharField(help_text="조회 기간 (7d / 30d / 90d)")
    labels = serializers.ListField(
        child=serializers.CharField(),
        help_text="날짜 배열 (YYYY-MM-DD). 오늘 포함 period 일치",
    )
    views = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="labels와 같은 길이의 일별 조회수 배열",
    )
    clicks = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="labels와 같은 길이의 일별 클릭수 배열",
    )


class BlockStatSerializer(serializers.Serializer):
    block_id = serializers.IntegerField(help_text="블록 ID")
    type = serializers.CharField(help_text="블록 타입 (single_link / profile / contact)")
    label = serializers.CharField(help_text="블록 대표 레이블 (data.label 또는 data.headline)")
    is_enabled = serializers.BooleanField(help_text="현재 노출 여부")
    clicks = serializers.IntegerField(help_text="기간 내 클릭수")
    click_rate = serializers.FloatField(help_text="클릭율 = 클릭수 / 페이지 조회수 × 100 (%)")


class BlockStatsSerializer(serializers.Serializer):
    period = serializers.CharField()
    blocks = BlockStatSerializer(many=True)


class LinkStatSerializer(serializers.Serializer):
    """서브링크 단위 클릭 통계 항목."""

    block_id = serializers.IntegerField(help_text="블록 ID")
    link_id = serializers.CharField(help_text="서브링크 ID (social: 플랫폼 키, group_link: 개별 링크 ID, 빈 문자열이면 블록 단위)")
    type = serializers.CharField(help_text="블록 타입 (social / group_link / single_link 등)")
    label = serializers.CharField(help_text="서브링크 표시명 (link_id가 있으면 해당 서브링크명, 없으면 블록 레이블)")
    is_enabled = serializers.BooleanField(help_text="현재 노출 여부")
    clicks = serializers.IntegerField(help_text="기간 내 클릭수")
    click_rate = serializers.FloatField(help_text="클릭율 = 클릭수 / 페이지 조회수 x 100 (%)")


class LinkClicksStatsSerializer(serializers.Serializer):
    """GET multipages/{id}/stats/links/ 응답. 서브링크별 클릭수."""

    period = serializers.CharField(help_text="조회 기간 (7d / 30d / 90d)")
    total_clicks = serializers.IntegerField(help_text="해당 기간 페이지 전체 클릭수")
    link_clicks = LinkStatSerializer(many=True, help_text="서브링크별 클릭수 배열")


class RecordViewSerializer(serializers.Serializer):
    """POST /api/pages/@{slug}/view/ 요청 바디 (모두 선택)."""
    referer = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="방문자 브라우저의 document.referrer 값. 없으면 빈 문자열.",
    )


class RecordClickSerializer(serializers.Serializer):
    """POST /api/pages/@{slug}/blocks/{block_id}/click/ 요청 바디 (모두 선택)."""
    referer = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="방문자 브라우저의 document.referrer 값.",
    )
    link_id = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="서브링크 식별자. social 블록: 플랫폼 키(instagram 등), group_link: 개별 링크 ID. 없으면 빈 문자열.",
    )


# ─── 문의 시리얼라이저 ────────────────────────────────────────

class ContactInquirySubmitSerializer(serializers.ModelSerializer):
    """
    POST /api/pages/@{slug}/inquiries/ — 방문자가 문의를 보낼 때 사용.
    page 필드는 뷰에서 slug로 자동 주입하므로 요청 바디에서는 제외.
    """

    class Meta:
        model = ContactInquiry
        fields = ["name", "category", "email", "phone", "subject", "content", "agreed_to_terms"]

    def validate_phone(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("휴대폰번호는 필수입니다.")
        return value.strip()

    def validate_agreed_to_terms(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError("이용약관 및 개인정보 처리방침에 동의해야 문의를 보낼 수 있습니다.")
        return value


class ContactInquirySerializer(serializers.ModelSerializer):
    """GET /api/pages/me/inquiries/ — 페이지 관리자용 목록/상세 조회."""

    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = ContactInquiry
        fields = [
            "id", "name", "category", "category_display",
            "email", "phone", "subject", "content",
            "agreed_to_terms", "memo", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "name", "category", "category_display",
            "email", "phone", "subject", "content",
            "agreed_to_terms", "created_at", "updated_at",
        ]


class ContactInquiryMemoSerializer(serializers.ModelSerializer):
    """PATCH /api/pages/me/inquiries/{id}/memo/ — 관리자 메모 수정 전용."""

    class Meta:
        model = ContactInquiry
        fields = ["id", "memo", "updated_at"]
        read_only_fields = ["id", "updated_at"]


# ─── 구독 시리얼라이저 ────────────────────────────────────────

class PageSubscriptionSubmitSerializer(serializers.ModelSerializer):
    """
    POST /api/pages/@{slug}/subscriptions/ — 방문자가 구독 등록할 때 사용.
    page 필드는 뷰에서 slug로 자동 주입하므로 요청 바디에서는 제외.
    """

    class Meta:
        model = PageSubscription
        fields = ["name", "category", "email", "phone", "agreed_to_terms"]

    def validate_agreed_to_terms(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError("개인정보 수집 및 이용에 동의해야 구독할 수 있습니다.")
        return value

    def validate_email(self, value: str) -> str:
        if not value or not value.strip():
            raise serializers.ValidationError("이메일은 필수입니다.")
        return value.strip().lower()


class PageSubscriptionSerializer(serializers.ModelSerializer):
    """GET /api/pages/me/subscriptions/ — 페이지 관리자용 목록 조회."""

    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = PageSubscription
        fields = [
            "id", "name", "category", "category_display",
            "email", "phone", "agreed_to_terms", "memo", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "name", "category", "category_display",
            "email", "phone", "agreed_to_terms", "created_at", "updated_at",
        ]


class PageSubscriptionMemoSerializer(serializers.ModelSerializer):
    """PATCH /api/pages/me/subscriptions/{id}/ — 관리자 메모 수정 전용."""

    class Meta:
        model = PageSubscription
        fields = ["id", "memo", "updated_at"]
        read_only_fields = ["id", "updated_at"]


# ─── 미디어 시리얼라이저 ──────────────────────────────────────

class PageMediaSerializer(serializers.ModelSerializer):
    """GET·POST /api/pages/me/media/ — 업로드된 미디어 파일 정보."""

    url = serializers.SerializerMethodField(
        help_text="파일 접근 URL. block.data 의 thumbnail_url / avatar_url 등에 그대로 사용."
    )
    size_display = serializers.SerializerMethodField(
        help_text="사람이 읽기 좋은 파일 크기 (예: 1.2 MB)"
    )

    class Meta:
        model = PageMedia
        fields = ["id", "original_name", "mime_type", "size", "size_display", "url", "created_at"]
        read_only_fields = ["id", "original_name", "mime_type", "size", "size_display", "url", "created_at"]

    def get_url(self, obj) -> str:
        request = self.context.get("request")
        if not obj.file:
            return ""
        if request:
            return request.build_absolute_uri(obj.file.url)
        return obj.file.url

    def get_size_display(self, obj) -> str:
        size = obj.size
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"
