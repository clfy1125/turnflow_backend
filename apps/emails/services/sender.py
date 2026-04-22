"""
High-level email sending API.

`send_email()` creates an `EmailLog` row and enqueues a Celery task.  Views
should call this, not Resend directly — it guarantees an audit trail and async
delivery (per CLAUDE.md §5.3: views must not block on external APIs).

`send_email_sync()` performs the provider call inline.  Only the Celery task
and the admin "test send" endpoint should use this.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.conf import settings
from django.utils import timezone

from ..models import EmailLog, EmailStatus, EmailTemplate
from .renderer import render_template
from .resend_client import ResendSendError, send_resend_email

logger = logging.getLogger(__name__)


class EmailTemplateMissing(Exception):
    """Requested template key is missing or inactive."""


def _default_context() -> dict[str, Any]:
    """Variables that are always injected so admins can reference them in any template."""
    return {
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
    }


def _strip_html(html: str) -> str:
    """Cheap HTML → text fallback when template.text_body is empty."""
    text = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</\s*p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def send_email(
    template_key: str,
    to_email: str,
    context: dict[str, Any] | None = None,
    *,
    user=None,
) -> EmailLog:
    """Render a template, persist an EmailLog (status=pending), then enqueue
    the provider call on Celery.  Returns the log row immediately.
    """
    try:
        template = EmailTemplate.objects.get(key=template_key, is_active=True)
    except EmailTemplate.DoesNotExist as exc:
        raise EmailTemplateMissing(
            f"No active EmailTemplate for key={template_key!r}. "
            "Run `python manage.py seed_email_templates` to install defaults."
        ) from exc

    ctx = {**_default_context(), **(context or {})}

    subject = render_template(template.subject, ctx)
    html_body = render_template(template.html_body, ctx)
    text_body = render_template(template.text_body or _strip_html(html_body), ctx)

    log = EmailLog.objects.create(
        user=user,
        template=template,
        template_key=template_key,
        to_email=to_email,
        from_email=settings.RESEND_FROM_EMAIL,
        subject=subject,
        rendered_html=html_body,
        rendered_text=text_body,
        context_snapshot=ctx,
        status=EmailStatus.PENDING,
    )

    # Import here to avoid circular import at Django startup.
    from ..tasks import send_email_task

    send_email_task.delay(log.id)
    return log


def send_email_sync(log_id: int) -> EmailLog:
    """Actually call Resend for a pending EmailLog. Used by the Celery task."""
    log = EmailLog.objects.select_related("template").get(pk=log_id)

    if log.status == EmailStatus.SENT:
        logger.info("EmailLog %s already sent — skipping", log_id)
        return log

    from_name = (log.template.from_name if log.template else "") or settings.RESEND_FROM_NAME

    log.attempts += 1
    try:
        message_id = send_resend_email(
            to_email=log.to_email,
            subject=log.subject,
            html_body=log.rendered_html,
            text_body=log.rendered_text,
            from_email=log.from_email,
            from_name=from_name,
            reply_to=settings.SUPPORT_EMAIL or None,
        )
        log.status = EmailStatus.SENT
        log.provider_message_id = message_id
        log.sent_at = timezone.now()
        log.error_message = ""
    except ResendSendError as exc:
        log.status = EmailStatus.FAILED
        log.error_message = str(exc)[:4000]
    log.save(
        update_fields=["status", "provider_message_id", "sent_at", "error_message", "attempts"]
    )
    return log
