"""
Email system models.

- `EmailTemplate` : admin-editable subject/body + `{{var}}` placeholders.
- `EmailToken`    : short-lived verification/reset tokens.
- `EmailLog`      : audit log of outbound deliveries (incl. provider message id).
"""

from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from .constants import TEMPLATE_CHOICES


class EmailTemplate(models.Model):
    """Admin-editable email template. One row per `key`."""

    key = models.CharField(
        max_length=64, unique=True, choices=TEMPLATE_CHOICES, verbose_name="Template Key"
    )
    subject = models.CharField(max_length=255, verbose_name="제목 (subject line)")
    html_body = models.TextField(verbose_name="HTML 본문")
    text_body = models.TextField(
        blank=True,
        help_text="순수 텍스트 fallback. 비워두면 HTML에서 자동 추출.",
        verbose_name="텍스트 본문",
    )
    from_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="비워두면 settings.RESEND_FROM_NAME 사용",
        verbose_name="발신자 이름 (override)",
    )
    is_active = models.BooleanField(default=True, verbose_name="활성화")
    available_variables = models.JSONField(
        default=dict,
        blank=True,
        help_text="이 템플릿에서 사용 가능한 {{변수}} 목록 (자동 채움)",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_email_templates",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "email_templates"
        verbose_name = "Email Template"
        verbose_name_plural = "Email Templates"
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key} ({'active' if self.is_active else 'inactive'})"


class EmailTokenPurpose(models.TextChoices):
    EMAIL_VERIFY = "email_verify", "Email Verification"
    PASSWORD_RESET = "password_reset", "Password Reset"


class EmailToken(models.Model):
    """Short-lived token bundle (6-digit code + opaque URL token)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_tokens"
    )
    purpose = models.CharField(max_length=32, choices=EmailTokenPurpose.choices)
    code = models.CharField(max_length=6, help_text="6-digit numeric (plaintext, short TTL)")
    token_hash = models.CharField(
        max_length=64, unique=True, help_text="sha256(token) — token itself never stored"
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "email_tokens"
        verbose_name = "Email Token"
        verbose_name_plural = "Email Tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "purpose", "used_at"]),
            models.Index(fields=["token_hash"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} / {self.purpose} / {self.created_at:%Y-%m-%d %H:%M}"

    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()

    def mark_used(self) -> None:
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    def issue(
        cls,
        *,
        user,
        purpose: str,
        ttl_minutes: int,
        request_ip: str | None = None,
    ) -> tuple["EmailToken", str]:
        """Create a new token row. Returns (row, raw_token).  Raw token is
        returned only once — it is hashed before storage."""
        raw_token = secrets.token_urlsafe(48)
        code = f"{secrets.randbelow(1_000_000):06d}"
        row = cls.objects.create(
            user=user,
            purpose=purpose,
            code=code,
            token_hash=cls.hash_token(raw_token),
            expires_at=timezone.now() + timezone.timedelta(minutes=ttl_minutes),
            request_ip=request_ip,
        )
        return row, raw_token

    @classmethod
    def consume(cls, *, raw_token: str | None = None, code: str | None = None,
                user=None, purpose: str) -> "EmailToken | None":
        """Look up a live token by raw token OR (user + code), atomically mark used."""
        qs = cls.objects.filter(purpose=purpose, used_at__isnull=True, expires_at__gt=timezone.now())
        if raw_token:
            qs = qs.filter(token_hash=cls.hash_token(raw_token))
        elif user and code:
            qs = qs.filter(user=user, code=code)
        else:
            return None
        row = qs.order_by("-created_at").first()
        if row:
            row.mark_used()
        return row


class EmailStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    BOUNCED = "bounced", "Bounced"


class EmailLog(models.Model):
    """Delivery audit log. One row per send attempt."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_logs",
    )
    template = models.ForeignKey(
        EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs"
    )
    template_key = models.CharField(max_length=64, db_index=True)
    to_email = models.EmailField()
    from_email = models.EmailField()
    subject = models.CharField(max_length=255)
    rendered_html = models.TextField(blank=True)
    rendered_text = models.TextField(blank=True)
    context_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=EmailStatus.choices, default=EmailStatus.PENDING, db_index=True
    )
    provider_message_id = models.CharField(max_length=255, blank=True, db_index=True)
    error_message = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "email_logs"
        verbose_name = "Email Log"
        verbose_name_plural = "Email Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["template_key", "status"]),
            models.Index(fields=["to_email", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.status}] {self.template_key} -> {self.to_email}"


class OnboardingSchedule(models.Model):
    """Scheduled onboarding drip row — persisted so we can cancel on
    account deletion and recover from Celery losing queued tasks."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="onboarding_schedules"
    )
    template_key = models.CharField(max_length=64)
    scheduled_for = models.DateTimeField(db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "email_onboarding_schedules"
        ordering = ["scheduled_for"]
        unique_together = [("user", "template_key")]
        indexes = [
            models.Index(fields=["scheduled_for", "sent_at", "cancelled_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} / {self.template_key} @ {self.scheduled_for:%Y-%m-%d}"
