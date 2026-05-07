"""
YouTube integration models.

YouTube uses Google OAuth 2.0 (offline access → long-lived refresh token).

Refs:
- https://developers.google.com/youtube/v3/docs
- https://developers.google.com/youtube/v3/determine_quota_cost
- ``videos.insert`` costs **1,600 quota units** (default daily quota = 10,000)
- ``commentThreads.list`` costs **1 unit**
- ``comments.setModerationStatus`` costs **50 units**
"""

import uuid
from datetime import date, timedelta

from django.db import models
from django.utils import timezone

from apps.integrations.encryption import EncryptedTextField


class YouTubeAccountConnection(models.Model):
    """Workspace → YouTube channel connection (Google OAuth)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        ERROR = "error", "Error"

    class Meta:
        db_table = "youtube_account_connections"
        verbose_name = "YouTube Account Connection"
        verbose_name_plural = "YouTube Account Connections"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "status"]),
            models.Index(fields=["external_account_id"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "external_account_id"],
                name="uniq_youtube_conn_per_workspace",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="youtube_connections",
        verbose_name="Workspace",
    )

    # YouTube channel ID (e.g. "UCxxxxxxxxxxxxxx").
    external_account_id = models.CharField(
        max_length=255, db_index=True, verbose_name="YouTube channel ID"
    )
    channel_title = models.CharField(max_length=255, blank=True, default="")
    channel_thumbnail_url = models.URLField(blank=True, default="")
    google_user_id = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Google account ``sub`` claim (per-app pseudonymous identifier).",
    )
    google_email = models.EmailField(
        blank=True, default="",
        help_text="Authenticated Google email (only for display; do not authorize on this).",
    )

    _encrypted_access_token = models.TextField(verbose_name="Encrypted Access Token")
    access_token = EncryptedTextField("_encrypted_access_token")
    _encrypted_refresh_token = models.TextField(
        blank=True, default="", verbose_name="Encrypted Refresh Token"
    )
    refresh_token = EncryptedTextField("_encrypted_refresh_token")
    token_expires_at = models.DateTimeField(null=True, blank=True)

    scopes = models.JSONField(default=list)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE,
    )
    last_verified_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.channel_title or self.external_account_id} ({self.workspace.name})"

    def is_token_expired(self, *, leeway_seconds: int = 60) -> bool:
        if not self.token_expires_at:
            return False
        return timezone.now() + timedelta(seconds=leeway_seconds) >= self.token_expires_at

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


class YouTubeOAuthState(models.Model):
    """Short-lived state token for popup OAuth flows."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=255, unique=True, db_index=True)
    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="youtube_oauth_states",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "youtube_oauth_states"

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"YouTubeOAuthState(state={self.state[:12]}...)"


