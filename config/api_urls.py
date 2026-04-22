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
    path("pages/", include("apps.pages.urls", namespace="pages")),
    path("ai/", include("apps.ai_jobs.urls", namespace="ai_jobs")),
    path("admin/emails/", include("apps.emails.urls_admin", namespace="admin_emails")),
]
