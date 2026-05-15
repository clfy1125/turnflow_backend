"""
Insights API URL 라우팅.

마운트 위치: config/api_urls.py 에서 `insights/` prefix.
최종 URL:
    /api/v1/insights/workspaces/{workspace_id}/media/
    /api/v1/insights/workspaces/{workspace_id}/media/{media_id}/
    /api/v1/insights/workspaces/{workspace_id}/media/{media_id}/diagnosis/
    /api/v1/insights/workspaces/{workspace_id}/aggregate/
    /api/v1/insights/workspaces/{workspace_id}/sync-jobs/
    /api/v1/insights/workspaces/{workspace_id}/sync-jobs/{job_id}/
"""

from django.urls import path

from .views import (
    AccountAudienceInsightView,
    MediaAggregateView,
    MediaDetailView,
    MediaDiagnosisView,
    MediaListView,
    SyncJobCreateView,
    SyncJobDetailView,
)

app_name = "insights"

urlpatterns = [
    path(
        "workspaces/<uuid:workspace_id>/media/",
        MediaListView.as_view(),
        name="media-list",
    ),
    path(
        "workspaces/<uuid:workspace_id>/media/<uuid:media_id>/",
        MediaDetailView.as_view(),
        name="media-detail",
    ),
    path(
        "workspaces/<uuid:workspace_id>/media/<uuid:media_id>/diagnosis/",
        MediaDiagnosisView.as_view(),
        name="media-diagnosis",
    ),
    path(
        "workspaces/<uuid:workspace_id>/aggregate/",
        MediaAggregateView.as_view(),
        name="media-aggregate",
    ),
    path(
        "workspaces/<uuid:workspace_id>/sync-jobs/",
        SyncJobCreateView.as_view(),
        name="sync-jobs-create",
    ),
    path(
        "workspaces/<uuid:workspace_id>/sync-jobs/<uuid:job_id>/",
        SyncJobDetailView.as_view(),
        name="sync-jobs-detail",
    ),
    path(
        "workspaces/<uuid:workspace_id>/accounts/<uuid:account_id>/audience-insight/",
        AccountAudienceInsightView.as_view(),
        name="account-audience-insight",
    ),
]
