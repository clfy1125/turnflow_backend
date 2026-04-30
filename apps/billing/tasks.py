"""
Billing Celery tasks — 구독 결제 관련 배치 작업.

1. check_missed_payments   — 매일 02:00, past_due 구독의 미수금 확인
2. handle_grace_period_expiry — 매일 03:00, 유예 기간 만료 구독 다운그레이드
3. handle_cancelled_expiry — 매일 03:30, 취소(일시정지) 구독 기간 만료 다운그레이드
4. handle_trial_expiry     — 매일 04:00, 레퍼럴 트라이얼 만료 구독 다운그레이드
5. notify_expiring_subscriptions — 매일 09:00, 만료 임박 구독 안내
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 7


@shared_task(name="billing.check_missed_payments")
def check_missed_payments():
    """
    past_due 상태인 구독 확인.
    current_period_end가 지난 active 구독을 past_due로 전환.
    (PayApp failurl이 호출되지 않는 edge case 대비)
    """
    from .models import UserSubscription, SubscriptionStatus

    now = timezone.now()

    # current_period_end가 지났는데 여전히 active인 유료 구독
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
    from .models import UserSubscription, SubscriptionStatus
    from .subscription_utils import get_free_plan
    from .payapp_service import PayAppClient, PayAppError

    cutoff = timezone.now() - timedelta(days=GRACE_PERIOD_DAYS)
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_grace_period_expiry: free 플랜이 존재하지 않음")
        return 0

    expired_subs = UserSubscription.objects.filter(
        status=SubscriptionStatus.PAST_DUE,
        current_period_end__lt=cutoff,
    )

    count = 0
    for sub in expired_subs:
        # PayApp 정기결제 해지 (완전 해지)
        if sub.payapp_rebill_no:
            try:
                PayAppClient.cancel_rebill(sub.payapp_rebill_no)
            except PayAppError:
                logger.warning(
                    "grace_period: PayApp 정기결제 해지 실패 rebill_no=%s",
                    sub.payapp_rebill_no,
                )

        _downgrade_to_free(sub, free_plan, reason="grace_period")
        count += 1

    return count


@shared_task(name="billing.handle_trial_expiry")
def handle_trial_expiry():
    """
    TRIALING 상태이고 current_period_end가 지난 구독을 무료로 다운그레이드.
    레퍼럴 트라이얼이 만료된 사용자가 결제 안 했을 때 적용된다.
    """
    from .models import SubscriptionStatus, UserSubscription
    from .subscription_utils import get_free_plan

    now = timezone.now()
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_trial_expiry: free 플랜이 존재하지 않음")
        return 0

    expired_subs = UserSubscription.objects.filter(
        status=SubscriptionStatus.TRIALING,
        current_period_end__lt=now,
    ).exclude(current_period_end__isnull=True)

    count = 0
    for sub in expired_subs:
        _downgrade_to_free(sub, free_plan, reason="trial_expired")
        count += 1

    if count:
        logger.info("handle_trial_expiry: %d건 트라이얼 만료 → free 다운그레이드", count)
    return count


@shared_task(name="billing.handle_cancelled_expiry")
def handle_cancelled_expiry():
    """
    cancelled(일시정지) 상태이고 current_period_end가 지난 구독을 무료로 다운그레이드.
    rebillStop로 일시정지된 상태에서 구독 기간이 끝나면 free로 전환.
    """
    from .models import UserSubscription, SubscriptionStatus
    from .subscription_utils import get_free_plan
    from .payapp_service import PayAppClient, PayAppError

    now = timezone.now()
    free_plan = get_free_plan()
    if not free_plan:
        logger.error("handle_cancelled_expiry: free 플랜이 존재하지 않음")
        return 0

    expired_subs = UserSubscription.objects.filter(
        status=SubscriptionStatus.CANCELLED,
        current_period_end__lt=now,
        plan__name__in=["pro", "pro_plus"],
    ).exclude(current_period_end__isnull=True)

    count = 0
    for sub in expired_subs:
        # 완전 해지 (rebillCancel)
        if sub.payapp_rebill_no:
            try:
                PayAppClient.cancel_rebill(sub.payapp_rebill_no)
            except PayAppError:
                logger.warning(
                    "cancelled_expiry: PayApp 정기결제 해지 실패 rebill_no=%s",
                    sub.payapp_rebill_no,
                )

        _downgrade_to_free(sub, free_plan, reason="cancelled_expiry")
        count += 1

    return count


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
