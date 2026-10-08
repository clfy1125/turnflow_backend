"""문자(SMS) 주기잡 2종을 ScheduledJob 에 시드.

⚠️ **프로덕션은 celery_beat 를 상시 가동하지 않는다**(compose `profiles: ["fallback"]`).
외부 tick(CF 워커 → `/api/v1/internal/scheduler/tick`)이 `ScheduledJob.next_due_at`
기준으로만 발사한다. 따라서 ``config.settings.base.CELERY_BEAT_SCHEDULE`` 에 넣는 것만으로는
**프로덕션에서 영영 실행되지 않는다.** (dev 는 beat 가 떠 있어 그쪽을 읽는다)

- ``sms.check_balance`` (09:10 · 18:10 KST): 알리고 잔액 감시 → Telegram 경보.
  포인트 소진 = 휴대폰 인증 불가 = **신규 가입 전면 중단**이라 조용히 지나가면 안 된다.
  ⚠️ ``ScheduledJob._next_cron`` 의 cron_hour grammar 는 ``*`` / ``*/N`` / **단일 숫자**만
     지원한다. ``"9,18"`` 같은 목록은 미지원 분기로 떨어져 **매시간 재평가**된다
     (= 의도한 하루 2회가 아니라 24회). 그래서 **행을 두 개**로 나눈다.
- ``sms.purge_old_logs`` (03:50 KST): SmsLog 180일 · PhoneVerification 30일 파기.
  처리방침에 고지한 보유기간을 코드가 강제하는 장치라 안 돌면 고지 위반이 된다.
"""

from django.db import migrations
from django.utils import timezone

_JOBS = [
    # (key, task, cron_hour, cron_minute)
    ("sms-check-balance-am", "sms.check_balance", "9", "10"),
    ("sms-check-balance-pm", "sms.check_balance", "18", "10"),
    ("sms-purge-old-logs", "sms.purge_old_logs", "3", "50"),
]


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    for key, task, hour, minute in _JOBS:
        ScheduledJob.objects.update_or_create(
            key=key,
            defaults={
                "task": task,
                "interval_seconds": None,
                "cron_minute": minute,
                "cron_hour": hour,
                "cron_day_of_week": "",
                "queue": "billing",  # housekeeping 큐 (기존 관례)
                "enabled": True,
                # 즉시 due — 첫 tick 이 정상 cadence 로 재계산한다.
                # ⚠️ check_balance 는 SMS_MOCK_MODE/자격증명 미설정이면 스스로 no-op 이라
                #    배포 직후 즉시 1회 도는 것이 안전하다(오히려 잔액 0 을 바로 알려준다).
                "next_due_at": timezone.now(),
            },
        )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key__in=[k for k, _, _, _ in _JOBS]).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0021_seed_deepseek_balance_job")]

    operations = [migrations.RunPython(seed, unseed)]
