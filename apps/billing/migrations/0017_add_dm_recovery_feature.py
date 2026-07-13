# Generated for 실패 DM 복구(recovery) 프로 전용 게이트

from django.db import migrations

# 실패 DM 복구(recovery)는 프로 전용 기능이다. spam_filter 와 동일한 정책.
# check_feature 는 키 누락 시 False(미보유)로 동작하지만, test_plan_seed 의 전-키-존재
# 계약과 owner_has_feature 게이트 일관성을 위해 모든 플랜에 명시적으로 심는다.
# 현재 billing SubscriptionPlan 은 free/basic/pro/admin 만 존재 → 프로 티어는 pro/admin.
PRO_TIER_NAMES = {"pro", "admin"}


def add_dm_recovery(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all():
        features = dict(plan.features or {})
        features["dm_recovery"] = plan.name in PRO_TIER_NAMES
        plan.features = features
        plan.save(update_fields=["features"])


def remove_dm_recovery(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all():
        features = dict(plan.features or {})
        features.pop("dm_recovery", None)
        plan.features = features
        plan.save(update_fields=["features"])


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0016_usersubscription_ig_account_activation_changed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(add_dm_recovery, remove_dm_recovery),
    ]
