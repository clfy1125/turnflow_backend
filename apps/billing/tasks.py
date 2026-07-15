"""
Billing Celery tasks — 토스 빌링 정기결제 배치.

토스에는 스케줄러가 없다 — 갱신 과금의 주체는 우리다:
1. process_due_renewals        — 10분 주기 디스패처. 갱신 도래 구독을 건별 태스크로 분산
2. charge_subscription_renewal — 건별 과금. 중복 과금 방지가 최우선 (아래 원칙)
3. reconcile_pending_payments  — 30분 주기. 모호 실패(PENDING)를 조회 API로 확정
4. process_toss_webhook        — 웹훅 이벤트 처리 (검증 = paymentKey 재조회)
5. check_missed_payments       — 갱신 파이프라인 고장 감시 (좀비 구독 ERROR 로그)
6. handle_grace_period_expiry  — past_due 7일 유예 만료 → free 다운그레이드
7. handle_trial_expiry         — 빌링키 없는(무카드 레퍼럴) 트라이얼 만료 → 다운그레이드
8. handle_cancelled_expiry     — 해지 예약 구독 기간 만료 → 다운그레이드

중복 과금 방지 원칙:
- 과금 1회 시도 = PENDING PaymentHistory 1행. toss_order_id 는 주기·차수당
  결정적(tfsub-{id}-{period_end}-a{n}) + unique → get_or_create 가 소유권 락.
- 승인 API 는 행에 저장된 Idempotency-Key 로 호출 — 토스가 15일간 중복 방지.
- 거절(TossError)과 모호(TossNetworkError)를 구분: 모호는 상태를 바꾸지 않고
  PENDING 유지 → reconcile 이 실상태로 확정. 거절만 dunning 진입.
- CANCELLED 구독은 절대 과금하지 않는다 (안 긁는 것이 곧 해지).
"""

import logging
import re
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 7
# 갱신 실패 재시도: period_end 기준 D+1 / D+3 / D+5 (3회 소진 후 D+7 다운그레이드)
DUNNING_RETRY_OFFSET_DAYS = {1: 1, 2: 3, 3: 5}
MAX_RENEWAL_ATTEMPTS = 3
PENDING_RECONCILE_AFTER_MINUTES = 30

_RENEWAL_ORDER_RE = re.compile(r"^tfsub-[0-9a-f]{10}-\d{8}-a\d+$")
# 비례(proration) 즉시청구 주문 — PENDING→DONE 확정 시 구독 상태 자동 반영 근거.
_PRORATE_UP_RE = re.compile(r"^tfsub-[0-9a-f]{10}-up-([a-z]+)-(\d+)-\d{8}$")
_PRORATE_EX_RE = re.compile(r"^tfsub-[0-9a-f]{10}-ex-\d+-(\d+)-\d{8}$")


def _match_prorate_order(order_id: str):
    """비례 주문 orderId 파싱 → ('up', plan_name, extra) | ('ex', None, count) | None."""
    m = _PRORATE_UP_RE.match(order_id or "")
    if m:
        return ("up", m.group(1), int(m.group(2)))
    m = _PRORATE_EX_RE.match(order_id or "")
    if m:
        return ("ex", None, int(m.group(1)))
    return None


def _finalize_prorate_success(sub_id, prorate) -> None:
    """비례 즉시청구가 뒤늦게 확정될 때 구독 상태를 orderId 에 담긴 대상대로 반영 (멱등)."""
    from .models import SubscriptionPlan
    from .toss_flows import _apply_extra_state, _apply_upgrade_state

    kind, plan_name, count = prorate
    if kind == "up":
        try:
            new_plan = SubscriptionPlan.objects.get(name=plan_name, is_active=True)
        except SubscriptionPlan.DoesNotExist:
            logger.error(
                "reconcile: 비례 업그레이드 대상 플랜 없음 plan=%s sub=%s", plan_name, sub_id
            )
            return
        _apply_upgrade_state(sub_id, new_plan, count)
    else:
        _apply_extra_state(sub_id, count)


def _log_summary(task_name: str, processed: int, failed: int) -> None:
    if not processed and not failed:
        return
    log = logger.error if failed else logger.info
    log("%s: processed=%d failed=%d", task_name, processed, failed)


def _ops_alert(message: str) -> None:
    """운영 텔레그램 알림 (best-effort)."""
    try:
        from apps.core.telegram import send_telegram_notification

        send_telegram_notification(message)
    except Exception:  # noqa: BLE001 - 알림 실패가 결제 처리를 막으면 안 됨
        logger.exception("billing ops 알림 실패")


def _safe_delete_billing_key(sub, reason: str) -> None:
    """토스 빌링키 삭제 (best-effort).

    실질 해지는 우리가 승인 호출을 멈추는 것 — 삭제 실패해도 과금 위험 0이므로
    PayApp 시절의 '보류-재시도' 패턴은 폐기하고 로그만 남긴다.
    """
    from .toss_service import TossBillingClient, TossError

    if not sub.has_billing_key:
        return
    try:
        TossBillingClient.delete_billing_key(sub.toss_billing_key, sub.toss_customer_key)
    except TossError as e:
        logger.info("%s: 빌링키 삭제 실패(무시) user=%s code=%s", reason, sub.user.email, e.code)


