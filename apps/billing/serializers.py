"""
Billing serializers
"""

from rest_framework import serializers

from .models import PlanChoices, PlanLimits, UsageCounter


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
            "list_price",
            "features",
            "sort_order",
        ]


class UserSubscriptionSerializer(serializers.ModelSerializer):
    """내 구독 조회용"""

    plan = SubscriptionPlanSerializer(read_only=True)
    plan_id = serializers.UUIDField(source="plan.id", read_only=True)
    pending_plan_name = serializers.CharField(
        source="pending_plan.name",
        read_only=True,
        default=None,
        help_text="예약된 플랜 변경 (다음 갱신 시 적용). null이면 예약 없음",
    )
    has_billing_key = serializers.BooleanField(
        read_only=True,
        help_text="결제 카드(빌링키) 등록 여부",
    )

    class Meta:
        model = None  # set below
        fields = [
            "id",
            "plan",
            "plan_id",
            "status",
            "current_period_start",
            "current_period_end",
            "has_billing_key",
            "card_company",
            "card_number_masked",
            "monthly_amount_snapshot",
            "extra_ig_accounts",
            "pending_plan_name",
            "trial_used_at",
            "cancelled_at",
            "created_at",
            "updated_at",
        ]


class ChangeSubscriptionRequestSerializer(serializers.Serializer):
    """플랜 변경 요청용 (빌링키 보유 유료 사용자 전용)"""

    plan_name = serializers.ChoiceField(
        choices=["basic", "pro"],
        help_text="변경할 플랜 코드명. 업그레이드=즉시 과금, 다운그레이드=다음 갱신 시 적용",
    )
    extra_ig_accounts = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=10,
        default=0,
        help_text="pro 업그레이드 시 함께 설정할 추가 IG 계정 수 (계정당 +9,900원/월)",
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
            "toss_order_id",
            "receipt_url",
            "card_company",
            "card_number_masked",
            "failure_code",
            "failure_message",
            "paid_at",
            "created_at",
        ]


# ──────────────────────────────────────────────
# 토스 빌링 Serializers
# ──────────────────────────────────────────────


class TossConfirmRequestSerializer(serializers.Serializer):
    """빌링키 등록 확정 요청 (SDK requestBillingAuth 성공 후)"""

    auth_key = serializers.CharField(
        max_length=300,
        help_text="successUrl 쿼리로 받은 authKey (일회성)",
    )
    plan_name = serializers.ChoiceField(
        choices=["basic", "pro"],
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "구독 시작할 플랜. 생략 시 카드 변경으로 동작 (유료 구독자 전용). "
            "pro 최초 구독은 30일 무료 체험으로 시작"
        ),
    )
    referral_code = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        default="",
        help_text="제휴 코드 — pro 최초 구독(무료 체험 시작) 시에만 유효. 체험 +30일",
    )
    extra_ig_accounts = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=10,
        default=0,
        help_text="pro 전용 추가 IG 계정 수 (계정당 +9,900원/월)",
    )


class TossDevIssueRequestSerializer(serializers.Serializer):
    """dev 전용 — 카드번호 직접 입력 빌링키 발급 (TOSS_DEV_CARD_AUTH_ENABLED)"""

    card_number = serializers.CharField(
        max_length=20,
        help_text="카드 번호. 테스트 키에서는 앞 6자리(BIN)만 유효하면 등록됨",
    )
    card_expiration_year = serializers.CharField(max_length=2, help_text="유효기간 연 (YY)")
    card_expiration_month = serializers.CharField(max_length=2, help_text="유효기간 월 (MM)")
    customer_identity_number = serializers.CharField(
        max_length=10, help_text="생년월일 6자리(YYMMDD) 또는 사업자번호 10자리"
    )
    card_password = serializers.CharField(
        max_length=2,
        required=False,
        allow_blank=True,
        default="",
        help_text="카드 비밀번호 앞 2자리 (테스트에서는 생략 가능)",
    )
    plan_name = serializers.ChoiceField(
        choices=["basic", "pro"],
        required=False,
        allow_null=True,
        default=None,
    )
    referral_code = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        default="",
    )
    extra_ig_accounts = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=10,
        default=0,
    )


class ExtraAccountsRequestSerializer(serializers.Serializer):
    """추가 IG 계정 수 변경 요청 (pro 전용)"""

    count = serializers.IntegerField(
        min_value=0,
        max_value=10,
        help_text="변경할 추가 계정 총 수 (증가분은 즉시 결제, 감소는 무과금)",
    )


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
    from .models import PaymentHistory, ReferralRedemption, SubscriptionPlan, UserSubscription

    SubscriptionPlanSerializer.Meta.model = SubscriptionPlan
    UserSubscriptionSerializer.Meta.model = UserSubscription
    PaymentHistorySerializer.Meta.model = PaymentHistory
    ReferralRedemptionSerializer.Meta.model = ReferralRedemption


_patch_serializer_models()
