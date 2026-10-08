"""문자(SMS/LMS) 발송 감사 로그.

``apps.emails.EmailLog`` 와 같은 자리를 차지한다 — "우리가 이 사람에게 무엇을 언제
보냈는가" 를 CS·분쟁·과금 대사에서 되짚을 수 있어야 한다. 문자는 메일과 달리 **건당
돈이 나가므로**(알리고 선불 충전) 발송량 추이를 보는 것 자체가 비용 통제다.

⚠️ **본문에 인증번호를 그대로 적지 않는다.** 저장 시 ``mask_code`` 로 숫자 6자리를
   가린다. OTP 를 평문으로 남기면 DB 열람 권한 하나가 곧 계정 탈취 권한이 된다
   (지침 14 — 토큰·키 평문 저장 금지와 같은 이유).
⚠️ 수신번호는 개인정보다. 조회 화면(어드민)에서는 마스킹해 보여주고, 원문은 발송·CS
   목적 외로 쓰지 않는다. 보존기간은 ``sms.purge_old_logs`` 가 강제한다.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.db import models

#: 본문에서 가릴 숫자 뭉치 — 4자리 이상 연속 숫자는 전부 인증번호로 간주한다.
_DIGIT_RUN = re.compile(r"\d{4,}")


def mask_code(body: str) -> str:
    """로그 저장용 본문 마스킹. ``[인증번호] 123456 …`` → ``[인증번호] ****** …``"""
    return _DIGIT_RUN.sub(lambda m: "*" * len(m.group(0)), body or "")


class SmsPurpose(models.TextChoices):
    PHONE_VERIFY = "phone_verify", "휴대폰 본인확인"
    NOTICE = "notice", "정보성 안내"


class SmsStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    MOCKED = "mocked", "Mocked"  # SMS_MOCK_MODE — 실제로 나가지 않았다


class SmsLog(models.Model):
    """발송 시도 1건 = 1행."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sms_logs",
    )
    purpose = models.CharField(
        max_length=32, choices=SmsPurpose.choices, default=SmsPurpose.PHONE_VERIFY, db_index=True
    )
    to_phone = models.CharField(max_length=20, db_index=True, verbose_name="수신번호(숫자만)")
    sender = models.CharField(max_length=20, blank=True, default="", verbose_name="발신번호")
    body = models.TextField(blank=True, default="", verbose_name="본문(인증번호 마스킹됨)")
    status = models.CharField(
        max_length=16, choices=SmsStatus.choices, default=SmsStatus.PENDING, db_index=True
    )
    provider = models.CharField(max_length=16, default="aligo")
    provider_message_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    result_code = models.IntegerField(null=True, blank=True, verbose_name="알리고 result_code")
    error_message = models.TextField(blank=True, default="")
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sms_logs"
        verbose_name = "SMS 발송 로그"
        verbose_name_plural = "SMS 발송 로그 목록"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["to_phone", "created_at"]),
            models.Index(fields=["purpose", "status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.status}] {self.purpose} -> {self.to_phone[:3]}****{self.to_phone[-4:]}"
