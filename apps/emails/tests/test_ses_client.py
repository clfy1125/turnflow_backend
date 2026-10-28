"""Amazon SES 발신 경로 테스트.

``test_cloudflare_client`` 의 대응물 — 공급자를 바꿔도 **같은 계약**이 유지되는지
(인자·반환값·실패 시 FAILED 기록·재시도 유발) 를 단언한다.
"""

from unittest.mock import MagicMock

import pytest

from apps.emails.models import EmailLog, EmailStatus
from apps.emails.services import sender
from apps.emails.services.ses_client import SESSendError, _format_sender, send_ses_email

# ──────────────────────────────────────────────
# 클라이언트 자체
# ──────────────────────────────────────────────


def _args(**over):
    base = {
        "to_email": "user@example.com",
        "subject": "제목",
        "html_body": "<p>본문</p>",
        "text_body": "본문",
        "from_email": "contact@turnflow.link",
        "from_name": "TurnFlow",
    }
    base.update(over)
    return base


def test_send_ses_email_returns_message_id(monkeypatch, settings):
    settings.AWS_SES_REGION = "ap-northeast-2"
    settings.AWS_SES_CONFIGURATION_SET = ""
    client = MagicMock()
    client.send_email.return_value = {"MessageId": "0100018f-abc"}
    monkeypatch.setattr("apps.emails.services.ses_client._build_client", lambda: client)

    assert send_ses_email(**_args()) == "0100018f-abc"

    kw = client.send_email.call_args.kwargs
    assert kw["FromEmailAddress"] == "TurnFlow <contact@turnflow.link>"
    assert kw["Destination"] == {"ToAddresses": ["user@example.com"]}
    simple = kw["Content"]["Simple"]
    assert simple["Subject"]["Charset"] == "UTF-8"
    assert simple["Body"]["Html"]["Data"] == "<p>본문</p>"
    assert simple["Body"]["Text"]["Data"] == "본문"
    # 지정하지 않은 선택 인자는 **보내지 않는다** — 빈 ConfigurationSetName 을
    # 넘기면 SES 가 "그런 구성 세트 없음"으로 거절한다.
    assert "ConfigurationSetName" not in kw
    assert "ReplyToAddresses" not in kw


def test_send_ses_email_passes_reply_to_and_config_set(monkeypatch, settings):
    settings.AWS_SES_REGION = "ap-northeast-2"
    settings.AWS_SES_CONFIGURATION_SET = "turnflow-prod"
    client = MagicMock()
    client.send_email.return_value = {"MessageId": "x"}
    monkeypatch.setattr("apps.emails.services.ses_client._build_client", lambda: client)

    send_ses_email(**_args(reply_to="contact@clfy.ai.kr"))

    kw = client.send_email.call_args.kwargs
    assert kw["ReplyToAddresses"] == ["contact@clfy.ai.kr"]
    assert kw["ConfigurationSetName"] == "turnflow-prod"


def test_send_ses_email_wraps_provider_exception(monkeypatch, settings):
    settings.AWS_SES_REGION = "ap-northeast-2"
    client = MagicMock()
    client.send_email.side_effect = RuntimeError("MessageRejected: not verified")
    monkeypatch.setattr("apps.emails.services.ses_client._build_client", lambda: client)

    with pytest.raises(SESSendError) as exc:
        send_ses_email(**_args())
    assert "MessageRejected" in str(exc.value)


def test_format_sender_encodes_korean_name():
    """한글 발신자명은 RFC 2047 로 인코딩돼야 한다 — 날것으로 넣으면 SES 가 거절한다."""
    out = _format_sender("contact@turnflow.link", "턴플로우")
    assert out.endswith("<contact@turnflow.link>")
    assert "=?utf-8?" in out.lower()


def test_format_sender_without_name_is_bare_address():
    assert _format_sender("contact@turnflow.link", None) == "contact@turnflow.link"


# ──────────────────────────────────────────────
# 디스패처 (sender.send_email_sync)
# ──────────────────────────────────────────────


def _pending_log():
    return EmailLog.objects.create(
        to_email="user@example.com",
        from_email="contact@turnflow.link",
        subject="제목",
        rendered_html="<p>hi</p>",
        rendered_text="hi",
        status=EmailStatus.PENDING,
    )


@pytest.mark.django_db
def test_send_email_sync_uses_ses_by_default(monkeypatch, settings):
    """기본 공급자가 SES 다 — 설정을 비워둬도 Cloudflare 로 새면 안 된다."""
    settings.EMAIL_FROM_NAME = "TurnFlow"
    settings.SUPPORT_EMAIL = "contact@clfy.ai.kr"
    settings.EMAIL_PROVIDER = "ses"
    log = _pending_log()

    ses = MagicMock(return_value="ses_id")
    cf = MagicMock(return_value="cf_id")
    monkeypatch.setattr(sender, "send_ses_email", ses)
    monkeypatch.setattr(sender, "send_cloudflare_email", cf)

    out = sender.send_email_sync(log.id)

    assert out.status == EmailStatus.SENT
    assert out.provider_message_id == "ses_id"
    cf.assert_not_called()
    kwargs = ses.call_args.kwargs
    # 발신은 SES 에 DKIM 확인된 turnflow.link 로 고정, Reply-To 만 문의 주소.
    # (`_dmarc` 가 p=reject 라 정렬이 깨지면 스팸이 아니라 **거부**된다)
    assert kwargs["from_email"] == "contact@turnflow.link"
    assert kwargs["reply_to"] == "contact@clfy.ai.kr"


@pytest.mark.django_db
def test_send_email_sync_marks_failed_on_ses_error(monkeypatch, settings):
    settings.EMAIL_PROVIDER = "ses"
    log = _pending_log()
    monkeypatch.setattr(sender, "send_ses_email", MagicMock(side_effect=SESSendError("sandbox")))

    out = sender.send_email_sync(log.id)

    assert out.status == EmailStatus.FAILED
    assert "sandbox" in out.error_message
    assert out.attempts == 1


@pytest.mark.django_db
def test_task_raises_so_celery_retries(monkeypatch, settings):
    """⭐ 이게 이번 사고의 절반이다 — 공급자 실패가 **재시도를 유발해야** 한다.

    종전엔 ``send_email_sync`` 가 예외를 삼키고 정상 반환해서 태스크의
    ``autoretry_for``/``max_retries`` 가 한 번도 돌지 않았다(819건 재시도 0회).
    """
    from apps.emails.tasks import EmailDeliveryFailed, send_email_task

    settings.EMAIL_PROVIDER = "ses"
    log = _pending_log()
    monkeypatch.setattr(sender, "send_ses_email", MagicMock(side_effect=SESSendError("throttled")))

    with pytest.raises(EmailDeliveryFailed):
        send_email_task(log.id)

    log.refresh_from_db()
    assert log.status == EmailStatus.FAILED
