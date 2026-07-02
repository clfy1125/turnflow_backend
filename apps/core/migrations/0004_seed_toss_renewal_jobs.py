"""토스 빌링 갱신 파이프라인을 프로덕션 스케줄러(ScheduledJob)에 시드.

⚠️ 프로덕션은 celery_beat 를 상시 가동하지 않는다(profiles: [fallback]). 외부 tick
(CF 워커 → /api/v1/internal/scheduler/tick)이 ScheduledJob.next_due_at 기준으로만 발사한다.
따라서 config.settings.base.CELERY_BEAT_SCHEDULE 에 추가한 것만으로는 프로덕션에서
갱신 과금이 절대 일어나지 않는다 — 여기 ScheduledJob 로도 반드시 등록해야 한다.

- process_due_renewals    (10분): 갱신 도래 구독 과금 디스패치 — 토스 정기결제의 심장
- reconcile_pending_payments (30분): 모호 실패(PENDING) 결제 확정 안전망

0002 가 시드한 기존 billing 잡(check/grace/cancel/trial expiry)은 그대로 두고 2건만 추가.
"""

from django.db import migrations
from django.utils import timezone

# (key, task, interval_seconds, queue)
_JOBS = [
    ("process-due-renewals", "billing.process_due_renewals", 600, "billing"),
    ("reconcile-pending-payments", "billing.reconcile_pending_payments", 1800, "billing"),
]


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    now = timezone.now()
    for key, task, interval, queue in _JOBS:
        ScheduledJob.objects.update_or_create(
            key=key,
            defaults={
                "task": task,
                "interval_seconds": interval,
                "cron_minute": "",
                "cron_hour": "",
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

    dependencies = [("core", "0003_seed_resubscribe_webhooks_job")]

    operations = [migrations.RunPython(seed, unseed)]
