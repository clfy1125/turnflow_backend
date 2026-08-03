"""캠페인 게시물 permalink 스위퍼 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create) — 재적용/이미 존재해도 안전.

- dm-sweep-media-permalinks(6시간): media_url 이 빈 specific_media 캠페인의 permalink 를
  IG 에서 조회해 채운다. 어드민 캠페인 목록/상세의 '게시물 보기' 링크 소스.
  생성 경로 전부에 백필 훅을 걸었지만(2026-08-03), 새 경로가 또 생기거나 생성 시점 IG API 가
  일시 실패한 건은 스스로 낫지 않는다 — 이 스위퍼가 최종 안전망이다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "dm-sweep-media-permalinks"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.sweep_missing_media_permalinks",
            "interval_seconds": 6 * 3600,  # 6h — 표시용 보강이라 잦을 필요 없다
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

    dependencies = [("core", "0011_seed_insta_report_jobs")]

    operations = [migrations.RunPython(apply, revert)]
