"""
API v1 URL configuration
"""

from django.urls import path, include
from apps.core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("auth/", include("apps.authentication.urls")),
    path("", include("apps.workspace.urls")),
    path("", include("apps.billing.urls")),
    path("integrations/", include("apps.integrations.urls")),
]
