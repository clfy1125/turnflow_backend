"""캠페인 게시물 썸네일 스위퍼 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create) — 재적용/이미 존재해도 안전.

- dm-sweep-campaign-thumbnails(6시간): thumbnail_url 이 빈 캠페인의 게시물 이미지를 IG 에서
  받아 우리 스토리지(R2)에 재호스팅한다. IG CDN URL 은 서명된 일시 URL 이라 저장해도 곧
  깨지므로 사본 보관이 유일한 영구 해법이다(apps/integrations/media_thumbnail.py 참고).
  생성/게시물변경 훅 + 목록조회 기회 발행이 1차 경로이고, 이 스위퍼가 최종 안전망이다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "dm-sweep-campaign-thumbnails"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.sweep_missing_campaign_thumbnails",
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

    dependencies = [("core", "0012_seed_media_permalink_sweep_job")]

    operations = [migrations.RunPython(apply, revert)]