def _fmt_local_date(dt) -> str:
    return timezone.localdate(dt).isoformat() if dt else "-"


def payment_success_email(sub, payment) -> None:
    """결제 완료 안내 메일 enqueue (best-effort — 실패해도 결제 처리에 영향 없음)."""
    try:
        from apps.emails.tasks import send_payment_success_email

        card = f"{sub.card_company} {sub.card_number_masked}".strip()
        ctx = {
            "plan_name": sub.plan.display_name,
            "amount_str": f"{payment.amount:,}",
            "paid_date": _fmt_local_date(payment.paid_at or timezone.now()),
            "card_info": card or "등록된 카드",
            "next_billing_date": _fmt_local_date(sub.current_period_end),
        }
        send_payment_success_email.delay(sub.user_id, ctx)
    except Exception:  # noqa: BLE001 - 메일 enqueue 실패가 결제 확정을 막으면 안 됨
        logger.exception("결제 완료 메일 enqueue 실패 user_id=%s", getattr(sub, "user_id", "?"))


def payment_failed_email(sub, payment, code: str, message: str) -> None:
    """결제 실패 안내 메일 enqueue (best-effort)."""
    try:
        from apps.emails.tasks import send_payment_failed_email

        grace_end = (
            sub.current_period_end + timedelta(days=GRACE_PERIOD_DAYS)
            if sub.current_period_end
            else None
        )
        ctx = {
            "plan_name": (sub.pending_plan or sub.plan).display_name,
            "amount_str": f"{payment.amount:,}",
            "failure_reason": message or code or "결제 승인에 실패했습니다",
            "grace_end_date": _fmt_local_date(grace_end),
        }
        send_payment_failed_email.delay(sub.user_id, ctx)
    except Exception:  # noqa: BLE001
        logger.exception("결제 실패 메일 enqueue 실패 user_id=%s", getattr(sub, "user_id", "?"))


# ──────────────────────────────────────────────
# 1) 갱신 디스패처
# ──────────────────────────────────────────────


@shared_task(name="billing.process_due_renewals")
def process_due_renewals():
    """갱신 도래 구독을 건별 과금 태스크로 분산 (10분 주기).

    대상 (모두 빌링키 보유 필수):
    - ACTIVE/TRIALING + current_period_end 경과  → 정기 갱신 / 트라이얼 첫 과금
    - PAST_DUE + next_billing_retry_at 경과      → dunning 재시도
    CANCELLED 은 절대 포함하지 않는다. admin/free 는 빌링키가 없어 자연 제외.
    """
    from django.db.models import Q

    from .models import SubscriptionStatus, UserSubscription

    now = timezone.now()
    due_ids = list(
        UserSubscription.objects.filter(
            Q(
                status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING],
                current_period_end__lte=now,
            )
            | Q(
                status=SubscriptionStatus.PAST_DUE,
                next_billing_retry_at__isnull=False,
                next_billing_retry_at__lte=now,
            )
        )
        .exclude(_encrypted_toss_billing_key="")
        .exclude(current_period_end__isnull=True)
        .values_list("id", flat=True)[:500]
    )

    for sub_id in due_ids:
        charge_subscription_renewal.delay(str(sub_id))

    if due_ids:
        logger.info("process_due_renewals: %d건 디스패치", len(due_ids))
    return {"dispatched": len(due_ids)}


# ──────────────────────────────────────────────
# 2) 건별 갱신 과금
# ──────────────────────────────────────────────


def _renewal_amount_for(sub) -> tuple:
    """(target_plan, amount) — 예약 플랜이 있으면 그 기준. 추가 계정은 pro만 가산.

    추가 계정 '축소'가 예약돼 있으면(pending_extra_ig_accounts) 이번 청구부터 그 값으로
    가산 — 축소가 다음 갱신액에 반영되게 한다. (charge 전에 호출되므로 여기서 반영 필수)
    """
    from .models import EXTRA_IG_ACCOUNT_PRICE

    target_plan = sub.pending_plan or sub.plan
    base = sub.pending_amount_snapshot if sub.pending_plan_id else sub.monthly_amount_snapshot
    if base is None:
        base = target_plan.monthly_price
    amount = base
    if target_plan.name == "pro":
        extra = (
            sub.pending_extra_ig_accounts
            if sub.pending_extra_ig_accounts is not None
            else sub.extra_ig_accounts
        )
        amount += EXTRA_IG_ACCOUNT_PRICE * extra
    return target_plan, amount


