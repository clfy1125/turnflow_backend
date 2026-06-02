"""apps/pages/admin_urls.py — 어드민 AI 레퍼런스 라우팅.

config/api_urls.py 에서 `path("admin/", include("apps.pages.admin_urls", namespace="admin_pages"))` 로 마운트.
"""
from django.urls import path

from .admin_views import (
    AdminPageReferenceSnapshotStatusView,
    AdminPageReferenceSnapshotTriggerView,
    AdminPageReferenceUpdateView,
    AdminReferenceCategoryDetailView,
    AdminReferenceCategoryListCreateView,
    AdminReferencePageListView,
)

app_name = "admin_pages"

urlpatterns = [
    path(
        "reference-categories/",
        AdminReferenceCategoryListCreateView.as_view(),
        name="ref-category-list-create",
    ),
    path(
        "reference-categories/<int:pk>/",
        AdminReferenceCategoryDetailView.as_view(),
        name="ref-category-detail",
    ),
    path(
        "reference-pages/",
        AdminReferencePageListView.as_view(),
        name="ref-page-list",
    ),
    path(
        "pages/<slug:slug>/reference/",
        AdminPageReferenceUpdateView.as_view(),
        name="page-reference-update",
    ),
    path(
        "pages/<slug:slug>/reference/snapshot/",
        AdminPageReferenceSnapshotTriggerView.as_view(),
        name="page-reference-snapshot-trigger",
    ),
    path(
        "pages/<slug:slug>/reference/snapshot/status/",
        AdminPageReferenceSnapshotStatusView.as_view(),
        name="page-reference-snapshot-status",
    ),
]
