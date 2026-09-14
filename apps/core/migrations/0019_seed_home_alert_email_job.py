"""홈 알림 이메일(연결 끊김·월 한도 소진) 주기 잡 시드.

CF tick 은 ScheduledJob DB 행의 next_due_at 으로 due 판단한다(0002 참고) — 여기서 DB 행을
직접 시드한다. 멱등(update_or_create).

- home-send-alert-emails(매일 10:20 KST): 자동화가 멈춘 상태로 24시간 넘게 콘솔에
  들어오지 않은 사용자에게 안내 메일 1통. 같은 사건은 7일에 1통을 넘지 않는다.

⚠️ **이중 잠금이다 — 배포해도 메일이 나가지 않는다.**
   ① 이 잡은 enabled=False 로 시드된다.
   ② 태스크 자체가 settings.HOME_ALERT_EMAILS_ENABLED(기본 False)로 한 번 더 막힌다.
   실사용자에게 나가는 메일이라, 문구 검수 → 발송 도메인 평판 확인 → 대상 수 실측을 마친 뒤
   사람이 둘 다 켜는 순서를 강제한다. 켜는 법:

       # 1) 환경변수 HOME_ALERT_EMAILS_ENABLED=True 로 배포
       # 2) ScheduledJob.objects.filter(key="home-send-alert-emails").update(
       #        enabled=True, next_due_at=timezone.now())

   대상 수 먼저 세는 법: send_alert_emails 는 게이트가 꺼져 있으면 아무 것도 하지 않으므로,
   apps.home.alerts.build_home_alerts 로 직접 세어 볼 것.
"""

from django.db import migrations
from django.utils import timezone

_KEY = "home-send-alert-emails"


def apply(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.update_or_create(
        key=_KEY,
        defaults={
            "task": "home.send_alert_emails",
            "interval_seconds": None,
            "cron_minute": "20",
            "cron_hour": "10",
            "cron_day_of_week": "",
            "queue": "",
            "enabled": False,  # ⚠️ 의도적으로 꺼진 채 — 모듈 docstring 참고
            "next_due_at": timezone.now(),
        },
    )


def revert(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0018_seed_latest_media_refresh_job")]

    operations = [migrations.RunPython(apply, revert)]
