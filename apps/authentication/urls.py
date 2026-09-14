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
from .email_views import EmailRegisterVerifyView, EmailRegisterView
from .instagram_views import InstagramLoginStartView, InstagramLoginView
from .kakao_views import KakaoLoginView
from .popup_state_views import PopupStateView
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
    path("kakao/", KakaoLoginView.as_view(), name="kakao-login"),
    # 인스타그램 로그인/가입 — 가입 + 워크스페이스 + IG 연동이 한 번에 끝난다.
    # ⚠️ 기본 비활성(INSTAGRAM_LOGIN_ENABLED=False) — 켜기 전에 테스트 URL 에서 검증할 것.
    path("instagram/start/", InstagramLoginStartView.as_view(), name="instagram-login-start"),
    path("instagram/", InstagramLoginView.as_view(), name="instagram-login"),
    path("me/", MeView.as_view(), name="me"),
    # 성장 팝업 노출 상태 (기기 간 동기화 — 프론트 요청 경로는 users/me/... 였으나
    # 이 저장소의 "나" 리소스는 전부 auth/me/ 아래라 규칙을 맞췄다)
    path("me/popup-state/", PopupStateView.as_view(), name="me-popup-state"),
    # 이메일 등록·인증 (인스타 로그인 사용자 — 자리표시 이메일 계정 전용)
    path("me/email/", EmailRegisterView.as_view(), name="me-email-register"),
    path("me/email/verify/", EmailRegisterVerifyView.as_view(), name="me-email-verify"),
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
