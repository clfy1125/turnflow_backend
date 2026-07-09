"""
토스 빌링 비즈니스 플로우 — 뷰/웹훅 태스크/dev 헬퍼가 공유하는 서비스 레이어.

시나리오 (confirm_billing):
- trial:       pro 첫 구독 + trial_used_at 없음 → 과금 없이 TRIALING 30일
               (+유효 제휴코드 시 code.trial_days 가산). 첫 과금은 갱신 태스크가.
- charge_now:  basic 구독 / 트라이얼 소진 후 pro 재구독 → 즉시 첫 과금.
- attach_only: 트라이얼 중(무카드 레퍼럴 포함) 카드 등록 → 키만 부착,
               기간 불변(트라이얼 적층 금지).
- card_change: 유료 사용자 plan_name 생략 → 카드 교체. PAST_DUE면 즉시 재시도.

원칙:
- 외부(토스) 호출은 DB 트랜잭션 밖에서. 구독 상태 반영은 select_for_update로.
- 과금 1회 시도 = PENDING PaymentHistory 1행 (toss_order_id unique = 소유권).
- 승인 거절(TossError)과 모호(TossNetworkError)를 반드시 구분 —
  모호는 PENDING으로 남겨 reconcile 태스크가 확정한다.
"""

import logging
import math
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import (
    EXTRA_IG_ACCOUNT_PRICE,
    PaymentHistory,
    PaymentStatus,
    ReferralCode,
    ReferralRedemption,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
    generate_customer_key,
)
from .subscription_utils import ensure_subscription
from .toss_service import TossBillingClient, TossError, TossNetworkError

logger = logging.getLogger(__name__)

PERIOD_DAYS = 30  # 월간 구독 주기
TRIAL_BASE_DAYS = 30  # 프로 최초 카드 등록 무료 기간


class BillingFlowError(Exception):
    """뷰가 그대로 응답으로 변환하는 플로우 오류."""

    def __init__(self, detail: str, status_code: int = 400, extra: dict | None = None):
        self.detail = detail
        self.status_code = status_code
        self.extra = extra or {}
        super().__init__(detail)


class ChargeDeclinedError(BillingFlowError):
    """토스가 승인을 명시적으로 거절 (한도초과/정지카드 등) — 402."""

    def __init__(self, toss_error: TossError, payment: PaymentHistory):
        self.toss_code = toss_error.code
        self.payment = payment
        super().__init__(
            detail=f"결제가 거절되었습니다: {toss_error.message}",
            status_code=402,
            extra={"toss_code": toss_error.code},
        )


class ChargePendingError(BillingFlowError):
    """네트워크 모호 실패 — 결제 여부 미확정. reconcile 태스크가 확정 — 202."""

    def __init__(self, payment: PaymentHistory):
        self.payment = payment
        super().__init__(
            detail="결제 결과를 확인 중입니다. 잠시 후 결제 내역을 확인해주세요.",
            status_code=202,
        )


# ──────────────────────────────────────────────
# 기본 헬퍼
# ──────────────────────────────────────────────


def get_current_selling_price(plan: SubscriptionPlan) -> int:
    """현재 판매가 — 구독 시작 시 스냅샷돼 그랜드파더링된다."""
    return plan.monthly_price


# ──────────────────────────────────────────────
# 비례배분 (proration) — 주기 중간 증액(업그레이드/추가계정 증가) 즉시청구용
#
# 원칙:
# - 분모는 고정 30일(PERIOD_DAYS). 주기 전진이 relativedelta 가 아니라 순수
#   +30일 고정이라 달력월(28~31일) 오차가 없다.
# - 남은 비율은 (current_period_end - now)/30일 을 초 단위로 계산(달력일 반올림 오차 회피).
# - 청구분은 floor, 크레딧분은 ceil — 둘 다 사용자에게 유리한 방향.
# - 크레딧 base = 사용자 그랜드파더링 스냅샷(monthly_amount_snapshot),
#   신규 청구 base = 신규 플랜 현재가(get_current_selling_price) — 반드시 분리.
# - 정기 갱신(renewal_amount / _renewal_amount_for)에는 절대 적용하지 않는다(항상 전액).
# ──────────────────────────────────────────────


def proration_ratio(sub: UserSubscription, now=None) -> float:
    """현재 주기의 남은 비율 (0.0~1.0). current_period_end 가 None(free)이거나
    이미 지났으면(dunning) 0.0 으로 clamp — 그 경우 즉시청구는 스킵된다."""
    now = now or timezone.now()
    end = sub.current_period_end
    if end is None:
        return 0.0
    remaining = (end - now).total_seconds()
    ratio = remaining / (PERIOD_DAYS * 86400)
    return max(0.0, min(1.0, ratio))


def _prorate(amount: int, ratio: float, *, mode: str = "floor") -> int:
    """월 전액을 잔여 비율로 안분한 정수 KRW. mode=floor(청구분)/ceil(크레딧분)."""
    value = amount * ratio
    return math.floor(value) if mode == "floor" else math.ceil(value)


def _upgrade_charge_breakdown(
    sub: UserSubscription, new_plan, extra_ig_accounts: int, now=None
) -> tuple[int, int, int]:
    """업그레이드 비례 청구 분해 → (신규 잔여분, 기존 크레딧, 순청구액).

    견적(preview)과 실청구가 동일 계산을 쓰도록 단일 소스로 둔다.
    - 신규 잔여분 base = 신규 플랜 현재가 + 9,900×신규추가계정 (floor, 사용자 유리)
    - 기존 크레딧 base = 사용자 스냅샷가 + 9,900×기존추가계정 (ceil, 사용자 유리)
    """
    ratio = proration_ratio(sub, now)
    new_full = get_current_selling_price(new_plan) + EXTRA_IG_ACCOUNT_PRICE * extra_ig_accounts
    old_base = sub.monthly_amount_snapshot
    if old_base is None:
        old_base = sub.plan.monthly_price
    old_full = old_base + EXTRA_IG_ACCOUNT_PRICE * sub.extra_ig_accounts
    new_prorated = _prorate(new_full, ratio, mode="floor")
    credit = _prorate(old_full, ratio, mode="ceil")
    return new_prorated, credit, max(0, new_prorated - credit)