class YouTubeVideoPost(models.Model):
    """A single ``videos.insert`` request."""

    class Status(models.TextChoices):
        QUEUED = "queued", "큐 대기"
        UPLOADING = "uploading", "업로드 중"
        PROCESSING = "processing", "YouTube 처리 중"
        PUBLISHED = "published", "발행 완료"
        FAILED = "failed", "발행 실패"

    class PrivacyStatus(models.TextChoices):
        PRIVATE = "private", "비공개"
        UNLISTED = "unlisted", "일부 공개"
        PUBLIC = "public", "공개"

    class Meta:
        db_table = "youtube_video_posts"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["connection", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["youtube_video_id"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        YouTubeAccountConnection,
        on_delete=models.CASCADE,
        related_name="video_posts",
    )

    title = models.CharField(max_length=100)  # YouTube hard limit
    description = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    category_id = models.CharField(max_length=20, default="22")  # 22 = People & Blogs
    privacy_status = models.CharField(
        max_length=20, choices=PrivacyStatus.choices, default=PrivacyStatus.PRIVATE,
    )
    made_for_kids = models.BooleanField(default=False)

    video_file_path = models.CharField(
        max_length=500, blank=True, default="",
        help_text="서버 측 영상 파일 경로 (MEDIA_ROOT 기반 또는 절대경로)",
    )
    video_size_bytes = models.BigIntegerField(default=0)

    youtube_video_id = models.CharField(
        max_length=255, blank=True, default="", db_index=True,
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True,
    )
    fail_reason = models.TextField(blank=True, default="")
    api_response = models.JSONField(default=dict, blank=True)

    quota_units_consumed = models.IntegerField(
        default=0,
        help_text="이번 발행이 소비한 quota units (videos.insert 성공 시 1600)",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"YouTubePost({self.id}, {self.status})"

    def mark_failed(self, reason: str, response: dict = None):
        self.status = self.Status.FAILED
        self.fail_reason = reason[:2000]
        if response is not None:
            self.api_response = response
        self.save(update_fields=["status", "fail_reason", "api_response", "updated_at"])

    def mark_published(self, video_id: str, response: dict = None):
        self.youtube_video_id = video_id
        self.status = self.Status.PUBLISHED
        if response is not None:
            self.api_response = response
        self.published_at = timezone.now()
        self.save(
            update_fields=[
                "youtube_video_id", "status", "api_response", "published_at", "updated_at",
            ]
        )


class YouTubeQuotaUsage(models.Model):
    """Per-day quota counter (units) — guards against accidental quota exhaustion."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    day = models.DateField(default=date.today, unique=True, db_index=True)
    units_used = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "youtube_quota_usage"
        ordering = ["-day"]

    def __str__(self):
        return f"{self.day}: {self.units_used} units"

    @classmethod
    def add_units(cls, units: int) -> int:
        """Atomically add ``units`` to today's counter and return the new total."""
        from django.db.models import F

        today = timezone.localdate()
        cls.objects.get_or_create(day=today, defaults={"units_used": 0})
        cls.objects.filter(day=today).update(units_used=F("units_used") + units)
        return cls.objects.get(day=today).units_used

    @classmethod
    def units_used_today(cls) -> int:
        today = timezone.localdate()
        row = cls.objects.filter(day=today).first()
        return row.units_used if row else 0


# ─────────────────────────────────────────────────────────────────────────────
# Comment moderation
# ─────────────────────────────────────────────────────────────────────────────

class YouTubeSpamFilterConfig(models.Model):
    """Per-channel rule knobs for comment spam detection."""

    class Status(models.TextChoices):
        ACTIVE = "active", "활성"
        INACTIVE = "inactive", "비활성"

    class Action(models.TextChoices):
        REVIEW = "review", "heldForReview"
        REJECT = "reject", "rejected"
        # ``markAsSpam`` is deprecated by Google; we use setModerationStatus instead.

    class Meta:
        db_table = "youtube_spam_filter_configs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection"], name="uniq_youtube_spam_filter_per_connection",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.OneToOneField(
        YouTubeAccountConnection,
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
        help_text=(
            "review = comments.setModerationStatus(heldForReview); "
            "reject = comments.setModerationStatus(rejected). 각각 50 quota units."
        ),
    )
    ban_authors_on_reject = models.BooleanField(
        default=False,
        help_text="rejected 처리 시 banAuthor=true 추가. 향후 댓글도 자동 차단.",
    )
    total_spam_detected = models.IntegerField(default=0)
    total_moderated = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def __str__(self):
        return f"YouTubeSpamFilter({self.connection_id}, {self.status})"


class YouTubeCommentLog(models.Model):
    """Cached comment + moderation outcome (top-level comment, not reply)."""

    class Status(models.TextChoices):
        PENDING = "pending", "대기"
        CLEAN = "clean", "정상"
        DETECTED = "detected", "스팸 감지"
        REVIEW = "review", "heldForReview 처리"
        REJECTED = "rejected", "rejected 처리"
        FAILED = "failed", "처리 실패"

    class Meta:
        db_table = "youtube_comment_logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["connection", "status"]),
            models.Index(fields=["external_video_id"]),
            models.Index(fields=["external_comment_id"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_comment_id"],
                name="uniq_youtube_comment_per_connection",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        YouTubeAccountConnection,
        on_delete=models.CASCADE,
        related_name="comment_logs",
    )
    external_video_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    external_thread_id = models.CharField(max_length=255, blank=True, default="")
    external_comment_id = models.CharField(max_length=255)
    commenter_channel_id = models.CharField(max_length=255, blank=True, default="")
    commenter_display_name = models.CharField(max_length=255, blank=True, default="")
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
        return f"YouTubeComment({self.external_comment_id}, {self.status})"

    def mark_clean(self):
        self.status = self.Status.CLEAN
        self.save(update_fields=["status", "updated_at"])

    def mark_detected(self, score: float, reasons: list):
        self.status = self.Status.DETECTED
        self.score = score
        self.reasons = reasons
        self.save(update_fields=["status", "score", "reasons", "updated_at"])

    def mark_review(self, response: dict = None):
        self.status = self.Status.REVIEW
        self.moderated_at = timezone.now()
        if response is not None:
            self.api_response = response
        self.save(
            update_fields=["status", "moderated_at", "api_response", "updated_at"],
        )

    def mark_rejected(self, response: dict = None):
        self.status = self.Status.REJECTED
        self.moderated_at = timezone.now()
        if response is not None:
            self.api_response = response
        self.save(
            update_fields=["status", "moderated_at", "api_response", "updated_at"],
        )

    def mark_failed(self, message: str, response: dict = None):
        self.status = self.Status.FAILED
        self.error_message = message[:2000]
        if response is not None:
            self.api_response = response
        self.save(
            update_fields=["status", "error_message", "api_response", "updated_at"],
        )
