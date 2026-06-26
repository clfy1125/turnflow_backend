"""내부 컨트롤플레인 엔드포인트(/internal/scheduler/tick) 인증.

외부 Cron(CF Cron / Google Cloud Scheduler / Healthchecks)만 호출할 수 있도록:
  1) 공유 시크릿 헤더 ``X-Scheduler-Secret`` 상수시간 비교 (IG webhook verify_token 패턴 선례)
  2) (선택) 송신 IP 허용목록 ``SCHEDULER_TICK_ALLOWED_IPS``

Caddy 가 trusted_proxies(CF)로 진짜 클라이언트 IP 를 복원하므로 XFF 첫 값을 신뢰한다.
"""

from __future__ import annotations

import hmac

from django.conf import settings


def get_client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def verify_scheduler_request(request) -> tuple[bool, str]:
    """(허용 여부, 거부 사유) 반환."""
    secret = getattr(settings, "SCHEDULER_TICK_SECRET", "") or ""
    if not secret:
        # fail-closed: 시크릿 미설정이면 활성화 안 된 것으로 간주.
        return False, "secret_not_configured"

    provided = request.META.get("HTTP_X_SCHEDULER_SECRET", "")
    if not (provided and hmac.compare_digest(provided, secret)):
        return False, "bad_secret"

    allowed = getattr(settings, "SCHEDULER_TICK_ALLOWED_IPS", []) or []
    if allowed and get_client_ip(request) not in allowed:
        return False, "ip_not_allowed"

    return True, ""
