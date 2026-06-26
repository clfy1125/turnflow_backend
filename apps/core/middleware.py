"""
Custom middleware for request/response processing
"""

import logging
import uuid

from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger(__name__)

# DR: active_site 게이트가 절대 막으면 안 되는 경로(헬스/내부 스케줄러). 이걸 막으면 failover 가 깨진다.
_GATE_EXEMPT_PREFIXES = ("/api/v1/healthz", "/api/v1/internal/scheduler/")
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RequestIDMiddleware:
    """
    Middleware to add a unique request ID to each request
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        request.id = request_id

        # Add to response headers
        response = self.get_response(request)
        response["X-Request-ID"] = request_id

        return response


class LoggingMiddleware:
    """
    Middleware for logging requests and responses
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only log non-2xx responses
        if response.status_code >= 400:
            logger.warning(
                f"{request.method} {request.path} -> {response.status_code}",
                extra={
                    "request_id": getattr(request, "id", None),
                    "method": request.method,
                    "path": request.path,
                    "status_code": response.status_code,
                },
            )

        return response


class ActiveSiteGateMiddleware:
    """DR split-brain 방지 — passive 사이트(SITE_ID != active_site)의 요청을 503 으로 차단.

    - 평상시 active 사이트(콜로)에서는 완전 no-op.
    - passive 에서는 쓰기(POST/PUT/PATCH/DELETE)를 항상 차단. 읽기는 ``PASSIVE_ALLOW_READS``
      가 True 일 때만 통과(기본 False = fully-dark).
    - ``/api/v1/healthz*``, ``/api/v1/internal/scheduler/*`` 는 하드 예외(이게 막히면 failover 불가).

    상세: DR_IMPLEMENTATION_PLAN.md §5.3.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(_GATE_EXEMPT_PREFIXES):
            from apps.core.site_control import is_active_site

            if not is_active_site():
                allow_reads = getattr(settings, "PASSIVE_ALLOW_READS", False)
                if request.method in _WRITE_METHODS or not allow_reads:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": {
                                "code": 503,
                                "message": "이 서버는 현재 대기(standby) 상태입니다. 잠시 후 다시 시도하세요.",
                                "details": {
                                    "reason": "not_active_site",
                                    "site": getattr(settings, "SITE_ID", "?"),
                                },
                            },
                        },
                        status=503,
                    )
        return self.get_response(request)
