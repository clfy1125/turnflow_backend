"""
Billing models: Plans and Usage tracking
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from datetime import datetime
import uuid


class PlanChoices(models.TextChoices):
    """
    Subscription plan tiers
    """

    STARTER = "starter", "Starter"
    PRO = "pro", "Pro"
    ENTERPRISE = "enterprise", "Enterprise"


class PlanLimits:
    """
    Plan limits configuration (code constants)
    """

    LIMITS = {
        PlanChoices.STARTER: {
            "comments_collected_per_month": 1000,
            "dm_sent_per_month": 100,
            "workspaces": 1,
            "team_members": 3,
            "automations": 5,
        },
        PlanChoices.PRO: {
            "comments_collected_per_month": 10000,
            "dm_sent_per_month": 1000,
            "workspaces": 5,
            "team_members": 10,
            "automations": 50,
        },
        PlanChoices.ENTERPRISE: {
            "comments_collected_per_month": -1,  # Unlimited
            "dm_sent_per_month": -1,  # Unlimited
            "workspaces": -1,  # Unlimited
            "team_members": -1,  # Unlimited
            "automations": -1,  # Unlimited
        },
    }

    @classmethod
    def get_limit(cls, plan: str, metric: str) -> int:
        """
        Get limit for a specific plan and metric
        Returns -1 for unlimited
        """
        return cls.LIMITS.get(plan, cls.LIMITS[PlanChoices.STARTER]).get(metric, 0)

    @classmethod
    def is_unlimited(cls, plan: str, metric: str) -> bool:
        """Check if a metric is unlimited for the plan"""
        return cls.get_limit(plan, metric) == -1

    @classmethod
    def get_all_limits(cls, plan: str) -> dict:
        """Get all limits for a plan"""
        return cls.LIMITS.get(plan, cls.LIMITS[PlanChoices.STARTER])


class UsageCounter(models.Model):
    """
    Monthly usage tracking per workspace
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspace.Workspace", on_delete=models.CASCADE, related_name="usage_counters"
    )

    # Period (monthly)
    year = models.IntegerField()
    month = models.IntegerField()  # 1-12

    # Usage metrics
    comments_collected = models.IntegerField(default=0)
    dm_sent = models.IntegerField(default=0)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "usage_counters"
        unique_together = [["workspace", "year", "month"]]
        indexes = [
            models.Index(fields=["workspace", "year", "month"]),
            models.Index(fields=["year", "month"]),
        ]
        ordering = ["-year", "-month"]

    def __str__(self):
        return f"{self.workspace.name} - {self.year}/{self.month:02d}"

    @classmethod
    def get_current_period(cls, workspace):
        """
        Get or create usage counter for current month
        """
        now = timezone.now()
        year = now.year
        month = now.month

        counter, created = cls.objects.get_or_create(
            workspace=workspace,
            year=year,
            month=month,
            defaults={"comments_collected": 0, "dm_sent": 0},
        )
        return counter

    def increment(self, metric: str, amount: int = 1):
        """
        Increment a usage metric
        """
        if metric not in ["comments_collected", "dm_sent"]:
            raise ValueError(f"Invalid metric: {metric}")

        current_value = getattr(self, metric)
        setattr(self, metric, current_value + amount)
        self.save(update_fields=[metric, "updated_at"])

    def check_limit(self, metric: str, amount: int = 1) -> bool:
        """
        Check if incrementing would exceed plan limit
        Returns True if within limit, False if would exceed
        """
        plan = self.workspace.plan
        limit = PlanLimits.get_limit(plan, f"{metric}_per_month")

        # Unlimited
        if limit == -1:
            return True

        current_value = getattr(self, metric)
        return (current_value + amount) <= limit

    def get_remaining(self, metric: str) -> int:
        """
        Get remaining quota for a metric
        Returns -1 for unlimited
        """
        plan = self.workspace.plan
        limit = PlanLimits.get_limit(plan, f"{metric}_per_month")

        if limit == -1:
            return -1

        current_value = getattr(self, metric)
        remaining = limit - current_value
        return max(0, remaining)


# ──────────────────────────────────────────────
# 개인 구독 시스템 (Personal Subscription)
# ──────────────────────────────────────────────


class SubscriptionPlan(models.Model):
    """
    DB-driven subscription plan configuration.
    features JSONField로 확장 가능한 기능 제한 관리.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=30, unique=True)  # free / pro / pro_plus
    display_name = models.CharField(max_length=50)  # 무료 / 프로 / 프로 플러스
    monthly_price = models.IntegerField(default=0, help_text="월 요금 (원)")
    yearly_price = models.IntegerField(default=0, help_text="연 요금 (원)")
    features = models.JSONField(
        default=dict,
        help_text="기능 제한 설정. 예: {max_pages: 3, ai_generation: false, ...}",
    )
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "subscription_plans"
        ordering = ["sort_order"]

    def __str__(self):
        return self.display_name


class SubscriptionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    CANCELLED = "cancelled", "Cancelled"
    PAST_DUE = "past_due", "Past Due"
    TRIALING = "trialing", "Trialing"


class BillingCycle(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    YEARLY = "yearly", "Yearly"


class UserSubscription(models.Model):
    """
    User 1:1 구독 정보.
    토스페이먼츠 필드는 nullable — 승인 후 연동만 추가하면 됨.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
    )
    billing_cycle = models.CharField(
        max_length=10,
        choices=BillingCycle.choices,
        default=BillingCycle.MONTHLY,
    )
    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)

    # 토스페이먼츠 (nullable — 승인 대기)
    toss_customer_key = models.CharField(max_length=200, null=True, blank=True)
    toss_billing_key = models.CharField(max_length=200, null=True, blank=True)
    toss_subscription_id = models.CharField(max_length=200, null=True, blank=True)

    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_subscriptions"

    def __str__(self):
        return f"{self.user.email} - {self.plan.display_name} ({self.status})"

    @property
    def is_active(self):
        return self.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)

    @property
    def is_paid_plan(self):
        return self.plan.name != "free"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"


