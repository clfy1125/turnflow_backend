"""홈 알림용 「최신 게시물」 사전 조회 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — config.settings.base
의 CELERY_BEAT_SCHEDULE 변경만으론 프로덕션에 반영되지 않으므로 여기서 DB 행을 직접 시드한다.
멱등(update_or_create).

- ig-refresh-latest-media(1시간): 활성 IG 연동의 최신 게시물 1건을 DB 에 적어 둔다.
  홈 알림 `recent_post_no_campaign`(최근 7일 내 게시물인데 캠페인이 없음) 판정의 유일한 근거다.

왜 미리 적어 두는가 — 홈은 로그인한 **모든** 사용자의 첫 화면이다. 거기서 게시물 목록을
직접 부르면 홈 진입 1회 = Graph 호출 1회가 되고, Graph 쿼터는 앱 단위 공유라 그 비용이
다른 워크스페이스의 댓글 수집·DM 발송을 굶긴다. 여기서 계정당 시간당 1콜만 쓴다.

enabled=True 로 시드해도 안전하다 — **읽기 전용**이다(연동 행의 표시용 필드만 갱신하고
캠페인·발송에는 손대지 않는다). status 를 바꾸는 0017 과 성격이 다르다.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "ig-refresh-latest-media"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "integrations.refresh_latest_media",
            "interval_seconds": 3600,  # 1h — "새 게시물" 판정 단위가 일(day)이라 충분하다.
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

    dependencies = [("core", "0017_seed_restricted_campaign_sweep_job")]

    operations = [migrations.RunPython(apply, revert)]
