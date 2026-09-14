"""home URL Configuration — /api/v1/home/ 아래 마운트 (config/api_urls.py)"""

from django.urls import path

from .views import HomeAlertDismissView, HomeAlertsView

app_name = "home"

urlpatterns = [
    path("alerts/", HomeAlertsView.as_view(), name="alerts"),
    path("alerts/dismiss/", HomeAlertDismissView.as_view(), name="alerts-dismiss"),
]
