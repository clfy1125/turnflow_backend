"""웹훅 구독 재확정 잡을 CF tick 스케줄러(ScheduledJob)에 시드.

config/settings/base.py 의 CELERY_BEAT_SCHEDULE 항목은 CF tick 이 읽지 않는다
(tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단 — 0002 참고). 따라서 6시간 주기
웹훅 재확정 잡(apps.integrations.tasks.resubscribe_all_webhooks)을 여기서 시드해야 실제 발동한다.
멱등(update_or_create) — 재적용/이미 존재해도 안전.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "integrations-resubscribe-webhooks"
_TASK = "apps.integrations.tasks.resubscribe_all_webhooks"


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": _TASK,
            "interval_seconds": 6 * 3600,  # 6시간
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            "queue": "",
            "enabled": True,
            "next_due_at": timezone.now(),  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
        },
    )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0002_seed_dr_state")]

    operations = [migrations.RunPython(seed, unseed)]
