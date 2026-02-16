"""
URL Configuration for Workspace app
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import WorkspaceViewSet

app_name = "workspace"

router = DefaultRouter()
router.register(r"workspaces", WorkspaceViewSet, basename="workspace")

urlpatterns = [
    path("", include(router.urls)),
]
