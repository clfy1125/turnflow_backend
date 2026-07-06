"""론칭 하드닝(#6): 웹훅 재구독 주기 6h→1h + 인프라 헬스 경고 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 갱신/시드한다.
멱등(update/update_or_create) — 재적용/이미 존재해도 안전.

- 재구독(integrations-resubscribe-webhooks): Meta 가 웹훅을 auto-disable 하면 캠페인이 무음 정지되는
  노출창을 6h→1h 로 축소.
- 인프라 헬스 경고(apps.integrations.tasks.dm_infra_health_alert): Redis 메모리(noeviction freeze)·
  브로커 큐 적체·deferred DM 밀림 등 '조용히 터지는' 신호를 5분마다 Telegram 노출.
"""

from django.db import migrations
from django.utils import timezone

_RESUB_KEY = "integrations-resubscribe-webhooks"
_ALERT_KEY = "integrations-dm-infra-health-alert"
_ALERT_TASK = "apps.integrations.tasks.dm_infra_health_alert"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    # 1) 재구독 6h → 1h
    ScheduledJob.objects.filter(key=_RESUB_KEY).update(interval_seconds=3600)
    # 2) 인프라 헬스 경고 잡 시드 (5분)
    ScheduledJob.objects.update_or_create(
        key=_ALERT_KEY,
        defaults={
            "task": _ALERT_TASK,
            "interval_seconds": 300,
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            "queue": "",  # 기본 라우팅 → celery 큐(celery_default)
            "enabled": True,
            "next_due_at": timezone.now(),  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_RESUB_KEY).update(interval_seconds=6 * 3600)
    ScheduledJob.objects.filter(key=_ALERT_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0004_seed_toss_renewal_jobs")]

    operations = [migrations.RunPython(apply, revert)]
