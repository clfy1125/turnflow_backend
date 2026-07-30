"""인스타 성장 리포트 주기 잡 2종을 ScheduledJob 에 시드.

⚠️ 프로덕션은 celery_beat 를 상시 가동하지 않는다(profiles: [fallback]). 외부 tick
(CF 워커 → /api/v1/internal/scheduler/tick)이 ScheduledJob.next_due_at 기준으로만 발사한다.
따라서 CELERY_BEAT_SCHEDULE 추가만으로는 프로덕션에서 실행되지 않는다.

- insta_reports.sweep_stale (30분): 워커 OOM/강제종료로 running 에 박힌 잡을 실패 확정.
  방치하면 "동시 생성 1건" 제한 때문에 사용자가 새 리포트를 영구히 만들 수 없다.
- insta_reports.purge_caches (매일 04:40 KST): 90일 경과 AI 캐시 정리.
  리포트 PDF·집계는 삭제하지 않는다(제품 결정: 계속 보관).
"""

from django.db import migrations
from django.utils import timezone

_SWEEP_KEY = "insta-reports-sweep-stale"
_PURGE_KEY = "insta-reports-purge-caches"


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_SWEEP_KEY,
        defaults={
            "task": "insta_reports.sweep_stale",
            "interval_seconds": 60 * 30,
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            "queue": "reports",
            "enabled": True,
            "next_due_at": timezone.now(),
        },
    )
    ScheduledJob.objects.update_or_create(
        key=_PURGE_KEY,
        defaults={
            "task": "insta_reports.purge_caches",
            "interval_seconds": None,
            "cron_minute": "40",
            "cron_hour": "4",
            "cron_day_of_week": "",
            "queue": "reports",
            "enabled": True,
            "next_due_at": timezone.now(),
        },
    )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key__in=[_SWEEP_KEY, _PURGE_KEY]).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0010_seed_snapshot_daily_metrics_job")]

    operations = [migrations.RunPython(seed, unseed)]
