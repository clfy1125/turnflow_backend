"""Insights API kill-switch.

`settings.INSIGHTS_API_ENABLED` 가 False 일 때 `/api/v1/insights/*` 의 모든 요청에
표준 에러 포맷(`apps/core/exceptions.custom_exception_handler` 형태)으로 503 응답.

스키마/문서(`/api/schema/`, `/api/docs/`)는 영향 없음 — 운영자가 어떤 엔드포인트가
점검 중인지 확인할 수 있어야 하기 때문.
"""

from __future__ import annotations

from django.conf import settings
from django.http import JsonResponse

_BLOCK_PREFIX = "/api/v1/insights/"


class InsightsDisabledMiddleware:
    """INSIGHTS_API_ENABLED=False 일 때 insights 엔드포인트 전체를 503 으로 차단."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            not getattr(settings, "INSIGHTS_API_ENABLED", False)
            and request.path.startswith(_BLOCK_PREFIX)
        ):
            return JsonResponse(
                {
                    "success": False,
                    "error": {
                        "code": 503,
                        "message": "Insights API 는 현재 비활성 상태입니다.",
                        "details": {
                            "reason": "insights_disabled",
                            "hint": "출시 후 활성화 예정 — 운영자는 INSIGHTS_API_ENABLED=True 로 켜면 됩니다.",
                        },
                    },
                },
                status=503,
            )
        return self.get_response(request)
