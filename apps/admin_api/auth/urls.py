"""apps/admin_api/auth/urls.py — /api/v1/admin/auth/ 라우팅.

이 프리픽스는 어드민 토큰 게이트에서 **반드시 제외**된다 — 여기서 토큰을 받아가는데
토큰이 있어야 들어올 수 있으면 로그인 자체가 불가능하다
(:data:`apps.admin_api.gate.ADMIN_AUTH_PREFIX`).
"""

from django.urls import path

from .views import AdminLoginView, AdminMfaVerifyView, AdminTokenRefreshView
from .views_manage import (
    AdminBackupCodeRegenerateView,
    AdminDeviceRevokeView,
    AdminMfaConfirmView,
    AdminMfaSetupView,
    AdminMfaStatusView,
)

app_name = "admin_auth"

urlpatterns = [
    # 로그인 흐름 (무인증)
    path("login/", AdminLoginView.as_view(), name="login"),
    path("mfa/verify/", AdminMfaVerifyView.as_view(), name="mfa-verify"),
    path("refresh/", AdminTokenRefreshView.as_view(), name="refresh"),
    # 등록·관리
    path("mfa/setup/", AdminMfaSetupView.as_view(), name="mfa-setup"),
    path("mfa/confirm/", AdminMfaConfirmView.as_view(), name="mfa-confirm"),
    path("mfa/status/", AdminMfaStatusView.as_view(), name="mfa-status"),
    path(
        "mfa/backup-codes/regenerate/",
        AdminBackupCodeRegenerateView.as_view(),
        name="mfa-backup-codes-regenerate",
    ),
    path("devices/<int:pk>/", AdminDeviceRevokeView.as_view(), name="device-revoke"),
]