class PaymentHistory(models.Model):
    """
    결제 내역. 토스페이먼츠 필드는 nullable — 승인 후 채움.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="payments",
    )
    subscription = models.ForeignKey(
        UserSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )
    amount = models.IntegerField(help_text="결제 금액 (원)")
    status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
    )
    payment_method = models.CharField(max_length=50, null=True, blank=True)
    description = models.CharField(max_length=200, default="")

    # 토스페이먼츠 (nullable — 승인 대기)
    toss_payment_key = models.CharField(max_length=200, null=True, blank=True)
    toss_order_id = models.CharField(max_length=200, null=True, blank=True)

    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payment_history"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.email} - {self.amount}원 ({self.status})"


# ──────────────────────────────────────────────
# AI 토큰 잔액 (AI Token Balance)
# ──────────────────────────────────────────────


class AiTokenBalance(models.Model):
    """
    사용자별 AI 토큰 잔액.
    정기구독 갱신 시 월 토큰이 리셋되고, AI 작업 성공 시 차감된다.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_token_balance",
    )
    balance = models.IntegerField(
        default=0,
        verbose_name="토큰 잔액",
        help_text="현재 사용 가능한 AI 토큰 수",
    )
    total_used = models.IntegerField(
        default=0,
        verbose_name="총 사용량",
        help_text="서비스 가입 이후 총 사용한 토큰 수",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_token_balances"
        verbose_name = "AI 토큰 잔액"
        verbose_name_plural = "AI 토큰 잔액 목록"

    def __str__(self):
        return f"{self.user.email} - 잔액: {self.balance}"

    def has_enough(self, cost: int) -> bool:
        """토큰이 충분한지 확인."""
        return self.balance >= cost

    def deduct(self, cost: int, description: str = "") -> "AiTokenLedger":
        """
        토큰 차감.  balance가 부족하면 ValueError.
        원자적으로 처리하기 위해 select_for_update()와 함께 사용 권장.
        """
        if self.balance < cost:
            raise ValueError(f"토큰 부족: 잔액 {self.balance}, 필요 {cost}")
        self.balance -= cost
        self.total_used += cost
        self.save(update_fields=["balance", "total_used", "updated_at"])
        return AiTokenLedger.objects.create(
            user=self.user,
            amount=-cost,
            balance_after=self.balance,
            description=description,
        )

    def grant(self, amount: int, description: str = "") -> "AiTokenLedger":
        """토큰 지급 (구독 갱신, 수동 지급 등)."""
        self.balance += amount
        self.save(update_fields=["balance", "updated_at"])
        return AiTokenLedger.objects.create(
            user=self.user,
            amount=amount,
            balance_after=self.balance,
            description=description,
        )

    def reset_to(self, amount: int, description: str = "") -> "AiTokenLedger":
        """구독 갱신 시 월 토큰으로 리셋."""
        old_balance = self.balance
        self.balance = amount
        self.save(update_fields=["balance", "updated_at"])
        return AiTokenLedger.objects.create(
            user=self.user,
            amount=amount - old_balance,
            balance_after=self.balance,
            description=description,
        )

    @classmethod
    def get_or_create_for_user(cls, user):
        """유저의 토큰 잔액 가져오기. 없으면 생성 (초기 30 토큰 지급)."""
        obj, created = cls.objects.get_or_create(
            user=user,
            defaults={"balance": 30},
        )
        if created:
            AiTokenLedger.objects.create(
                user=user,
                amount=30,
                balance_after=30,
                description="신규 가입 토큰 지급",
            )
        return obj


class AiTokenLedger(models.Model):
    """
    AI 토큰 사용/충전 내역.
    amount > 0: 충전 (구독 갱신, 수동 지급 등)
    amount < 0: 차감 (AI 작업 성공)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_token_ledger",
    )
    amount = models.IntegerField(
        verbose_name="변동량",
        help_text="양수=충전, 음수=차감",
    )
    balance_after = models.IntegerField(
        verbose_name="변동 후 잔액",
    )
    description = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="설명",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ai_token_ledger"
        ordering = ["-created_at"]
        verbose_name = "AI 토큰 내역"
        verbose_name_plural = "AI 토큰 내역 목록"

    def __str__(self):
        sign = "+" if self.amount > 0 else ""
        return f"{self.user.email} {sign}{self.amount} → {self.balance_after}"
