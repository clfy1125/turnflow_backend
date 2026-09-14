"""퍼널 이벤트(analytics.FunnelEvent) 보존기간 정리 주기 잡 시드.

⚠️ **프로덕션에는 celery beat 가 없다.** 주기 실행은 CF cron 워커
(`turnflow-scheduler-tick`, 매분) → `POST /api/v1/internal/scheduler/tick` → `ScheduledJob`
DB 행의 `next_due_at` 으로 due 판단이다(core 0002 참고). 즉 `config/settings/base.py` 의
`CELERY_BEAT_SCHEDULE` 에만 넣으면 **운영에서는 영영 돌지 않는다** — 로컬에서만 돈다.
그래서 여기서 DB 행을 직접 시드한다. 멱등(update_or_create).

- analytics-cleanup-funnel-events(매일 **03:40 KST** — _next_cron 이 settings.TIME_ZONE 로 계산): 보존기간
  (`FUNNEL_EVENT_RETENTION_DAYS`, 기본 180일) 초과 FunnelEvent 를 10k 청크로 삭제.

enabled=True 로 시드해도 안전하다 — 삭제 대상이 **자기 앱의 만료된 계측 행뿐**이고,
사용자 데이터·발송·과금에는 손대지 않는다. 대상이 없으면 no-op 로 끝난다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "analytics-cleanup-funnel-events"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "analytics.cleanup_funnel_events",
            "interval_seconds": None,  # cron 형 — interval 이 있으면 그쪽이 우선한다(모델 주석)
            "cron_minute": "40",
            "cron_hour": "3",
            "cron_day_of_week": "",
            "queue": "billing",  # housekeeping 큐 (기존 정리 잡들과 동일)
            "enabled": True,
            "next_due_at": timezone.now(),
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0019_seed_home_alert_email_job")]

    operations = [migrations.RunPython(apply, revert)]