def _finalize_renewal_success(sub_id, payment_id, toss_payment: dict) -> None:
    """갱신 승인 성공 반영 — 멱등 (charge 태스크 / 웹훅 / reconcile 이 공유)."""
    from .models import PaymentHistory, PaymentStatus, SubscriptionStatus, UserSubscription
    from .toss_flows import PERIOD_DAYS, apply_payment_success_fields, mark_converted_to_paid

    now = timezone.now()
    did_apply = False
    with transaction.atomic():
        # of=("self",): pending_plan 이 nullable FK(OUTER JOIN)라 조인행 락 불가 —
        # 구독 행만 잠근다.
        sub = (
            UserSubscription.objects.select_for_update(of=("self",))
            .select_related("plan", "pending_plan", "user")
            .get(id=sub_id)
        )
        payment = PaymentHistory.objects.select_for_update().get(pk=payment_id)

        if payment.status != PaymentStatus.PAID:
            apply_payment_success_fields(payment, toss_payment)
            payment.save()

        # 기간 연장이 이미 반영됐는지(중복 호출) 판별: 이 주문의 주기가 이미 지났으면 skip
        was_trialing = sub.status == SubscriptionStatus.TRIALING
        old_end = sub.current_period_end
        order_period = payment.toss_order_id.split("-")[2] if payment.toss_order_id else ""
        already_applied = (
            old_end is not None
            and _RENEWAL_ORDER_RE.match(payment.toss_order_id or "")
            and old_end.strftime("%Y%m%d") != order_period
        )
        if not already_applied:
            # 결제가 기간 만료 직후(3일 내)면 만료일부터 이어붙여 사용자 손해 없음.
            # 오래 지난 뒤(장기 dunning) 성공이면 오늘부터 30일.
            start = old_end if old_end and (now - old_end) <= timedelta(days=3) else now
            sub.current_period_start = start
            sub.current_period_end = start + timedelta(days=PERIOD_DAYS)
            sub.status = SubscriptionStatus.ACTIVE
            if sub.pending_plan_id:
                sub.plan = sub.pending_plan
                sub.monthly_amount_snapshot = sub.pending_amount_snapshot
                sub.pending_plan = None
                sub.pending_amount_snapshot = None
                if sub.plan.name != "pro":
                    sub.extra_ig_accounts = 0
                    sub.pending_extra_ig_accounts = None  # pro 이탈 → 추가계정 개념 소멸
            # 추가 계정 축소 예약 확정 (pro 유지 시에만 의미)
            if sub.pending_extra_ig_accounts is not None:
                if sub.plan.name == "pro":
                    sub.extra_ig_accounts = sub.pending_extra_ig_accounts
                sub.pending_extra_ig_accounts = None
            sub.renewal_attempts = 0
            sub.next_billing_retry_at = None
            sub.last_billing_error = ""
            if not sub.pro_activated_at:
                sub.pro_activated_at = now
            sub.save()
            did_apply = True

    if was_trialing:
        mark_converted_to_paid(sub.user, now)
    logger.info(
        "구독 갱신 성공: user=%s plan=%s amount=%d order=%s",
        sub.user.email,
        sub.plan.name,
        payment.amount,
        payment.toss_order_id,
    )
    # 실제로 기간이 연장된 경우에만 1회 안내 (웹훅/reconcile 중복 호출 시 재발송 방지)
    if did_apply:
        payment_success_email(sub, payment)
        # 허용량이 줄었을 수 있으니 활성 IG 계정 초과분을 자동 비활성 (갱신 트랜잭션 밖).
        _enforce_ig_activation_after_renewal(sub)


def _enforce_ig_activation_after_renewal(sub) -> None:
    """갱신 후 활성 IG 계정이 허용량을 초과하면 오래된 순으로 유지하고 초과분을 자동 비활성.

    갱신 트랜잭션 커밋 후 별도로 수행 — 여기서 실패해도 갱신 자체는 유지되며,
    GET /billing/ig-account-activation/ 이 라이브로 재계산하므로 방어가 이중화된다.
    비활성 처리 시 해당 계정의 활성 캠페인은 PAUSE, in-flight DM 은 SKIPPED 된다.
    """
    from apps.integrations.models import IGAccountConnection

    from .models import UserSubscription
    from .subscription_utils import get_ig_account_allowance

    try:
        allowance = get_ig_account_allowance(sub.user)
    except Exception:
        logger.exception("IG 활성 계정 허용량 계산 실패: user=%s", getattr(sub.user, "email", "?"))
        return
    if allowance is None or allowance < 0:
        return  # 무제한(관리자/무제한 플랜)

    active = list(
        IGAccountConnection.objects.filter(
            workspace__owner=sub.user,
            status=IGAccountConnection.Status.ACTIVE,
            is_active=True,
        ).order_by("created_at")
    )
    if len(active) <= allowance:
        return

    # 오래된 순으로 allowance 개 유지, 나머지 소프트 비활성.
    excess = active[allowance:]
    for conn in excess:
        try:
            conn.deactivate(reason="plan_downgrade_renewal")
        except Exception:
            logger.exception("IG 계정 자동 비활성 실패: conn=%s", conn.id)

    with transaction.atomic():
        locked = UserSubscription.objects.select_for_update().get(pk=sub.pk)
        locked.ig_activation_review_needed = True
        locked.save(update_fields=["ig_activation_review_needed", "updated_at"])
    logger.info(
        "갱신 후 IG 활성 계정 자동 조정: user=%s allowance=%d 비활성=%d",
        sub.user.email,
        allowance,
        len(excess),
    )


