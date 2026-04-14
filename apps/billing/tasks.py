"""
Billing Celery tasks — 구독 결제 관련 배치 작업.

1. check_missed_payments   — 매일 02:00, past_due 구독의 미수금 확인
2. handle_grace_period_expiry — 매일 03:00, 유예 기간 만료 구독 다운그레이드
3. notify_expiring_subscriptions — 매일 09:00, 만료 임박 구독 안내
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
        # PayApp 정기결제 해지
        if sub.payapp_rebill_no:
            try:
                PayAppClient.cancel_rebill(sub.payapp_rebill_no)
            except PayAppError:
                logger.warning(
                    "grace_period: PayApp 정기결제 해지 실패 rebill_no=%s",
                    sub.payapp_rebill_no,
                )

        sub.plan = free_plan
        sub.status = SubscriptionStatus.ACTIVE
        sub.current_period_end = None
        sub.payapp_rebill_no = None
        sub.payapp_pay_url = None
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=[
            "plan", "status", "current_period_end",
            "payapp_rebill_no", "payapp_pay_url",
            "cancelled_at", "updated_at",
        ])
        count += 1
        logger.info(
            "grace_period: user=%s → free 다운그레이드", sub.user.email
        )

    return count
