"""기존 댓글 소급 발송 스캐너 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create) — 재적용/이미 존재해도 안전.

- dm-scan-campaigns-for-backfill(1분): backfill_started_at 이 비어 있고 지금 발송 가능한
  캠페인을 찾아 integrations.backfill_campaign_comments 를 발사한다. 1분 주기인 이유는
  **예약 캠페인의 시작 시각 도래를 감지하는 유일한 경로**이기 때문이다 — 이 시스템에는
  예약 시작 이벤트가 없고(enforce_campaign_schedules 는 종료 전담), 시작 게이팅은 발송
  경로의 schedule_window_q 가 담당한다. 대기열 자체가 DB 상태(NULL)라 tick 이 유실돼도
  다음 tick 에 그대로 회복된다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "dm-scan-campaigns-for-backfill"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.scan_campaigns_for_backfill",
            "interval_seconds": 60,
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            "queue": "",
            "enabled": True,
            "next_due_at": timezone.now(),
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0014_seed_conversion_consent_job")]

    operations = [migrations.RunPython(apply, revert)]
