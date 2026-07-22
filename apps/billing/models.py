"""
Billing models: Plans and Usage tracking
"""

import hashlib
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.integrations.encryption import EncryptedTextField


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
            "videos_published_per_month": 10,
            "comments_moderated_per_month": 500,
            "workspaces": 1,
            "team_members": 3,
            "automations": 5,
        },
        PlanChoices.PRO: {
            "comments_collected_per_month": 10000,
            "dm_sent_per_month": 1000,
            "videos_published_per_month": 100,
            "comments_moderated_per_month": 10000,
            "workspaces": 5,
            "team_members": 10,
            "automations": 50,
        },
        PlanChoices.ENTERPRISE: {
            "comments_collected_per_month": -1,  # Unlimited
            "dm_sent_per_month": -1,  # Unlimited
            "videos_published_per_month": -1,  # Unlimited
            "comments_moderated_per_month": -1,  # Unlimited
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
    videos_published = models.IntegerField(default=0)
    comments_moderated = models.IntegerField(default=0)

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
            defaults={
                "comments_collected": 0,
                "dm_sent": 0,
                "videos_published": 0,
                "comments_moderated": 0,
            },
        )
        return counter

    def increment(self, metric: str, amount: int = 1):
        """
        Increment a usage metric
        """
        if metric not in [
            "comments_collected",
            "dm_sent",
            "videos_published",
            "comments_moderated",
        ]:
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
    name = models.CharField(max_length=30, unique=True)  # free / basic / pro / admin
    display_name = models.CharField(max_length=50)  # 무료 / 베이직 / 프로
    monthly_price = models.IntegerField(default=0, help_text="월 요금 (원) — 현재 판매가")
    list_price = models.IntegerField(
        default=0,
        help_text="정가 (원). 할인 표시용 — monthly_price보다 크면 할인 판매 중",
    )
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
    PAUSED = "paused", "Paused"  # 리텐션 일시정지 — 잔여 유료기간 후 무과금 정지, 만료 시 자동 재개


EXTRA_IG_ACCOUNT_PRICE = 9900  # 프로 추가 IG 계정 단가 (원/월)

# ── 리텐션(해지 방어) 정책 ──
PAUSE_ALLOWED_MONTHS = (1, 2, 3)  # 허용 정지 개월 수
PAUSE_MIN_INTERVAL_DAYS = 365  # 정지 재사용 최소 간격 (연 1회)
RETENTION_DISCOUNT_PERCENT = 50  # 리텐션 할인율 (다음 1회 갱신)


def apply_retention_discount(amount: int, *, pending: bool) -> int:
    """리텐션 할인 적용 (다음 1회 갱신에만). pending=False면 원금 그대로.

    갱신 청구액 계산(_renewal_amount_for)과 표시용 renewal_amount 프로퍼티가
    같은 규칙을 쓰도록 단일 소스로 둔다.
    """
    if not pending:
        return amount
    return amount * (100 - RETENTION_DISCOUNT_PERCENT) // 100


def hash_billing_key(billing_key: str) -> str:
    """빌링키 역조회용 SHA-256 (BILLING_DELETED 웹훅은 빌링키만 옴)."""
    return hashlib.sha256(billing_key.encode()).hexdigest()


def generate_customer_key() -> str:
    """토스 customerKey 생성 — 유추 불가 랜덤, 유저당 고정 저장."""
    return f"tf_{uuid.uuid4().hex}"


