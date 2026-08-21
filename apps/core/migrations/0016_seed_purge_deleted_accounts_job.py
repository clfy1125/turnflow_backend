"""탈퇴 유예 만료 계정 파기 주기 잡 시드 (웹 단독 탈퇴 ④).

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) —
config.settings.base 의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로
여기서 DB 행을 직접 시드한다. 멱등(update_or_create) — 재적용/이미 존재해도 안전.

- purge-deleted-accounts(매일 KST 04:10): deletion_scheduled_at 이 도래한 계정을
  하드 삭제한다. 이 잡이 안 돌면 "탈퇴 확정 후 7일 뒤 영구 삭제" 라는 **대외 고지를
  우리가 어기는 것**이 되므로, 다른 정리 배치들과 달리 기능 플래그·dry-run 을 두지
  않았다(안전장치는 유예 기간과 복구 경로 쪽에 있다).

  하루 1회로 충분한 이유: 유예가 일(day) 단위라 분 단위 정밀도가 의미 없고, 하드 삭제는
  CASCADE 가 넓게 걸려 무거운 작업이라 한가한 새벽에 모아서 도는 편이 낫다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "purge-deleted-accounts"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "authentication.purge_deleted_accounts",
            "interval_seconds": None,
            "cron_minute": "10",
            "cron_hour": "4",
            "cron_day_of_week": "",
            "queue": "billing",
            "enabled": True,
            "next_due_at": timezone.now(),
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0015_seed_campaign_backfill_scan_job")]

    operations = [migrations.RunPython(apply, revert)]