def compute_upgrade_charge(
    sub: UserSubscription, new_plan, extra_ig_accounts: int, now=None
) -> int:
    """업그레이드 즉시 청구액 = (신규 플랜 잔여분) − (기존 플랜 잔여 크레딧), 최소 0.

    반드시 sub 상태를 갱신하기 전(스냅샷 덮어쓰기 전)에 호출해야 크레딧이 정확하다.
    """
    return _upgrade_charge_breakdown(sub, new_plan, extra_ig_accounts, now)[2]


def compute_extra_accounts_charge(sub: UserSubscription, delta: int, now=None) -> int:
    """추가 계정 증가분(delta>0)의 잔여일 일할 청구액 (floor)."""
    ratio = proration_ratio(sub, now)
    return _prorate(EXTRA_IG_ACCOUNT_PRICE * delta, ratio, mode="floor")


def ensure_customer_key(sub: UserSubscription) -> str:
    if not sub.toss_customer_key:
        sub.toss_customer_key = generate_customer_key()
        sub.save(update_fields=["toss_customer_key", "updated_at"])
    return sub.toss_customer_key


def one_off_order_id(sub: UserSubscription, tag: str) -> str:
    """최초 결제/추가계정 등 1회성 주문 ID. tag: init | extra"""
    return f"tfsub-{sub.id.hex[:10]}-{tag}-{uuid.uuid4().hex[:8]}"


def renewal_order_id(sub: UserSubscription, period_end, attempt: int) -> str:
    """갱신 주문 ID — 주기당 결정적 (동시 실행 시 unique 충돌로 소유권 판별)."""
    return f"tfsub-{sub.id.hex[:10]}-{period_end:%Y%m%d}-a{attempt}"


def proration_upgrade_order_id(sub: UserSubscription, new_plan, extra: int) -> str:
    """업그레이드 비례 청구 주문 ID — (대상 플랜·추가계정·주기)당 결정적.

    같은 주기에 같은 대상으로의 재요청(더블클릭)은 동일 ID → get_or_create 소유권
    락으로 이중과금 방어. orderId 가 대상 상태를 담아 reconcile 재적용의 근거가 된다.
    """
    return f"tfsub-{sub.id.hex[:10]}-up-{new_plan.name}-{extra}-{sub.current_period_end:%Y%m%d}"


def proration_extra_order_id(sub: UserSubscription, to_count: int) -> str:
    """추가계정 증가 비례 청구 주문 ID — (현재수→목표수·주기)당 결정적.

    from(현재수)을 포함해 '축소 후 재증가'가 이전 주문과 충돌하지 않게 한다.
    """
    return (
        f"tfsub-{sub.id.hex[:10]}-ex-{sub.extra_ig_accounts}-{to_count}"
        f"-{sub.current_period_end:%Y%m%d}"
    )


# 토스 발급사 코드 → 카드사명 (API 버전에 따라 cardCompany 대신 card.issuerCode 만 오는 경우 대비).
# 표시용이므로 미지 코드는 코드 문자열을 그대로 노출한다.
ISSUER_CODES = {
    "11": "국민",
    "21": "하나",
    "3C": "유니온페이",
    "31": "BC",
    "33": "우리BC",
    "34": "수협",
    "35": "전북",
    "36": "씨티",
    "37": "우체국",
    "38": "새마을",
    "39": "저축",
    "3A": "케이뱅크",
    "3K": "기업BC",
    "41": "신한",
    "42": "제주",
    "46": "광주",
    "51": "삼성",
    "52": "산업",
    "61": "현대",
    "62": "신협",
    "71": "롯데",
    "91": "NH",
    "W1": "우리",
    "15": "카카오뱅크",
    "24": "토스뱅크",
    "4M": "마스터",
    "4V": "비자",
    "4J": "JCB",
    "6D": "다이너스",
    "7A": "아멕스",
}


def _card_display(issue_or_payment: dict) -> tuple[str, str]:
    """토스 응답에서 카드사/마스킹 번호 추출.

    API 버전에 따라 top-level cardCompany/cardNumber 이거나 card 객체(company/number
    또는 issuerCode)일 수 있어 순서대로 폴백한다.
    """
    card = issue_or_payment.get("card") or {}
    company = issue_or_payment.get("cardCompany") or card.get("company") or ""
    if not company and card.get("issuerCode"):
        company = ISSUER_CODES.get(card["issuerCode"], card["issuerCode"])
    number = issue_or_payment.get("cardNumber") or card.get("number") or ""
    return company, number


def apply_payment_success_fields(payment: PaymentHistory, toss_payment: dict):
    """승인 성공 Payment 객체 → PaymentHistory 반영. save는 호출측."""
    company, number = _card_display(toss_payment)
    payment.status = PaymentStatus.PAID
    payment.toss_payment_key = toss_payment.get("paymentKey", "")
    payment.payment_method = "card"
    payment.receipt_url = (toss_payment.get("receipt") or {}).get("url", "")
    payment.card_company = company
    payment.card_number_masked = number
    payment.paid_at = timezone.now()


def mark_converted_to_paid(user, now=None):
    """레퍼럴 트라이얼 사용자의 첫 실과금 시 유료 전환 마킹 (멱등)."""
    now = now or timezone.now()
    updated = ReferralRedemption.objects.filter(user=user, converted_to_paid=False).update(
        converted_to_paid=True, converted_at=now
    )
    if updated:
        logger.info("레퍼럴 유료 전환 마킹: user=%s", user.email)


def _activate_all_pages(user):
    from apps.pages.models import Page

    Page.objects.filter(user=user, is_active=False).update(is_active=True)


# ──────────────────────────────────────────────
# 과금 실행 (1회성 — 최초 결제 / 추가 계정)
# ──────────────────────────────────────────────


