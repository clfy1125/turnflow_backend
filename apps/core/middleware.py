"""
Custom middleware for request/response processing
"""

import uuid
import logging

logger = logging.getLogger(__name__)


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
        # Log request
        logger.info(
            f"Request: {request.method} {request.path}",
            extra={
                "request_id": getattr(request, "id", None),
                "method": request.method,
                "path": request.path,
                "user": str(request.user) if hasattr(request, "user") else "Anonymous",
            },
        )

        response = self.get_response(request)

        # Log response
        logger.info(
            f"Response: {response.status_code}",
            extra={
                "request_id": getattr(request, "id", None),
                "status_code": response.status_code,
            },
        )

        return response
