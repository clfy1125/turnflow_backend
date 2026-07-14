"""실패 DM 복구 재댓글 폴링 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create) — 재적용/이미 존재해도 안전.

- dm-poll-recovery-recomments(1h): 안내 대댓글이 게시된 RECOVERY_PENDING 의 원 댓글
  replies edge 를 재조회해, comments 웹훅이 유실된 스레드 답글 재댓글을 복구 라우팅.
  없으면 웹훅 유실 시 스레드 답글 복구가 TTL(기본 7일) 만료까지 조용히 멈춘다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "dm-poll-recovery-recomments"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.poll_recovery_recomments",
            "interval_seconds": 3600,
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

    dependencies = [("core", "0006_seed_pacer_recovery_analytics_jobs")]

    operations = [migrations.RunPython(apply, revert)]