def execute_immediate_charge(
    sub: UserSubscription, amount: int, description: str, tag: str
) -> PaymentHistory:
    """빌링키로 즉시 승인. PENDING 행 생성 → 승인 → paid 확정.

    - TossError(거절): failed 마킹 후 ChargeDeclinedError
    - TossNetworkError(모호): PENDING 유지 후 ChargePendingError
      (reconcile_pending_payments 가 get_payment_by_order_id 로 확정)
    """
    payment = PaymentHistory.objects.create(
        user=sub.user,
        subscription=sub,
        amount=amount,
        status=PaymentStatus.PENDING,
        description=description,
        toss_order_id=one_off_order_id(sub, tag),
        toss_idempotency_key=str(uuid.uuid4()),
    )

    try:
        result = TossBillingClient.charge(
            billing_key=sub.toss_billing_key,
            customer_key=sub.toss_customer_key,
            amount=amount,
            order_id=payment.toss_order_id,
            order_name=description,
            idempotency_key=payment.toss_idempotency_key,
            customer_email=sub.user.email,
        )
    except TossNetworkError:
        logger.warning(
            "즉시 과금 결과 모호(PENDING 유지): user=%s order=%s",
            sub.user.email,
            payment.toss_order_id,
        )
        raise ChargePendingError(payment) from None
    except TossError as e:
        payment.status = PaymentStatus.FAILED
        payment.failure_code = e.code[:64]
        payment.failure_message = e.message[:200]
        payment.save(update_fields=["status", "failure_code", "failure_message"])
        logger.info(
            "즉시 과금 거절: user=%s order=%s code=%s",
            sub.user.email,
            payment.toss_order_id,
            e.code,
        )
        raise ChargeDeclinedError(e, payment) from None

    apply_payment_success_fields(payment, result)
    payment.save()
    logger.info(
        "즉시 과금 성공: user=%s order=%s amount=%d",
        sub.user.email,
        payment.toss_order_id,
        amount,
    )
    return payment


def charge_prorated(
    sub: UserSubscription, amount: int, description: str, order_id: str
) -> PaymentHistory:
    """결정적 orderId 로 비례 즉시 승인 — 재시도/더블클릭 이중과금 방어.

    갱신 과금과 동일한 소유권 패턴: 구독 행 락 안에서 PENDING 을 선점(get_or_create)
    하고, 외부 승인 호출은 락 밖에서 한다.
    - 이미 PAID 인 동일 주문: 재청구 없이 그 payment 반환(멱등).
    - 이미 PENDING(다른 요청/모호): ChargePendingError.
    - 이전 FAILED: 같은 주문을 재사용하되 새 Idempotency-Key 로 재승인.
    - 같은 구독에 다른 미확정(PENDING) 비례 주문이 있으면 ChargePendingError
      (서로 다른 목표로의 동시 요청 → 이중 청구 방지. 이전 건 확정 후 재시도).
    """
    with transaction.atomic():
        UserSubscription.objects.select_for_update().get(pk=sub.pk)  # 동시 요청 직렬화
        prefix = f"tfsub-{sub.id.hex[:10]}-"
        inflight = (
            PaymentHistory.objects.filter(subscription=sub, status=PaymentStatus.PENDING)
            .filter(
                Q(toss_order_id__startswith=prefix + "up-")
                | Q(toss_order_id__startswith=prefix + "ex-")
            )
            .exclude(toss_order_id=order_id)
            .first()
        )
        if inflight is not None:
            raise ChargePendingError(inflight)
        payment, created = PaymentHistory.objects.get_or_create(
            toss_order_id=order_id,
            defaults={
                "user": sub.user,
                "subscription": sub,
                "amount": amount,
                "status": PaymentStatus.PENDING,
                "description": description,
                "toss_idempotency_key": str(uuid.uuid4()),
            },
        )
        if not created:
            if payment.status == PaymentStatus.PAID:
                return payment
            if payment.status == PaymentStatus.PENDING:
                raise ChargePendingError(payment)
            # 이전 FAILED — 새 시도로 재사용 (새 멱등키여야 토스가 캐시 실패를 반환하지 않음)
            payment.status = PaymentStatus.PENDING
            payment.amount = amount
            payment.description = description
            payment.failure_code = ""
            payment.failure_message = ""
            payment.toss_idempotency_key = str(uuid.uuid4())
            payment.save(
                update_fields=[
                    "status",
                    "amount",
                    "description",
                    "failure_code",
                    "failure_message",
                    "toss_idempotency_key",
                ]
            )

    try:
        result = TossBillingClient.charge(
            billing_key=sub.toss_billing_key,
            customer_key=sub.toss_customer_key,
            amount=amount,
            order_id=order_id,
            order_name=description,
            idempotency_key=payment.toss_idempotency_key,
            customer_email=sub.user.email,
        )
    except TossNetworkError:
        logger.warning(
            "비례 과금 결과 모호(PENDING 유지): user=%s order=%s", sub.user.email, order_id
        )
        raise ChargePendingError(payment) from None
    except TossError as e:
        payment.status = PaymentStatus.FAILED
        payment.failure_code = e.code[:64]
        payment.failure_message = e.message[:200]
        payment.save(update_fields=["status", "failure_code", "failure_message"])
        logger.info("비례 과금 거절: user=%s order=%s code=%s", sub.user.email, order_id, e.code)
        raise ChargeDeclinedError(e, payment) from None

    apply_payment_success_fields(payment, result)
    payment.save()
    logger.info("비례 과금 성공: user=%s order=%s amount=%d", sub.user.email, order_id, amount)
    return payment


# ──────────────────────────────────────────────
# 레퍼럴 (제휴코드)
# ──────────────────────────────────────────────


def _validate_referral_for_trial(user, code_str: str) -> ReferralCode:
    """confirm의 제휴코드 사전 검증 — 외부 호출 전에 fail-fast."""
    code_str = (code_str or "").strip().upper()
    if not code_str:
        raise BillingFlowError("제휴 코드를 입력해주세요.")
    if ReferralRedemption.objects.filter(user=user).exists():
        raise BillingFlowError("이미 제휴/레퍼럴 코드를 사용하셨습니다.")
    try:
        code = ReferralCode.objects.select_related("target_plan").get(code=code_str)
    except ReferralCode.DoesNotExist:
        raise BillingFlowError("존재하지 않는 제휴 코드입니다.") from None
    ok, reason = code.is_redeemable()
    if not ok:
        raise BillingFlowError(reason)
    return code