def _register_renewal_failure(sub_id, payment_id, code: str, message: str) -> None:
    """갱신 승인 거절 반영 — 실패 마킹 + dunning 스케줄 (단일 트랜잭션)."""
    from .models import PaymentHistory, PaymentStatus, SubscriptionStatus, UserSubscription

    now = timezone.now()
    with transaction.atomic():
        sub = (
            UserSubscription.objects.select_for_update(of=("self",))
            .select_related("user")
            .get(id=sub_id)
        )
        payment = PaymentHistory.objects.select_for_update().get(pk=payment_id)
        if payment.status == PaymentStatus.PAID:
            return  # 레이스 방어 — 이미 성공 확정됐으면 실패 등록 금지
        payment.status = PaymentStatus.FAILED
        payment.failure_code = code[:64]
        payment.failure_message = message[:200]
        payment.save(update_fields=["status", "failure_code", "failure_message"])

        sub.renewal_attempts += 1
        sub.status = SubscriptionStatus.PAST_DUE
        sub.last_billing_error = f"{code}: {message}"[:200]
        if sub.renewal_attempts <= MAX_RENEWAL_ATTEMPTS:
            offset = DUNNING_RETRY_OFFSET_DAYS.get(sub.renewal_attempts, 5)
            base = sub.current_period_end or now
            retry_at = base + timedelta(days=offset)
            if retry_at <= now:
                retry_at = now + timedelta(hours=6)
            sub.next_billing_retry_at = retry_at
        else:
            sub.next_billing_retry_at = None  # 소진 — handle_grace_period_expiry 가 D+7 처리
        sub.save(
            update_fields=[
                "renewal_attempts",
                "status",
                "last_billing_error",
                "next_billing_retry_at",
                "updated_at",
            ]
        )

    logger.warning(
        "구독 갱신 거절: user=%s attempt=%d code=%s retry_at=%s",
        sub.user.email,
        sub.renewal_attempts,
        code,
        sub.next_billing_retry_at.isoformat() if sub.next_billing_retry_at else "-",
    )
    if sub.renewal_attempts == 1:
        _ops_alert(
            f"💳 구독 갱신 결제 실패\n- user: {sub.user.email}\n- 사유: {code}\n"
            f"- 재시도: {sub.next_billing_retry_at:%Y-%m-%d %H:%M}"
        )
        # 고객 안내는 첫 실패 시 1회 (유예 7일·카드 변경 안내 포함)
        payment_failed_email(sub, payment, code, message)


@shared_task(name="billing.charge_subscription_renewal", bind=True, max_retries=3)
def charge_subscription_renewal(self, sub_id: str):
    """구독 1건 갱신 과금. 디스패처/카드변경 후 재시도 트리거가 호출."""
    from .models import PaymentHistory, PaymentStatus, SubscriptionStatus, UserSubscription
    from .toss_flows import renewal_order_id
    from .toss_service import TossBillingClient, TossError, TossNetworkError

    now = timezone.now()

    # ── TXN 1: 락 + due 재검증 + 주문 소유권 획득 (외부 호출 전, 짧게) ──
    with transaction.atomic():
        # of=("self",): pending_plan 이 nullable FK(OUTER JOIN)라 조인행 락 불가.
        sub = (
            UserSubscription.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("plan", "pending_plan", "user")
            .filter(id=sub_id)
            .first()
        )
        if sub is None:
            return {"result": "locked_or_missing"}

        due = (
            sub.has_billing_key
            and sub.current_period_end is not None
            and (
                (
                    sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
                    and sub.current_period_end <= now
                )
                or (
                    sub.status == SubscriptionStatus.PAST_DUE
                    and sub.next_billing_retry_at is not None
                    and sub.next_billing_retry_at <= now
                )
            )
        )
        if not due:
            return {"result": "not_due"}

        target_plan, amount = _renewal_amount_for(sub)
        attempt = sub.renewal_attempts
        order_id = renewal_order_id(sub, sub.current_period_end, attempt)
        description = f"턴플로우 {target_plan.display_name} 월간 구독 갱신"

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
                return {"result": "already_paid"}
            if payment.status == PaymentStatus.PENDING:
                # 다른 워커 진행 중이거나 모호 상태 — reconcile 소관
                return {"result": "in_progress"}
            # FAILED 인데 attempts 미증가 — _register_renewal_failure 가 원자적이라
            # 정상 흐름에선 도달 불가. 방어적으로 실패 등록을 재수행.
            logger.error("charge_renewal: FAILED 주문 재조우 (dunning 미등록?) order=%s", order_id)
            failure = (payment.failure_code or "UNKNOWN", payment.failure_message or "")
        else:
            failure = None

        billing_key = sub.toss_billing_key
        customer_key = sub.toss_customer_key
        user_email = sub.user.email
        idempotency_key = payment.toss_idempotency_key
        payment_id = payment.pk

    if failure is not None:
        _register_renewal_failure(sub_id, payment_id, failure[0], failure[1])
        return {"result": "failure_reregistered"}

    # ── 외부 호출 (락 밖) ──
    try:
        result = TossBillingClient.charge(
            billing_key=billing_key,
            customer_key=customer_key,
            amount=amount,
            order_id=order_id,
            order_name=description,
            idempotency_key=idempotency_key,
            customer_email=user_email,
        )
    except TossNetworkError as exc:
        # 모호 — PENDING 유지, 동일 멱등키로 Celery 재시도. 소진 시 reconcile 이 확정.
        logger.warning(
            "갱신 과금 모호(재시도 %d/%d): order=%s", self.request.retries + 1, 3, order_id
        )
        try:
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        except self.MaxRetriesExceededError:
            return {"result": "pending_ambiguous"}
    except TossError as e:
        _register_renewal_failure(sub_id, payment_id, e.code, e.message)
        return {"result": "declined", "code": e.code}

    _finalize_renewal_success(sub_id, payment_id, result)
    return {"result": "paid"}


