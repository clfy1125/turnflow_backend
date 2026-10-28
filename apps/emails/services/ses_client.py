"""Thin wrapper around Amazon SES v2 (``boto3`` ``sesv2.send_email``).

``cloudflare_client`` 와 **같은 모양**을 유지한다 — 인자·반환값·예외가 같아야
``sender.send_email_sync`` 이 공급자를 한 줄로 갈아끼울 수 있다.

전제 (코드 밖에서 1회):
- ``turnflow.link`` 가 SES 에 **Verified identity** 로 등록돼 있어야 한다
  (DKIM CNAME 3개 + SPF + DMARC — DNS 가 Route 53 이라 콘솔에서 자동 생성된다).
- SES 계정이 **샌드박스를 벗어나** 있어야 임의 수신자에게 보낼 수 있다.
  샌드박스에서는 인증된 주소 외 전부 ``MessageRejected`` 다.
- IAM 자격증명에 ``ses:SendEmail`` 권한.

⚠️ 공급자를 Cloudflare Email Sending 에서 옮긴 이유 — CF 는 **발신 도메인이
Cloudflare DNS 존에 있을 때만** 발신을 허용한다. 2026-10-18 ``turnflow.link``
네임서버를 Route 53 으로 옮기면서 존이 빠졌고, 그 순간부터 모든 발송이
``email.invalid`` 로 거절됐다(10일간 819건 전량 실패). 도메인 DNS 와 메일
공급자를 **같은 곳**에 두는 것이 이 사고의 교훈이다.

토큰·자격증명은 절대 로깅하지 않는다. 수신자 주소도 마스킹해서 남긴다.
"""

from __future__ import annotations

import logging
from email.utils import formataddr

from django.conf import settings

logger = logging.getLogger(__name__)


class SESSendError(Exception):
    """Raised when Amazon SES rejects or fails to accept the message."""


def _mask_email(email: str) -> str:
    """``jane.doe@turnflow.link`` -> ``j***@turnflow.link`` (avoid logging full PII)."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    head = local[0] if local else ""
    return f"{head}***@{domain}"


def _build_client():
    """Instantiate the boto3 SES v2 client.  Split out so tests can patch it
    without needing credentials or network."""
    import boto3

    kwargs: dict = {"region_name": settings.AWS_SES_REGION}
    # 키를 비워두면 boto3 기본 체인(인스턴스 역할 / 환경변수)을 쓴다.
    if settings.AWS_SES_ACCESS_KEY_ID and settings.AWS_SES_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_SES_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SES_SECRET_ACCESS_KEY
    return boto3.client("sesv2", **kwargs)


def _format_sender(from_email: str, from_name: str | None) -> str:
    """``("TurnFlow", "contact@turnflow.link")`` -> ``TurnFlow <contact@turnflow.link>``.

    ``charset="utf-8"`` 이 있어야 한글 발신자명이 RFC 2047 로 인코딩된다 — 없으면
    boto3 가 비ASCII 헤더를 거절한다(템플릿이 발신자명을 한글로 둘 수 있다).
    """
    if not from_name:
        return from_email
    return formataddr((from_name, from_email), charset="utf-8")


def send_ses_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    from_email: str,
    from_name: str | None = None,
    reply_to: str | None = None,
) -> str:
    """Send via Amazon SES.  Returns the SES message id, raises ``SESSendError``.

    SES 는 거절을 **예외로** 돌려준다(``MessageRejected`` 등) — Cloudflare 처럼
    200 안에 bounce 를 숨기지 않으므로 별도 응답 검사가 필요 없다.
    """
    if not settings.AWS_SES_REGION:
        raise SESSendError("AWS_SES_REGION is not configured")

    client = _build_client()

    content: dict = {
        "Simple": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": html_body, "Charset": "UTF-8"},
                "Text": {"Data": text_body, "Charset": "UTF-8"},
            },
        }
    }
    params: dict = {
        "FromEmailAddress": _format_sender(from_email, from_name),
        "Destination": {"ToAddresses": [to_email]},
        "Content": content,
    }
    if reply_to:
        params["ReplyToAddresses"] = [reply_to]
    if settings.AWS_SES_CONFIGURATION_SET:
        params["ConfigurationSetName"] = settings.AWS_SES_CONFIGURATION_SET

    try:
        resp = client.send_email(**params)
    except Exception as exc:
        logger.exception("SES email send failed to=%s subject=%s", _mask_email(to_email), subject)
        raise SESSendError(str(exc)) from exc

    return (resp or {}).get("MessageId", "") or ""
