"""유료전환 2차 동의 요청 메일(D-14/D-3) 배치를 프로덕션 스케줄러(ScheduledJob)에 시드.

⚠️ 프로덕션은 celery_beat 를 상시 가동하지 않는다(profiles: [fallback]). 외부 tick
(CF 워커 → /api/v1/internal/scheduler/tick)이 ScheduledJob.next_due_at 기준으로만 발사한다.
따라서 config.settings.base.CELERY_BEAT_SCHEDULE 추가만으로는 프로덕션에서 실행되지 않는다.

- notify_conversion_consent (매일 10:30 KST): 30일 초과 체험의 첫 결제 D-14 / D-3 동의 요청

과금 차단 자체는 배치가 아니라 ``billing.charge_subscription_renewal`` 안의 게이트가
수행하므로, 이 잡이 실패해도 무동의 결제는 발생하지 않는다(메일만 안 나간다).
"""

from django.db import migrations
from django.utils import timezone

_JOBS = [
    (
        "billing-notify-conversion-consent",
        "billing.notify_conversion_consent",
        None,
        "10",
        "30",
        "billing",
    ),
]


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    now = timezone.now()
    for key, task, interval, cron_hour, cron_minute, queue in _JOBS:
        ScheduledJob.objects.update_or_create(
            key=key,
            defaults={
                "task": task,
                "interval_seconds": interval,
                "cron_minute": cron_minute,
                "cron_hour": cron_hour,
                "cron_day_of_week": "",
                "queue": queue,
                "enabled": True,
                "next_due_at": now,  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
            },
        )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key__in=[j[0] for j in _JOBS]).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0013_seed_campaign_thumbnail_sweep_job")]

    operations = [migrations.RunPython(seed, unseed)]
