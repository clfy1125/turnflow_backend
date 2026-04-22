"""
Admin email management URLs — mounted at /api/v1/admin/emails/.
"""

from django.urls import path

from .views_admin import (
    EmailLogDetailView,
    EmailLogListView,
    EmailTemplateDetailView,
    EmailTemplateListView,
    EmailTemplatePreviewView,
    EmailTemplateTestSendView,
)

app_name = "admin_emails"

urlpatterns = [
    path("templates/", EmailTemplateListView.as_view(), name="template-list"),
    path("templates/<str:key>/", EmailTemplateDetailView.as_view(), name="template-detail"),
    path("templates/<str:key>/preview/", EmailTemplatePreviewView.as_view(), name="template-preview"),
    path("templates/<str:key>/test-send/", EmailTemplateTestSendView.as_view(), name="template-test-send"),
    path("logs/", EmailLogListView.as_view(), name="log-list"),
    path("logs/<int:pk>/", EmailLogDetailView.as_view(), name="log-detail"),
]
