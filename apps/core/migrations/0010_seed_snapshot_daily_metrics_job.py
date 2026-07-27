"""일별 구독 스냅샷 태스크(billing.snapshot_daily_metrics)를 ScheduledJob 에 시드 (P-4).

⚠️ 프로덕션은 celery_beat 를 상시 가동하지 않는다(profiles: [fallback]). 외부 tick
(CF 워커 → /api/v1/internal/scheduler/tick)이 ScheduledJob.next_due_at 기준으로만 발사한다.
따라서 config.settings.base.CELERY_BEAT_SCHEDULE 추가만으로는 프로덕션에서 실행되지 않는다.

- snapshot_daily_metrics (매일 00:20 KST): 구독 상태/MRR/결제 코호트 일별 스냅샷 적재
  (멱등 upsert — 과거 시점 상태는 재구성 불가라 적재 시작일부터 정확 데이터가 쌓인다).
"""

from django.db import migrations
from django.utils import timezone

_KEY = "billing-snapshot-daily-metrics"


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "billing.snapshot_daily_metrics",
            "interval_seconds": None,
            "cron_minute": "20",
            "cron_hour": "0",
            "cron_day_of_week": "",
            "queue": "billing",
            "enabled": True,
            "next_due_at": timezone.now(),  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
        },
    )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0009_seed_retention_jobs")]

    operations = [migrations.RunPython(seed, unseed)]
