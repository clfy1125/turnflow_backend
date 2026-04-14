"""
Data migration: Seed subscription plans and assign Free to existing users.
"""

from django.db import migrations


def seed_plans_and_subscriptions(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    UserSubscription = apps.get_model("billing", "UserSubscription")
    User = apps.get_model("authentication", "User")

    # Free plan
    free = SubscriptionPlan.objects.create(
        name="free",
        display_name="무료",
        monthly_price=0,
        yearly_price=0,
        features={
            "max_pages": 3,
            "ai_generation": False,
            "remove_logo": False,
            "custom_css": False,
        },
        sort_order=0,
        is_active=True,
    )

    # Pro plan
    SubscriptionPlan.objects.create(
        name="pro",
        display_name="프로",
        monthly_price=9900,
        yearly_price=99000,
        features={
            "max_pages": -1,
            "ai_generation": True,
            "remove_logo": True,
            "custom_css": False,
        },
        sort_order=1,
        is_active=True,
    )

    # Pro Plus plan
    SubscriptionPlan.objects.create(
        name="pro_plus",
        display_name="프로 플러스",
        monthly_price=19900,
        yearly_price=199000,
        features={
            "max_pages": -1,
            "ai_generation": True,
            "remove_logo": True,
            "custom_css": True,
        },
        sort_order=2,
        is_active=True,
    )

    # Assign Free plan to all existing users
    from django.utils import timezone

    now = timezone.now()
    for user in User.objects.all():
        if not UserSubscription.objects.filter(user=user).exists():
            UserSubscription.objects.create(
                user=user,
                plan=free,
                status="active",
                billing_cycle="monthly",
                current_period_start=now,
            )


def reverse_seed(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    UserSubscription = apps.get_model("billing", "UserSubscription")
    UserSubscription.objects.all().delete()
    SubscriptionPlan.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0002_subscriptionplan_usersubscription_paymenthistory"),
        ("authentication", "0002_alter_user_managers"),
    ]

    operations = [
        migrations.RunPython(seed_plans_and_subscriptions, reverse_seed),
    ]
