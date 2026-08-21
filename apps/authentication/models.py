"""
Custom User model with email as username
"""

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """Custom user manager for email-based authentication"""

    def create_user(self, email, password=None, **extra_fields):
        """Create and return a regular user with email and password"""
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create and return a superuser with email and password"""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Custom User model
    - Email is used as the username field
    - Additional fields can be added later for profile, workspace relations, etc.
    """

    email = models.EmailField(unique=True, verbose_name="Email Address")
    full_name = models.CharField(max_length=255, blank=True, verbose_name="Full Name")
    is_email_verified = models.BooleanField(default=False, verbose_name="Email Verified")
    email_verified_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Email Verified At"
    )
    # 마케팅(광고성) 수신 동의 — 정보통신망법. 윈백 등 마케팅 메일 발송의 필수 게이트.
    # 현재 수집 경로(가입/설정)가 연결되기 전까지 기본 False → 마케팅 발송 dormant.
    marketing_opt_in = models.BooleanField(default=False, verbose_name="마케팅 수신 동의")
    marketing_opt_in_at = models.DateTimeField(
        null=True, blank=True, verbose_name="마케팅 수신 동의 시각"
    )

    # ── 회원탈퇴 유예 (Google Play 계정 삭제 정책 / 개인정보보호법 §21) ──────────────
    # 웹 단독 탈퇴(`turnflow.link/delete-account`)는 즉시 하드 삭제가 아니라
    #   ① 즉시 비활성화(is_active=False) + 구독 해지  ② `deletion_scheduled_at` 후 영구 파기
    # 2단으로 처리한다. 유예를 두는 이유가 두 개다:
    #   - 개인정보보호법 §21① "지체 없이 파기" 를 지키면서 오탈퇴를 되돌릴 창구가 필요하다.
    #     (유예 목적·기간을 고지하고 취소 경로를 실제로 제공해야 '지연'이 아닌 '유예'로 방어된다)
    #   - 탈퇴 자체가 메일함 접근만으로 가능하므로, 메일함 탈취 시 복구 경로가 된다.
    # ⚠️ 이 필드가 채워진 계정은 `is_active=False` 라 `authenticate()` 가 None 을 준다.
    #    LoginView 가 그 상태를 구분해 복구 안내를 내려준다 — 안 하면 "비밀번호 틀림"으로 보인다.
    deletion_requested_at = models.DateTimeField(
        null=True, blank=True, verbose_name="탈퇴 확정 시각"
    )
    deletion_scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="영구 삭제 예정 시각",
        help_text="이 시각이 지나면 authentication.purge_deleted_accounts 가 하드 삭제한다",
    )

    username = None  # Remove username field

    # Override username to use email
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # No additional required fields for createsuperuser

    objects = UserManager()

    class Meta:
        db_table = "users"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-date_joined"]

    def __str__(self):
        return self.email

    @property
    def display_name(self):
        """Return full name if available, otherwise email"""
        return self.full_name if self.full_name else self.email

    @property
    def is_pending_deletion(self) -> bool:
        """탈퇴 확정됐으나 아직 영구 파기 전(유예 중)인가."""
        return self.deletion_scheduled_at is not None
