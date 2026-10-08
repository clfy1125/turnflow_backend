"""SMS 유지보수 배치."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="sms.purge_old_logs")
def purge_old_logs() -> dict:
    """보존기간이 지난 ``SmsLog`` 를 지운다 (개인정보 최소 보유 — 수신번호가 들어 있다).

    기본 180일. 통신비밀·전자상거래 분쟁 대응에 필요한 기간을 남기되 그 이상 들고 있지
    않는다. 만료된 ``PhoneVerification`` 행도 같은 배치에서 정리한다 — 그쪽은 코드
    해시와 번호를 들고 있어 더 짧게(30일) 간다.
    """
    from apps.authentication.models import PhoneVerification

    from .models import SmsLog

    now = timezone.now()
    sms_days = int(getattr(settings, "SMS_LOG_RETENTION_DAYS", 180))
    otp_days = int(getattr(settings, "PHONE_VERIFICATION_RETENTION_DAYS", 30))

    sms_deleted, _ = SmsLog.objects.filter(
        created_at__lt=now - timezone.timedelta(days=sms_days)
    ).delete()
    otp_deleted, _ = PhoneVerification.objects.filter(
        created_at__lt=now - timezone.timedelta(days=otp_days)
    ).delete()

    logger.info("sms.purge_old_logs: sms=%s otp=%s", sms_deleted, otp_deleted)
    return {"sms_logs_deleted": sms_deleted, "phone_verifications_deleted": otp_deleted}
