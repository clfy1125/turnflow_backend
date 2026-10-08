"""문자 발송 + 감사 로그 — 앱 전체가 쓰는 **단일 진입점**.

``aligo.send_sms`` 를 직접 부르지 말 것. 여기를 거치지 않으면 ``SmsLog`` 가 남지 않아
"보냈는데 못 받았다" CS 를 추적할 수 없고, 비용(건당 과금) 집계도 구멍이 난다.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from .aligo import AligoError, SendResult, send_sms
from .models import SmsLog, SmsPurpose, SmsStatus, mask_code

logger = logging.getLogger(__name__)


def send_and_log(
    *,
    to: str,
    body: str,
    purpose: str = SmsPurpose.PHONE_VERIFY,
    user=None,
    title: str = "",
    request_ip: str | None = None,
) -> tuple[SendResult | None, SmsLog]:
    """문자를 보내고 결과를 ``SmsLog`` 에 남긴다.

    성공 ``(SendResult, log)`` · 실패 ``AligoError`` 를 **그대로 올린다**(뷰가 사용자
    문구를 고른다). 실패해도 로그 행은 ``failed`` 로 남는다 — 실패를 남기지 않으면
    알리고 잔액 소진 같은 사고가 "요청이 아예 없었던 것"처럼 보인다.
    """
    from .aligo import _credentials  # 발신번호는 로그에만 쓴다

    sender = _credentials()[2]
    log = SmsLog.objects.create(
        user=user,
        purpose=purpose,
        to_phone=to,
        sender=sender,
        body=mask_code(body),
        status=SmsStatus.PENDING,
        request_ip=request_ip,
    )
    try:
        result = send_sms(to=to, body=body, title=title)
    except AligoError as exc:
        log.status = SmsStatus.FAILED
        log.result_code = exc.result_code
        log.error_message = f"{exc.code}: {exc.message}"
        log.save(update_fields=["status", "result_code", "error_message"])
        raise

    log.status = SmsStatus.MOCKED if result.mocked else SmsStatus.SENT
    log.provider_message_id = result.message_id
    log.result_code = result.result_code
    log.sent_at = timezone.now()
    log.save(update_fields=["status", "provider_message_id", "result_code", "sent_at"])
    return result, log
