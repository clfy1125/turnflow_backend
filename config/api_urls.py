"""
API v1 URL configuration
"""

from django.urls import include, path

from apps.core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("auth/", include("apps.authentication.urls")),
    path("", include("apps.workspace.urls")),
    path("", include("apps.billing.urls")),
    path("integrations/", include("apps.integrations.urls")),
    path("pages/", include("apps.pages.urls", namespace="pages")),
    path("link/", include("apps.pages.link_urls", namespace="link")),
    path("ai/", include("apps.ai_jobs.urls", namespace="ai_jobs")),
    path("insights/", include("apps.insights.urls", namespace="insights")),
    path("admin/emails/", include("apps.emails.urls_admin", namespace="admin_emails")),
    path("admin/", include("apps.pages.admin_urls", namespace="admin_pages")),
    path("admin/", include("apps.admin_api.urls", namespace="admin_api")),
    path("tiktok/", include("apps.tiktok.urls", namespace="tiktok")),
    path("youtube/", include("apps.youtube.urls", namespace="youtube")),
]
