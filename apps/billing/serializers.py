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


# Avoid circular import: set model references after class definition
def _patch_serializer_models():
    from .models import SubscriptionPlan, UserSubscription, PaymentHistory

    SubscriptionPlanSerializer.Meta.model = SubscriptionPlan
    UserSubscriptionSerializer.Meta.model = UserSubscription
    PaymentHistorySerializer.Meta.model = PaymentHistory


_patch_serializer_models()
