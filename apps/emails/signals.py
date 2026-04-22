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
        if not instance.is_email_verified and instance.has_usable_password():
            send_verification_email.delay(instance.id)

        if settings.ONBOARDING_ENABLED:
            send_welcome_email.apply_async(args=[instance.id], countdown=5)
            schedule_onboarding.delay(instance.id)
            logger.info("emails.onboarding queued for user=%s", instance.id)
        else:
            logger.info(
                "emails.onboarding disabled via ONBOARDING_ENABLED=False (user=%s)",
                instance.id,
            )
    except Exception:
        logger.exception("Failed to enqueue onboarding emails for user=%s", instance.id)
