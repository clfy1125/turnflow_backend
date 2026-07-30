"""인스타 성장 리포트 URL.

마운트: config/api_urls.py 에서 `insta-reports/` prefix.
최종 URL:
    GET  /api/v1/insta-reports/targets/
    POST /api/v1/insta-reports/
    GET  /api/v1/insta-reports/
    GET  /api/v1/insta-reports/{id}/
    GET  /api/v1/insta-reports/{id}/download/

⚠️ `integrations/instagram/...` 아래에 두지 않은 이유: integrations 라우터가
   `instagram` 을 ViewSet prefix 로 잡고 있어(`instagram/{pk}/`) `instagram/reports/` 가
   pk="reports" 로 먹힌다.
"""

from django.urls import path

from .views import (
    ReportDetailView,
    ReportDownloadView,
    ReportListCreateView,
    ReportTargetsView,
)

app_name = "insta_reports"

urlpatterns = [
    path("targets/", ReportTargetsView.as_view(), name="targets"),
    path("", ReportListCreateView.as_view(), name="list-create"),
    path("<uuid:pk>/", ReportDetailView.as_view(), name="detail"),
    path("<uuid:pk>/download/", ReportDownloadView.as_view(), name="download"),
]