# ──────────────────────────────────────────────
# 3) 모호 결제 확정 (reconcile)
# ──────────────────────────────────────────────


@shared_task(name="billing.reconcile_pending_payments")
def reconcile_pending_payments():
    """30분 이상 PENDING 인 결제를 토스 조회로 확정 (워커 크래시/모호 실패 안전망)."""
    from .models import PaymentHistory, PaymentStatus
    from .toss_service import TossBillingClient, TossError, TossNetworkError

    cutoff = timezone.now() - timedelta(minutes=PENDING_RECONCILE_AFTER_MINUTES)
    stale = list(
        PaymentHistory.objects.filter(
            status=PaymentStatus.PENDING,
            created_at__lt=cutoff,
            toss_order_id__isnull=False,
        ).select_related("subscription")[:200]
    )

    processed, failed = 0, 0
    for payment in stale:
        try:
            try:
                data = TossBillingClient.get_payment_by_order_id(payment.toss_order_id)
            except TossError as e:
                if e.http_status == 404:
                    # 토스에 주문이 없음 = 승인 요청이 도달하지 않음 → 실패 확정
                    _reconcile_mark_failed(payment, "NOT_REACHED", "토스 미도달 (재시도 예정)")
                    processed += 1
                else:
                    logger.warning(
                        "reconcile: 조회 오류 order=%s code=%s", payment.toss_order_id, e.code
                    )
                    failed += 1
                continue
            except TossNetworkError:
                failed += 1
                continue

            toss_status = data.get("status", "")
            if toss_status == "DONE":
                _reconcile_confirm_paid(payment, data)
                processed += 1
            elif toss_status in ("CANCELED",):
                from .toss_flows import apply_refund

                # 확정 전 취소됨 — paid 로 만들었다 환불하는 대신 바로 환불 반영
                payment.status = PaymentStatus.PAID  # apply_refund 의 상태 가드 통과용
                payment.toss_payment_key = payment.toss_payment_key or data.get("paymentKey")
                payment.save(update_fields=["status", "toss_payment_key"])
                apply_refund(payment, downgrade=True, reason="reconcile_canceled")
                processed += 1
            elif toss_status in ("ABORTED", "EXPIRED"):
                _reconcile_mark_failed(payment, toss_status, "토스 승인 실패 확정")
                processed += 1
            # READY/IN_PROGRESS 등은 다음 주기에 재확인
        except Exception:
            failed += 1
            logger.exception("reconcile: payment=%s 처리 오류", payment.pk)

    _log_summary("reconcile_pending_payments", processed, failed)
    return {"processed": processed, "failed": failed}


def _reconcile_confirm_paid(payment, toss_payment: dict) -> None:
    """PENDING → 성공 확정. 갱신 주문은 구독 연장까지, 그 외는 운영 알림."""
    from .toss_flows import apply_payment_success_fields

    if _RENEWAL_ORDER_RE.match(payment.toss_order_id or "") and payment.subscription_id:
        _finalize_renewal_success(payment.subscription_id, payment.pk, toss_payment)
        return

    # 비례(업그레이드/추가계정) 주문 — PENDING→DONE 확정 시 구독 상태를 자동 반영.
    # orderId 에 대상(플랜·계정수)이 담겨 있어 재적용이 결정적·멱등이다.
    prorate = _match_prorate_order(payment.toss_order_id or "")
    if prorate and payment.subscription_id:
        apply_payment_success_fields(payment, toss_payment)
        payment.save()
        _finalize_prorate_success(payment.subscription_id, prorate)
        logger.info(
            "reconcile: 비례 주문 뒤늦게 확정 — 구독 자동 반영 order=%s", payment.toss_order_id
        )
        return

    # init(신규구독 첫 결제) 주문 — confirm 플로우가 중단된 극히 드문 케이스. 결제만 확정하고
    # 구독 상태(플랜/기간/슬롯)는 수동 확인 필요 → 운영 알림.
    apply_payment_success_fields(payment, toss_payment)
    payment.save()
    logger.error(
        "reconcile: 1회성 주문 뒤늦게 성공 확정 — 구독 상태 수동 확인 필요 order=%s user=%s",
        payment.toss_order_id,
        payment.user.email,
    )
    _ops_alert(
        f"⚠️ 결제 수동 확인 필요 (confirm 중단 후 승인 확정)\n"
        f"- user: {payment.user.email}\n- order: {payment.toss_order_id}\n"
        f"- amount: {payment.amount}원\n→ 구독 플랜/기간 반영 여부 확인 필요"
    )


