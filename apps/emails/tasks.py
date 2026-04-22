"""
Celery tasks for email delivery + onboarding drip.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .constants import (
    TEMPLATE_EMAIL_VERIFICATION,
    TEMPLATE_ONBOARDING_DAY_3,
    TEMPLATE_ONBOARDING_DAY_7,
    TEMPLATE_ONBOARDING_DAY_14,
    TEMPLATE_WELCOME,
)
from .models import EmailToken, EmailTokenPurpose, OnboardingSchedule
from .services.sender import EmailTemplateMissing, send_email, send_email_sync

logger = logging.getLogger(__name__)

User = get_user_model()


@shared_task(
    name="emails.send_email",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def send_email_task(log_id: int) -> str:
    log = send_email_sync(log_id)
    return log.status


def _user_context(user) -> dict:
    return {
        "full_name": user.full_name or user.email.split("@")[0],
        "email": user.email,
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "dashboard_url": f"{settings.FRONTEND_URL}/dashboard",
        "docs_url": f"{settings.FRONTEND_URL}/docs",
        "joined_date": timezone.localdate(user.date_joined).isoformat(),
    }


@shared_task(name="emails.send_verification_email")
def send_verification_email(user_id: int) -> None:
    """Issue a new verification token and send the verification email."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    if user.is_email_verified:
        return

    token_row, raw_token = EmailToken.issue(
        user=user,
        purpose=EmailTokenPurpose.EMAIL_VERIFY,
        ttl_minutes=settings.EMAIL_VERIFICATION_TTL_MINUTES,
    )
    ctx = _user_context(user)
    ctx.update(
        {
            "verification_code": token_row.code,
            "verification_url": f"{settings.FRONTEND_URL}/verify-email?token={raw_token}",
            "expires_minutes": settings.EMAIL_VERIFICATION_TTL_MINUTES,
        }
    )
    try:
        send_email(TEMPLATE_EMAIL_VERIFICATION, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("email_verification template missing — did you run seed_email_templates?")


@shared_task(name="emails.send_password_reset_email")
def send_password_reset_email(user_id: int) -> None:
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    token_row, raw_token = EmailToken.issue(
        user=user,
        purpose=EmailTokenPurpose.PASSWORD_RESET,
        ttl_minutes=settings.PASSWORD_RESET_TTL_MINUTES,
    )
    ctx = _user_context(user)
    ctx.update(
        {
            "reset_code": token_row.code,
            "reset_url": f"{settings.FRONTEND_URL}/reset-password?token={raw_token}",
            "expires_minutes": settings.PASSWORD_RESET_TTL_MINUTES,
        }
    )
    from .constants import TEMPLATE_PASSWORD_RESET

    try:
        send_email(TEMPLATE_PASSWORD_RESET, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("password_reset template missing")


@shared_task(name="emails.send_welcome_email")
def send_welcome_email(user_id: int) -> None:
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return
    try:
        send_email(TEMPLATE_WELCOME, user.email, _user_context(user), user=user)
    except EmailTemplateMissing:
        logger.error("welcome template missing")


_DRIP_TEMPLATES = {
    3: TEMPLATE_ONBOARDING_DAY_3,
    7: TEMPLATE_ONBOARDING_DAY_7,
    14: TEMPLATE_ONBOARDING_DAY_14,
}


@shared_task(name="emails.send_onboarding_drip")
def send_onboarding_drip(user_id: int, day: int) -> None:
    """Send day-N onboarding email if the schedule is still active."""
    template_key = _DRIP_TEMPLATES.get(day)
    if template_key is None:
        logger.warning("send_onboarding_drip: unknown day=%s", day)
        return

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    schedule = OnboardingSchedule.objects.filter(
        user=user, template_key=template_key
    ).first()
    if not schedule or schedule.sent_at or schedule.cancelled_at:
        return

    ctx = _user_context(user)
    ctx.update(
        {
            "feature_highlight": "Auto DM 자동화",
            "tip_of_week": "댓글 키워드 규칙으로 반복 작업을 줄여보세요.",
            "cta_url": f"{settings.FRONTEND_URL}/dashboard",
            "upgrade_url": f"{settings.FRONTEND_URL}/billing/plans",
            "trial_days_left": max(0, 14 - (timezone.now() - user.date_joined).days),
        }
    )
    try:
        send_email(template_key, user.email, ctx, user=user)
    except EmailTemplateMissing:
        logger.error("onboarding template %s missing", template_key)
        return

    schedule.sent_at = timezone.now()
    schedule.save(update_fields=["sent_at"])


@shared_task(name="emails.schedule_onboarding")
def schedule_onboarding(user_id: int) -> None:
    """Create OnboardingSchedule rows + queue Celery ETA tasks for the drip."""
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return

    now = timezone.now()
    for day in settings.ONBOARDING_DRIP_DAYS:
        template_key = _DRIP_TEMPLATES.get(day)
        if not template_key:
            continue
        scheduled_for = now + timedelta(days=day)
        OnboardingSchedule.objects.update_or_create(
            user=user,
            template_key=template_key,
            defaults={"scheduled_for": scheduled_for, "sent_at": None, "cancelled_at": None},
        )
        send_onboarding_drip.apply_async(
            args=[user.id, day], eta=scheduled_for
        )
