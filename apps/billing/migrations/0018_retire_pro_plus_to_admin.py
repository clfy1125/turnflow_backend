"""유령 플랜 pro_plus 정리 — 잔여 구독을 admin 으로 이관 후 삭제.

배경(2026-07-14 prod 실측):
- pro_plus 는 0014_plan_restructure 에서 폐기 대상이었으나, 당시 UserSubscription 5건이
  plan(PROTECT)으로 물고 있어 삭제되지 못하고 is_active=False 로만 방치됐다.
- 그 features 는 개편 전 스키마라 현행 키(spam_filter/ai_unlimited/analytics_export/
  dm_monthly_limit/max_ig_accounts)가 없다 → check_feature 가 전부 False 를 반환해
  얹혀 있던 내부/테스트 계정이 프로 전용 기능을 못 쓴다(staff 우회는 DM한도·IG계정뿐).
- 정작 내부용 admin 플랜(features 전부 true, is_active=False)은 구독자 0명이었다.
- dev DB 에는 pro_plus 가 아예 없어(4개 플랜) 이 불일치가 드러나지 않았다.

조치: pro_plus 구독 전부를 admin 으로 이관(전부 빌링키 없음·current_period_end=NULL 이라
과금 파이프라인 무관·무료 comp 계정) → 참조가 사라진 pro_plus 삭제.

멱등·안전:
- pro_plus 가 없으면(=dev, 또는 이미 정리됨) 즉시 no-op.
- admin 플랜이 없으면 이관 목적지가 없으므로 아무것도 하지 않고 pro_plus 를 보존(안전 중단).
- pending_plan(SET_NULL) 이 pro_plus 인 건 삭제를 막지 않지만 명시적으로 정리한다.
- 여전히 참조가 남아 삭제 불가하면(예상 밖 FK) is_active=False 로 폴백.
"""

from django.db import migrations
from django.db.models import ProtectedError


def retire_pro_plus(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    UserSubscription = apps.get_model("billing", "UserSubscription")

    pro_plus = SubscriptionPlan.objects.filter(name="pro_plus").first()
    if not pro_plus:
        return  # dev / 이미 정리됨 → no-op

    admin = SubscriptionPlan.objects.filter(name="admin").first()
    if not admin:
        # 이관 목적지 부재 → 데이터 손실 방지 위해 중단(pro_plus 유지).
        return

    # 1) 잔여 구독 plan(PROTECT) 을 admin 으로 이관 → features 전부 true 로 승격.
    UserSubscription.objects.filter(plan=pro_plus).update(plan=admin)
    # 2) 예약 플랜(pending_plan, SET_NULL) 이 pro_plus 를 가리키면 정리.
    UserSubscription.objects.filter(pending_plan=pro_plus).update(pending_plan=None)
    # 3) 참조 소거 완료 → 삭제. 예상 밖 FK 가 남으면 비활성 폴백.
    try:
        pro_plus.delete()
    except ProtectedError:
        pro_plus.is_active = False
        pro_plus.save(update_fields=["is_active"])


def reverse(apps, schema_editor):
    """일방향 데이터 정리 — 어떤 admin 구독이 원래 pro_plus 였는지 식별 불가하므로
    구독 재배치는 되돌리지 않는다. 롤백 호환을 위해 비활성 pro_plus 껍데기만 복원한다."""
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    if not SubscriptionPlan.objects.filter(name="pro_plus").exists():
        SubscriptionPlan.objects.create(
            name="pro_plus",
            display_name="프로 플러스(폐기)",
            monthly_price=0,
            list_price=0,
            features={},
            sort_order=98,
            is_active=False,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0017_add_dm_recovery_feature"),
    ]

    operations = [
        migrations.RunPython(retire_pro_plus, reverse),
    ]
