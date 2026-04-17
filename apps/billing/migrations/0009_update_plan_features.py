"""
Data migration: Update subscription plan features to new spec.

free:  max_pages=1, remove_logo=false, custom_css=false, ai_generation=false
pro:   max_pages=5, remove_logo=true, custom_css=true, ai_generation=true (unlimited AI)
pro_plus: is_active=False (결제 비활성)
"""

from django.db import migrations


def update_plan_features(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")

    # Free plan
    try:
        free = SubscriptionPlan.objects.get(name="free")
        free.features = {
            "max_pages": 1,
            "ai_generation": False,
            "remove_logo": False,
            "custom_css": False,
        }
        free.save(update_fields=["features"])
    except SubscriptionPlan.DoesNotExist:
        pass

    # Pro plan
    try:
        pro = SubscriptionPlan.objects.get(name="pro")
        pro.features = {
            "max_pages": 5,
            "ai_generation": True,
            "remove_logo": True,
            "custom_css": True,
        }
        pro.save(update_fields=["features"])
    except SubscriptionPlan.DoesNotExist:
        pass

    # Pro Plus — 결제 비활성화
    try:
        pro_plus = SubscriptionPlan.objects.get(name="pro_plus")
        pro_plus.is_active = False
        pro_plus.save(update_fields=["is_active"])
    except SubscriptionPlan.DoesNotExist:
        pass


def reverse_plan_features(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")

    try:
        free = SubscriptionPlan.objects.get(name="free")
        free.features = {
            "max_pages": 3,
            "ai_generation": False,
            "remove_logo": False,
            "custom_css": False,
        }
        free.save(update_fields=["features"])
    except SubscriptionPlan.DoesNotExist:
        pass

    try:
        pro = SubscriptionPlan.objects.get(name="pro")
        pro.features = {
            "max_pages": -1,
            "ai_generation": True,
            "remove_logo": True,
            "custom_css": False,
        }
        pro.save(update_fields=["features"])
    except SubscriptionPlan.DoesNotExist:
        pass

    try:
        pro_plus = SubscriptionPlan.objects.get(name="pro_plus")
        pro_plus.is_active = True
        pro_plus.save(update_fields=["is_active"])
    except SubscriptionPlan.DoesNotExist:
        pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0008_subscription_activation_fields"),
    ]

    operations = [
        migrations.RunPython(update_plan_features, reverse_plan_features),
    ]
