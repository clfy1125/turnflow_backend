"""
High-level email sending API.

`send_email()` creates an `EmailLog` row and enqueues a Celery task.  Views
should call this, not the email provider directly — it guarantees an audit
trail and async delivery (per CLAUDE.md §5.3: views must not block on external APIs).

`send_email_sync()` performs the provider call inline.  Only the Celery task
and the admin "test send" endpoint should use this.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any

from django.conf import settings
from django.utils import timezone

from ..models import EmailLog, EmailStatus, EmailTemplate
from .cloudflare_client import CloudflareSendError, send_cloudflare_email
from .renderer import render_template

logger = logging.getLogger(__name__)


class EmailTemplateMissing(Exception):
    """Requested template key is missing or inactive."""


def _default_context() -> dict[str, Any]:
    """Variables that are always injected so admins can reference them in any template.

    Includes brand/company metadata so the shared footer (logo, 사업자 정보) renders
    on every template without each caller having to pass it.
    """
    return {
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "brand_url": settings.BRAND_URL,
        "logo_url": settings.EMAIL_LOGO_URL,
        "company_name": settings.COMPANY_NAME,
        "company_ceo": settings.COMPANY_CEO,
        "company_reg_no": settings.COMPANY_REG_NO,
        "company_address": settings.COMPANY_ADDRESS,
        "company_phone": settings.COMPANY_PHONE,
    }


# `<a href="...">라벨</a>` → `라벨 (https://...)`. Without this the plain-text part
# carries the CTA wording but no link at all — mailto: links are left alone since
# the address is already spelled out in the label.
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<label>.*?)</a>',
    re.I | re.S,
)
_BLOCK_END_RE = re.compile(r"</\s*(p|div|tr|li|h[1-6]|table)\s*>", re.I)
_CELL_END_RE = re.compile(r"</\s*(td|th)\s*>", re.I)
# `<head>` holds the <title>/<style> text, and a naive tag strip would dump the
# raw CSS (and its Korean comments) into the plain-text part.
_DROP_RE = re.compile(r"<(head|style|script)\b[^>]*>.*?</\s*\1\s*>", re.I | re.S)
# Preheader span: hidden in HTML, so it must not be repeated in the text part.
_HIDDEN_RE = re.compile(r"<span\b[^>]*display:\s*none[^>]*>.*?</\s*span\s*>", re.I | re.S)


def _strip_html(html: str) -> str:
    """Cheap HTML → text fallback when template.text_body is empty.

    Keeps hrefs (a text-only client would otherwise get a dead CTA) and inserts
    breaks at block boundaries so the table-based layout doesn't collapse into
    one run-on line.
    """

    def _anchor(m: re.Match) -> str:
        href = m.group("href").strip()
        label = re.sub(r"<[^>]+>", "", m.group("label")).strip()
        if href.lower().startswith("mailto:") or not label:
            return label or href
        return f"{label} ({href})" if label != href else href

    text = _DROP_RE.sub("", html)
    text = _HIDDEN_RE.sub("", text)
    text = _ANCHOR_RE.sub(_anchor, text)
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    text = _CELL_END_RE.sub(" ", text)
    text = _BLOCK_END_RE.sub("\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    # Collapse whitespace runs. \xa0 is the unescaped &nbsp; from the spacer rows —
    # note this must stay a single backslash inside the raw string, otherwise the
    # class degenerates into the literal characters \, x, a and 0.
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
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
        from_email=settings.EMAIL_FROM_ADDRESS,
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
    """Actually call the email provider for a pending EmailLog. Used by the Celery task."""
    log = EmailLog.objects.select_related("template").get(pk=log_id)

    if log.status == EmailStatus.SENT:
        logger.info("EmailLog %s already sent — skipping", log_id)
        return log

    from_name = (log.template.from_name if log.template else "") or settings.EMAIL_FROM_NAME

    log.attempts += 1
    try:
        message_id = send_cloudflare_email(
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
    except CloudflareSendError as exc:
        log.status = EmailStatus.FAILED
        log.error_message = str(exc)[:4000]
    log.save(
        update_fields=["status", "provider_message_id", "sent_at", "error_message", "attempts"]
    )
    return log
