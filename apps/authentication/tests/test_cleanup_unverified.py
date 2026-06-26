"""
미인증 계정 정리 태스크(authentication.cleanup_unverified_accounts) 테스트.

주의(프로젝트 메모리):
- 테스트 DB 가 깨끗하지 않으므로 **내가 생성한 특정 계정의 존재 여부** 로만 단언한다(델타).
- 설정 토글은 `settings` 픽스처로 한다(@override_settings 클래스 데코레이터 금지).
"""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.authentication.tasks import cleanup_unverified_accounts

User = get_user_model()


@pytest.fixture(autouse=True)
def _mute_signup_emails(monkeypatch):
    """User 생성 시 post_save 시그널이 Celery 브로커를 건드리지 않게 무력화."""
    for name in ("send_verification_email", "send_welcome_email", "schedule_onboarding"):
        monkeypatch.setattr(f"apps.emails.signals.{name}", MagicMock())


@pytest.fixture
def cleanup_on(settings):
    """정리 기능 활성 + 실삭제 모드, 보존 1일."""
    settings.UNVERIFIED_ACCOUNT_CLEANUP_ENABLED = True
    settings.UNVERIFIED_ACCOUNT_CLEANUP_DRY_RUN = False
    settings.UNVERIFIED_ACCOUNT_RETENTION_DAYS = 1
    return settings


def _make_user(email, *, days_old=2, verified=False, usable_password=True, **extra):
    user = User.objects.create_user(
        email=email,
        password="StrongPass123!" if usable_password else None,
        is_email_verified=verified,
        **extra,
    )
    if not usable_password:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    # date_joined 는 default=now 이므로 과거로 되돌려 "경과"를 시뮬레이트.
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=days_old))
    return user


@pytest.mark.django_db
class TestCleanupUnverifiedAccounts:
    def test_deletes_old_unverified_email_account(self, cleanup_on):
        user = _make_user("cleanup-old-unverified@example.com", days_old=2)
        cleanup_unverified_accounts()
        assert not User.objects.filter(pk=user.pk).exists()

    def test_keeps_recently_joined_unverified(self, cleanup_on):
        user = _make_user("cleanup-recent@example.com", days_old=0)
        cleanup_unverified_accounts()
        assert User.objects.filter(pk=user.pk).exists()

    def test_keeps_verified_account(self, cleanup_on):
        user = _make_user("cleanup-verified@example.com", days_old=5, verified=True)
        cleanup_unverified_accounts()
        assert User.objects.filter(pk=user.pk).exists()

    def test_keeps_staff_account(self, cleanup_on):
        user = _make_user("cleanup-staff@example.com", days_old=5, is_staff=True)
        cleanup_unverified_accounts()
        assert User.objects.filter(pk=user.pk).exists()

    def test_keeps_workspace_owner(self, cleanup_on):
        from apps.workspace.models import Workspace

        user = _make_user("cleanup-owner@example.com", days_old=5)
        Workspace.objects.create(name="Cleanup Owner WS", owner=user)
        cleanup_unverified_accounts()
        assert User.objects.filter(pk=user.pk).exists()

    def test_keeps_social_account_without_password(self, cleanup_on):
        user = _make_user("cleanup-social@example.com", days_old=5, usable_password=False)
        cleanup_unverified_accounts()
        assert User.objects.filter(pk=user.pk).exists()

    def test_dry_run_keeps_everything(self, settings):
        settings.UNVERIFIED_ACCOUNT_CLEANUP_ENABLED = True
        settings.UNVERIFIED_ACCOUNT_CLEANUP_DRY_RUN = True
        settings.UNVERIFIED_ACCOUNT_RETENTION_DAYS = 1
        user = _make_user("cleanup-dryrun@example.com", days_old=3)
        result = cleanup_unverified_accounts()
        assert User.objects.filter(pk=user.pk).exists()
        assert result["enabled"] is True
        assert result["dry_run"] is True
        assert result["deleted"] == 0

    def test_disabled_is_noop(self, settings):
        settings.UNVERIFIED_ACCOUNT_CLEANUP_ENABLED = False
        user = _make_user("cleanup-disabled@example.com", days_old=3)
        result = cleanup_unverified_accounts()
        assert User.objects.filter(pk=user.pk).exists()
        assert result["enabled"] is False
