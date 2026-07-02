"""
Data migration: 론칭 요금제 개편 (PayApp → 토스페이먼츠 전환과 동반).

- free:  링크페이지 1, 가입 시 AI 토큰 2회(코드 레벨), DM 월 200건, 커스텀 CSS 허용
- basic: 신설 3,900원(정가 5,900) — 페이지 5, 배지 제거, AI 무제한, 분석/엑셀
- pro:   9,900원(론칭 프로모, 정가 15,900) — DM 무제한, 스팸필터, 다계정(+extra)
         프로모 종료 시 운영에서 monthly_price만 13,500으로 올리면 됨
         (기존 구독자는 UserSubscription.monthly_amount_snapshot으로 그랜드파더링).
- admin: 운영 DB 수동 생성 행 존중 — 존재하면 features 신규 키만 merge(가격/노출 불변),
         없으면 비노출(is_active=False) 행 생성.
- pro_plus: 폐기. FK(PROTECT) 참조가 남아 있으면 삭제 대신 비활성 유지.

features 키는 전 플랜 공통으로 전부 기입한다 — subscription_utils.check_limit은
키 누락 시 '차단'으로 동작하므로 누락은 곧 버그다.
"""

from django.db import migrations
from django.db.models import ProtectedError

FREE_FEATURES = {
    "max_pages": 1,
    "ai_generation": True,  # 표시용 — 실 게이트는 AI 토큰 잔액(가입 시 2개)
    "ai_unlimited": False,
    "remove_logo": False,
    "custom_css": True,
    "dm_monthly_limit": 200,
    "analytics_export": False,
    "spam_filter": False,
    "max_ig_accounts": 1,
}

BASIC_FEATURES = {
    "max_pages": 5,
    "ai_generation": True,
    "ai_unlimited": True,
    "remove_logo": True,
    "custom_css": True,
    "dm_monthly_limit": 200,
    "analytics_export": True,
    "spam_filter": False,
    "max_ig_accounts": 1,
}

PRO_FEATURES = {
    "max_pages": 5,
    "ai_generation": True,
    "ai_unlimited": True,
    "remove_logo": True,
    "custom_css": True,
    "dm_monthly_limit": -1,
    "analytics_export": True,
    "spam_filter": True,
    "max_ig_accounts": 1,  # 기본 1계정 — 추가분은 UserSubscription.extra_ig_accounts
}

ADMIN_FEATURES = {
    "max_pages": -1,
    "ai_generation": True,
    "ai_unlimited": True,
    "remove_logo": True,
    "custom_css": True,
    "dm_monthly_limit": -1,
    "analytics_export": True,
    "spam_filter": True,
    "max_ig_accounts": -1,
}


def restructure_plans(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")

    SubscriptionPlan.objects.update_or_create(
        name="free",
        defaults={
            "display_name": "무료",
            "monthly_price": 0,
            "list_price": 0,
            "features": FREE_FEATURES,
            "sort_order": 0,
            "is_active": True,
        },
    )

    SubscriptionPlan.objects.update_or_create(
        name="basic",
        defaults={
            "display_name": "베이직",
            "monthly_price": 3900,
            "list_price": 5900,
            "features": BASIC_FEATURES,
            "sort_order": 1,
            "is_active": True,
        },
    )

    SubscriptionPlan.objects.update_or_create(
        name="pro",
        defaults={
            "display_name": "프로",
            "monthly_price": 9900,  # 론칭 프로모 — 종료 시 13,500으로 운영 변경
            "list_price": 15900,
            "features": PRO_FEATURES,
            "sort_order": 2,
            "is_active": True,
        },
    )

    # admin — 운영 수동 행 존중: 가격/표시/노출은 건드리지 않고 신규 키만 채움
    admin_plan = SubscriptionPlan.objects.filter(name="admin").first()
    if admin_plan:
        features = dict(admin_plan.features or {})
        for key, value in ADMIN_FEATURES.items():
            features.setdefault(key, value)
        admin_plan.features = features
        admin_plan.save(update_fields=["features"])
    else:
        SubscriptionPlan.objects.create(
            name="admin",
            display_name="관리자",
            monthly_price=0,
            list_price=0,
            features=ADMIN_FEATURES,
            sort_order=99,
            is_active=False,  # 공개 플랜 목록 비노출
        )

    # pro_plus 폐기 — UserSubscription.plan / ReferralCode.target_plan(PROTECT)이
    # 참조 중이면 삭제 불가 → 비활성 유지 폴백
    pro_plus = SubscriptionPlan.objects.filter(name="pro_plus").first()
    if pro_plus:
        try:
            pro_plus.delete()
        except ProtectedError:
            pro_plus.is_active = False
            pro_plus.save(update_fields=["is_active"])


def reverse_plans(apps, schema_editor):
    """0012 직후 상태로 복원. basic/admin은 신설분만 제거 시도."""
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")

    try:
        free = SubscriptionPlan.objects.get(name="free")
        free.features = {
            "max_pages": 1,
            "ai_generation": False,
            "remove_logo": False,
            "custom_css": True,
        }
        free.monthly_price = 0
        free.list_price = 0
        free.save(update_fields=["features", "monthly_price", "list_price"])
    except SubscriptionPlan.DoesNotExist:
        pass

    try:
        pro = SubscriptionPlan.objects.get(name="pro")
        pro.features = {
            "max_pages": 5,
            "ai_generation": True,
            "remove_logo": True,
            "custom_css": True,
        }
        pro.monthly_price = 9900
        pro.list_price = 0
        pro.save(update_fields=["features", "monthly_price", "list_price"])
    except SubscriptionPlan.DoesNotExist:
        pass

    for name in ("basic",):
        plan = SubscriptionPlan.objects.filter(name=name).first()
        if plan:
            try:
                plan.delete()
            except ProtectedError:
                plan.is_active = False
                plan.save(update_fields=["is_active"])


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0013_subscriptionplan_list_price"),
    ]

    operations = [
        migrations.RunPython(restructure_plans, reverse_plans),
    ]