def _reconcile_mark_failed(payment, code: str, message: str) -> None:
    """PENDING → 실패 확정. 갱신 주문이면 dunning 도 등록."""
    if _RENEWAL_ORDER_RE.match(payment.toss_order_id or "") and payment.subscription_id:
        _register_renewal_failure(payment.subscription_id, payment.pk, code, message)
        return

    from .models import PaymentStatus

    payment.status = PaymentStatus.FAILED
    payment.failure_code = code[:64]
    payment.failure_message = message[:200]
    payment.save(update_fields=["status", "failure_code", "failure_message"])


# ──────────────────────────────────────────────
# 4) 토스 웹훅 처리
# ──────────────────────────────────────────────


@shared_task(name="billing.process_toss_webhook", bind=True, max_retries=3, default_retry_delay=60)
def process_toss_webhook(self, log_id: str):
    """웹훅 이벤트 처리. 본문을 신뢰하지 않고 paymentKey 재조회로 검증한다."""
    from .models import TossWebhookLog
    from .toss_service import TossNetworkError

    log = TossWebhookLog.objects.filter(id=log_id).first()
    if log is None or log.processed:
        return {"result": "skipped"}

    try:
        if log.event_type == "BILLING_DELETED":
            _webhook_billing_deleted(log)
        elif log.event_type in ("PAYMENT_STATUS_CHANGED", "CANCEL_STATUS_CHANGED"):
            _webhook_payment_event(log)
        else:
            logger.info("토스 웹훅 미처리 이벤트 (기록만): %s", log.event_type)
    except TossNetworkError as exc:
        raise self.retry(exc=exc) from exc
    except Exception as e:
        log.process_error = str(e)[:2000]
        log.save(update_fields=["process_error"])
        logger.exception("토스 웹훅 처리 오류: log=%s type=%s", log_id, log.event_type)
        raise self.retry(exc=e) from e

    log.processed = True
    log.save(update_fields=["processed"])
    return {"result": "processed", "event": log.event_type}


def _webhook_payment_event(log) -> None:
    """PAYMENT_STATUS_CHANGED / CANCEL_STATUS_CHANGED — 재조회 검증 후 반영."""
    from .models import PaymentHistory, PaymentStatus
    from .toss_flows import apply_refund
    from .toss_service import TossBillingClient, TossError

    payment_key = log.payment_key
    if not payment_key:
        logger.info("토스 웹훅 paymentKey 없음 (기록만): %s", log.event_type)
        return

    try:
        data = TossBillingClient.get_payment(payment_key)
    except TossError as e:
        # 조회 불가 = 위조 본문이거나 남의 결제 — 무시
        logger.warning("토스 웹훅 재조회 실패(무시): key=%s... code=%s", payment_key[:12], e.code)
        return

    order_id = data.get("orderId", "")
    payment = (
        PaymentHistory.objects.filter(toss_payment_key=payment_key).first()
        or PaymentHistory.objects.filter(toss_order_id=order_id).first()
    )
    if payment is None:
        logger.info("토스 웹훅: 우리 주문 아님 (무시) order=%s", order_id)
        return

    toss_status = data.get("status", "")

    if toss_status == "DONE" and payment.status == PaymentStatus.PENDING:
        _reconcile_confirm_paid(payment, data)
    elif toss_status == "CANCELED":
        if payment.status == PaymentStatus.PENDING:
            payment.status = PaymentStatus.PAID  # 환불 가드 통과용 중간 확정
            payment.toss_payment_key = payment.toss_payment_key or payment_key
            payment.save(update_fields=["status", "toss_payment_key"])
        apply_refund(payment, downgrade=True, reason="webhook_cancel")
    elif toss_status == "PARTIAL_CANCELED":
        _record_partial_cancels(payment, data)
    elif toss_status in ("ABORTED", "EXPIRED") and payment.status == PaymentStatus.PENDING:
        _reconcile_mark_failed(payment, toss_status, "토스 웹훅 실패 통보")


def _record_partial_cancels(payment, toss_payment: dict) -> None:
    """부분취소 — 취소 트랜잭션별 음수 금액 행 기록 (멱등: transactionKey 로 dedup)."""
    from .models import PaymentHistory, PaymentStatus

    for cancel in toss_payment.get("cancels") or []:
        tx_key = cancel.get("transactionKey", "")
        if not tx_key:
            continue
        order_id = f"cancel-{tx_key[:56]}"
        PaymentHistory.objects.get_or_create(
            toss_order_id=order_id,
            defaults={
                "user": payment.user,
                "subscription": payment.subscription,
                "amount": -int(cancel.get("cancelAmount", 0)),
                "status": PaymentStatus.REFUNDED,
                "payment_method": payment.payment_method,
                "description": f"부분 환불 (원거래: {payment.toss_order_id})",
                "receipt_url": payment.receipt_url,
                "paid_at": timezone.now(),
            },
        )
    logger.info("부분취소 반영: order=%s", payment.toss_order_id)


