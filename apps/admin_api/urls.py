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
    AdminCampaignQueueStateView,
    AdminCampaignResumeView,
    AdminCampaignTimeseriesView,
    AdminDMBacklogView,
    AdminDMLogDetailView,
    AdminDMLogListView,
    AdminDMLogRetryView,
    AdminDMLogReverifyView,
    AdminDMRecipientListView,
    AdminDMVerificationStatsView,
    AdminIGConnectionListView,
)
from apps.admin_api.views.billing import AdminSubscriptionPlanListView
from apps.admin_api.views.dashboard import AdminMetricsOverviewView
from apps.admin_api.views.dashboard_marketing import AdminMarketingDashboardView
from apps.admin_api.views.dashboard_ops import AdminOpsDashboardView
from apps.admin_api.views.identity import AdminMeView
from apps.admin_api.views.marketing import (
    AdminChannelLinkDetailView,
    AdminChannelLinkListCreateView,
)
from apps.admin_api.views.pages import (
    AdminPageDetailView,
    AdminPageInquiryListView,
    AdminPageListView,
    AdminPageSubscriptionListView,
)
from apps.admin_api.views.referral import (
    AdminReferralCodeDetailView,
    AdminReferralCodeListCreateView,
    AdminReferralCodeRedemptionsView,
)
from apps.admin_api.views.snapshot import AdminPayingSnapshotView, AdminTrialSnapshotView
from apps.admin_api.views.spam import AdminSpamLogListView
from apps.admin_api.views.users import (
    AdminUserDetailView,
    AdminUserListView,
    AdminUserPasswordResetView,
    AdminUserPaymentHistoryView,
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
    path(
        "dashboard/operations/",
        AdminOpsDashboardView.as_view(),
        name="dashboard-operations",
    ),
    path(
        "dashboard/marketing/",
        AdminMarketingDashboardView.as_view(),
        name="dashboard-marketing",
    ),
    # B-2. 전체 현황 타일 → 회원 명단 (SNAP-1/2). 최고 관리자 전용 —
    #      /admin/snapshot/** 는 RBAC 화이트리스트에 없어 marketing_viewer 는 403.
    path("snapshot/paying/", AdminPayingSnapshotView.as_view(), name="snapshot-paying"),
    path("snapshot/trial/", AdminTrialSnapshotView.as_view(), name="snapshot-trial"),
    # C. 회원(계정) 관리
    path("users/", AdminUserListView.as_view(), name="user-list"),
    path("users/<int:pk>/", AdminUserDetailView.as_view(), name="user-detail"),
    # USR-5 — 회원별 결제 이력 (목록이 길어질 수 있어 상세 응답과 분리)
    path(
        "users/<int:pk>/payments/",
        AdminUserPaymentHistoryView.as_view(),
        name="user-payments",
    ),
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
    # DM-3 — 유저 콘솔과 같은 스키마·집계, 워크스페이스 필터만 제거한 어드민 판
    path(
        "auto-dm/campaigns/<uuid:pk>/queue-state/",
        AdminCampaignQueueStateView.as_view(),
        name="campaign-queue-state",
    ),
    path(
        "auto-dm/campaigns/<uuid:pk>/timeseries/",
        AdminCampaignTimeseriesView.as_view(),
        name="campaign-timeseries",
    ),
    path("auto-dm/recipients/", AdminDMRecipientListView.as_view(), name="dm-recipient-list"),
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
    # F-2. 스팸 차단 댓글 로그 (운영 대시보드 '자세히 보기' 드릴다운, OPS-3)
    path("spam/logs/", AdminSpamLogListView.as_view(), name="spam-log-list"),
    # G. 레퍼럴 코드 관리 (마케팅 프로모션)
    path(
        "referral-codes/",
        AdminReferralCodeListCreateView.as_view(),
        name="referral-code-list",
    ),
    path(
        "referral-codes/<uuid:pk>/",
        AdminReferralCodeDetailView.as_view(),
        name="referral-code-detail",
    ),
    path(
        "referral-codes/<uuid:pk>/redemptions/",
        AdminReferralCodeRedemptionsView.as_view(),
        name="referral-code-redemptions",
    ),
    # H. 마케팅 채널 링크 (UTM 링크 생성기 서버 저장 — 전 관리자 공용)
    path(
        "marketing/channel-links/",
        AdminChannelLinkListCreateView.as_view(),
        name="channel-link-list",
    ),
    path(
        "marketing/channel-links/<int:pk>/",
        AdminChannelLinkDetailView.as_view(),
        name="channel-link-detail",
    ),
]
