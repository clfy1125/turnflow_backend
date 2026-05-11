"""
TikTok integration models.

Scope (post video-feature removal): TikTok Business API only.
Ref: https://business-api.tiktok.com/portal/docs

OAuth provider:  business-api.tiktok.com (Business Center / Advertiser auth)
Used scopes:     Ad Comments + TikTok Accounts

All comment moderation is performed against an authorized advertiser_id.
"""

import uuid

from django.db import models
from django.utils import timezone

from apps.integrations.encryption import EncryptedTextField


class TikTokAccountConnection(models.Model):
    """
    Workspace ↔ TikTok Business advertiser connection.

    One OAuth grant from a Business Center owner can authorize multiple
    advertiser IDs; we store one row per (workspace, advertiser_id) so that
    permissions and stats are tracked per advertiser.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        ERROR = "error", "Error"

    class Meta:
        db_table = "tiktok_account_connections"
        verbose_name = "TikTok Account Connection"
        verbose_name_plural = "TikTok Account Connections"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "status"]),
            models.Index(fields=["external_account_id"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "external_account_id"],
                name="uniq_tiktok_conn_per_workspace",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="tiktok_connections",
        verbose_name="Workspace",
    )

    # In the Business API context this is the TikTok advertiser_id.
    external_account_id = models.CharField(
        max_length=255, verbose_name="advertiser_id", db_index=True
    )
    bc_id = models.CharField(
        max_length=255, blank=True, default="", verbose_name="Business Center ID"
    )
    advertiser_name = models.CharField(
        max_length=255, blank=True, default="", verbose_name="광고 계정명"
    )

    # Encrypted tokens. Business API access tokens are long-lived (no rotation
    # by default), but we still encrypt at rest and track an expiry hint when
    # one is returned.
    _encrypted_access_token = models.TextField(verbose_name="Encrypted Access Token")
    access_token = EncryptedTextField("_encrypted_access_token")
    _encrypted_refresh_token = models.TextField(
        blank=True, default="", verbose_name="Encrypted Refresh Token"
    )
    refresh_token = EncryptedTextField("_encrypted_refresh_token")

    token_expires_at = models.DateTimeField(null=True, blank=True)

    scopes = models.JSONField(default=list, verbose_name="Granted scopes")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE,
    )
    last_verified_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.advertiser_name or self.external_account_id} ({self.workspace.name})"

    def is_token_expired(self) -> bool:
        if not self.token_expires_at:
            return False
        return timezone.now() >= self.token_expires_at

    def mark_as_verified(self):
        self.last_verified_at = timezone.now()
        self.status = self.Status.ACTIVE
        self.error_message = ""
        self.save(update_fields=["last_verified_at", "status", "error_message", "updated_at"])

    def mark_as_error(self, error_message: str):
        self.status = self.Status.ERROR
        self.error_message = error_message
        self.save(update_fields=["status", "error_message", "updated_at"])

    @classmethod
    def get_active_connection(cls, workspace):
        return cls.objects.filter(workspace=workspace, status=cls.Status.ACTIVE).first()


class TikTokOAuthState(models.Model):
    """Short-lived state token for the popup OAuth flow (CSRF + workspace pinning)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=255, unique=True, db_index=True)
    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="tiktok_oauth_states",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "tiktok_oauth_states"
        verbose_name = "TikTok OAuth State"
        verbose_name_plural = "TikTok OAuth States"

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"TikTokOAuthState(state={self.state[:12]}...)"


# ─────────────────────────────────────────────────────────────────────────────
# Spam detection config (heuristic rule knobs — provider-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

class TikTokSpamFilterConfig(models.Model):
    """Per-connection rule knobs for comment spam detection."""

    class Status(models.TextChoices):
        ACTIVE = "active", "활성"
        INACTIVE = "inactive", "비활성"

    class Action(models.TextChoices):
        REVIEW = "review", "검토 큐로"
        HIDE = "hide", "숨김"
        DELETE = "delete", "삭제"

    class Meta:
        db_table = "tiktok_spam_filter_configs"
        verbose_name = "TikTok Spam Filter Config"
        verbose_name_plural = "TikTok Spam Filter Configs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection"], name="uniq_tiktok_spam_filter_per_connection",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.OneToOneField(
        TikTokAccountConnection,
        on_delete=models.CASCADE,
        related_name="spam_filter",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.INACTIVE,
    )
    spam_keywords = models.JSONField(default=list, blank=True)
    block_urls = models.BooleanField(default=True)
    block_shortened_urls = models.BooleanField(default=True)
    min_length = models.IntegerField(default=2)
    max_emoji_ratio = models.FloatField(default=0.7)
    max_mentions = models.IntegerField(default=3)
    score_threshold = models.FloatField(default=1.0)
    default_action = models.CharField(
        max_length=10, choices=Action.choices, default=Action.REVIEW,
    )
    total_spam_detected = models.IntegerField(default=0)
    total_hidden = models.IntegerField(default=0)
    total_deleted = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def __str__(self):
        return f"TikTokSpamFilter({self.connection_id}, {self.status})"


