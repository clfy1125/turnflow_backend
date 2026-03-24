"""
Authentication URL Configuration
"""

from django.urls import path

from .views import (
    RegisterView,
    LoginView,
    MeView,
    TokenRefreshView,
    AccountDeleteView,
    GoogleLoginView,
)

app_name = "authentication"

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("google/", GoogleLoginView.as_view(), name="google-login"),
    path("me/", MeView.as_view(), name="me"),
    path("me/delete/", AccountDeleteView.as_view(), name="account-delete"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
