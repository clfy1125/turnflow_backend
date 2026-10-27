"""대표번호(제로콜 070) 전화 → 문의 안내 문자.

회사 대표번호로 전화가 오면 제로콜 ARS 가 "문자로 안내드리겠습니다" 한마디만 하고 끊고,
제로콜 웹훅(``CALL_RECEIVED`` / ``CALL_COMPLETED``)을 받은 **우리 서버가 SENS 로** 문의
경로(고객문의 페이지·제휴 메일)를 보낸다. 상담원 연결은 없다.

## 왜 제로콜 ARS 의 「문자 발송」 노드를 안 쓰나
그 노드는 반복 발송 제한이 없고, 제로콜 문자 200건/월은 **장문 1통 = 3건**으로 깎인다.
우리가 보내면 번호별 쿨다운·전역 상한을 걸 수 있고 단문(90byte)으로 고정할 수 있다.
⚠️ 제로콜 공개 API·웹훅에는 **누른 번호(ARS 경로)가 없다**(2026-10-27 문서 5종 확인 —
   ``ivrPath`` 는 콘솔 내부 API 에만 있다). 그래서 번호별 분기는 하지 않고 한 문자에 다 담는다.

## 위협모델 — 웹훅에 서명이 없다
제로콜 웹훅은 서명 헤더가 없다. 본문의 ``fromNumber`` 로 문자를 보내므로, URL 이 새면
**아무 번호로나 우리 돈으로 문자를 쏘는 경로**(SMS 펌핑)가 된다. 그래서
① URL 경로의 시크릿 ② 휴대폰 번호만 ③ 번호별 쿨다운(DB) ④ 시간·일 전역 상한(DB) 을 건다.
번호별·전역을 캐시가 아니라 **DB(SmsLog)** 로 세는 이유는 휴대폰 인증과 같다 — Redis flush
한 번에 상한이 다 풀리면 안 된다.
⚠️ 이 문자도 휴대폰 인증과 **같은 NCP 일일 한도**를 쓴다. 전역 상한을 OTP 한도보다 한참
   낮게 둬야 장난 전화가 신규 가입(인증번호)을 막지 못한다.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

#: 문자를 보낼 웹훅 이벤트. 둘 다 받아서 **먼저 온 쪽**으로 보낸다 — CALL_COMPLETED 는
#: STT·요약이 끝난 뒤에 오므로 늦을 수 있고, ARS 만 거친 통화에 오는지 보장이 없다.
TRIGGER_EVENTS = ("CALL_RECEIVED", "CALL_COMPLETED")

_CALL_SEEN_KEY = "sms:zerocall:call:{}"
_PHONE_LOCK_KEY = "sms:zerocall:lock:{}"
_CAP_ALERT_KEY = "sms:zerocall:cap-alert:{}"


def _cfg(name: str, default):
    return getattr(settings, name, default)


def webhook_secret_matches(given: str) -> bool:
    """URL 경로 시크릿 비교. 설정이 비어 있으면 기능 자체가 꺼진 것으로 본다(fail-closed)."""
    expected = (_cfg("ZEROCALL_WEBHOOK_SECRET", "") or "").strip()
    if not expected:
        return False
    return hmac.compare_digest(expected.encode(), (given or "").encode())


@dataclass(frozen=True)
class CallEvent:
    event_type: str
    call_id: str
    from_number: str
    to_number: str


def parse_call_event(payload) -> CallEvent | None:
    """웹훅 본문 → 문자 대상 통화. 대상이 아니면 ``None``.

    ``{"eventType": ..., "timestamp": ..., "data": {callHistoryId, fromNumber, toNumber, ...}}``
    """
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("eventType") or "")
    data = payload.get("data")
    if event_type not in TRIGGER_EVENTS or not isinstance(data, dict):
        return None
    # 우리가 건 전화(발신)의 통화 완료에는 보내지 않는다.
    if event_type == "CALL_COMPLETED" and data.get("isInbound") is False:
        return None
    call_id = str(data.get("callHistoryId") or "")
    from_number = str(data.get("fromNumber") or "")
    if not call_id or not from_number:
        return None
    return CallEvent(
        event_type=event_type,
        call_id=call_id,
        from_number=from_number,
        to_number=str(data.get("toNumber") or ""),
    )


def claim_call(call_id: str) -> bool:
    """같은 통화의 두 이벤트(수신·완료)와 재전송을 한 번으로 접는다."""
    return cache.add(_CALL_SEEN_KEY.format(call_id), 1, timeout=60 * 60 * 24)


def inquiry_body() -> str:
    return _cfg("ZEROCALL_INQUIRY_SMS_BODY", "")


def _recent_count(*, since, to_phone: str | None = None) -> int:
    from .models import SmsLog, SmsPurpose, SmsStatus

    qs = SmsLog.objects.filter(
        purpose=SmsPurpose.PHONE_INQUIRY,
        created_at__gte=since,
        status__in=[SmsStatus.SENT, SmsStatus.PENDING, SmsStatus.MOCKED],
    )
    if to_phone:
        qs = qs.filter(to_phone=to_phone)
    return qs.count()


def _alert_cap(kind: str, cap: int) -> None:
    """상한 도달 경보 — 한 시간에 한 번만 운다."""
    if not cache.add(_CAP_ALERT_KEY.format(kind), 1, timeout=60 * 60):
        return
    from apps.core.telegram import send_telegram_notification

    send_telegram_notification(
        f"📞 대표번호 문의 안내 문자 {kind} 상한({cap}) 도달 — 발송을 멈췄습니다.\n"
        "장난·반복 전화 의심. 제로콜 통화 기록을 확인하세요."
    )


def send_inquiry_guide(*, from_number: str, call_id: str = "") -> str:
    """안내 문자 1통. 결과 머신 키를 돌려준다(로그·테스트용).

    ``sent`` · ``not_mobile`` · ``cooldown`` · ``hourly_cap`` · ``daily_cap`` · ``locked`` ·
    ``no_body`` · ``failed``
    """
    from apps.authentication.phone import InvalidPhoneNumber, mask_phone, normalize_phone

    from .models import SmsPurpose
    from .ncp import SmsProviderError
    from .services import send_and_log

    body = inquiry_body()
    if not body:
        return "no_body"
    try:
        phone = normalize_phone(from_number)
    except InvalidPhoneNumber:
        # 유선·070·발신번호 표시 제한 — 문자를 받을 수 없거나 받을 사람이 아니다.
        return "not_mobile"

    # 같은 번호가 동시에 두 번 들어와도(수신·완료 이벤트 경합) 한 통만 나가게.
    lock = _PHONE_LOCK_KEY.format(phone)
    if not cache.add(lock, 1, timeout=30):
        return "locked"
    try:
        now = timezone.now()
        cooldown_h = int(_cfg("ZEROCALL_INQUIRY_COOLDOWN_HOURS", 24))
        if _recent_count(since=now - timedelta(hours=cooldown_h), to_phone=phone):
            return "cooldown"

        hourly = int(_cfg("ZEROCALL_INQUIRY_HOURLY_CAP", 30))
        if hourly > 0 and _recent_count(since=now - timedelta(hours=1)) >= hourly:
            logger.error("zerocall_inquiry: 시간당 상한(%s) 도달 — 발송 차단", hourly)
            _alert_cap("시간당", hourly)
            return "hourly_cap"
        daily = int(_cfg("ZEROCALL_INQUIRY_DAILY_CAP", 100))
        if daily > 0 and _recent_count(since=now - timedelta(days=1)) >= daily:
            logger.error("zerocall_inquiry: 일일 상한(%s) 도달 — 발송 차단", daily)
            _alert_cap("일일", daily)
            return "daily_cap"

        try:
            send_and_log(to=phone, body=body, purpose=SmsPurpose.PHONE_INQUIRY, title="턴플로우")
        except SmsProviderError as exc:
            logger.warning(
                "zerocall_inquiry: 발송 실패 to=%s call=%s code=%s",
                mask_phone(phone),
                call_id,
                exc.code,
            )
            return "failed"
        logger.info("zerocall_inquiry: sent to=%s call=%s", mask_phone(phone), call_id)
        return "sent"
    finally:
        cache.delete(lock)
