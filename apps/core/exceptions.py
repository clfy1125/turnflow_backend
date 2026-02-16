"""
Custom exception handlers for standardized API responses
"""

from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
from django.core.exceptions import ValidationError as DjangoValidationError


class PlanLimitExceededError(Exception):
    """
    Exception raised when plan usage limit is exceeded
    """

    def __init__(self, metric: str, limit: int, current: int, plan: str):
        self.metric = metric
        self.limit = limit
        self.current = current
        self.plan = plan
        self.message = (
            f"Plan limit exceeded for {metric}. Current: {current}, Limit: {limit}, Plan: {plan}"
        )
        super().__init__(self.message)


def custom_exception_handler(exc, context):
    """
    Custom exception handler that provides standardized error responses
    """
    # Handle plan limit exceeded error
    if isinstance(exc, PlanLimitExceededError):
        error_data = {
            "success": False,
            "error": {
                "code": "PLAN_LIMIT_EXCEEDED",
                "message": "플랜 사용량 한도를 초과했습니다",
                "details": {
                    "metric": exc.metric,
                    "current": exc.current,
                    "limit": exc.limit,
                    "plan": exc.plan,
                },
            },
        }
        return Response(error_data, status=status.HTTP_429_TOO_MANY_REQUESTS)

    # Call REST framework's default exception handler first
    response = exception_handler(exc, context)

    if response is not None:
        # Standardize error response format
        error_data = {
            "success": False,
            "error": {
                "code": response.status_code,
                "message": get_error_message(exc, response),
                "details": (
                    response.data if isinstance(response.data, dict) else {"detail": response.data}
                ),
            },
        }
        response.data = error_data
    else:
        # Handle Django validation errors
        if isinstance(exc, DjangoValidationError):
            error_data = {
                "success": False,
                "error": {
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation Error",
                    "details": {"detail": list(exc.messages)},
                },
            }
            response = Response(error_data, status=status.HTTP_400_BAD_REQUEST)

    return response


def get_error_message(exc, response):
    """
    Get a user-friendly error message
    """
    if hasattr(exc, "detail"):
        if isinstance(exc.detail, dict):
            # Get first error message
            for key, value in exc.detail.items():
                if isinstance(value, list):
                    return value[0] if value else str(exc.detail)
                return str(value)
        return str(exc.detail)

    return str(exc)
