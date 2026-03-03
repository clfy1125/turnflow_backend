from drf_spectacular.utils import OpenApiExample, extend_schema_field, extend_schema_serializer
from rest_framework import serializers
from django.db.models import Q
from django.utils import timezone

from .models import Block, Page
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
        fields = ["id", "slug", "title", "is_public", "created_at", "updated_at"]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]


class PagePublicSerializer(serializers.ModelSerializer):
    """공개 페이지 조회 - is_enabled=True 블록 포함."""

    blocks = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = ["slug", "title", "is_public", "blocks"]

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