def _webhook_billing_deleted(log) -> None:
    """BILLING_DELETED — 빌링키 무효화. 해시로 구독을 찾아 키 제거 + 갱신 중단."""
    from .models import SubscriptionStatus, UserSubscription

    data = (log.raw_data or {}).get("data") or {}
    hashed = str(data.get("billingKey", ""))
    if hashed.startswith("sha256:"):
        hashed = hashed[len("sha256:") :]
    if not hashed:
        return

    sub = UserSubscription.objects.filter(toss_billing_key_hash=hashed).first()
    if sub is None:
        logger.info("BILLING_DELETED: 매칭 구독 없음 (이미 교체/삭제됨)")
        return

    with transaction.atomic():
        locked = UserSubscription.objects.select_for_update().get(pk=sub.pk)
        locked.clear_billing_key()
        update_fields = [
            "_encrypted_toss_billing_key",
            "toss_billing_key_hash",
            "billing_key_issued_at",
            "card_company",
            "card_number_masked",
            "updated_at",
        ]
        if locked.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
            locked.status = SubscriptionStatus.CANCELLED
            locked.cancelled_at = timezone.now()
            update_fields += ["status", "cancelled_at"]
        locked.next_billing_retry_at = None
        update_fields.append("next_billing_retry_at")
        locked.save(update_fields=update_fields)

    logger.warning(
        "BILLING_DELETED 처리: user=%s → 빌링키 제거, 갱신 중단 (기간말까지 이용)",
        sub.user.email,
    )


# ──────────────────────────────────────────────
# 5) 갱신 파이프라인 감시
# ──────────────────────────────────────────────


@shared_task(name="billing.check_missed_payments")
def check_missed_payments():
    """갱신 파이프라인 고장 감지 — 기간이 6시간 이상 지났는데 상태 전이가 없는
    좀비 구독을 ERROR 로그로 노출 (beat/워커/디스패처 장애 시그널).

    실제 PAST_DUE 전환은 charge_subscription_renewal 이 거절 시점에 직접 수행한다.
    """
    from .models import SubscriptionStatus, UserSubscription

    cutoff = timezone.now() - timedelta(hours=6)
    zombies = list(
        UserSubscription.objects.filter(
            status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING],
            current_period_end__lt=cutoff,
        )
        .exclude(_encrypted_toss_billing_key="")
        .values_list("user__email", flat=True)[:50]
    )
    if zombies:
        logger.error(
            "check_missed_payments: 갱신 미처리 좀비 구독 %d건 — 갱신 파이프라인 점검 필요: %s",
            len(zombies),
            ", ".join(zombies[:10]),
        )
        _ops_alert(
            f"🧟 갱신 미처리 구독 {len(zombies)}건 — process_due_renewals 파이프라인 점검 필요"
        )
    return len(zombies)


# ──────────────────────────────────────────────
# 6) 유예/트라이얼/해지 만료 처리
# ──────────────────────────────────────────────


@shared_task(name="billing.handle_grace_period_expiry")
def handle_grace_period_expiry():
    """past_due 상태가 GRACE_PERIOD_DAYS일 이상 경과한 구독을 무료로 다운그레이드."""
    from .models import SubscriptionStatus, UserSubscription
    from .subscription_utils import get_free_plan

    cutoff = timezone.now() - timedelta(days=GRACE_PERIOD_DAYS)
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_grace_period_expiry: free 플랜이 존재하지 않음")
        return {"processed": 0, "failed": 0}

    expired_subs = UserSubscription.objects.filter(
        status=SubscriptionStatus.PAST_DUE,
        current_period_end__lt=cutoff,
    ).select_related("user", "plan")

    processed, failed = 0, 0
    for sub in expired_subs.iterator():
        try:
            _safe_delete_billing_key(sub, "grace_period")
            with transaction.atomic():
                _downgrade_to_free(sub, free_plan, reason="grace_period")
            processed += 1
        except Exception:
            failed += 1
            logger.exception("grace_period: sub=%s 처리 중 예기치 못한 오류", sub.id)

    _log_summary("handle_grace_period_expiry", processed, failed)
    return {"processed": processed, "failed": failed}


@shared_task(name="billing.handle_trial_expiry")
def handle_trial_expiry():
    """빌링키 없는(무카드 레퍼럴) 트라이얼 만료 → 무료 다운그레이드.

    카드 등록 트라이얼(빌링키 보유)은 process_due_renewals 가 첫 과금을 수행하므로
    여기서 절대 다운그레이드하면 안 된다.
    """
    from .models import SubscriptionStatus, UserSubscription
    from .subscription_utils import get_free_plan

    now = timezone.now()
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_trial_expiry: free 플랜이 존재하지 않음")
        return {"processed": 0, "failed": 0}

    expired_subs = (
        UserSubscription.objects.filter(
            status=SubscriptionStatus.TRIALING,
            current_period_end__lt=now,
            _encrypted_toss_billing_key="",  # 빌링키 보유 트라이얼은 과금 대상 — 제외
        )
        .exclude(current_period_end__isnull=True)
        .select_related("user", "plan")
    )

    processed, failed = 0, 0
    for sub in expired_subs.iterator():
        try:
            with transaction.atomic():
                _downgrade_to_free(sub, free_plan, reason="trial_expired")
            processed += 1
        except Exception:
            failed += 1
            logger.exception("trial_expired: sub=%s 처리 중 예기치 못한 오류", sub.id)

    _log_summary("handle_trial_expiry", processed, failed)
    return {"processed": processed, "failed": failed}


