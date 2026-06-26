"""
환영 메일 발송 시점 테스트.

규칙:
- 이메일 가입(미인증) → 가입 시 환영 메일 미발송, 인증 메일만 발송.
- 코드/링크 인증 성공 시점 → 환영 메일 1회 발송.
- 구글 OAuth(가입 시 자동 인증) → 가입 즉시 환영 메일 발송.
"""

from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from apps.emails.models import EmailToken, EmailTokenPurpose

User = get_user_model()


@pytest.fixture
def signal_tasks(monkeypatch):
    """signals.py 가 참조하는 Celery 태스크들을 목으로 교체하고 반환."""
    mocks = {}
    for name in ("send_verification_email", "send_welcome_email", "schedule_onboarding"):
        m = MagicMock()
        monkeypatch.setattr(f"apps.emails.signals.{name}", m)
        mocks[name] = m
    return mocks


@pytest.mark.django_db
class TestWelcomeEmailTiming:
    def test_email_signup_does_not_send_welcome(self, signal_tasks):
        """이메일 가입(미인증): 인증 메일만, 환영 메일은 보류."""
        User.objects.create_user(
            email="welcome-email-signup@example.com", password="StrongPass123!"
        )
        signal_tasks["send_verification_email"].delay.assert_called_once()
        signal_tasks["send_welcome_email"].apply_async.assert_not_called()
        signal_tasks["send_welcome_email"].delay.assert_not_called()

    def test_oauth_verified_signup_sends_welcome_immediately(self, signal_tasks):
        """가입 시점에 이미 인증됨(OAuth): 즉시 환영, 인증 메일 미발송."""
        user = User.objects.create_user(email="welcome-oauth@example.com", is_email_verified=True)
        user.set_unusable_password()
        user.save(update_fields=["password"])

        signal_tasks["send_welcome_email"].apply_async.assert_called_once()
        signal_tasks["send_verification_email"].delay.assert_not_called()

    def test_welcome_sent_on_verification_success(self, signal_tasks, monkeypatch):
        """코드/링크 인증 성공 시 환영 메일 1회 발송, 재인증 시 미발송."""
        view_welcome = MagicMock()
        monkeypatch.setattr("apps.emails.views_auth.send_welcome_email", view_welcome)

        user = User.objects.create_user(
            email="welcome-on-verify@example.com", password="StrongPass123!"
        )
        client = APIClient()
        url = "/api/v1/auth/email/verify/"

        # 1차 인증 → 전환 발생 → 환영 1회.
        _, raw_token = EmailToken.issue(
            user=user, purpose=EmailTokenPurpose.EMAIL_VERIFY, ttl_minutes=30
        )
        resp = client.post(url, {"token": raw_token}, format="json")
        assert resp.status_code == status.HTTP_200_OK
        user.refresh_from_db()
        assert user.is_email_verified is True
        view_welcome.delay.assert_called_once_with(user.id)

        # 이미 인증된 상태에서 또 다른 유효 토큰으로 재호출 → 환영 재발송 없음.
        _, raw_token2 = EmailToken.issue(
            user=user, purpose=EmailTokenPurpose.EMAIL_VERIFY, ttl_minutes=30
        )
        resp2 = client.post(url, {"token": raw_token2}, format="json")
        assert resp2.status_code == status.HTTP_200_OK
        view_welcome.delay.assert_called_once()  # 여전히 1회