class UserSubscription(models.Model):
    """
    User 1:1 구독 정보.
    토스페이먼츠 빌링키 정기결제 연동. 월간 구독만 지원.
    갱신 과금은 PG가 아닌 우리 스케줄러(billing.process_due_renewals)가 주도한다.
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

    # ── 토스페이먼츠 빌링 ──
    toss_customer_key = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        verbose_name="토스 customerKey",
        help_text="유저당 고정 랜덤 키 (tf_{uuid4.hex}). 유추 불가해야 함 — 외부 노출 금지",
    )
    _encrypted_toss_billing_key = models.TextField(
        blank=True,
        default="",
        verbose_name="토스 빌링키 (암호문)",
    )
    toss_billing_key = EncryptedTextField("_encrypted_toss_billing_key")
    toss_billing_key_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="빌링키 SHA-256",
        help_text="BILLING_DELETED 웹훅(빌링키만 옴) 역조회용 — Fernet 암호문은 비결정적",
    )
    billing_key_issued_at = models.DateTimeField(null=True, blank=True)
    card_company = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="빌링키 발급 응답의 cardCompany (표시용)",
    )
    card_number_masked = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="마스킹된 카드번호 (표시용, 예: 433012******123*)",
    )

    # ── 청구액 ──
    monthly_amount_snapshot = models.IntegerField(
        null=True,
        blank=True,
        verbose_name="월 청구액 스냅샷",
        help_text="구독 시작 시점 판매가 고정 (프로모 그랜드파더링). 갱신 청구 기본액",
    )
    extra_ig_accounts = models.PositiveSmallIntegerField(
        default=0,
        verbose_name="추가 IG 계정 수",
        help_text="프로 전용 — 계정당 +9,900원/월 갱신 합산",
    )
    pending_plan = models.ForeignKey(
        SubscriptionPlan,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pending_subscriptions",
        verbose_name="예약된 플랜 변경",
        help_text="다운그레이드 예약 — 다음 갱신 시점에 적용",
    )
    pending_amount_snapshot = models.IntegerField(
        null=True,
        blank=True,
        help_text="예약 플랜의 예약 시점 판매가",
    )
    pending_extra_ig_accounts = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="예약된 추가 IG 계정 수",
        help_text="추가 계정 '축소' 예약 — 다음 갱신 시점에 extra_ig_accounts 로 확정. null=예약 없음",
    )

    # ── 트라이얼 ──
    trial_used_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="프로 트라이얼 사용 시각",
        help_text="카드등록 무료 1개월은 1인 1회 — 다운그레이드돼도 지우지 않음(어뷰징 방어)",
    )

    # ── Dunning (갱신 실패 재시도) ──
    renewal_attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="현재 주기 갱신 과금 시도 횟수 (성공 시 0으로 리셋)",
    )
    next_billing_retry_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="past_due 재시도 예정 시각 (D+1/D+3/D+5)",
    )
    last_billing_error = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="마지막 과금 실패 사유 (토스 code: message)",
    )

    # ── 리텐션: 일시정지(Pause) ──
    pause_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="정지 자동 재개 예정일",
        help_text="paused 구독의 자동 유료 재개(+과금) 시각. = 잔여 유료기간 종료일 + paused_months",
    )
    paused_months = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="정지 개월 수",
        help_text="1/2/3. paused 상태에서만 유효",
    )
    last_pause_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="마지막 정지 요청 시각",
        help_text="연 1회 제한(can_pause) 판정용 — 재개해도 지우지 않음",
    )
    pause_resume_reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="재개 3일 전 고지 발송 시각",
        help_text="자동 결제 재개 사전 고지 중복 방지",
    )

    # ── 리텐션: 다음 1회 할인 쿠폰 ──
    retention_discount_pending = models.BooleanField(
        default=False,
        verbose_name="다음 갱신 할인 대기",
        help_text="True면 다음 1회 갱신에 RETENTION_DISCOUNT_PERCENT 적용 후 자동 소멸",
    )
    retention_discount_used_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="리텐션 할인 사용 시각",
        help_text="1인 1회 — 부여되면 기록, 재부여 차단(어뷰징 방어)",
    )

    cancelled_at = models.DateTimeField(null=True, blank=True)
    page_activation_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="페이지 활성화 변경 일시",
        help_text="하루 1회 제한용 — 마지막으로 페이지 활성화 조정한 시각",
    )
    ig_account_activation_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="IG 계정 활성화 변경 일시",
        help_text="하루 1회 제한용 — 마지막으로 IG 계정 활성화 조정한 시각",
    )
    ig_activation_review_needed = models.BooleanField(
        default=False,
        verbose_name="IG 활성 계정 재선택 필요",
        help_text="갱신 시 허용량 초과로 자동 비활성이 발생 → 사용자가 활성 계정을 다시 고르도록 유도. POST 시 해제",
    )
    pro_activated_at = models.DateTimeField(
        null=True,
        blank=True,
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

    @property
    def has_billing_key(self) -> bool:
        return bool(self._encrypted_toss_billing_key)

    @property
    def renewal_amount(self) -> int:
        """다음 갱신 청구액. 예약 플랜 > 스냅샷 > 현재 플랜가 순 + 추가 계정 가산.

        추가 계정 '축소'가 예약돼 있으면(pending_extra_ig_accounts) 그 값 기준으로 미리 반영.
        단, 다음 주기의 플랜이 pro 가 아니면 추가 계정 개념이 없으므로 가산하지 않는다.
        리텐션 할인이 대기 중이면 다음 1회에 한해 할인 적용(표시·청구 동일 규칙).
        """
        base = (
            self.pending_amount_snapshot if self.pending_plan_id else self.monthly_amount_snapshot
        )
        if base is None:
            base = self.plan.monthly_price

        next_plan = self.pending_plan if self.pending_plan_id else self.plan
        if next_plan and next_plan.name != "pro":
            amount = base
        else:
            extra = (
                self.pending_extra_ig_accounts
                if self.pending_extra_ig_accounts is not None
                else self.extra_ig_accounts
            )
            amount = base + EXTRA_IG_ACCOUNT_PRICE * extra
        return apply_retention_discount(amount, pending=self.retention_discount_pending)

    @property
    def can_pause(self) -> bool:
        """이번에 구독 일시정지가 가능한지 (연 1회·active 유료·카드 보유).

        프론트 리텐션 위저드의 정지 오퍼 노출 조건이자 POST /billing/pause/ 의 서버 게이트.
        """
        if self.plan.name in ("free", "admin"):
            return False
        if self.status != SubscriptionStatus.ACTIVE:
            return False
        if not self.has_billing_key:
            return False
        if self.current_period_end is None:
            return False
        if self.last_pause_at and (timezone.now() - self.last_pause_at) < timedelta(
            days=PAUSE_MIN_INTERVAL_DAYS
        ):
            return False
        return True

    @property
    def retention_discount_available(self) -> bool:
        """리텐션 할인(다음 1회 50%) 을 지금 받을 수 있는지 (1인 1회·active 유료·카드 보유)."""
        if self.plan.name in ("free", "admin"):
            return False
        if self.status != SubscriptionStatus.ACTIVE:
            return False
        if not self.has_billing_key:
            return False
        return self.retention_discount_used_at is None

    def set_billing_key(self, billing_key: str, *, card_company: str = "", card_number: str = ""):
        """빌링키 저장 (암호화 + 해시 + 카드 표시정보). save는 호출 측 책임."""
        self.toss_billing_key = billing_key
        self.toss_billing_key_hash = hash_billing_key(billing_key)
        self.billing_key_issued_at = timezone.now()
        self.card_company = card_company or ""
        self.card_number_masked = card_number or ""

    def clear_billing_key(self):
        """빌링키 제거 (해지/BILLING_DELETED). save는 호출 측 책임."""
        self.toss_billing_key = ""
        self.toss_billing_key_hash = None
        self.billing_key_issued_at = None
        self.card_company = ""
        self.card_number_masked = ""


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"


class PaymentHistory(models.Model):
    """
    결제 내역. 과금 시도 시 pending 행을 먼저 만들고(주문 소유권 락),
    토스 승인 결과로 paid/failed를 확정한다.
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

    # ── 토스페이먼츠 ──
    toss_payment_key = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        unique=True,
        verbose_name="토스 paymentKey",
        help_text="승인 응답의 paymentKey — 취소/조회에 사용",
    )
    toss_order_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        verbose_name="토스 orderId",
        help_text="우리가 생성하는 주문 ID — 과금 시도 멱등/소유권 키",
    )
    toss_idempotency_key = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="승인 API Idempotency-Key (UUID4) — 모호 실패 재시도 시 재사용",
    )
    receipt_url = models.URLField(
        max_length=500,
        null=True,
        blank=True,
        verbose_name="매출전표 URL",
        help_text="토스 receipt.url — 영수증 URL",
    )
    card_company = models.CharField(max_length=50, blank=True, default="")
    card_number_masked = models.CharField(max_length=30, blank=True, default="")
    failure_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="토스 승인 거절 코드",
    )
    failure_message = models.CharField(max_length=200, blank=True, default="")

    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payment_history"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.email} - {self.amount}원 ({self.status})"