class TikTokCommentLog(models.Model):
    """A cached ad-comment + moderation outcome (one row per external comment id)."""

    class Status(models.TextChoices):
        PENDING = "pending", "대기"
        CLEAN = "clean", "정상"
        DETECTED = "detected", "스팸 감지"
        HIDDEN = "hidden", "숨김 완료"
        DELETED = "deleted", "삭제 완료"
        REVIEW = "review", "검토 대기"
        FAILED = "failed", "처리 실패"

    class Meta:
        db_table = "tiktok_comment_logs"
        verbose_name = "TikTok Comment Log"
        verbose_name_plural = "TikTok Comment Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["connection", "status"]),
            models.Index(fields=["ad_id"]),
            models.Index(fields=["external_comment_id"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_comment_id"],
                name="uniq_tiktok_comment_per_connection",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        TikTokAccountConnection,
        on_delete=models.CASCADE,
        related_name="comment_logs",
    )

    # Business API context: comments are attached to ads, not organic videos.
    advertiser_id = models.CharField(max_length=255, blank=True, default="")
    ad_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    creative_id = models.CharField(max_length=255, blank=True, default="")
    parent_comment_id = models.CharField(max_length=255, blank=True, default="")
    external_comment_id = models.CharField(max_length=255)

    commenter_external_id = models.CharField(max_length=255, blank=True, default="")
    commenter_username = models.CharField(max_length=255, blank=True, default="")
    text = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    score = models.FloatField(default=0.0)
    reasons = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True, default="")
    api_response = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    moderated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"TikTokComment({self.external_comment_id}, {self.status})"

    def mark_clean(self):
        self.status = self.Status.CLEAN
        self.save(update_fields=["status", "updated_at"])

    def mark_detected(self, score: float, reasons: list):
        self.status = self.Status.DETECTED
        self.score = score
        self.reasons = reasons
        self.save(update_fields=["status", "score", "reasons", "updated_at"])

    def mark_hidden(self, response: dict = None):
        self.status = self.Status.HIDDEN
        self.moderated_at = timezone.now()
        if response is not None:
            self.api_response = response
        self.save(
            update_fields=["status", "moderated_at", "api_response", "updated_at"],
        )

    def mark_deleted(self, response: dict = None):
        self.status = self.Status.DELETED
        self.moderated_at = timezone.now()
        if response is not None:
            self.api_response = response
        self.save(
            update_fields=["status", "moderated_at", "api_response", "updated_at"],
        )

    def mark_review(self, score: float, reasons: list):
        self.status = self.Status.REVIEW
        self.score = score
        self.reasons = reasons
        self.save(update_fields=["status", "score", "reasons", "updated_at"])

    def mark_failed(self, message: str, response: dict = None):
        self.status = self.Status.FAILED
        self.error_message = message[:2000]
        if response is not None:
            self.api_response = response
        self.save(
            update_fields=["status", "error_message", "api_response", "updated_at"],
        )


class TikTokBlockedWord(models.Model):
    """
    Per-connection cache of TikTok Business "blockedword" entries.

    TikTok keeps an authoritative list on its side; this table is our local
    mirror so we can show/diff/sync without round-tripping the API on every
    request. ``external_id`` is TikTok's internal blockedword id when known.
    """

    class Meta:
        db_table = "tiktok_blocked_words"
        verbose_name = "TikTok Blocked Word"
        verbose_name_plural = "TikTok Blocked Words"
        ordering = ["word"]
        indexes = [
            models.Index(fields=["connection", "word"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "word"],
                name="uniq_tiktok_blockedword_per_connection",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        TikTokAccountConnection,
        on_delete=models.CASCADE,
        related_name="blocked_words",
    )
    word = models.CharField(max_length=255)
    external_id = models.CharField(
        max_length=255, blank=True, default="",
        help_text="TikTok 측 blockedword id (확인된 경우만)",
    )
    is_synced = models.BooleanField(
        default=False,
        help_text="False = 로컬에만 있음 / True = TikTok 측에도 반영 완료",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"TikTokBlockedWord({self.word})"
