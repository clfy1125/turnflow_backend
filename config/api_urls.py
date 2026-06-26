"""
API v1 URL configuration
"""

from django.urls import include, path

from apps.core.views import healthz, live, ready
from apps.core.views_internal import scheduler_tick

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("healthz/live", live, name="healthz-live"),
    path("healthz/ready", ready, name="healthz-ready"),
    # DR 내부 컨트롤플레인 — 외부 Cron 전용(공유시크릿+IP allowlist)
    path("internal/scheduler/tick", scheduler_tick, name="scheduler-tick"),
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
