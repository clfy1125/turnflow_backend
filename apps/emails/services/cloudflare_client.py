"""
Thin wrapper around the Cloudflare Email Sending SDK (Cloudflare Email Service).

Kept minimal so it's easy to mock in tests and to swap providers later.

Backs ``POST /accounts/{account_id}/email/sending/send`` via the official
``cloudflare`` Python SDK (>=5.0).  Prerequisites (one-time, outside this code):
- ``turnflow.link`` must be onboarded on Cloudflare DNS with SPF/DKIM/DMARC.
- The account needs the Workers Paid plan to send to arbitrary recipients.
- ``CLOUDFLARE_EMAIL_API_KEY`` must be a token with "Email Sending: Edit".

Never log the API token (``cfut_…``) or the full recipient address — see the
security audit note on PII in exception logs.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class CloudflareSendError(Exception):
    """Raised when Cloudflare Email Sending returns an error."""


def _mask_email(email: str) -> str:
    """``jane.doe@turnflow.link`` -> ``j***@turnflow.link`` (avoid logging full PII)."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    head = local[0] if local else ""
    return f"{head}***@{domain}"


def _build_client(api_token: str):
    """Instantiate the Cloudflare SDK client.  Split out so tests can patch it
    without importing the (optional) ``cloudflare`` package."""
    from cloudflare import Cloudflare

    return Cloudflare(api_token=api_token)


def send_cloudflare_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    from_email: str,
    from_name: str | None = None,
    reply_to: str | None = None,
) -> str:
    """Send via Cloudflare Email Sending.  Returns the Cloudflare message id on
    success, raises ``CloudflareSendError`` on failure.
    """
    if not settings.CLOUDFLARE_EMAIL_API_KEY:
        raise CloudflareSendError("CLOUDFLARE_EMAIL_API_KEY is not configured")
    if not settings.CLOUDFLARE_EMAIL_ACCOUNT_ID:
        raise CloudflareSendError("CLOUDFLARE_EMAIL_ACCOUNT_ID is not configured")

    client = _build_client(settings.CLOUDFLARE_EMAIL_API_KEY)

    # ``from_`` accepts a plain address string or an object whose key is
    # ``address`` (NOT ``email``).  Use the object form to carry a display name.
    sender = {"address": from_email, "name": from_name} if from_name else from_email

    params: dict = {
        "account_id": settings.CLOUDFLARE_EMAIL_ACCOUNT_ID,
        "from_": sender,
        "to": to_email,  # str | list[str]
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    if reply_to:
        params["reply_to"] = reply_to

    try:
        resp = client.email_sending.send(**params)
    except Exception as exc:
        logger.exception(
            "Cloudflare email send failed to=%s subject=%s", _mask_email(to_email), subject
        )
        raise CloudflareSendError(str(exc)) from exc

    delivered = list(getattr(resp, "delivered", None) or [])
    queued = list(getattr(resp, "queued", None) or [])
    bounces = list(getattr(resp, "permanent_bounces", None) or [])

    # A 200 with only permanent bounces is a delivery failure — surface it so the
    # EmailLog is marked FAILED (and the Celery task retries) rather than "sent".
    if bounces and not delivered and not queued:
        raise CloudflareSendError(f"permanent bounce for recipient: {bounces}")

    return getattr(resp, "message_id", "") or ""
