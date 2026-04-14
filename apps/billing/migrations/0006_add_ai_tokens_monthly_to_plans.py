"""
Data migration: Add ai_tokens_monthly to SubscriptionPlan features
and reset existing token balances to match subscription plan.

구독 등급별 월 토큰: free=3, pro=100, pro_plus=500
"""

from django.db import migrations

PLAN_TOKENS = {
    "free": 3,
    "pro": 100,
    "pro_plus": 500,
}


def add_ai_tokens_to_plans(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")

    for plan in SubscriptionPlan.objects.all():
        monthly_tokens = PLAN_TOKENS.get(plan.name, 3)
        features = plan.features or {}
        features["ai_tokens_monthly"] = monthly_tokens
        plan.features = features
        plan.save(update_fields=["features"])


def reset_existing_balances(apps, schema_editor):
    """기존 사용자의 토큰 잔액을 구독 플랜 기준으로 리셋."""
    AiTokenBalance = apps.get_model("billing", "AiTokenBalance")
    UserSubscription = apps.get_model("billing", "UserSubscription")

    for balance in AiTokenBalance.objects.select_related("user").all():
        sub = UserSubscription.objects.filter(user=balance.user).select_related("plan").first()
        if sub and sub.plan:
            monthly = PLAN_TOKENS.get(sub.plan.name, 3)
        else:
            monthly = 3
        balance.balance = monthly
        balance.save(update_fields=["balance"])


def reverse(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all():
        features = plan.features or {}
        features.pop("ai_tokens_monthly", None)
        plan.features = features
        plan.save(update_fields=["features"])


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0005_seed_initial_tokens"),
    ]

    operations = [
        migrations.RunPython(add_ai_tokens_to_plans, reverse),
        migrations.RunPython(reset_existing_balances, migrations.RunPython.noop),
    ]
