"""toss-billing 브랜치 신규 주기 잡 시드 + v4.3 케이던스 드리프트 보정.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create/update) — 재적용/이미 존재해도 안전.

신규 시드 4건 (base.py CELERY_BEAT_SCHEDULE 과 동일 정의):
- dm-reconcile-pacer-pointers(60s): v4.3 페이서 '빈 슬롯 홀'(삭제/일시중지 잔여 포인터) 회수.
  없으면 슬롯 누수로 DM 디스패치가 굶을 수 있다.
- dm-recovery-pending-expiry(1h): RECOVERY_PENDING 이 캠페인 TTL 초과 시 RECOVERY_EXPIRED 종결.
- analytics-cleanup-landing-visits(매일 03:30 KST, billing 큐): LandingVisit 보존일 초과분 삭제.
- maintain-partitions(매일 02:00 KST, billing 큐): EventInbox 일별 파티션 선생성/드롭.
  ※ base.py 에는 있었으나 0002 시드에 빠져 있던 잠재 결함 — 그간 DEFAULT 파티션이 흡수해 무증상.

케이던스 보정 2건: dm-requeue-deferred / dm-reconcile-stuck-submitting 60s→30s (v4.3 설계값).
"""

from django.db import migrations
from django.utils import timezone

# (key, task, interval_seconds, cron_minute, cron_hour, queue)
_NEW_JOBS = [
    (
        "dm-reconcile-pacer-pointers",
        "apps.integrations.tasks.reconcile_pacer_pointers",
        60,
        "",
        "",
        "",
    ),
    (
        "dm-recovery-pending-expiry",
        "integrations.handle_recovery_pending_expiry",
        3600,
        "",
        "",
        "",
    ),
    (
        "analytics-cleanup-landing-visits",
        "analytics.cleanup_landing_visits",
        None,
        "30",
        "3",
        "billing",
    ),
    (
        "maintain-partitions",
        "integrations.maintain_partitions",
        None,
        "0",
        "2",
        "billing",
    ),
]

_CADENCE_FIX = {  # key -> interval_seconds (v4.3 설계값으로 동기화)
    "dm-requeue-deferred": 30,
    "dm-reconcile-stuck-submitting": 30,
}


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    for key, task, interval, cmin, chour, queue in _NEW_JOBS:
        ScheduledJob.objects.update_or_create(
            key=key,
            defaults={
                "task": task,
                "interval_seconds": interval,
                "cron_minute": cmin,
                "cron_hour": chour,
                "cron_day_of_week": "",
                "queue": queue,
                "enabled": True,
                "next_due_at": timezone.now(),  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
            },
        )
    for key, interval in _CADENCE_FIX.items():
        ScheduledJob.objects.filter(key=key).update(interval_seconds=interval)


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key__in=[j[0] for j in _NEW_JOBS]).delete()
    for key in _CADENCE_FIX:
        ScheduledJob.objects.filter(key=key).update(interval_seconds=60)


class Migration(migrations.Migration):

    dependencies = [("core", "0005_tighten_resubscribe_and_infra_alert")]

    operations = [migrations.RunPython(apply, revert)]
