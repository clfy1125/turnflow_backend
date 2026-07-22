"""리텐션(해지 방어) 주기 태스크를 프로덕션 스케줄러(ScheduledJob)에 시드.

⚠️ 프로덕션은 celery_beat 를 상시 가동하지 않는다(profiles: [fallback]). 외부 tick
(CF 워커 → /api/v1/internal/scheduler/tick)이 ScheduledJob.next_due_at 기준으로만 발사한다.
따라서 config.settings.base.CELERY_BEAT_SCHEDULE 추가만으로는 프로덕션에서 실행되지 않는다.

- handle_pause_expiry           (매시간): 정지 만료 → 자동 유료 재개 + 갱신 과금 트리거
- notify_pause_resume_reminder  (매일 09:30 KST): 정지 재개 3일 전 사전 고지 메일
- send_winback_emails           (매일 10:00 KST): 해지 N일 후 복귀 유도 메일 (WINBACK_ENABLED 게이트)
"""

from django.db import migrations
from django.utils import timezone

# (key, task, interval_seconds, cron_hour, cron_minute, queue)
_JOBS = [
    ("billing-handle-pause-expiry", "billing.handle_pause_expiry", 3600, "", "", "billing"),
    (
        "billing-notify-pause-resume-reminder",
        "billing.notify_pause_resume_reminder",
        None,
        "9",
        "30",
        "billing",
    ),
    ("billing-send-winback-emails", "billing.send_winback_emails", None, "10", "0", "billing"),
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

    dependencies = [("core", "0008_seed_dm_migration_purge_job")]

    operations = [migrations.RunPython(seed, unseed)]
