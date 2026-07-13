"""
Cloudflare Email Sending client + sender wiring tests.

The `cloudflare` SDK is not imported here — `_build_client` is patched with a
fake so these run without the package installed and without network access.
"""

from unittest.mock import MagicMock

import pytest

from apps.emails.models import EmailLog, EmailStatus
from apps.emails.services import cloudflare_client, sender
from apps.emails.services.cloudflare_client import CloudflareSendError, send_cloudflare_email


class _FakeResp:
    def __init__(self, message_id="cf_msg_1", delivered=None, queued=None, permanent_bounces=None):
        self.message_id = message_id
        self.delivered = delivered if delivered is not None else []
        self.queued = queued if queued is not None else []
        self.permanent_bounces = permanent_bounces if permanent_bounces is not None else []


class _FakeEmailSending:
    def __init__(self, recorder, resp=None, exc=None):
        self._recorder = recorder
        self._resp = resp
        self._exc = exc

    def send(self, **kwargs):
        self._recorder.update(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._resp


class _FakeClient:
    def __init__(self, email_sending):
        self.email_sending = email_sending


def _patch_client(monkeypatch, *, resp=None, exc=None):
    captured: dict = {}
    fake = _FakeClient(_FakeEmailSending(captured, resp=resp, exc=exc))
    monkeypatch.setattr(cloudflare_client, "_build_client", lambda _token: fake)
    return captured


# --------------------------- client-level --------------------------- #


def test_send_builds_named_sender_and_returns_message_id(monkeypatch, settings):
    settings.CLOUDFLARE_EMAIL_API_KEY = "cfut_dummy"
    settings.CLOUDFLARE_EMAIL_ACCOUNT_ID = "acct_123"
    captured = _patch_client(
        monkeypatch, resp=_FakeResp(message_id="cf_abc", delivered=["u@e.com"])
    )

    message_id = send_cloudflare_email(
        to_email="u@e.com",
        subject="hi",
        html_body="<p>hi</p>",
        text_body="hi",
        from_email="contact@turnflow.link",
        from_name="TurnFlow",
        reply_to="contact@turnflow.link",
    )

    assert message_id == "cf_abc"
    assert captured["account_id"] == "acct_123"
    # from_ carries the display name via the {address,name} object (key is `address`).
    assert captured["from_"] == {"address": "contact@turnflow.link", "name": "TurnFlow"}
    assert captured["to"] == "u@e.com"
    assert captured["reply_to"] == "contact@turnflow.link"
    assert captured["html"] == "<p>hi</p>"


def test_send_plain_string_sender_when_no_name(monkeypatch, settings):
    settings.CLOUDFLARE_EMAIL_API_KEY = "cfut_dummy"
    settings.CLOUDFLARE_EMAIL_ACCOUNT_ID = "acct_123"
    captured = _patch_client(monkeypatch, resp=_FakeResp())

    send_cloudflare_email(
        to_email="u@e.com",
        subject="s",
        html_body="<p>x</p>",
        text_body="x",
        from_email="contact@turnflow.link",
        from_name=None,
    )
    assert captured["from_"] == "contact@turnflow.link"
    assert "reply_to" not in captured  # omitted when not provided


def test_missing_api_key_raises(monkeypatch, settings):
    settings.CLOUDFLARE_EMAIL_API_KEY = ""
    settings.CLOUDFLARE_EMAIL_ACCOUNT_ID = "acct_123"
    with pytest.raises(CloudflareSendError, match="CLOUDFLARE_EMAIL_API_KEY"):
        send_cloudflare_email(
            to_email="u@e.com",
            subject="s",
            html_body="h",
            text_body="t",
            from_email="contact@turnflow.link",
        )


def test_sdk_exception_wrapped(monkeypatch, settings):
    settings.CLOUDFLARE_EMAIL_API_KEY = "cfut_dummy"
    settings.CLOUDFLARE_EMAIL_ACCOUNT_ID = "acct_123"
    _patch_client(monkeypatch, exc=RuntimeError("boom"))
    with pytest.raises(CloudflareSendError, match="boom"):
        send_cloudflare_email(
            to_email="u@e.com",
            subject="s",
            html_body="h",
            text_body="t",
            from_email="contact@turnflow.link",
        )


def test_permanent_bounce_only_is_failure(monkeypatch, settings):
    settings.CLOUDFLARE_EMAIL_API_KEY = "cfut_dummy"
    settings.CLOUDFLARE_EMAIL_ACCOUNT_ID = "acct_123"
    _patch_client(
        monkeypatch,
        resp=_FakeResp(message_id="x", delivered=[], queued=[], permanent_bounces=["u@e.com"]),
    )
    with pytest.raises(CloudflareSendError, match="permanent bounce"):
        send_cloudflare_email(
            to_email="u@e.com",
            subject="s",
            html_body="h",
            text_body="t",
            from_email="contact@turnflow.link",
        )


def test_mask_email():
    assert cloudflare_client._mask_email("jane.doe@turnflow.link") == "j***@turnflow.link"
    assert cloudflare_client._mask_email("bad-input") == "***"


# --------------------------- sender wiring --------------------------- #


def _pending_log():
    return EmailLog.objects.create(
        template_key="welcome",
        to_email="user@example.com",
        from_email="contact@turnflow.link",
        subject="hi",
        rendered_html="<p>hi</p>",
        rendered_text="hi",
        status=EmailStatus.PENDING,
    )


@pytest.mark.django_db
def test_send_email_sync_marks_sent(monkeypatch, settings):
    settings.EMAIL_FROM_NAME = "TurnFlow"
    settings.SUPPORT_EMAIL = "contact@turnflow.link"
    log = _pending_log()

    mock_send = MagicMock(return_value="cf_provider_id")
    monkeypatch.setattr(sender, "send_cloudflare_email", mock_send)

    out = sender.send_email_sync(log.id)

    assert out.status == EmailStatus.SENT
    assert out.provider_message_id == "cf_provider_id"
    assert out.sent_at is not None
    assert out.attempts == 1
    kwargs = mock_send.call_args.kwargs
    assert kwargs["to_email"] == "user@example.com"
    assert kwargs["from_email"] == "contact@turnflow.link"
    assert kwargs["from_name"] == "TurnFlow"
    assert kwargs["reply_to"] == "contact@turnflow.link"


@pytest.mark.django_db
def test_send_email_sync_marks_failed(monkeypatch):
    log = _pending_log()
    monkeypatch.setattr(
        sender,
        "send_cloudflare_email",
        MagicMock(side_effect=CloudflareSendError("nope")),
    )

    out = sender.send_email_sync(log.id)

    assert out.status == EmailStatus.FAILED
    assert "nope" in out.error_message
    assert out.attempts == 1
