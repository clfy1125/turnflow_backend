"""apps/admin_api/urls.py — /api/v1/admin/ 백오피스 라우팅.

config/api_urls.py 에서
``path("admin/", include("apps.admin_api.urls", namespace="admin_api"))`` 로 마운트된다.
기존 admin 라우팅(apps.pages.admin_urls, apps.emails.urls_admin)과 경로가 겹치지 않도록
패턴을 분리했다 — pages 의 ``pages/<slug>/reference/...`` 와 본 앱의 ``pages/<slug>/`` 는
세그먼트가 달라 Django URL 백트래킹으로 안전하게 공존한다.

PK 컨버터:
- User: int  → ``<int:pk>``
- Workspace / Membership / AutoDMCampaign / SentDMLog / IGAccountConnection: UUID → ``<uuid:...>``
- Page: slug → ``<slug:slug>``
"""

from django.urls import path

from apps.admin_api.views.autodm import (
    AdminCampaignDetailView,
    AdminCampaignListView,
    AdminCampaignPauseView,
    AdminCampaignResumeView,
    AdminDMBacklogView,
    AdminDMLogDetailView,
    AdminDMLogListView,
    AdminDMLogRetryView,
    AdminDMLogReverifyView,
    AdminDMVerificationStatsView,
    AdminIGConnectionListView,
)
from apps.admin_api.views.billing import AdminSubscriptionPlanListView
from apps.admin_api.views.dashboard import AdminMetricsOverviewView
from apps.admin_api.views.identity import AdminMeView
from apps.admin_api.views.pages import (
    AdminPageDetailView,
    AdminPageInquiryListView,
    AdminPageListView,
    AdminPageSubscriptionListView,
)
from apps.admin_api.views.users import (
    AdminUserDetailView,
    AdminUserListView,
    AdminUserPasswordResetView,
    AdminUserSubscriptionUpdateView,
)
from apps.admin_api.views.workspaces import (
    AdminWorkspaceDetailView,
    AdminWorkspaceListView,
    AdminWorkspaceMemberDetailView,
)

app_name = "admin_api"

urlpatterns = [
    # A. 어드민 신원 / 게이팅
    path("me/", AdminMeView.as_view(), name="me"),
    # B. 대시보드 지표
    path("metrics/overview/", AdminMetricsOverviewView.as_view(), name="metrics-overview"),
    # C. 회원(계정) 관리
    path("users/", AdminUserListView.as_view(), name="user-list"),
    path("users/<int:pk>/", AdminUserDetailView.as_view(), name="user-detail"),
    path(
        "users/<int:pk>/password-reset/",
        AdminUserPasswordResetView.as_view(),
        name="user-password-reset",
    ),
    path(
        "users/<int:pk>/subscription/",
        AdminUserSubscriptionUpdateView.as_view(),
        name="user-subscription-update",
    ),
    # 구독 플랜(요금제) 목록 — 백오피스 드롭다운/라벨 소스 (비활성 포함)
    path(
        "subscription-plans/",
        AdminSubscriptionPlanListView.as_view(),
        name="subscription-plan-list",
    ),
    # D. 워크스페이스 & 멤버십
    path("workspaces/", AdminWorkspaceListView.as_view(), name="workspace-list"),
    path("workspaces/<uuid:pk>/", AdminWorkspaceDetailView.as_view(), name="workspace-detail"),
    path(
        "workspaces/<uuid:workspace_id>/members/<uuid:membership_id>/",
        AdminWorkspaceMemberDetailView.as_view(),
        name="workspace-member-detail",
    ),
    # E. 페이지 관리 / 모더레이션
    path("pages/", AdminPageListView.as_view(), name="page-list"),
    path("pages/<slug:slug>/", AdminPageDetailView.as_view(), name="page-detail"),
    path(
        "pages/<slug:slug>/inquiries/",
        AdminPageInquiryListView.as_view(),
        name="page-inquiries",
    ),
    path(
        "pages/<slug:slug>/subscriptions/",
        AdminPageSubscriptionListView.as_view(),
        name="page-subscriptions",
    ),
    # F. 자동 DM 모니터링
    path("auto-dm/campaigns/", AdminCampaignListView.as_view(), name="campaign-list"),
    path(
        "auto-dm/campaigns/<uuid:pk>/",
        AdminCampaignDetailView.as_view(),
        name="campaign-detail",
    ),
    path(
        "auto-dm/campaigns/<uuid:pk>/pause/",
        AdminCampaignPauseView.as_view(),
        name="campaign-pause",
    ),
    path(
        "auto-dm/campaigns/<uuid:pk>/resume/",
        AdminCampaignResumeView.as_view(),
        name="campaign-resume",
    ),
    path("auto-dm/logs/", AdminDMLogListView.as_view(), name="dmlog-list"),
    path("auto-dm/logs/<uuid:pk>/", AdminDMLogDetailView.as_view(), name="dmlog-detail"),
    path(
        "auto-dm/logs/<uuid:pk>/retry/",
        AdminDMLogRetryView.as_view(),
        name="dmlog-retry",
    ),
    path(
        "auto-dm/logs/<uuid:pk>/reverify/",
        AdminDMLogReverifyView.as_view(),
        name="dmlog-reverify",
    ),
    path(
        "dm-verification/stats/",
        AdminDMVerificationStatsView.as_view(),
        name="dm-verification-stats",
    ),
    path("auto-dm/backlog/", AdminDMBacklogView.as_view(), name="dm-backlog"),
    path("ig-connections/", AdminIGConnectionListView.as_view(), name="ig-connection-list"),
]
