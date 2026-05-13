"""
Billing Celery tasks — 구독 결제 관련 배치 작업.

스케줄(시간 단위로 자주 돌려, 단일 실행 누락/지연 위험을 줄임):
1. check_missed_payments        — 매시간, current_period_end 지난 active → past_due 전환
2. handle_grace_period_expiry   — 매시간, 유예 기간(GRACE_PERIOD_DAYS) 만료 구독 다운그레이드
3. handle_cancelled_expiry      — 매시간, 취소(일시정지) 구독 기간 만료 다운그레이드
4. handle_trial_expiry          — 매시간, 레퍼럴 트라이얼 만료 구독 다운그레이드

안정성 원칙:
- 개별 구독 처리를 try/except + transaction.atomic 으로 격리해 한 건 실패가 배치 전체를 막지 않도록 한다.
- PayApp rebillCancel 호출이 실패하면 다운그레이드를 **보류** 하고 `payapp_rebill_no` 를 유지한다.
  다음 배치 차수에서 같은 구독을 다시 시도해 PayApp 잔존 정기결제를 만들지 않는다.
- 실패 건수가 0보다 크면 ERROR 로그를 남겨 모니터링/알림에 노출되게 한다.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 7


def _safe_payapp_cancel(rebill_no: str, reason: str) -> bool:
    """
    PayApp 정기결제 완전 해지(rebillCancel) 시도.
    성공 시 True, 실패 시 False 리턴. 실패하면 호출 측은 다운그레이드를 **보류** 해야 한다.
    """
    from .payapp_service import PayAppClient, PayAppError

    try:
        PayAppClient.cancel_rebill(rebill_no)
        return True
    except PayAppError as e:
        logger.warning(
            "%s: PayApp rebillCancel 실패 rebill_no=%s errno=%s msg=%s",
            reason, rebill_no, getattr(e, "errno", ""), e,
        )
        return False


def _log_summary(task_name: str, processed: int, failed: int) -> None:
    if not processed and not failed:
        return
    log = logger.error if failed else logger.info
    log("%s: processed=%d failed=%d", task_name, processed, failed)


@shared_task(name="billing.check_missed_payments")
def check_missed_payments():
    """
    past_due 상태인 구독 확인.
    current_period_end가 지난 active 구독을 past_due로 전환.
    (PayApp failurl이 호출되지 않는 edge case 대비)
    """
    from .models import SubscriptionStatus, UserSubscription

    now = timezone.now()

    overdue = UserSubscription.objects.filter(
        status=SubscriptionStatus.ACTIVE,
        current_period_end__lt=now,
        plan__name__in=["pro", "pro_plus"],
    ).exclude(current_period_end__isnull=True)

    count = overdue.update(status=SubscriptionStatus.PAST_DUE)
    if count:
        logger.info("check_missed_payments: %d건 past_due 전환", count)
    return count


@shared_task(name="billing.handle_grace_period_expiry")
def handle_grace_period_expiry():
    """
    past_due 상태가 GRACE_PERIOD_DAYS일 이상 경과한 구독을 무료로 다운그레이드.
    """
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
    )

    processed, failed = 0, 0
    for sub in expired_subs.iterator():
        try:
            if sub.payapp_rebill_no and not _safe_payapp_cancel(
                sub.payapp_rebill_no, "grace_period"
            ):
                failed += 1
                continue

            with transaction.atomic():
                _downgrade_to_free(sub, free_plan, reason="grace_period")
            processed += 1
        except Exception:
            failed += 1
            logger.exception(
                "grace_period: sub=%s 처리 중 예기치 못한 오류", sub.id,
            )

    _log_summary("handle_grace_period_expiry", processed, failed)
    return {"processed": processed, "failed": failed}


@shared_task(name="billing.handle_trial_expiry")
def handle_trial_expiry():
    """
    TRIALING 상태이고 current_period_end가 지난 구독을 무료로 다운그레이드.
    레퍼럴 트라이얼이 만료된 사용자가 결제 안 했을 때 적용된다.
    트라이얼은 PayApp 정기결제와 연계가 없을 수 있어 rebill 해지 실패 시에도 다운그레이드는 진행한다
    (단, 실패는 로그로 노출).
    """
    from .models import SubscriptionStatus, UserSubscription
    from .subscription_utils import get_free_plan

    now = timezone.now()
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_trial_expiry: free 플랜이 존재하지 않음")
        return {"processed": 0, "failed": 0}

    expired_subs = UserSubscription.objects.filter(
        status=SubscriptionStatus.TRIALING,
        current_period_end__lt=now,
    ).exclude(current_period_end__isnull=True)

    processed, failed = 0, 0
    for sub in expired_subs.iterator():
        try:
            if sub.payapp_rebill_no:
                _safe_payapp_cancel(sub.payapp_rebill_no, "trial_expired")

            with transaction.atomic():
                _downgrade_to_free(sub, free_plan, reason="trial_expired")
            processed += 1
        except Exception:
            failed += 1
            logger.exception(
                "trial_expired: sub=%s 처리 중 예기치 못한 오류", sub.id,
            )

    _log_summary("handle_trial_expiry", processed, failed)
    return {"processed": processed, "failed": failed}


@shared_task(name="billing.handle_cancelled_expiry")
def handle_cancelled_expiry():
    """
    cancelled(일시정지) 상태이고 current_period_end가 지난 구독을 무료로 다운그레이드.
    rebillStop으로 일시정지된 상태에서 구독 기간이 끝나면 free로 전환.

    PayApp rebillCancel 실패 시:
      - 다운그레이드를 **보류** 하고 payapp_rebill_no를 유지한다.
      - 다음 배치 차수(매시간)에 다시 시도해 PayApp 측에 dangling 정기결제가 남지 않게 한다.
    """
    from .models import SubscriptionStatus, UserSubscription
    from .subscription_utils import get_free_plan

    now = timezone.now()
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_cancelled_expiry: free 플랜이 존재하지 않음")
        return {"processed": 0, "failed": 0}

    expired_subs = UserSubscription.objects.filter(
        status=SubscriptionStatus.CANCELLED,
        current_period_end__lt=now,
        plan__name__in=["pro", "pro_plus"],
    ).exclude(current_period_end__isnull=True)

    processed, failed = 0, 0
    for sub in expired_subs.iterator():
        try:
            if sub.payapp_rebill_no and not _safe_payapp_cancel(
                sub.payapp_rebill_no, "cancelled_expiry"
            ):
                failed += 1
                continue

            with transaction.atomic():
                _downgrade_to_free(sub, free_plan, reason="cancelled_expiry")
            processed += 1
        except Exception:
            failed += 1
            logger.exception(
                "cancelled_expiry: sub=%s 처리 중 예기치 못한 오류", sub.id,
            )

    _log_summary("handle_cancelled_expiry", processed, failed)
    return {"processed": processed, "failed": failed}


def _downgrade_to_free(sub, free_plan, reason: str = ""):
    """구독을 free 플랜으로 다운그레이드 + 페이지 비활성화 + 로고 복원."""
    from apps.pages.models import Page

    old_plan = sub.plan.name

    sub.plan = free_plan
    sub.status = "active"  # SubscriptionStatus.ACTIVE
    sub.current_period_end = None
    sub.payapp_rebill_no = None
    sub.payapp_pay_url = None
    sub.cancelled_at = timezone.now()
    sub.pro_activated_at = None
    sub.save(update_fields=[
        "plan", "status", "current_period_end",
        "payapp_rebill_no", "payapp_pay_url",
        "cancelled_at", "pro_activated_at", "updated_at",
    ])

    # 페이지 비활성화: 가장 먼저 생성된 1개만 활성, 나머지 비활성
    max_pages = free_plan.features.get("max_pages", 1)
    user_pages = Page.objects.filter(user=sub.user).order_by("created_at")
    active_ids = list(user_pages.values_list("id", flat=True)[:max_pages])
    if active_ids:
        Page.objects.filter(user=sub.user, id__in=active_ids).update(is_active=True)
        Page.objects.filter(user=sub.user).exclude(id__in=active_ids).update(is_active=False)

    # 로고 복원 (logoStyle: "hidden" → 제거)
    for page in user_pages:
        data = page.data or {}
        ds = data.get("design_settings", {})
        if ds.get("logoStyle") == "hidden":
            ds["logoStyle"] = "default"
            data["design_settings"] = ds
            page.data = data
            page.save(update_fields=["data", "updated_at"])

    # 커스텀 CSS 초기화
    user_pages.exclude(custom_css="").update(custom_css="", updated_at=timezone.now())

    logger.info(
        "%s: user=%s %s → free 다운그레이드", reason, sub.user.email, old_plan,
    )
