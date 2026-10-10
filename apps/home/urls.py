"""home URL Configuration — /api/v1/home/ 아래 마운트 (config/api_urls.py)"""

from django.urls import path

from .dev_views import DevTestAccountListView, DevTestAccountReseedView
from .views import HomeAlertDismissView, HomeAlertsView

app_name = "home"

urlpatterns = [
    path("alerts/", HomeAlertsView.as_view(), name="alerts"),
    path("alerts/dismiss/", HomeAlertDismissView.as_view(), name="alerts-dismiss"),
    # dev 전용 — 운영은 DEBUG=False 라 두 경로 모두 404 를 낸다(dev_views._dev_only).
    path("dev/test-accounts/", DevTestAccountListView.as_view(), name="dev-test-accounts"),
    path(
        "dev/test-accounts/reseed/",
        DevTestAccountReseedView.as_view(),
        name="dev-test-accounts-reseed",
    ),
]
