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


class UserSubscription(models.Model):
    """
    User 1:1 구독 정보.
    PayApp 정기결제(rebill) 연동. 월간 구독만 지원.
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
    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)

    # PayApp 정기결제
    payapp_rebill_no = models.CharField(
        max_length=100, null=True, blank=True,
        verbose_name="PayApp 정기결제 등록번호",
        help_text="rebillRegist 응답의 rebill_no",
    )
    payapp_rebill_expire = models.DateField(
        null=True, blank=True,
        verbose_name="PayApp 정기결제 만료일",
        help_text="rebillExpire로 설정한 만료일",
    )
    payapp_pay_url = models.URLField(
        max_length=500, null=True, blank=True,
        verbose_name="PayApp 결제 URL",
        help_text="최초 결제 시 프론트가 리다이렉트할 URL",
    )

    cancelled_at = models.DateTimeField(null=True, blank=True)
    page_activation_changed_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name="페이지 활성화 변경 일시",
        help_text="하루 1회 제한용 — 마지막으로 페이지 활성화 조정한 시각",
    )
    pro_activated_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name="유료 플랜 활성화 일시",
        help_text="환불 7일 심사용 — 유료 플랜 첫 결제 완료 시각",
    )
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

    # PayApp
    payapp_mul_no = models.CharField(
        max_length=100, null=True, blank=True, unique=True,
        verbose_name="PayApp 결제요청번호",
        help_text="PayApp mul_no — 멱등 키",
    )
    payapp_rebill_no = models.CharField(
        max_length=100, null=True, blank=True,
        verbose_name="PayApp 정기결제 등록번호",
    )
    receipt_url = models.URLField(
        max_length=500, null=True, blank=True,
        verbose_name="매출전표 URL",
        help_text="PayApp csturl — 카드 결제 시 영수증 URL",
    )
    pay_type_display = models.CharField(
        max_length=50, null=True, blank=True,
        verbose_name="결제수단 표시명",
        help_text="예: 신용카드, 휴대전화, 카카오페이 등",
    )

    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payment_history"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.email} - {self.amount}원 ({self.status})"


# ──────────────────────────────────────────────
# PayApp 웹훅 로그 (멱등 보장용)
# ──────────────────────────────────────────────


class PayAppWebhookLog(models.Model):
    """
    PayApp feedbackurl / failurl 수신 로그.
    동일 (mul_no, pay_state) 조합에 대해 unique 제약으로 중복 처리 방지.
    """

    WEBHOOK_TYPES = [
        ("feedback", "Feedback (결제통보)"),
        ("fail", "Fail (정기결제 실패)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook_type = models.CharField(max_length=20, choices=WEBHOOK_TYPES)
    mul_no = models.CharField(
        max_length=100, null=True, blank=True, db_index=True,
        verbose_name="결제요청번호",
    )
    rebill_no = models.CharField(
        max_length=100, null=True, blank=True, db_index=True,
        verbose_name="정기결제 등록번호",
    )
    pay_state = models.CharField(
        max_length=10,
        verbose_name="결제요청 상태",
    )
    raw_data = models.JSONField(
        default=dict,
        verbose_name="수신 데이터 원본",
    )
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payapp_webhook_logs"
        unique_together = [["mul_no", "pay_state"]]
        ordering = ["-created_at"]
        verbose_name = "PayApp 웹훅 로그"
        verbose_name_plural = "PayApp 웹훅 로그 목록"

    def __str__(self):
        return f"[{self.webhook_type}] mul_no={self.mul_no} pay_state={self.pay_state}"


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
        """유저의 토큰 잔액 가져오기. 없으면 생성 (초기 2 토큰 지급 - free 플랜)."""
        obj, created = cls.objects.get_or_create(
            user=user,
            defaults={"balance": 2},
        )
        if created:
            AiTokenLedger.objects.create(
                user=user,
                amount=2,
                balance_after=2,
                description="신규 가입 토큰 지급 (free 플랜)",
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


# ──────────────────────────────────────────────
# 레퍼럴 코드 (Referral Code)
# ──────────────────────────────────────────────


class ReferralCode(models.Model):
    """
    레퍼럴 코드.
    사용자가 코드를 입력하면 결제 없이 일정 기간 동안 target_plan 트라이얼을 받는다.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(
        max_length=50,
        unique=True,
        verbose_name="코드",
        help_text="입력 시 대소문자 무시. 저장은 대문자로 정규화.",
    )
    description = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="설명",
    )
    target_plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="referral_codes",
        verbose_name="트라이얼 대상 플랜",
        help_text="레퍼럴 사용 시 일시 부여할 플랜 (보통 pro)",
    )
    trial_days = models.PositiveIntegerField(
        default=30,
        verbose_name="트라이얼 기간(일)",
    )
    is_active = models.BooleanField(default=True, verbose_name="활성")
    max_uses = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="최대 사용 횟수",
        help_text="null이면 무제한",
    )
    current_uses = models.PositiveIntegerField(
        default=0,
        verbose_name="현재 사용 횟수",
    )
    valid_from = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="사용 시작 시각",
    )
    valid_until = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="사용 종료 시각",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "referral_codes"
        ordering = ["-created_at"]
        verbose_name = "레퍼럴 코드"
        verbose_name_plural = "레퍼럴 코드 목록"

    def __str__(self):
        return f"{self.code} ({self.trial_days}일 → {self.target_plan.display_name})"

    def save(self, *args, **kwargs):
        if self.code:
            self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    def is_redeemable(self) -> tuple[bool, str]:
        """현재 시점에서 사용 가능한지 + 사유 메시지."""
        if not self.is_active:
            return False, "비활성화된 코드입니다."
        now = timezone.now()
        if self.valid_from and now < self.valid_from:
            return False, "아직 사용할 수 없는 코드입니다."
        if self.valid_until and now > self.valid_until:
            return False, "유효 기간이 만료된 코드입니다."
        if self.max_uses is not None and self.current_uses >= self.max_uses:
            return False, "사용 횟수가 모두 소진된 코드입니다."
        return True, ""


class ReferralRedemption(models.Model):
    """
    레퍼럴 사용 이력. 1유저당 1회만 사용 가능 (OneToOne).
    트라이얼이 끝난 뒤 유료 전환 여부도 함께 추적한다.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="referral_redemption",
        verbose_name="사용자",
    )
    referral_code = models.ForeignKey(
        ReferralCode,
        on_delete=models.PROTECT,
        related_name="redemptions",
        verbose_name="레퍼럴 코드",
    )
    trial_started_at = models.DateTimeField(verbose_name="트라이얼 시작")
    trial_ends_at = models.DateTimeField(verbose_name="트라이얼 종료")
    converted_to_paid = models.BooleanField(
        default=False,
        verbose_name="유료 전환 여부",
        help_text="트라이얼 후 정기결제 완료 시 True",
    )
    converted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="유료 전환 시각",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "referral_redemptions"
        ordering = ["-created_at"]
        verbose_name = "레퍼럴 사용 이력"
        verbose_name_plural = "레퍼럴 사용 이력 목록"

    def __str__(self):
        return f"{self.user.email} ← {self.referral_code.code}"