# ──────────────────────────────────────────────
# 토스 웹훅 로그 (멱등 보장용)
# ──────────────────────────────────────────────


class TossWebhookLog(models.Model):
    """
    토스페이먼츠 웹훅 수신 로그.
    토스 웹훅에는 이벤트 ID/서명이 없으므로 본문 구조 기반 dedup_key(unique)로
    중복 처리를 방지하고, 실제 상태는 paymentKey 재조회로 검증한다.
    raw_data 저장 전 billingKey는 해시로 치환한다 (평문 저장 금지).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(
        max_length=50,
        verbose_name="이벤트 타입",
        help_text="PAYMENT_STATUS_CHANGED / CANCEL_STATUS_CHANGED / BILLING_DELETED 등",
    )
    dedup_key = models.CharField(
        max_length=255,
        unique=True,
        help_text="본문 구조 기반 중복 방지 키",
    )
    payment_key = models.CharField(
        max_length=200,
        blank=True,
        default="",
        db_index=True,
    )
    order_id = models.CharField(max_length=64, blank=True, default="")
    raw_data = models.JSONField(default=dict, verbose_name="수신 데이터 (빌링키는 해시 치환)")
    processed = models.BooleanField(default=False)
    process_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "toss_webhook_logs"
        ordering = ["-created_at"]
        verbose_name = "토스 웹훅 로그"
        verbose_name_plural = "토스 웹훅 로그 목록"

    def __str__(self):
        return f"[{self.event_type}] dedup={self.dedup_key} processed={self.processed}"


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
