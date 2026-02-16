"""
Core views for health check and monitoring
"""

from django.http import JsonResponse
from django.db import connection


def healthz(request):
    """
    Health check endpoint
    Returns 200 OK if the service is healthy
    """
    try:
        # Check database connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

        return JsonResponse({"status": "healthy", "database": "connected"})
    except Exception as e:
        return JsonResponse({"status": "unhealthy", "error": str(e)}, status=500)