def _consume_referral(user, code: ReferralCode, now, trial_ends) -> ReferralRedemption:
    """트라이얼 시작 트랜잭션 안에서 호출 — 코드 락 + 사용 처리."""
    locked = ReferralCode.objects.select_for_update().get(pk=code.pk)
    ok, reason = locked.is_redeemable()
    if not ok:
        raise BillingFlowError(reason)
    ReferralCode.objects.filter(pk=locked.pk).update(
        current_uses=F("current_uses") + 1, updated_at=now
    )
    return ReferralRedemption.objects.create(
        user=user,
        referral_code=locked,
        trial_started_at=now,
        trial_ends_at=trial_ends,
    )


# ──────────────────────────────────────────────
# confirm — 빌링키 등록 + 구독 시작/카드 변경
# ──────────────────────────────────────────────


def confirm_billing(
    user,
    *,
    auth_key: str | None = None,
    dev_card: dict | None = None,
    plan_name: str | None = None,
    referral_code: str | None = None,
    extra_ig_accounts: int = 0,
) -> dict:
    """빌링키 발급/부착 + 시나리오 실행. 뷰와 dev 헬퍼가 공유.

    Returns: {subscription, payment, first_charge_at, detail, scenario}
    Raises: BillingFlowError (하위: ChargeDeclinedError, ChargePendingError)
    """
    sub = ensure_subscription(user)
    if sub.plan.name == "admin":
        raise BillingFlowError("관리자 플랜은 결제 대상이 아닙니다.")

    # ── 시나리오 결정 (외부 호출 전 fail-fast) ──
    new_plan = None
    if plan_name:
        try:
            new_plan = SubscriptionPlan.objects.get(name=plan_name, is_active=True)
        except SubscriptionPlan.DoesNotExist:
            raise BillingFlowError("플랜을 찾을 수 없습니다.", status_code=404) from None
        if new_plan.name == "free":
            raise BillingFlowError("무료 플랜은 결제가 필요하지 않습니다.")

    if extra_ig_accounts and (new_plan is None or new_plan.name != "pro"):
        raise BillingFlowError("추가 IG 계정은 프로 플랜에서만 구매할 수 있습니다.")

    if new_plan is None:
        # 카드 변경 — 유료 구독자(트라이얼 포함) 전용
        if not sub.is_paid_plan:
            raise BillingFlowError("구독할 플랜을 선택해주세요. (plan_name)")
        scenario = "card_change"
    elif sub.status == SubscriptionStatus.TRIALING:
        # 트라이얼 중(무카드 레퍼럴 포함) — 같은 플랜 카드 등록만 허용, 기간 불변
        if sub.plan_id != new_plan.id:
            raise BillingFlowError(
                "트라이얼 중에는 플랜을 변경할 수 없습니다. 트라이얼 종료 후 변경해주세요."
            )
        scenario = "attach_only"
    elif sub.is_paid_plan:
        raise BillingFlowError(
            "이미 유료 구독 중입니다. 플랜 변경은 change-plan API, "
            "카드 변경은 plan_name 없이 호출해주세요."
        )
    elif new_plan.name == "pro" and sub.trial_used_at is None:
        scenario = "trial"
    else:
        scenario = "charge_now"

    referral = None
    if referral_code:
        if scenario != "trial":
            raise BillingFlowError(
                "제휴 코드는 프로 플랜 최초 구독(무료 체험 시작) 시에만 사용할 수 있습니다."
            )
        referral = _validate_referral_for_trial(user, referral_code)

    # ── 빌링키 발급 (외부 호출 — 트랜잭션 밖) ──
    customer_key = ensure_customer_key(sub)
    try:
        if auth_key:
            issue = TossBillingClient.issue_billing_key(auth_key, customer_key)
        elif dev_card:
            issue = TossBillingClient.issue_billing_key_by_card(
                customer_key=customer_key, **dev_card
            )
        else:
            raise BillingFlowError("auth_key가 필요합니다.")
    except TossNetworkError:
        raise BillingFlowError(
            "카드 등록 중 통신 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            status_code=502,
        ) from None
    except TossError as e:
        raise BillingFlowError(
            f"카드 등록에 실패했습니다: {e.message}",
            status_code=400,
            extra={"toss_code": e.code},
        ) from None

    if issue.get("customerKey") != customer_key:
        logger.error("빌링키 발급 customerKey 불일치: user=%s", user.email)
        raise BillingFlowError("카드 등록 정보가 일치하지 않습니다. 다시 시도해주세요.")

    billing_key = issue["billingKey"]
    card_company, card_number = _card_display(issue)
    now = timezone.now()

    # ── 구독 반영 (락) ──
    with transaction.atomic():
        locked = (
            UserSubscription.objects.select_for_update(of=("self",))
            .select_related("plan")
            .get(pk=sub.pk)
        )
        old_key = locked.toss_billing_key if locked.has_billing_key else ""
        locked.set_billing_key(billing_key, card_company=card_company, card_number=card_number)
        key_fields = [
            "_encrypted_toss_billing_key",
            "toss_billing_key_hash",
            "billing_key_issued_at",
            "card_company",
            "card_number_masked",
        ]

        if scenario == "trial":
            bonus_days = referral.trial_days if referral else 0
            trial_ends = now + timedelta(days=TRIAL_BASE_DAYS + bonus_days)
            locked.plan = new_plan
            locked.status = SubscriptionStatus.TRIALING
            locked.current_period_start = now
            locked.current_period_end = trial_ends
            locked.monthly_amount_snapshot = get_current_selling_price(new_plan)
            locked.extra_ig_accounts = extra_ig_accounts
            locked.trial_used_at = now
            locked.cancelled_at = None
            locked.renewal_attempts = 0
            locked.next_billing_retry_at = None
            locked.last_billing_error = ""
            locked.save(
                update_fields=key_fields
                + [
                    "plan",
                    "status",
                    "current_period_start",
                    "current_period_end",
                    "monthly_amount_snapshot",
                    "extra_ig_accounts",
                    "trial_used_at",
                    "cancelled_at",
                    "renewal_attempts",
                    "next_billing_retry_at",
                    "last_billing_error",
                    "updated_at",
                ]
            )
            if referral:
                _consume_referral(user, referral, now, trial_ends)
            _activate_all_pages(user)
        else:
            # attach_only / card_change / charge_now(키 먼저 저장, 과금은 아래서)
            locked.save(update_fields=key_fields + ["updated_at"])

    # 이전 빌링키 정리 (best-effort — 실패해도 과금 위험 없음)
    if old_key and old_key != billing_key:
        try:
            TossBillingClient.delete_billing_key(old_key, customer_key)
        except TossError:
            logger.info("이전 빌링키 삭제 실패(무시): user=%s", user.email)

    sub.refresh_from_db()

    if scenario == "trial":
        logger.info(
            "프로 트라이얼 시작: user=%s ends=%s referral=%s",
            user.email,
            sub.current_period_end.isoformat(),
            referral.code if referral else "-",
        )
        return {
            "subscription": sub,
            "payment": None,
            "first_charge_at": sub.current_period_end,
            "detail": "무료 체험이 시작되었습니다. 체험 종료 후 첫 결제가 진행됩니다.",
            "scenario": scenario,
        }

    if scenario == "attach_only":
        return {
            "subscription": sub,
            "payment": None,
            "first_charge_at": sub.current_period_end,
            "detail": "카드가 등록되었습니다. 체험 종료 시 첫 결제가 진행됩니다.",
            "scenario": scenario,
        }

    if scenario == "card_change":
        detail = "결제 카드가 변경되었습니다."
        if sub.status == SubscriptionStatus.PAST_DUE:
            from .tasks import charge_subscription_renewal

            transaction.on_commit(lambda: charge_subscription_renewal.delay(str(sub.id)))
            detail = "결제 카드가 변경되었습니다. 미납 결제를 재시도합니다."
        return {
            "subscription": sub,
            "payment": None,
            "first_charge_at": None,
            "detail": detail,
            "scenario": scenario,
        }

    # ── charge_now: 즉시 첫 과금 ──
    amount = get_current_selling_price(new_plan)
    if new_plan.name == "pro":
        amount += EXTRA_IG_ACCOUNT_PRICE * extra_ig_accounts
    description = f"턴플로우 {new_plan.display_name} 월간 구독"
    payment = execute_immediate_charge(sub, amount, description, tag="init")
    # ChargeDeclined/Pending 은 그대로 전파 — 빌링키는 부착된 상태로 유지된다.

    with transaction.atomic():
        locked = (
            UserSubscription.objects.select_for_update(of=("self",))
            .select_related("plan")
            .get(pk=sub.pk)
        )
        locked.plan = new_plan
        locked.status = SubscriptionStatus.ACTIVE
        locked.current_period_start = now
        locked.current_period_end = now + timedelta(days=PERIOD_DAYS)
        locked.monthly_amount_snapshot = get_current_selling_price(new_plan)
        locked.extra_ig_accounts = extra_ig_accounts if new_plan.name == "pro" else 0
        locked.cancelled_at = None
        locked.renewal_attempts = 0
        locked.next_billing_retry_at = None
        locked.last_billing_error = ""
        if not locked.pro_activated_at:
            locked.pro_activated_at = now
        locked.save(
            update_fields=[
                "plan",
                "status",
                "current_period_start",
                "current_period_end",
                "monthly_amount_snapshot",
                "extra_ig_accounts",
                "cancelled_at",
                "renewal_attempts",
                "next_billing_retry_at",
                "last_billing_error",
                "pro_activated_at",
                "updated_at",
            ]
        )
    _activate_all_pages(user)
    mark_converted_to_paid(user, now)

    sub.refresh_from_db()
    logger.info("첫 결제 완료: user=%s plan=%s amount=%d", user.email, new_plan.name, amount)
    return {
        "subscription": sub,
        "payment": payment,
        "first_charge_at": None,
        "detail": f"{new_plan.display_name} 플랜 구독이 시작되었습니다.",
        "scenario": scenario,
    }


