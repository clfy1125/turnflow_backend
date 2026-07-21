"""DM 캠페인 이전 원본 파기 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create) — 재적용/이미 존재해도 안전.

- dm-migration-purge-raw(매일): 완료 7일 지난 DMMigrationJob 의 원본(타인 댓글·DM) 파기
  (stage_data={}, 후보 evidence_raw=None) + 비종결 스테일 잡 failed 스위핑. 없으면 개인정보
  원본이 무기한 남고, 크래시 잡이 부분 UNIQUE 제약으로 연결을 영구 잠글 수 있다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "dm-migration-purge-raw"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.purge_dm_migration_raw",
            "interval_seconds": 86400,  # 24h
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            "queue": "",
            "enabled": True,
            "next_due_at": timezone.now(),  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0007_seed_recovery_recomment_poll_job")]

    operations = [migrations.RunPython(apply, revert)]
