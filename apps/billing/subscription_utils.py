"""
Subscription utility functions for plan checking and enforcement.
"""

from django.utils import timezone


def get_free_plan():
    """Free plan 가져오기 (캐시 가능)"""
    from .models import SubscriptionPlan
    return SubscriptionPlan.objects.get(name="free")


def ensure_subscription(user):
    """
    User에 subscription이 없으면 Free plan으로 자동 생성.
    Returns UserSubscription.
    """
    from .models import UserSubscription

    sub = getattr(user, "subscription", None)
    if sub is None:
        try:
            sub = UserSubscription.objects.get(user=user)
        except UserSubscription.DoesNotExist:
            sub = UserSubscription.objects.create(
                user=user,
                plan=get_free_plan(),
                current_period_start=timezone.now(),
            )
    return sub


def get_user_plan(user):
    """
    User의 현재 SubscriptionPlan 반환.
    구독이 없으면 Free plan 반환.
    """
    sub = ensure_subscription(user)
    return sub.plan


def check_feature(user, feature_name):
    """
    User가 특정 기능을 사용할 수 있는지 확인.
    bool 타입 feature → 직접 반환
    int 타입 feature → True (사용 가능 여부만, 수량은 check_limit 사용)
    """
    plan = get_user_plan(user)
    value = plan.features.get(feature_name, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return bool(value)


def check_limit(user, feature_name, current_count):
    """
    수량 제한 확인.
    -1 → 무제한 (True)
    그 외 → current_count < limit 일 때 True
    """
    plan = get_user_plan(user)
    limit = plan.features.get(feature_name)
    if limit is None:
        return False
    if limit == -1:
        return True
    return current_count < limit
