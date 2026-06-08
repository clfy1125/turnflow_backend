"""
Data migration: 회사 정책 변경 — 무료(free) 플랜에서도 커스텀 CSS 사용 허용.

free: custom_css false → true (그 외 기능 플래그는 0009 상태 유지)
"""

from django.db import migrations


def enable_free_custom_css(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")

    try:
        free = SubscriptionPlan.objects.get(name="free")
        features = dict(free.features or {})
        features["custom_css"] = True
        free.features = features
        free.save(update_fields=["features"])
    except SubscriptionPlan.DoesNotExist:
        pass


def disable_free_custom_css(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")

    try:
        free = SubscriptionPlan.objects.get(name="free")
        features = dict(free.features or {})
        features["custom_css"] = False
        free.features = features
        free.save(update_fields=["features"])
    except SubscriptionPlan.DoesNotExist:
        pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0011_usagecounter_comments_moderated_and_more"),
    ]

    operations = [
        migrations.RunPython(enable_free_custom_css, disable_free_custom_css),
    ]
