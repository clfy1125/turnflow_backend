"""웹훅이 끊긴 게시물 전용 빠른 폴링(3분) 주기 잡 시드.

prod 에는 celery beat 가 없다 — 주기 실행은 `CF cron 워커(매분) → POST /api/v1/internal/
scheduler/tick` 이 `core.ScheduledJob.next_due_at` 만 보고 발사한다. 그래서
`config.settings.base.CELERY_BEAT_SCHEDULE` 에 넣는 것만으로는 **프로덕션에서 영영 돌지
않는다.** 여기서 DB 행을 직접 시드한다. 멱등(update_or_create).

- dm-poll-silent-media (180초): 인스타가 '연령 제한 콘텐츠'로 분류한 게시물은 **댓글 웹훅만**
  끊기고 자동 DM 발송 자체는 2026-09 중순부터 정상이다(prod 실측 12,539건 중 2534066 0건).
  그래서 이 게시물들의 문제는 "DM 이 안 간다"가 아니라 **"1시간 폴러를 기다리느라 늦게
  간다"** 이고, 고객이 "처음엔 가다가 멈췄어요"라고 말하는 상태가 정확히 이것이다.

  지연이 성과를 깎는 것이 측정된다(폴링 발송 30일 n=9,583, 팔로우게이트 통과율):
      0~3분 63.0% · 3~10분 58.3% · 10~30분 53.0% · 30~60분 52.0%
  주기 N분이면 평균 지연 N/2분이므로 **3분 = 평균 1.5분 = 측정된 최고 구간 전부**.

⚠️ 1시간 레인(dm-poll-missed-comments)은 **그대로 둔다.** 이 잡은 그 위에 얹히는 추가
   레인이고, 발송 경로·중복 방어는 완전히 동일하다(_poll_one_media 재사용 →
   SentDMLog.idempotency_key 가 중복을 막는다). 겹쳐 돌아도 DM 이 두 번 나가지 않는다.

⚠️ enabled=True 로 시드한다 — 읽기 전용 보정이라 고객 데이터를 바꾸지 않고(발송은 원래
   하던 일이다), 1시간 레인이 이미 같은 일을 하고 있어 신규 위험이 없다. 끄려면:

       ScheduledJob.objects.filter(key="dm-poll-silent-media").update(enabled=False)
       # 또는 SILENT_MEDIA_POLL_ENABLED=False (코드 레벨 즉시 no-op)
"""

from django.db import migrations
from django.utils import timezone

_KEY = "dm-poll-silent-media"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.poll_silent_media_comments",
            "interval_seconds": 180,
            "cron_minute": "",
            "cron_hour": "",
            "cron_day_of_week": "",
            # queue="" → send_task 가 CELERY_TASK_ROUTES 로 라우팅(기본 celery_default).
            # ⚠️ 한 바퀴가 90초를 넘으면(대상 약 270개) 전용 큐로 분리할 것 —
            #    그때부터 기본 큐의 다른 잡(1시간 폴러 330초 등)과 슬롯을 다툰다.
            "queue": "",
            "enabled": True,
            "next_due_at": timezone.now(),
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0023_sms_health_job")]

    operations = [migrations.RunPython(apply, revert)]