# ──────────────────────────────────────────────
# 구독 상태 반영 (증액 성공 후 — change_plan/change_extra_accounts/reconcile 공유, 멱등)
# ──────────────────────────────────────────────


def _apply_upgrade_state(sub_pk, new_plan, extra_ig_accounts: int, now=None):
    """업그레이드 성공 후 구독 상태 반영. ★앵커(주기) 불변★ — 남은 기간만큼만 비례
    청구했으므로 current_period_start/end 를 리셋하지 않는다. 멱등."""
    now = now or timezone.now()
    with transaction.atomic():
        locked = UserSubscription.objects.select_for_update().select_related("user").get(pk=sub_pk)
        locked.plan = new_plan
        locked.status = SubscriptionStatus.ACTIVE
        locked.monthly_amount_snapshot = get_current_selling_price(new_plan)
        locked.extra_ig_accounts = extra_ig_accounts
        locked.pending_plan = None
        locked.pending_amount_snapshot = None
        locked.renewal_attempts = 0
        locked.next_billing_retry_at = None
        locked.last_billing_error = ""
        if not locked.pro_activated_at:
            locked.pro_activated_at = now
        locked.save(
            update_fields=[
                "plan",
                "status",
                "monthly_amount_snapshot",
                "extra_ig_accounts",
                "pending_plan",
                "pending_amount_snapshot",
                "renewal_attempts",
                "next_billing_retry_at",
                "last_billing_error",
                "pro_activated_at",
                "updated_at",
            ]
        )
    _activate_all_pages(locked.user)
    mark_converted_to_paid(locked.user, now)
    return locked


