"""자동 DM 프로 전용 4기능의 불리언 키를 ``SubscriptionPlan.features`` 에 심는다.

프론트가 지금은 플랜 **이름**(`pro`/`admin`)으로 잠금을 판정하고 있다. 이름 비교는
플랜이 하나 늘거나 이름이 바뀌는 순간 조용히 틀리므로, 서버가 내려주는 **기능 키**로
바꿔 달라는 요청(2026-10-15)에 대한 대응이다.

⚠️ 서버 게이트의 판정 소스는 **이 값이 아니다.** 실제 거절은
``apps/integrations/dm_plan_gate.ALLOWED_PLANS`` 가 한다. 여기 값은 **화면 잠금 표시용**
사본이다 — 둘이 어긋나면 "버튼은 눌리는데 저장이 403" 이 되므로, 한쪽을 고치면 반드시
다른 쪽도 같이 본다.

⚠️ ``features`` 는 JSON 컬럼이라 **통째로 덮어쓰면 기존 키가 날아간다.** 반드시 읽어서
병합할 것(아래가 그 방식).
"""

from django.db import migrations

DM_PRO_FEATURES = (
    "dm_trigger_all_posts",
    "dm_trigger_next_media",
    "dm_trigger_story_reply",
    "dm_follow_gate",
)

# dm_plan_gate.ALLOWED_PLANS 와 같아야 한다.
ALLOWED = {"pro", "pro+", "admin"}


def seed(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all():
        features = dict(plan.features or {})
        allowed = plan.name in ALLOWED
        changed = False
        for key in DM_PRO_FEATURES:
            if features.get(key) is not allowed:
                features[key] = allowed
                changed = True
        if changed:
            plan.features = features
            plan.save(update_fields=["features"])


def unseed(apps, schema_editor):
    SubscriptionPlan = apps.get_model("billing", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all():
        features = dict(plan.features or {})
        if any(features.pop(key, None) is not None for key in DM_PRO_FEATURES):
            plan.features = features
            plan.save(update_fields=["features"])


class Migration(migrations.Migration):
    dependencies = [("billing", "0026_usersubscription_trial_kind")]
    operations = [migrations.RunPython(seed, unseed)]
