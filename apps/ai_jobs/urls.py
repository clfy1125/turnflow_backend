from django.urls import path

from .views import (
    AiJobDetailView,
    AiJobListCreateView,
    AiJobRollbackView,
    AiLlmTryView,
    AiTokenBalanceView,
    PageAiJobListView,
)

app_name = "ai_jobs"

urlpatterns = [
    path("jobs/", AiJobListCreateView.as_view(), name="job-list-create"),
    path("jobs/<uuid:job_id>/", AiJobDetailView.as_view(), name="job-detail"),
    path(
        "jobs/<uuid:job_id>/rollback/",
        AiJobRollbackView.as_view(),
        name="job-rollback",
    ),
    path(
        "pages/<slug:slug>/jobs/",
        PageAiJobListView.as_view(),
        name="page-job-list",
    ),
    path("test/llm/", AiLlmTryView.as_view(), name="llm-try"),
    path("tokens/", AiTokenBalanceView.as_view(), name="token-balance"),
]