@shared_task(name="billing.handle_cancelled_expiry")
def handle_cancelled_expiry():
    """cancelled(해지 예약) 구독의 기간 만료 → 무료 다운그레이드 + 빌링키 삭제."""
    from .models import SubscriptionStatus, UserSubscription
    from .subscription_utils import get_free_plan

    now = timezone.now()
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_cancelled_expiry: free 플랜이 존재하지 않음")
        return {"processed": 0, "failed": 0}

    expired_subs = (
        UserSubscription.objects.filter(
            status=SubscriptionStatus.CANCELLED,
            current_period_end__lt=now,
        )
        # admin 은 운영 DB 행에 가격이 있을 수 있어(예: 18,900) 가격 필터로는 못 거른다
        # — 이름으로 명시 제외. free 는 다운그레이드 대상 자체가 아님.
        .exclude(plan__name__in=["free", "admin"])
        .exclude(current_period_end__isnull=True)
        .select_related("user", "plan")
    )

    processed, failed = 0, 0
    for sub in expired_subs.iterator():
        try:
            _safe_delete_billing_key(sub, "cancelled_expiry")
            with transaction.atomic():
                _downgrade_to_free(sub, free_plan, reason="cancelled_expiry")
            processed += 1
        except Exception:
            failed += 1
            logger.exception("cancelled_expiry: sub=%s 처리 중 예기치 못한 오류", sub.id)

    _log_summary("handle_cancelled_expiry", processed, failed)
    return {"processed": processed, "failed": failed}


def _downgrade_to_free(sub, free_plan, reason: str = ""):
    """구독을 free 플랜으로 다운그레이드 + 페이지 축소 + 로고 복원.

    유지하는 것: trial_used_at(트라이얼 1인 1회 어뷰징 방어), toss_customer_key(유저 고정),
    커스텀 CSS(free 도 허용 — 지우면 데이터 파괴).
    """
    from apps.pages.models import Page

    old_plan = sub.plan.name

    sub.plan = free_plan
    sub.status = "active"  # SubscriptionStatus.ACTIVE
    sub.current_period_end = None
    sub.cancelled_at = timezone.now()
    sub.pro_activated_at = None
    sub.clear_billing_key()
    sub.monthly_amount_snapshot = None
    sub.extra_ig_accounts = 0
    sub.pending_plan = None
    sub.pending_amount_snapshot = None
    sub.pending_extra_ig_accounts = None
    sub.renewal_attempts = 0
    sub.next_billing_retry_at = None
    sub.last_billing_error = ""
    sub.save(
        update_fields=[
            "plan",
            "status",
            "current_period_end",
            "cancelled_at",
            "pro_activated_at",
            "_encrypted_toss_billing_key",
            "toss_billing_key_hash",
            "billing_key_issued_at",
            "card_company",
            "card_number_masked",
            "monthly_amount_snapshot",
            "extra_ig_accounts",
            "pending_plan",
            "pending_amount_snapshot",
            "pending_extra_ig_accounts",
            "renewal_attempts",
            "next_billing_retry_at",
            "last_billing_error",
            "updated_at",
        ]
    )

    # 페이지 비활성화: 가장 먼저 생성된 max_pages개만 활성, 나머지 비활성.
    # 불변식(is_public ⟹ is_active): 슬롯을 뺏는 초과분은 is_public 도 함께 내려
    # "슬롯 없는데 공개(서빙)되는" 페이지가 생기지 않게 한다. 재업그레이드 후엔 사용자가 직접 재공개.
    max_pages = free_plan.features.get("max_pages", 1)
    user_pages = Page.objects.filter(user=sub.user).order_by("created_at")
    active_ids = list(user_pages.values_list("id", flat=True)[:max_pages])
    if active_ids:
        Page.objects.filter(user=sub.user, id__in=active_ids).update(is_active=True)
        Page.objects.filter(user=sub.user).exclude(id__in=active_ids).update(
            is_active=False, is_public=False
        )

    # IG 계정 비활성화: 가장 먼저 연동된 max_ig_accounts개만 활성, 나머지 소프트 비활성.
    # (하드 연결해제가 아니라 is_active=False — 토큰/데이터 보존, 활성 캠페인은 PAUSE)
    from apps.integrations.models import IGAccountConnection

    max_ig = free_plan.features.get("max_ig_accounts", 1)
    if max_ig != -1:
        ig_active = list(
            IGAccountConnection.objects.filter(
                workspace__owner=sub.user,
                status=IGAccountConnection.Status.ACTIVE,
                is_active=True,
            ).order_by("created_at")
        )
        if len(ig_active) > max_ig:
            for conn in ig_active[max_ig:]:
                conn.deactivate(reason=f"downgrade_free:{reason}")
            sub.ig_activation_review_needed = True
            sub.save(update_fields=["ig_activation_review_needed", "updated_at"])

    # 로고 복원 (logoStyle: "hidden" → 제거) — 배지 제거는 basic+ 전용
    for page in user_pages:
        data = page.data or {}
        ds = data.get("design_settings", {})
        if ds.get("logoStyle") == "hidden":
            ds["logoStyle"] = "default"
            data["design_settings"] = ds
            page.data = data
            page.save(update_fields=["data", "updated_at"])

    logger.info(
        "%s: user=%s %s → free 다운그레이드",
        reason,
        sub.user.email,
        old_plan,
    )
