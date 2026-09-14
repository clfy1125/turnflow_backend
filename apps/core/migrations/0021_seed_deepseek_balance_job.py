"""DeepSeek 잔액 감시(apps.core.tasks.check_deepseek_balance)를 ScheduledJob 에 시드.

⚠️ 프로덕션은 celery_beat 를 상시 가동하지 않는다(profiles: [fallback]). 외부 tick
(CF 워커 → /api/v1/internal/scheduler/tick)이 ScheduledJob.next_due_at 기준으로만 발사한다.
따라서 config.settings.base.CELERY_BEAT_SCHEDULE 추가만으로는 프로덕션에서 실행되지 않는다.

배경(2026-09-11~14): DeepSeek 계정 잔액이 0 이 되어
  - AI 페이지 생성(bio_remake)이 402 로 53건 실패(14명),
  - 인스타 리포트 6건은 **실패하지 않고** 전 슬롯을 템플릿 문장으로 대체해 배달(쿼터까지 소모)
되는 동안 3일간 아무도 몰랐다. 사람이 알아채야만 알 수 있는 상태를 없애는 것이 이 잡의 목적.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "check-deepseek-balance"


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "apps.core.tasks.check_deepseek_balance",
            "interval_seconds": 3 * 3600,  # 3시간
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            "queue": "billing",
            "enabled": True,
            "next_due_at": timezone.now(),  # 즉시 due — 첫 tick 이 정상 cadence 로 재계산
        },
    )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0020_seed_funnel_event_cleanup_job")]

    operations = [migrations.RunPython(seed, unseed)]