def _apply_extra_state(sub_pk, to_count: int):
    """추가 IG 계정 수 반영 (멱등). change_extra_accounts / reconcile 공유."""
    with transaction.atomic():
        locked = UserSubscription.objects.select_for_update().get(pk=sub_pk)
        locked.extra_ig_accounts = to_count
        locked.save(update_fields=["extra_ig_accounts", "updated_at"])
    return locked


# ──────────────────────────────────────────────
# 플랜 변경 (유료 ↔ 유료)
# ──────────────────────────────────────────────

_PLAN_RANK = {"basic": 1, "pro": 2}


def change_plan(user, plan_name: str, extra_ig_accounts: int = 0) -> dict:
    """빌링키 보유 유료 구독자의 플랜 변경.

    - 업그레이드(basic→pro): 남은 기간분 차액만 즉시 비례 청구 + 주기(앵커) 유지.
      (신규 플랜 잔여분 − 기존 플랜 잔여 크레딧, 다음 갱신부터 신규 플랜 전액)
    - 다운그레이드(pro→basic): pending_plan 예약 — 기간말(다음 갱신)에 적용, 무과금.
    - 같은 플랜 + 예약 존재: 예약 취소.
    """
    sub = ensure_subscription(user)

    if sub.plan.name == "admin":
        raise BillingFlowError("관리자 플랜은 결제 대상이 아닙니다.")
    if not sub.is_paid_plan:
        raise BillingFlowError(
            "무료 플랜 사용자입니다. 카드 등록과 함께 POST /billing/toss/confirm/ 으로 "
            "구독을 시작해주세요."
        )
    if not sub.has_billing_key:
        raise BillingFlowError("결제 카드가 등록되어 있지 않습니다. 카드를 먼저 등록해주세요.")
    if sub.status == SubscriptionStatus.TRIALING:
        raise BillingFlowError("무료 체험 중에는 플랜을 변경할 수 없습니다.")
    if sub.status == SubscriptionStatus.PAST_DUE:
        raise BillingFlowError(
            "결제 실패(미납) 상태입니다. 결제 수단을 갱신해 미납을 해소한 후 변경해주세요."
        )
    if sub.status == SubscriptionStatus.CANCELLED:
        raise BillingFlowError("해지 예약된 구독입니다. 구독을 재개한 후 변경해주세요.")

    try:
        new_plan = SubscriptionPlan.objects.get(name=plan_name, is_active=True)
    except SubscriptionPlan.DoesNotExist:
        raise BillingFlowError("플랜을 찾을 수 없습니다.", status_code=404) from None
    if new_plan.name == "free":
        raise BillingFlowError("무료 전환은 POST /billing/cancel/ 을 사용해주세요.")

    if new_plan.id == sub.plan_id:
        if sub.pending_plan_id:
            with transaction.atomic():
                locked = UserSubscription.objects.select_for_update().get(pk=sub.pk)
                locked.pending_plan = None
                locked.pending_amount_snapshot = None
                locked.save(update_fields=["pending_plan", "pending_amount_snapshot", "updated_at"])
            sub.refresh_from_db()
            return {
                "subscription": sub,
                "payment": None,
                "detail": "예약된 플랜 변경이 취소되었습니다.",
                "effective_at": None,
            }
        raise BillingFlowError("이미 동일한 플랜을 사용 중입니다.")

    if extra_ig_accounts and new_plan.name != "pro":
        raise BillingFlowError("추가 IG 계정은 프로 플랜에서만 설정할 수 있습니다.")

    is_upgrade = _PLAN_RANK.get(new_plan.name, 0) > _PLAN_RANK.get(sub.plan.name, 0)

    if not is_upgrade:
        # ── 다운그레이드 예약 — 다음 갱신에서 적용 ──
        with transaction.atomic():
            locked = UserSubscription.objects.select_for_update().get(pk=sub.pk)
            locked.pending_plan = new_plan
            locked.pending_amount_snapshot = get_current_selling_price(new_plan)
            locked.save(update_fields=["pending_plan", "pending_amount_snapshot", "updated_at"])
        sub.refresh_from_db()
        logger.info(
            "플랜 다운그레이드 예약: user=%s %s→%s effective=%s",
            user.email,
            sub.plan.name,
            new_plan.name,
            sub.current_period_end.isoformat() if sub.current_period_end else "-",
        )
        return {
            "subscription": sub,
            "payment": None,
            "detail": (
                f"{new_plan.display_name} 플랜으로 변경이 예약되었습니다. "
                "현재 결제 주기가 끝나는 시점에 적용됩니다."
            ),
            "effective_at": sub.current_period_end,
        }

    # ── 업그레이드 — 남은 기간분 차액만 즉시 비례 청구 + 주기(앵커) 유지 ──
    now = timezone.now()
    amount = compute_upgrade_charge(sub, new_plan, extra_ig_accounts, now=now)
    payment = None
    if amount > 0:
        payment = charge_prorated(
            sub,
            amount,
            f"턴플로우 {new_plan.display_name} 업그레이드 (잔여기간 비례)",
            proration_upgrade_order_id(sub, new_plan, extra_ig_accounts),
        )

    # 청구 성공(또는 0원 무과금) 후에만 구독 상태 반영 — 앵커는 유지한다.
    _apply_upgrade_state(sub.pk, new_plan, extra_ig_accounts, now=now)

    sub.refresh_from_db()
    logger.info(
        "플랜 업그레이드 완료: user=%s → %s amount=%d (잔여기간 비례)",
        user.email,
        new_plan.name,
        amount,
    )
    return {
        "subscription": sub,
        "payment": payment,
        "detail": f"{new_plan.display_name} 플랜으로 업그레이드되었습니다.",
        "effective_at": None,
    }


# ──────────────────────────────────────────────
# 추가 IG 계정 구매/축소
# ──────────────────────────────────────────────


