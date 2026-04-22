"""
Thin wrapper around the Resend Python SDK.

Kept minimal so it's easy to mock in tests and to swap providers later.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class ResendSendError(Exception):
    """Raised when Resend returns an error."""


def send_resend_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    from_email: str,
    from_name: str | None = None,
    reply_to: str | None = None,
) -> str:
    """Send via Resend.  Returns the Resend message id on success,
    raises ResendSendError on failure.
    """
    import resend

    if not settings.RESEND_API_KEY:
        raise ResendSendError("RESEND_API_KEY is not configured")

    resend.api_key = settings.RESEND_API_KEY

    source = f"{from_name} <{from_email}>" if from_name else from_email

    params: dict = {
        "from": source,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    if reply_to:
        params["reply_to"] = [reply_to]

    try:
        resp = resend.Emails.send(params)
    except Exception as exc:
        logger.exception("Resend send failed to=%s subject=%s", to_email, subject)
        raise ResendSendError(str(exc)) from exc

    if isinstance(resp, dict):
        return resp.get("id", "")
    # SDK returns an SendEmailResponse object in newer versions
    return getattr(resp, "id", "") or ""
