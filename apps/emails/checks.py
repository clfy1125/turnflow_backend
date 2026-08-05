"""System checks for outbound email configuration.

`FRONTEND_URL` is pasted into every link we mail out (인증·비밀번호 재설정·결제·
리포트 열어보기). When it is left at the `http://localhost:3000` default while the
provider is live, every mail still sends successfully — it just carries links that
are dead for the recipient. Nothing in the send path fails, so the only signal is a
customer telling us. This check turns that into a startup warning.

Registered from `EmailsConfig.ready()`; surfaced by `manage.py check`, which the
deploy runs before `migrate`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Warning as CheckWarning
from django.core.checks import register

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


@register()
def check_frontend_url_is_reachable_by_recipients(app_configs, **kwargs):
    """Warn when live email sending is paired with a local-only FRONTEND_URL."""
    frontend_url = getattr(settings, "FRONTEND_URL", "") or ""
    host = (urlparse(frontend_url).hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        return []

    # A configured provider means mail really leaves the box (dev included — the
    # dev stack shares the production Cloudflare sending token).
    if not getattr(settings, "CLOUDFLARE_EMAIL_API_KEY", ""):
        return []

    return [
        CheckWarning(
            f"FRONTEND_URL 이 로컬 주소({frontend_url})인데 이메일 발송은 실제로 동작합니다. "
            "발송되는 메일의 인증/재설정/결제/리포트 링크가 수신자에게 전부 죽은 주소로 나갑니다.",
            hint=(
                "환경(.env)의 FRONTEND_URL 을 수신자가 열 수 있는 프론트 주소로 설정하세요. "
                "dev=https://app.turnflow.link, prod=https://turnflow.link"
            ),
            id="emails.W001",
        )
    ]
