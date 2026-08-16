"""apps/admin_api/auth/http.py — 어드민 인증 응답 헬퍼.

에러는 프로젝트 표준 봉투(`apps.core.exceptions.custom_exception_handler` 와 같은 모양)로
직접 만든다. DRF 예외를 거치지 않는 이유는 **사유 코드(``error.details.code``)를 정확히
통제**해야 하기 때문이다 — 프론트가 `invalid_code` / `challenge_expired` /
`admin_token_required` 를 각각 다른 화면으로 분기한다.
"""

from __future__ import annotations

from rest_framework.response import Response


def auth_error(status_code: int, message: str, code: str, **extra) -> Response:
    """표준 에러 봉투. ``code`` 는 프론트 분기용 머신 키(HTTP 상태와 별개)."""
    return Response(
        {
            "success": False,
            "error": {
                "code": status_code,
                "message": message,
                "details": {"code": code, **extra},
            },
        },
        status=status_code,
    )


def client_ip(request) -> str | None:
    """X-Forwarded-For 우선. 감사로그(audit.py)와 같은 규칙."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def mask_email(email: str) -> str:
    """``cl***25@gmail.com`` — "어느 주소로 갔는지" 만 알려주고 주소는 흘리지 않는다."""
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if len(local) <= 4:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-2:]}@{domain}"
