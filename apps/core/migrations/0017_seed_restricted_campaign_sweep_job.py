"""게시물 제한(연령 제한 콘텐츠) 캠페인 자동 정지 스위퍼 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create) — 재적용/이미 존재해도 안전.

- dm-sweep-restricted-campaigns(1시간): 인스타가 '연령 제한 콘텐츠'로 분류한 게시물의 활성
  캠페인을 찾아 자동 정지한다. 그 게시물에서는 댓글 웹훅이 끊기고 비공개 답장이 거부되므로
  캠페인을 켜둔 채 두면 폴러가 매시간 새 댓글러를 찾아 시도하고 전부 실패해 **실패만 무한히
  쌓인다**(실서버 실측: 한 고객 257명·다른 고객 96명이 아무 알림 없이 누적).
  판정은 apps/integrations/ig_content_restriction.py 단일 소스, 외부 호출 0(우리 DB 만 읽는다).
  조사: docs/system/DM_2534066_MEDIA_BLOCK_CENSUS_2026-09-07.md

⚠️ **enabled=False 로 시드한다 — 배포 직후 자동으로 돌지 않는다.**
   이 잡은 **고객 캠페인의 status 를 바꾸는 쓰기 작업**이고, 배포 시점 prod 실측으로 첫 실행에
   13개 캠페인이 한 번에 정지된다. 그래서 배포 → dry-run 으로 대상 확인 → 고객 안내 준비 →
   그 다음에 사람이 켜는 순서를 강제한다. 켜는 법:

       ScheduledJob.objects.filter(key="dm-sweep-restricted-campaigns").update(
           enabled=True, next_due_at=timezone.now()
       )

   dry-run 확인: sweep_restricted_campaigns(dry_run=True) → {"details": [...]} 만 돌려주고
   아무 것도 바꾸지 않는다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "dm-sweep-restricted-campaigns"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.sweep_restricted_campaigns",
            # 1h — 댓글 폴러가 1시간 주기라 그보다 자주 돌 이유가 없다.
            "interval_seconds": 3600,
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            # queue="" → send_task 가 CELERY_TASK_ROUTES 로 라우팅(기본 celery_default).
            # DB 만 읽는 가벼운 잡이라 전용 큐가 필요 없다.
            "queue": "",
            # ⚠️ 의도적으로 꺼진 채로 시드한다 — 모듈 docstring 참고.
            "enabled": False,
            "next_due_at": timezone.now(),
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0016_seed_purge_deleted_accounts_job")]

    operations = [migrations.RunPython(apply, revert)]
