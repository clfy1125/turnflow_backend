"""알리고 잔액 감시 — 울려야 할 때만 울어야 한다.

경보가 과하면 운영자가 무시하게 되고, 모자라면 **신규 가입이 전면 중단된 걸 몇 시간 뒤에**
안다. deepseek 감시와 같은 규약(등급 악화 시 즉시 / 같으면 N시간마다 1회 / 경보가
나갔던 경우에만 회복 알림)을 쓰는지 못 박는다.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache

from apps.sms.tasks import _ALIGO_ALERT_STATE_KEY, check_balance

pytestmark = pytest.mark.django_db


@pytest.fixture
def live(settings):
    """감시가 실제로 돌도록 Mock 모드를 끄고 자격증명을 채운 것처럼 만든다."""
    settings.SMS_MOCK_MODE = False
    settings.ALIGO_API_KEY = "test-key"
    settings.ALIGO_USER_ID = "test-user"
    settings.ALIGO_SENDER = "01000000000"
    settings.ALIGO_BALANCE_WARN_COUNT = 1000
    settings.ALIGO_BALANCE_CRIT_COUNT = 200
    settings.ALIGO_BALANCE_REPEAT_HOURS = 24
    cache.delete(_ALIGO_ALERT_STATE_KEY)
    yield settings
    cache.delete(_ALIGO_ALERT_STATE_KEY)


def _run(sms_left):
    with patch("apps.sms.aligo.remaining_counts", return_value={"sms": sms_left}), patch(
        "apps.core.telegram.send_telegram_notification", return_value=True
    ) as tg:
        return check_balance(), tg


class TestLevels:
    def test_plenty_is_silent(self, live):
        res, tg = _run(5000)
        assert res["level"] == "ok"
        assert tg.call_count == 0, "넉넉할 땐 조용해야 한다"

    def test_warn_threshold(self, live):
        res, tg = _run(900)
        assert res["level"] == "warn"
        assert tg.call_count == 1
        assert "🟡" in tg.call_args[0][0]

    def test_crit_threshold(self, live):
        res, tg = _run(150)
        assert res["level"] == "crit"
        assert "🔴" in tg.call_args[0][0]

    def test_zero_says_signup_is_down(self, live):
        """0건이면 '문자 안 감'이 아니라 '가입 중단'이라고 말해야 운영자가 바로 움직인다."""
        _res, tg = _run(0)
        assert "신규 가입 전면 중단" in tg.call_args[0][0]


class TestDeduplication:
    def test_same_level_does_not_repeat(self, live):
        _run(900)
        _res, tg = _run(900)
        assert tg.call_count == 0, "같은 등급이 이어지면 24시간 안엔 다시 울리지 않는다"

    def test_worsening_alerts_immediately(self, live):
        _run(900)  # warn
        _res, tg = _run(150)  # crit
        assert tg.call_count == 1, "등급이 나빠지면 반복 주기를 기다리지 않는다"

    def test_recovery_only_after_an_alert(self, live):
        # 경보 없이 바로 ok → 조용히 끝낸다
        _res, tg = _run(5000)
        assert tg.call_count == 0
        # 경보가 나갔던 뒤의 회복만 알린다
        _run(150)
        _res, tg = _run(5000)
        assert tg.call_count == 1
        assert "🟢" in tg.call_args[0][0]


class TestFailureIsNotAnAlert:
    def test_lookup_failure_stays_quiet(self, live):
        """네트워크 블립으로 가짜 🔴 를 울리면 경보를 믿지 않게 된다."""
        with patch("apps.sms.aligo.remaining_counts", return_value={}), patch(
            "apps.core.telegram.send_telegram_notification"
        ) as tg:
            res = check_balance()
        assert res["ok"] is False
        assert tg.call_count == 0

    def test_mock_mode_skips(self, live):
        live.SMS_MOCK_MODE = True
        res = check_balance()
        assert res["skipped"] == "mock_or_unconfigured"

    def test_unconfigured_skips(self, live):
        live.ALIGO_API_KEY = ""
        res = check_balance()
        assert res["skipped"] == "mock_or_unconfigured"
