"""
Authentication URL Configuration
"""

from django.urls import path

from apps.emails.views_auth import (
    PasswordResetConfirmView,
    PasswordResetRequestView,
    SendVerificationEmailView,
    VerifyEmailView,
)

from .deletion_views import (
    AccountDeletionConfirmView,
    AccountDeletionPolicyView,
    AccountDeletionRequestView,
    AccountDeletionRestoreView,
    AccountDeletionVerifyView,
)
from .views import (
    AccountDeleteView,
    GoogleLoginView,
    LoginView,
    MeView,
    RegisterView,
    TokenRefreshView,
)

app_name = "authentication"

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("google/", GoogleLoginView.as_view(), name="google-login"),
    path("me/", MeView.as_view(), name="me"),
    path("me/delete/", AccountDeleteView.as_view(), name="account-delete"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    # Email verification + password reset (implemented in apps.emails)
    path(
        "email/send-verification/",
        SendVerificationEmailView.as_view(),
        name="email-send-verification",
    ),
    path("email/verify/", VerifyEmailView.as_view(), name="email-verify"),
    path(
        "password/reset-request/",
        PasswordResetRequestView.as_view(),
        name="password-reset-request",
    ),
    path(
        "password/reset-confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    # ── 웹 단독 회원탈퇴 (turnflow.link/delete-account, Google Play 정책) ──
    # 전부 공개(AllowAny). me/delete/ 와 달리 **로그인 없이** 동작해야 한다.
    path(
        "deletion/policy/",
        AccountDeletionPolicyView.as_view(),
        name="account-deletion-policy",
    ),
    path(
        "deletion/request/",
        AccountDeletionRequestView.as_view(),
        name="account-deletion-request",
    ),
    path(
        "deletion/verify/",
        AccountDeletionVerifyView.as_view(),
        name="account-deletion-verify",
    ),
    path(
        "deletion/confirm/",
        AccountDeletionConfirmView.as_view(),
        name="account-deletion-confirm",
    ),
    path(
        "deletion/restore/",
        AccountDeletionRestoreView.as_view(),
        name="account-deletion-restore",
    ),
]
