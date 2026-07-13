"""apps/admin_api/serializers/referral.py — 어드민 레퍼럴 코드 관리 시리얼라이저.

``/api/v1/admin/referral-codes/`` 에서 ``IsAdminUser`` 권한으로만 접근한다.
사용자용 레퍼럴 뷰(``apps/billing/referral_views.py`` — validate/redeem/my-status)와 달리
여기서는 **코드 자체의 CRUD + 사용 이력 조회**(백오피스 운영)를 담당한다.

- 읽기:  :class:`AdminReferralCodeSerializer` (target_plan 중첩 + 집계/사용가능 여부)
- 쓰기:  :class:`AdminReferralCodeWriteSerializer` (생성/수정 — 코드 정규화·검증)
- 이력:  :class:`AdminReferralRedemptionSerializer` (코드별 사용 내역)
"""

from __future__ import annotations

import re

from django.utils import timezone
from rest_framework import serializers

from apps.billing.models import ReferralCode, ReferralRedemption, SubscriptionPlan

from .billing import AdminSubscriptionPlanSerializer

# 코드 허용 문자: 대문자 영문 / 숫자 / 하이픈 / 언더스코어 (정규화 후 검사).
_CODE_RE = re.compile(r"^[A-Z0-9_-]{2,50}$")


class AdminReferralCodeSerializer(serializers.ModelSerializer):
    """레퍼럴 코드 1건 (읽기 전용). 목록/상세/변경 응답 공용.

    ``redemptions_count`` / ``converted_count`` 는 뷰에서 annotate 로 주입되면 그 값을,
    (생성/수정 직후처럼) 없으면 관계에서 직접 집계한다 — 어느 경로로 와도 정확한 수를 보장.
    """

    target_plan = AdminSubscriptionPlanSerializer(read_only=True)
    redemptions_count = serializers.SerializerMethodField(help_text="이 코드로 시작된 총 트라이얼 수")
    converted_count = serializers.SerializerMethodField(
        help_text="트라이얼 후 유료 결제로 전환된 수"
    )
    is_redeemable = serializers.SerializerMethodField(
        help_text="현재 시점에 사용 가능한지 (활성·기간·소진 종합 판정)"
    )
    redeemable_reason = serializers.SerializerMethodField(
        help_text="사용 불가 사유 (is_redeemable=false 일 때만 채워짐)"
    )

    class Meta:
        model = ReferralCode
        fields = [
            "id",
            "code",
            "description",
            "target_plan",
            "trial_days",
            "is_active",
            "max_uses",
            "current_uses",
            "valid_from",
            "valid_until",
            "redemptions_count",
            "converted_count",
            "is_redeemable",
            "redeemable_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_redemptions_count(self, obj) -> int:
        annotated = getattr(obj, "redemptions_count", None)
        return annotated if annotated is not None else obj.redemptions.count()

    def get_converted_count(self, obj) -> int:
        annotated = getattr(obj, "converted_count", None)
        return (
            annotated
            if annotated is not None
            else obj.redemptions.filter(converted_to_paid=True).count()
        )

    def get_is_redeemable(self, obj) -> bool:
        ok, _ = obj.is_redeemable()
        return ok

    def get_redeemable_reason(self, obj) -> str:
        _, reason = obj.is_redeemable()
        return reason


class AdminReferralCodeWriteSerializer(serializers.ModelSerializer):
    """레퍼럴 코드 생성/수정 요청.

    - ``code`` 는 대문자·공백제거로 정규화하여 저장(모델 ``save`` 와 동일 규칙)하고,
      정규화된 값 기준으로 대소문자 무시 중복을 사전 검증한다.
    - ``target_plan`` 은 SubscriptionPlan UUID(PK). 존재하지 않으면 400.
    - 생성 시 미지정 필드는 모델 기본값(trial_days=30, is_active=True 등)을 따른다.
    - 수정(PATCH)은 부분 갱신 — 보낸 필드만 반영된다.
    """

    code = serializers.CharField(
        max_length=50,
        help_text="레퍼럴 코드. 대소문자 무시(대문자로 저장), 영문·숫자·하이픈·언더스코어만 허용",
    )
    target_plan = serializers.PrimaryKeyRelatedField(
        # 트라이얼 대상은 **활성 유료 플랜만** — 운영용 비활성 플랜(admin, is_active=False)이나
        # 기본 무료 플랜(free)으로 트라이얼을 부여하면 의도치 않은 권한/무의미한 부여가 된다.
        # 코드 전반의 관례(subscription_views 등 is_active=True)와 일치. 미존재 시 400.
        queryset=SubscriptionPlan.objects.filter(is_active=True).exclude(name="free"),
        help_text="트라이얼로 부여할 플랜(SubscriptionPlan) UUID. 활성 유료 플랜만(보통 pro)",
    )
    trial_days = serializers.IntegerField(
        min_value=1,
        max_value=3650,
        required=False,
        default=30,
        help_text="트라이얼 기간(일). 1~3650, 기본 30",
    )
    description = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        default="",
        help_text="내부 메모/설명 (선택)",
    )
    is_active = serializers.BooleanField(
        required=False,
        default=True,
        help_text="활성 여부. false 면 사용 불가(soft-off)",
    )
    max_uses = serializers.IntegerField(
        min_value=1,
        required=False,
        allow_null=True,
        help_text="최대 사용 횟수. null(미지정)이면 무제한",
    )
    valid_from = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="사용 시작 시각(ISO8601). null 이면 즉시",
    )
    valid_until = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="사용 종료 시각(ISO8601). null 이면 무기한",
    )

    class Meta:
        model = ReferralCode
        fields = [
            "code",
            "description",
            "target_plan",
            "trial_days",
            "is_active",
            "max_uses",
            "valid_from",
            "valid_until",
        ]

    def validate_code(self, value: str) -> str:
        normalized = (value or "").strip().upper()
        if not _CODE_RE.match(normalized):
            raise serializers.ValidationError(
                "코드는 영문 대문자·숫자·하이픈(-)·언더스코어(_) 2~50자만 사용할 수 있습니다."
            )
        qs = ReferralCode.objects.filter(code=normalized)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("이미 존재하는 코드입니다.")
        return normalized

    def validate(self, attrs):
        # valid_from / valid_until 순서 (한쪽만 새로 와도 기존값과 비교)
        vf = attrs.get("valid_from", getattr(self.instance, "valid_from", None))
        vu = attrs.get("valid_until", getattr(self.instance, "valid_until", None))
        if vf and vu and vf > vu:
            raise serializers.ValidationError(
                {"valid_until": "종료 시각은 시작 시각보다 뒤여야 합니다."}
            )
        # max_uses 는 이미 사용된 횟수 미만으로 낮출 수 없음 (수정 시)
        if self.instance is not None and attrs.get("max_uses") is not None:
            if attrs["max_uses"] < self.instance.current_uses:
                raise serializers.ValidationError(
                    {
                        "max_uses": (
                            f"이미 {self.instance.current_uses}회 사용된 코드입니다 — "
                            "최대 사용 횟수는 그 이상이어야 합니다."
                        )
                    }
                )
        return attrs


class AdminReferralRedemptionSerializer(serializers.ModelSerializer):
    """코드별 사용 이력 1건 (읽기 전용). 누가·언제 트라이얼을 시작했고 유료 전환됐는지."""

    user_id = serializers.IntegerField(source="user.id", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True, default="")
    is_trial_active = serializers.SerializerMethodField()

    class Meta:
        model = ReferralRedemption
        fields = [
            "id",
            "user_id",
            "user_email",
            "user_full_name",
            "trial_started_at",
            "trial_ends_at",
            "converted_to_paid",
            "converted_at",
            "is_trial_active",
            "created_at",
        ]
        read_only_fields = fields

    def get_is_trial_active(self, obj) -> bool:
        return obj.trial_ends_at > timezone.now() and not obj.converted_to_paid
