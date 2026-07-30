# 인스타 성장 리포트(프로 전용) 플랜 기능 키 2종 추가.
#
# 정책(2026-07-29 확정):
#   · insta_report                       — 기능 보유 여부 (프로 전용)
#   · insta_report_monthly_per_account   — IG 계정 1개당 캘린더월 생성 횟수
#     프로 = 1. 추가 IG 계정(9,900원)을 붙이면 계정 수만큼 총량이 늘어난다
#     (연동 2개 → 각 계정 1회 → 이번 달 총 2회). admin = -1(무제한).
#
# check_feature/check_limit 은 키 누락 시 '차단'으로 동작하지만, test_plan_seed 의
# 전-키-존재 계약과 게이트 일관성을 위해 모든 플랜에 명시적으로 심는다(0017 과 동일 방식).

from django.db import migrations

PRO_TIER_NAMES = {"pro", "admin"}
FEATURE_KEY = "insta_report"
LIMIT_KEY = "insta_report_monthly_per_account"


def add_insta_report(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all():
        features = dict(plan.features or {})
        is_pro_tier = plan.name in PRO_TIER_NAMES
        features[FEATURE_KEY] = is_pro_tier
        if plan.name == "admin":
            features[LIMIT_KEY] = -1
        else:
            features[LIMIT_KEY] = 1 if is_pro_tier else 0
        plan.features = features
        plan.save(update_fields=["features"])


def remove_insta_report(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all():
        features = dict(plan.features or {})
        features.pop(FEATURE_KEY, None)
        features.pop(LIMIT_KEY, None)
        plan.features = features
        plan.save(update_fields=["features"])


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0022_referralcode_excluded_from_stats"),
    ]

    operations = [
        migrations.RunPython(add_insta_report, remove_insta_report),
    ]
