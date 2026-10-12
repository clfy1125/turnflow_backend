"""``sms.check_balance`` → ``sms.check_health`` 로 교체 (2026-10-12 알리고 → NCP SENS).

알리고는 선불이라 "잔여 건수"가 건강 지표였지만 SENS 는 후불이라 잔액 개념이 없다.
감시 대상을 **발송 실패율**로 바꿨다 — 사실 이쪽이 더 정직하다. 잔액이 남아 있어도
서명 오류·발신번호 말소·한도 초과면 똑같이 가입이 멈추는데 잔액 감시는 그걸 못 본다.

⚠️ 프로덕션은 celery_beat 를 상시 가동하지 않는다 → ``CELERY_BEAT_SCHEDULE`` 수정만으로는
   반영되지 않는다. 옛 잡 행을 지우지 않으면 **없어진 태스크를 매일 두 번 큐에 넣어**
   워커가 ``NotRegistered`` 로 실패한다.
"""

from django.db import migrations
from django.utils import timezone

_OLD_KEYS = ["sms-check-balance-am", "sms-check-balance-pm"]
_NEW_KEY = "sms-check-health"


def seed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key__in=_OLD_KEYS).delete()
    ScheduledJob.objects.update_or_create(
        key=_NEW_KEY,
        defaults={
            "task": "sms.check_health",
            "interval_seconds": None,
            # 실패율은 1시간 창으로 보므로 매시간 평가한다. cron_hour="*" 는 지원 문법.
            "cron_minute": "10",
            "cron_hour": "*",
            "cron_day_of_week": "",
            "queue": "billing",
            "enabled": True,
            "next_due_at": timezone.now(),
        },
    )


def unseed(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.filter(key=_NEW_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0022_seed_sms_jobs")]

    operations = [migrations.RunPython(seed, unseed)]
