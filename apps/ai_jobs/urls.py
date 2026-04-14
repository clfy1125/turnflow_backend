from django.urls import path

from .views import AiJobDetailView, AiJobListCreateView, AiTokenBalanceView

app_name = "ai_jobs"

urlpatterns = [
    path("jobs/", AiJobListCreateView.as_view(), name="job-list-create"),
    path("jobs/<uuid:job_id>/", AiJobDetailView.as_view(), name="job-detail"),
    path("tokens/", AiTokenBalanceView.as_view(), name="token-balance"),
]
