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