def change_extra_accounts(user, new_count: int) -> dict:
    """프로 전용 추가 IG 계정 수 변경.

    - 증가: 증가분 × 9,900원을 현재 주기 잔여일만큼 비례 즉시 청구 → 성공 시 반영.
      다음 갱신부터 계정 전체가 합산 청구된다.
    - 감소: 무과금·무환불. 현재 활성 연동 수가 새 허용량(1+count) 이하일 때만.
    """
    sub = ensure_subscription(user)

    if sub.plan.name != "pro":
        raise BillingFlowError("추가 IG 계정은 프로 플랜 전용입니다.")
    if not sub.has_billing_key:
        raise BillingFlowError("결제 카드가 등록되어 있지 않습니다.")
    if sub.status == SubscriptionStatus.TRIALING:
        raise BillingFlowError(
            "무료 체험 중에는 추가 계정을 변경할 수 없습니다. 체험 종료 후 이용해주세요."
        )
    if sub.status == SubscriptionStatus.PAST_DUE:
        raise BillingFlowError(
            "미납 상태에서는 추가 계정을 구매할 수 없습니다. 결제 수단을 확인해주세요."
        )
    if sub.status == SubscriptionStatus.CANCELLED:
        raise BillingFlowError("해지 예약된 구독입니다. 구독을 재개한 후 이용해주세요.")

    delta = new_count - sub.extra_ig_accounts
    if delta == 0:
        raise BillingFlowError("현재 설정과 동일합니다.")

    payment = None
    if delta > 0:
        amount = compute_extra_accounts_charge(sub, delta, now=timezone.now())
        if amount > 0:
            payment = charge_prorated(
                sub,
                amount,
                f"턴플로우 추가 IG 계정 {delta}개 (잔여기간 비례)",
                proration_extra_order_id(sub, new_count),
            )
    else:
        from apps.integrations.models import IGAccountConnection

        allowed_after = 1 + new_count
        current_active = IGAccountConnection.objects.filter(
            workspace__owner=user, status=IGAccountConnection.Status.ACTIVE
        ).count()
        if current_active > allowed_after:
            raise BillingFlowError(
                f"현재 연동된 IG 계정이 {current_active}개입니다. "
                f"{allowed_after}개 이하로 연동을 해제한 후 축소할 수 있습니다.",
                extra={"current_active": current_active, "allowed_after": allowed_after},
            )

    # 청구 성공(또는 0원 무과금)/감소 검증 통과 후에만 반영.
    _apply_extra_state(sub.pk, new_count)

    sub.refresh_from_db()
    logger.info("추가 IG 계정 변경: user=%s %+d → %d (잔여기간 비례)", user.email, delta, new_count)
    return {"subscription": sub, "payment": payment}


# ──────────────────────────────────────────────
# 견적 (preview) — 부작용 없는 계산. 실행(change_*)과 동일 가드·계산을 공유해
# '지금 변경하면 얼마 청구되는지'를 결제 전에 프론트에 보여준다. 토스 호출/DB 변경 없음.
# ──────────────────────────────────────────────


def _remaining_days(sub: UserSubscription, now) -> int:
    """표시용 남은 일수(달력 기준 근사). 금액 계산은 proration_ratio(초 단위)로 별도 수행."""
    if sub.current_period_end is None:
        return 0
    return max(0, (sub.current_period_end - now).days)


def preview_change_plan(user, plan_name: str, extra_ig_accounts: int = 0, now=None) -> dict:
    """change_plan 의 부작용 없는 견적. 실행과 동일 가드·계산 → 견적=실청구 보장.

    반환 dict: direction(upgrade|downgrade|noop), immediate_charge{amount,currency,
    description,proration}, effective_at, next_renewal_amount, next_renewal_at.
    """
    now = now or timezone.now()
    sub = ensure_subscription(user)

    if sub.plan.name == "admin":
        raise BillingFlowError("관리자 플랜은 결제 대상이 아닙니다.")
    if not sub.is_paid_plan:
        raise BillingFlowError(
            "무료 플랜 사용자입니다. 카드 등록과 함께 POST /billing/toss/confirm/ 으로 "
            "구독을 시작해주세요."
        )
    if not sub.has_billing_key:
        raise BillingFlowError("결제 카드가 등록되어 있지 않습니다. 카드를 먼저 등록해주세요.")
    if sub.status == SubscriptionStatus.TRIALING:
        raise BillingFlowError("무료 체험 중에는 플랜을 변경할 수 없습니다.")
    if sub.status == SubscriptionStatus.PAST_DUE:
        raise BillingFlowError(
            "결제 실패(미납) 상태입니다. 결제 수단을 갱신해 미납을 해소한 후 변경해주세요."
        )
    if sub.status == SubscriptionStatus.CANCELLED:
        raise BillingFlowError("해지 예약된 구독입니다. 구독을 재개한 후 변경해주세요.")

    try:
        new_plan = SubscriptionPlan.objects.get(name=plan_name, is_active=True)
    except SubscriptionPlan.DoesNotExist:
        raise BillingFlowError("플랜을 찾을 수 없습니다.", status_code=404) from None
    if new_plan.name == "free":
        raise BillingFlowError("무료 전환은 POST /billing/cancel/ 을 사용해주세요.")
    if extra_ig_accounts and new_plan.name != "pro":
        raise BillingFlowError("추가 IG 계정은 프로 플랜에서만 설정할 수 있습니다.")

    zero = {"amount": 0, "currency": "KRW", "description": "", "proration": None}

    if new_plan.id == sub.plan_id:
        return {
            "direction": "noop",
            "immediate_charge": {**zero, "description": "현재 플랜과 동일합니다."},
            "effective_at": None,
            "next_renewal_amount": sub.renewal_amount,
            "next_renewal_at": sub.current_period_end,
        }

    is_upgrade = _PLAN_RANK.get(new_plan.name, 0) > _PLAN_RANK.get(sub.plan.name, 0)

    if not is_upgrade:
        return {
            "direction": "downgrade",
            "immediate_charge": {
                **zero,
                "description": (
                    f"{new_plan.display_name} 다운그레이드는 다음 갱신에 적용됩니다(무과금)."
                ),
            },
            "effective_at": sub.current_period_end,
            "next_renewal_amount": get_current_selling_price(new_plan),
            "next_renewal_at": sub.current_period_end,
        }

    # 업그레이드 — 잔여기간 차액 비례
    new_prorated, credit, net = _upgrade_charge_breakdown(sub, new_plan, extra_ig_accounts, now)
    remaining = _remaining_days(sub, now)
    return {
        "direction": "upgrade",
        "immediate_charge": {
            "amount": net,
            "currency": "KRW",
            "description": (
                f"{new_plan.display_name} 잔여 {remaining}일분 비례 청구"
                + (f" (기존 플랜 크레딧 -{credit}원 차감)" if credit else "")
            ),
            "proration": {
                "period_days": PERIOD_DAYS,
                "remaining_days": remaining,
                "new_plan_prorated": new_prorated,
                "current_plan_credit": credit,
                "net": net,
            },
        },
        "effective_at": None,
        "next_renewal_amount": (
            get_current_selling_price(new_plan) + EXTRA_IG_ACCOUNT_PRICE * extra_ig_accounts
        ),
        "next_renewal_at": sub.current_period_end,
    }


