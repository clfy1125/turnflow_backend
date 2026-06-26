"""DR 컨트롤플레인 초기 시드.

- SiteControl pk=1 (active_site = settings.SITE_ID, epoch=1, mode=live, restore_complete=True)
- ScheduledJob: 기존 config/settings/base.py CELERY_BEAT_SCHEDULE 15개 엔트리를 그대로 시드.
  (#3 결정: 30초 잡 2개도 60초로 — 멱등이라 무손실, cadence만 거칠어짐.)

queue 빈 문자열 = CELERY_TASK_ROUTES 가 결정(기존 동작 유지). next_due_at = 적용 시각(즉시 due).
"""

from django.conf import settings
from django.db import migrations
from django.utils import timezone


# (key, task, interval_seconds, cron_minute, cron_hour, cron_day_of_week, queue)
_JOBS = [
    ("check-missed-payments", "billing.check_missed_payments", 3600, "", "", "", "billing"),
    ("handle-grace-period-expiry", "billing.handle_grace_period_expiry", 3600, "", "", "", "billing"),
    ("handle-cancelled-expiry", "billing.handle_cancelled_expiry", 3600, "", "", "", "billing"),
    ("handle-trial-expiry", "billing.handle_trial_expiry", 3600, "", "", "", "billing"),
    # DM 보증 시스템 (30초 → 60초; 멱등이라 무손실)
    ("dm-reconcile-accepted", "apps.integrations.tasks.reconcile_accepted_dms", 60, "", "", "", ""),
    ("dm-reconcile-stuck-submitting", "apps.integrations.tasks.reconcile_stuck_submitting", 60, "", "", "", ""),
    ("dm-requeue-deferred", "apps.integrations.tasks.requeue_deferred_dms", 60, "", "", "", ""),
    ("dm-dead-letter-alerter", "apps.integrations.tasks.dead_letter_alerter", 600, "", "", "", ""),
    ("dm-backlog-alert", "apps.integrations.tasks.dm_backlog_alert", 1800, "", "", "", ""),
    ("dm-enforce-campaign-schedules", "apps.integrations.tasks.enforce_campaign_schedules", 60, "", "", "", ""),
    ("dm-poll-missed-comments", "integrations.poll_missed_comments", 3600, "", "", "", ""),
    ("backup-health-check", "apps.core.tasks.backup_health_check", 1800, "", "", "", "billing"),
    # cron 형 (Asia/Seoul)
    ("ig-refresh-tokens-pending-expiry", "apps.integrations.tasks.refresh_ig_tokens_pending_expiry", None, "0", "*/6", "", ""),
    ("cleanup-unverified-accounts", "authentication.cleanup_unverified_accounts", None, "0", "4", "", "billing"),
    ("cleanup-comment-ledger", "integrations.cleanup_comment_ledger", None, "30", "4", "", ""),
]


def seed(apps, schema_editor):
    SiteControl = apps.get_model("core", "SiteControl")
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    now = timezone.now()

    SiteControl.objects.update_or_create(
        pk=1,
        defaults={
            "active_site": getattr(settings, "SITE_ID", "colo"),
            "epoch": 1,
            "mode": "live",
            "restore_complete": True,
            "note": "seeded by 0002_seed_dr_state",
        },
    )

    for key, task, interval, c_min, c_hour, c_dow, queue in _JOBS:
        ScheduledJob.objects.update_or_create(
            key=key,
            defaults={
                "task": task,
                "interval_seconds": interval,
                "cron_minute": c_min,
                "cron_hour": c_hour,
                "cron_day_of_week": c_dow,
                "queue": queue,
                "enabled": True,
                "next_due_at": now,  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
            },
        )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    SiteControl = apps.get_model("core", "SiteControl")
    ScheduledJob.objects.filter(key__in=[j[0] for j in _JOBS]).delete()
    SiteControl.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
