"""
API v1 URL configuration
"""

from django.urls import include, path

from apps.core.views import diag, healthz, live, ready
from apps.core.views_internal import scheduler_tick

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("healthz/live", live, name="healthz-live"),
    path("healthz/ready", ready, name="healthz-ready"),
    # DR 감지기 전용 진단(읽기전용·항상200·시크릿 인증) — 회색지대 신호 노출
    path("healthz/diag", diag, name="healthz-diag"),
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
    path("track/", include("apps.analytics.urls", namespace="analytics")),
    # 인스타 성장 리포트(프로 전용, PDF). integrations 라우터가 `instagram` 을 ViewSet
    # prefix 로 쓰고 있어 `integrations/instagram/reports/` 는 pk="reports" 로 먹힌다 → 별도 prefix.
    path("insta-reports/", include("apps.insta_reports.urls", namespace="insta_reports")),
    path("admin/emails/", include("apps.emails.urls_admin", namespace="admin_emails")),
    path("admin/", include("apps.pages.admin_urls", namespace="admin_pages")),
    path("admin/", include("apps.admin_api.urls", namespace="admin_api")),
    path("tiktok/", include("apps.tiktok.urls", namespace="tiktok")),
    path("youtube/", include("apps.youtube.urls", namespace="youtube")),
]
