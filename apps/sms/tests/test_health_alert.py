"""문자 발송 건강 감시 — 울려야 할 때만 울어야 한다.

경보가 과하면 운영자가 무시하게 되고, 모자라면 **신규 가입이 전면 중단된 걸 몇 시간 뒤에**
안다. deepseek 감시와 같은 규약(등급 악화 시 즉시 / 같으면 N시간마다 1회 / 경보가
나갔던 경우에만 회복)을 쓰는지 못 박는다.

⚠️ 2026-10-12 알리고(선불 잔액) → NCP SENS(후불) 교체로 감시 대상이 '잔여 건수'에서
   '발송 실패율'로 바뀌었다. 파일명은 히스토리를 위해 유지한다.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.sms.models import SmsLog, SmsStatus
from apps.sms.tasks import _HEALTH_ALERT_STATE_KEY, check_health

pytestmark = pytest.mark.django_db


@pytest.fixture
def live(settings):
    """감시가 실제로 돌도록 Mock 모드를 끄고 자격증명을 채운 것처럼 만든다."""
    settings.SMS_MOCK_MODE = False
    settings.NCP_ACCESS_KEY = "test-access"
    settings.NCP_SECRET_KEY = "test-secret"
    settings.NCP_SMS_SERVICE_ID = "ncp:sms:kr:000:test"
    settings.NCP_SMS_SENDER = "07000000000"
    settings.SMS_HEALTH_WINDOW_MINUTES = 60
    settings.SMS_HEALTH_MIN_SAMPLE = 5
    settings.SMS_HEALTH_WARN_FAIL_RATE = 0.3
    settings.SMS_HEALTH_CRIT_FAIL_RATE = 0.6
    settings.SMS_DAILY_VOLUME_WARN = 0  # 기본은 발송량 경보를 끄고 실패율만 본다
    settings.SMS_HEALTH_REPEAT_HOURS = 6
    cache.delete(_HEALTH_ALERT_STATE_KEY)
    # dev DB 를 공유하므로 창 안의 잔존 행을 치운다(memory: test-db-not-clean).
    SmsLog.objects.filter(created_at__gte=timezone.now() - timedelta(hours=26)).delete()
    yield settings
    cache.delete(_HEALTH_ALERT_STATE_KEY)
    SmsLog.objects.filter(to_phone__startswith="0109999").delete()


def _logs(sent: int, failed: int):
    for i in range(sent):
        SmsLog.objects.create(to_phone=f"0109999{i:04d}", status=SmsStatus.SENT)
    for i in range(failed):
        SmsLog.objects.create(to_phone=f"0109999{900 + i:04d}", status=SmsStatus.FAILED)


def _run():
    with patch("apps.core.telegram.send_telegram_notification", return_value=True) as tg:
        return check_health(), tg


class TestFailureRate:
    def test_all_good_is_silent(self, live):
        _logs(sent=10, failed=0)
        res, tg = _run()
        assert res["level"] == "ok"
        assert tg.call_count == 0

    def test_warn_threshold(self, live):
        _logs(sent=6, failed=4)  # 40%
        res, tg = _run()
        assert res["level"] == "warn"
        assert tg.call_count == 1
        assert "🟡" in tg.call_args[0][0]

    def test_crit_threshold(self, live):
        _logs(sent=2, failed=8)  # 80%
        res, tg = _run()
        assert res["level"] == "crit"
        assert "🔴" in tg.call_args[0][0]
        assert "신규 가입이 중단" in tg.call_args[0][0]

    def test_small_sample_is_not_judged(self, live):
        """1건 중 1건 실패를 100% 로 읽으면 경보가 쓰레기가 된다."""
        _logs(sent=0, failed=2)  # 표본 2 < MIN_SAMPLE 5
        res, tg = _run()
        assert res["level"] == "ok"
        assert tg.call_count == 0

    def test_old_failures_fall_out_of_the_window(self, live):
        _logs(sent=0, failed=10)
        SmsLog.objects.filter(to_phone__startswith="0109999").update(
            created_at=timezone.now() - timedelta(hours=3)
        )
        res, tg = _run()
        assert res["attempts"] == 0
        assert res["level"] == "ok"
        assert tg.call_count == 0


class TestVolume:
    def test_daily_volume_warns(self, live):
        live.SMS_DAILY_VOLUME_WARN = 5
        _logs(sent=6, failed=0)
        res, tg = _run()
        assert res["level"] == "warn"
        assert "오늘 발송" in tg.call_args[0][0]

    def test_volume_off_by_default(self, live):
        _logs(sent=50, failed=0)
        res, tg = _run()
        assert res["level"] == "ok"
        assert tg.call_count == 0


class TestDeduplication:
    def test_same_level_does_not_repeat(self, live):
        _logs(sent=6, failed=4)
        _run()
        _res, tg = _run()
        assert tg.call_count == 0

    def test_worsening_alerts_immediately(self, live):
        _logs(sent=6, failed=4)  # warn
        _run()
        SmsLog.objects.filter(to_phone__startswith="0109999").delete()
        _logs(sent=1, failed=9)  # crit
        _res, tg = _run()
        assert tg.call_count == 1

    def test_recovery_only_after_an_alert(self, live):
        _logs(sent=10, failed=0)
        _res, tg = _run()
        assert tg.call_count == 0  # 조용히 지나간 건 조용히 끝낸다

        SmsLog.objects.filter(to_phone__startswith="0109999").delete()
        _logs(sent=1, failed=9)
        _run()  # crit 경보
        SmsLog.objects.filter(to_phone__startswith="0109999").delete()
        _logs(sent=10, failed=0)
        _res, tg = _run()
        assert tg.call_count == 1
        assert "🟢" in tg.call_args[0][0]


class TestSkips:
    def test_mock_mode_skips(self, live):
        """mock 발송은 실패하지 않아 늘 건강해 보인다 — 판정하면 거짓 안심이 된다."""
        live.SMS_MOCK_MODE = True
        assert check_health()["skipped"] == "mock_or_unconfigured"

    def test_unconfigured_skips(self, live):
        live.NCP_ACCESS_KEY = ""
        assert check_health()["skipped"] == "mock_or_unconfigured"
