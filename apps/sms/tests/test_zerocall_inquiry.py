"""대표번호(제로콜) 전화 → 문의 안내 문자.

웹훅에 서명이 없어서, 이 경로가 새면 **아무 번호로나 우리 돈으로 문자를 쏘는 길**이 된다.
그래서 못 박는 것: 시크릿 불일치는 404 · 통화당 1통 · 번호당 쿨다운 · 전역 상한 ·
휴대폰 번호만 · 본문은 단문.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.sms import zerocall
from apps.sms.models import SmsLog, SmsPurpose, SmsStatus
from apps.sms.ncp import SMS_BYTE_LIMIT, sms_byte_length

pytestmark = pytest.mark.django_db

SECRET = "test-secret-abc"
URL = f"/api/v1/sms/zerocall/webhook/{SECRET}/"


@pytest.fixture(autouse=True)
def _setup(settings):
    settings.ZEROCALL_WEBHOOK_SECRET = SECRET
    settings.ZEROCALL_INQUIRY_COOLDOWN_HOURS = 24
    settings.ZEROCALL_INQUIRY_HOURLY_CAP = 30
    settings.ZEROCALL_INQUIRY_DAILY_CAP = 100
    settings.SMS_MOCK_MODE = True
    cache.clear()
    yield
    cache.clear()


def _payload(event="CALL_RECEIVED", call_id="call-1", from_number="01012345678", **extra):
    data = {"callHistoryId": call_id, "fromNumber": from_number, "toNumber": "07052364516"}
    data.update(extra)
    return {"eventType": event, "timestamp": "2026-10-27T09:20:11.000Z", "data": data}


def _inquiry_logs():
    return SmsLog.objects.filter(purpose=SmsPurpose.PHONE_INQUIRY)


def test_default_body_fits_single_sms(settings):
    from django.conf import settings as s

    body = s.ZEROCALL_INQUIRY_SMS_BODY
    assert "turnflow.link/support" in body and "contact@clfy.ai.kr" in body
    assert sms_byte_length(body) <= SMS_BYTE_LIMIT


@pytest.mark.parametrize("secret", ["wrong", ""])
def test_wrong_secret_is_404_and_sends_nothing(secret):
    with patch("apps.sms.tasks.send_phone_inquiry_guide.delay") as delay:
        resp = APIClient().post(
            f"/api/v1/sms/zerocall/webhook/{secret or 'x'}/", _payload(), format="json"
        )
    assert resp.status_code == 404
    delay.assert_not_called()


def test_feature_off_when_secret_unset(settings):
    settings.ZEROCALL_WEBHOOK_SECRET = ""
    resp = APIClient().post(URL, _payload(), format="json")
    assert resp.status_code == 404


def test_webhook_enqueues_once_per_call():
    with patch("apps.sms.tasks.send_phone_inquiry_guide.delay") as delay:
        c = APIClient()
        assert c.post(URL, _payload("CALL_RECEIVED"), format="json").status_code == 200
        # 같은 통화의 완료 이벤트·재전송은 접는다
        assert (
            c.post(URL, _payload("CALL_COMPLETED", isInbound=True), format="json").status_code
            == 200
        )
        assert c.post(URL, _payload("CALL_RECEIVED"), format="json").status_code == 200
    delay.assert_called_once_with("01012345678", "call-1")


@pytest.mark.parametrize(
    "payload",
    [
        {"eventType": "MESSAGE_RECEIVED", "data": {"callHistoryId": "x", "fromNumber": "010"}},
        _payload("CALL_COMPLETED", isInbound=False),  # 우리가 건 전화
        _payload(from_number=""),  # 발신번호 표시 제한
        {"garbage": True},
        [],
    ],
)
def test_non_target_events_are_acknowledged_but_ignored(payload):
    with patch("apps.sms.tasks.send_phone_inquiry_guide.delay") as delay:
        resp = APIClient().post(URL, payload, format="json")
    assert resp.status_code == 200
    delay.assert_not_called()


def test_send_logs_inquiry_purpose():
    assert zerocall.send_inquiry_guide(from_number="010-1234-5678", call_id="c") == "sent"
    log = _inquiry_logs().get()
    assert log.to_phone == "01012345678"
    assert log.status == SmsStatus.MOCKED


@pytest.mark.parametrize("number", ["0212345678", "07052364516", "anonymous"])
def test_non_mobile_numbers_are_skipped(number):
    assert zerocall.send_inquiry_guide(from_number=number) == "not_mobile"
    assert not _inquiry_logs().exists()


def test_same_number_cooldown():
    assert zerocall.send_inquiry_guide(from_number="01012345678", call_id="a") == "sent"
    assert zerocall.send_inquiry_guide(from_number="01012345678", call_id="b") == "cooldown"
    assert _inquiry_logs().count() == 1


def test_cooldown_expires():
    zerocall.send_inquiry_guide(from_number="01012345678")
    _inquiry_logs().update(created_at=timezone.now() - timedelta(hours=25))
    assert zerocall.send_inquiry_guide(from_number="01012345678") == "sent"


def test_cooldown_ignores_otp_logs():
    """같은 번호가 인증번호를 받았다고 안내 문자를 막으면 안 된다."""
    SmsLog.objects.create(
        purpose=SmsPurpose.PHONE_VERIFY, to_phone="01012345678", status=SmsStatus.SENT
    )
    assert zerocall.send_inquiry_guide(from_number="01012345678") == "sent"


def test_hourly_cap_stops_and_alerts_once(settings):
    settings.ZEROCALL_INQUIRY_HOURLY_CAP = 2
    with patch("apps.core.telegram.send_telegram_notification") as tg:
        assert zerocall.send_inquiry_guide(from_number="01000000001") == "sent"
        assert zerocall.send_inquiry_guide(from_number="01000000002") == "sent"
        assert zerocall.send_inquiry_guide(from_number="01000000003") == "hourly_cap"
        assert zerocall.send_inquiry_guide(from_number="01000000004") == "hourly_cap"
    assert _inquiry_logs().count() == 2
    assert tg.call_count == 1


def test_daily_cap(settings):
    settings.ZEROCALL_INQUIRY_HOURLY_CAP = 0
    settings.ZEROCALL_INQUIRY_DAILY_CAP = 1
    with patch("apps.core.telegram.send_telegram_notification"):
        assert zerocall.send_inquiry_guide(from_number="01000000001") == "sent"
        assert zerocall.send_inquiry_guide(from_number="01000000002") == "daily_cap"


def test_failed_send_does_not_start_cooldown():
    from apps.sms.ncp import SmsProviderError

    with patch("apps.sms.services.send_sms", side_effect=SmsProviderError("SMS_SEND_FAILED", "x")):
        assert zerocall.send_inquiry_guide(from_number="01012345678") == "failed"
    # 실패는 쿨다운·상한에 세지 않는다 — 다시 걸면 받을 수 있어야 한다
    assert zerocall.send_inquiry_guide(from_number="01012345678") == "sent"
