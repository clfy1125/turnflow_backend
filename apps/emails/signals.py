"""
Signup → send verification + welcome + schedule drip.

The auth views also call these paths explicitly to set `is_email_verified=True`
for OAuth signups before this signal fires.  Do not duplicate verification
emails here if a user is already verified (e.g. Google OAuth path).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.authentication.models import User

from .tasks import schedule_onboarding, send_verification_email, send_welcome_email

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def on_user_created(sender, instance: User, created: bool, **kwargs):
    if not created:
        return

    # Broker outages / test harnesses without Celery must NOT block signup.
    try:
        if not instance.is_email_verified:
            # 이메일 가입(사용 가능한 비밀번호 보유) → 인증 메일만 발송.
            # 환영 메일은 코드 인증 성공 시점(VerifyEmailView)으로 이동한다.
            if instance.has_usable_password():
                send_verification_email.delay(instance.id)
        else:
            # 가입 시점에 이미 인증된 계정(구글 OAuth 등) → 즉시 환영 메일.
            send_welcome_email.apply_async(args=[instance.id], countdown=5)

        # 3/7/14 day drip is marketing-style — gated behind ONBOARDING_ENABLED.
        if settings.ONBOARDING_ENABLED:
            schedule_onboarding.delay(instance.id)
            logger.info("emails.drip queued for user=%s", instance.id)
        else:
            logger.info("emails.drip disabled via ONBOARDING_ENABLED=False (user=%s)", instance.id)
    except Exception:
        logger.exception("Failed to enqueue onboarding emails for user=%s", instance.id)
