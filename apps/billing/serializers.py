"""
Billing serializers
"""

from rest_framework import serializers
from .models import UsageCounter, PlanLimits, PlanChoices


class PlanLimitSerializer(serializers.Serializer):
    """Serializer for plan limits"""

    comments_collected_per_month = serializers.IntegerField()
    dm_sent_per_month = serializers.IntegerField()
    workspaces = serializers.IntegerField()
    team_members = serializers.IntegerField()
    automations = serializers.IntegerField()


class CurrentPlanSerializer(serializers.Serializer):
    """Serializer for current plan information"""

    plan = serializers.ChoiceField(choices=PlanChoices.choices)
    plan_display = serializers.CharField()
    limits = PlanLimitSerializer()

    def to_representation(self, instance):
        """
        instance is expected to be a workspace
        """
        plan = instance.plan
        limits = PlanLimits.get_all_limits(plan)

        return {
            "plan": plan,
            "plan_display": dict(PlanChoices.choices).get(plan, "Unknown"),
            "limits": limits,
        }


class UsageSerializer(serializers.Serializer):
    """Serializer for usage data"""

    period = serializers.DictField(child=serializers.IntegerField())
    plan = serializers.ChoiceField(choices=PlanChoices.choices)
    usage = serializers.DictField(child=serializers.IntegerField())
    limits = serializers.DictField(child=serializers.IntegerField())
    remaining = serializers.DictField(child=serializers.IntegerField())


class UsageCounterSerializer(serializers.ModelSerializer):
    """Serializer for UsageCounter model"""

    workspace_id = serializers.UUIDField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)

    class Meta:
        model = UsageCounter
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "year",
            "month",
            "comments_collected",
            "dm_sent",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


# ──────────────────────────────────────────────
# 개인 구독 Serializers
# ──────────────────────────────────────────────


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    """구독 플랜 목록/상세용"""

    class Meta:
        model = None  # set below
        fields = [
            "id",
            "name",
            "display_name",
            "monthly_price",
            "features",
            "sort_order",
        ]


class UserSubscriptionSerializer(serializers.ModelSerializer):
    """내 구독 조회용"""

    plan = SubscriptionPlanSerializer(read_only=True)
    plan_id = serializers.UUIDField(source="plan.id", read_only=True)

    class Meta:
        model = None  # set below
        fields = [
            "id",
            "plan",
            "plan_id",
            "status",
            "current_period_start",
            "current_period_end",
            "cancelled_at",
            "created_at",
            "updated_at",
        ]


class ChangeSubscriptionRequestSerializer(serializers.Serializer):
    """플랜 변경 요청용"""

    plan_id = serializers.UUIDField()
    phone_number = serializers.CharField(
        max_length=20,
        required=False,
        default="",
        help_text="구매자 휴대전화번호 (예: 01012345678). PayApp 정기결제 등록 시 필요.",
    )


class PaymentHistorySerializer(serializers.ModelSerializer):
    """결제 내역 조회용"""

    class Meta:
        model = None  # set below
        fields = [
            "id",
            "amount",
            "status",
            "payment_method",
            "description",
            "payapp_mul_no",
            "receipt_url",
            "pay_type_display",
            "paid_at",
            "created_at",
        ]


# ──────────────────────────────────────────────
# 레퍼럴 코드 Serializers
# ──────────────────────────────────────────────


class ReferralCodeRedeemRequestSerializer(serializers.Serializer):
    """레퍼럴 코드 사용 요청"""

    code = serializers.CharField(
        max_length=50,
        help_text="레퍼럴 코드 (대소문자 무시)",
    )


class ReferralCodeValidateResponseSerializer(serializers.Serializer):
    """레퍼럴 코드 사전 검증 응답"""

    valid = serializers.BooleanField(help_text="사용 가능 여부")
    reason = serializers.CharField(
        required=False, allow_blank=True, help_text="사용 불가 사유 (valid=false일 때)"
    )
    trial_days = serializers.IntegerField(
        required=False, help_text="트라이얼 부여 일수 (valid=true일 때)"
    )
    plan = SubscriptionPlanSerializer(
        required=False, help_text="트라이얼로 부여될 플랜 (valid=true일 때)"
    )


class ReferralRedemptionSerializer(serializers.ModelSerializer):
    """레퍼럴 사용 이력 조회용"""

    referral_code_value = serializers.CharField(source="referral_code.code", read_only=True)
    plan = SubscriptionPlanSerializer(source="referral_code.target_plan", read_only=True)
    is_trial_active = serializers.SerializerMethodField()

    class Meta:
        model = None  # set below
        fields = [
            "id",
            "referral_code_value",
            "plan",
            "trial_started_at",
            "trial_ends_at",
            "is_trial_active",
            "converted_to_paid",
            "converted_at",
            "created_at",
        ]

    def get_is_trial_active(self, obj) -> bool:
        from django.utils import timezone

        return obj.trial_ends_at > timezone.now() and not obj.converted_to_paid


# Avoid circular import: set model references after class definition
def _patch_serializer_models():
    from .models import (
        PaymentHistory,
        ReferralRedemption,
        SubscriptionPlan,
        UserSubscription,
    )

    SubscriptionPlanSerializer.Meta.model = SubscriptionPlan
    UserSubscriptionSerializer.Meta.model = UserSubscription
    PaymentHistorySerializer.Meta.model = PaymentHistory
    ReferralRedemptionSerializer.Meta.model = ReferralRedemption


_patch_serializer_models()
