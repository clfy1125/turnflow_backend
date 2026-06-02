from django.urls import path

from .views import (
    AiClassifyPostsView,
    AiJobDetailView,
    AiJobListCreateView,
    AiJobRollbackView,
    AiLlmTryView,
    AiTokenBalanceView,
    PageAiJobListView,
    ReferenceCategoryListView,
    ReferenceCategoryPagesView,
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
    path("classify-posts/", AiClassifyPostsView.as_view(), name="classify-posts"),
    path("tokens/", AiTokenBalanceView.as_view(), name="token-balance"),
    # AI 레퍼런스 카테고리 & 페이지 조회 (공개)
    path("categories/", ReferenceCategoryListView.as_view(), name="ref-category-list"),
    path(
        "categories/<slug:slug>/references/",
        ReferenceCategoryPagesView.as_view(),
        name="ref-category-pages",
    ),
]