def preview_change_extra_accounts(user, count: int, now=None) -> dict:
    """change_extra_accounts 의 부작용 없는 견적. 실행과 동일 가드·계산.

    반환 dict: direction(increase|decrease|noop), delta, immediate_charge{...},
    effective_at, next_renewal_amount, unit_price.
    """
    now = now or timezone.now()
    sub = ensure_subscription(user)

    if sub.plan.name != "pro":
        raise BillingFlowError("추가 IG 계정은 프로 플랜 전용입니다.")
    if not sub.has_billing_key:
        raise BillingFlowError("결제 카드가 등록되어 있지 않습니다.")
    if sub.status == SubscriptionStatus.TRIALING:
        raise BillingFlowError(
            "무료 체험 중에는 추가 계정을 변경할 수 없습니다. 체험 종료 후 이용해주세요."
        )
    if sub.status == SubscriptionStatus.PAST_DUE:
        raise BillingFlowError(
            "미납 상태에서는 추가 계정을 구매할 수 없습니다. 결제 수단을 확인해주세요."
        )
    if sub.status == SubscriptionStatus.CANCELLED:
        raise BillingFlowError("해지 예약된 구독입니다. 구독을 재개한 후 이용해주세요.")

    delta = count - sub.extra_ig_accounts
    next_renewal_amount = sub.renewal_amount + EXTRA_IG_ACCOUNT_PRICE * delta
    zero = {"amount": 0, "currency": "KRW", "description": "", "proration": None}

    if delta == 0:
        return {
            "direction": "noop",
            "delta": 0,
            "immediate_charge": {**zero, "description": "현재 설정과 동일합니다."},
            "effective_at": None,
            "next_renewal_amount": sub.renewal_amount,
            "unit_price": EXTRA_IG_ACCOUNT_PRICE,
        }
    if delta < 0:
        return {
            "direction": "decrease",
            "delta": delta,
            "immediate_charge": {
                **zero,
                "description": "추가 계정 축소는 무과금 — 다음 갱신부터 낮은 금액으로 청구됩니다.",
            },
            "effective_at": None,
            "next_renewal_amount": next_renewal_amount,
            "unit_price": EXTRA_IG_ACCOUNT_PRICE,
        }

    net = compute_extra_accounts_charge(sub, delta, now=now)
    remaining = _remaining_days(sub, now)
    return {
        "direction": "increase",
        "delta": delta,
        "immediate_charge": {
            "amount": net,
            "currency": "KRW",
            "description": f"추가 계정 {delta}개 잔여 {remaining}일분 비례 청구",
            "proration": {
                "period_days": PERIOD_DAYS,
                "remaining_days": remaining,
                "unit_price": EXTRA_IG_ACCOUNT_PRICE,
                "units": delta,
                "full_amount": EXTRA_IG_ACCOUNT_PRICE * delta,
                "net": net,
            },
        },
        "effective_at": None,
        "next_renewal_amount": next_renewal_amount,
        "unit_price": EXTRA_IG_ACCOUNT_PRICE,
    }


# ──────────────────────────────────────────────
# 환불 반영 (뷰의 능동 환불 + 웹훅의 수동 환불이 수렴)
# ──────────────────────────────────────────────


def apply_refund(payment: PaymentHistory, *, downgrade: bool = True, reason: str = "") -> bool:
    """결제 환불 반영 — 멱등 (이미 REFUNDED면 no-op).

    토스 취소가 이미 완료됐다는 전제 하에 우리 DB만 반영한다
    (뷰: cancel_payment 성공 후 / 웹훅: 재조회로 CANCELED 확인 후).
    """
    with transaction.atomic():
        locked = PaymentHistory.objects.select_for_update().get(pk=payment.pk)
        if locked.status == PaymentStatus.REFUNDED:
            return False
        locked.status = PaymentStatus.REFUNDED
        locked.save(update_fields=["status"])

        sub = locked.subscription
        if downgrade and sub is not None:
            from .models import AiTokenBalance, AiTokenLedger
            from .subscription_utils import get_free_plan
            from .tasks import _downgrade_to_free

            _downgrade_to_free(sub, get_free_plan(), reason=reason or "refund")

            # 구독 결제로 부여된 AI 토큰 회수 (남아있는 만큼만)
            token_balance = AiTokenBalance.objects.filter(user=sub.user).first()
            if token_balance:
                granted = (
                    AiTokenLedger.objects.filter(
                        user=sub.user,
                        amount__gt=0,
                        description__contains="구독 결제 토큰 지급",
                    )
                    .order_by("-created_at")
                    .first()
                )
                if granted and token_balance.balance >= granted.amount:
                    token_balance.deduct(
                        granted.amount,
                        description=f"환불에 따른 토큰 회수 (order={locked.toss_order_id})",
                    )

    logger.info(
        "환불 반영: user=%s order=%s downgrade=%s",
        payment.user.email,
        payment.toss_order_id,
        downgrade,
    )
    return True
