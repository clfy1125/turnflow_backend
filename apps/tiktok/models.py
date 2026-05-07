"""
TikTok integration models.

Two distinct TikTok APIs are involved (kept separate intentionally):

1. **Content Posting API** (`developers.tiktok.com`, host `open.tiktokapis.com`)
   - Used for OAuth (Login Kit) + video publishing.
   - Scopes used here: ``user.info.basic``, ``video.publish``, ``video.upload``.
   - Until the app passes audit, every published video is forced to ``SELF_ONLY``
     privacy by TikTok regardless of what the user requests.

2. **Business API** (`business-api.tiktok.com`) — Phase 2.
   - Used for organic comment moderation (hide/unhide). Separate OAuth.
   - Modeled here as ``TikTokBusinessConnection`` but not wired in MVP.
"""

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.integrations.encryption import EncryptedTextField


class TikTokAccountConnection(models.Model):
    """Workspace → TikTok creator account (Content Posting / Login Kit OAuth)."""

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

    # TikTok identifies users by ``open_id`` (per-app stable ID).
    external_account_id = models.CharField(
        max_length=255, verbose_name="TikTok open_id", db_index=True
    )
    union_id = models.CharField(
        max_length=255, blank=True, default="", verbose_name="TikTok union_id"
    )
    username = models.CharField(max_length=255, blank=True, verbose_name="Display name")
    avatar_url = models.URLField(blank=True, default="", verbose_name="Avatar URL")

    # Encrypted tokens (Fernet via apps.integrations.encryption)
    _encrypted_access_token = models.TextField(verbose_name="Encrypted Access Token")
    access_token = EncryptedTextField("_encrypted_access_token")
    _encrypted_refresh_token = models.TextField(
        blank=True, default="", verbose_name="Encrypted Refresh Token"
    )
    refresh_token = EncryptedTextField("_encrypted_refresh_token")

    token_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)

    scopes = models.JSONField(default=list, verbose_name="Granted scopes")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, verbose_name="Status"
    )
    last_verified_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    # Audit gate flag — set True after TikTok lifts the post-visibility restriction.
    is_audited = models.BooleanField(
        default=False,
        help_text=(
            "False = unaudited TikTok client. Every publish call is forced to "
            "privacy_level=SELF_ONLY regardless of caller intent."
        ),
    )

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.username or self.external_account_id} ({self.workspace.name})"

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
    """Short-lived state token for popup OAuth flows (CSRF + workspace pinning)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=255, unique=True, db_index=True)
    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="tiktok_oauth_states",
    )
    code_verifier = models.CharField(
        max_length=128, blank=True, default="", help_text="PKCE code_verifier (S256)"
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


class TikTokVideoPost(models.Model):
    """
    A single video publish request to TikTok Content Posting API.

    State machine:
        QUEUED → INITIATING → UPLOADING (FILE_UPLOAD only) → PROCESSING → PUBLISHED
                                                                    \
                                                                     → FAILED
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "큐 대기"
        INITIATING = "initiating", "TikTok init 호출 중"
        UPLOADING = "uploading", "파일 업로드 중"
        PROCESSING = "processing", "TikTok 처리 중"
        PUBLISHED = "published", "발행 완료"
        FAILED = "failed", "발행 실패"

    class SourceType(models.TextChoices):
        FILE_UPLOAD = "FILE_UPLOAD", "FILE_UPLOAD"
        PULL_FROM_URL = "PULL_FROM_URL", "PULL_FROM_URL"

    # TikTok privacy_level enum (Content Posting API).
    # Until audited, only SELF_ONLY is honored.
    class Privacy(models.TextChoices):
        PUBLIC = "PUBLIC_TO_EVERYONE", "공개"
        FRIENDS = "MUTUAL_FOLLOW_FRIENDS", "친구만"
        FOLLOWER = "FOLLOWER_OF_CREATOR", "팔로워만"
        SELF_ONLY = "SELF_ONLY", "비공개"

    class Meta:
        db_table = "tiktok_video_posts"
        verbose_name = "TikTok Video Post"
        verbose_name_plural = "TikTok Video Posts"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["connection", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["publish_id"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        TikTokAccountConnection,
        on_delete=models.CASCADE,
        related_name="video_posts",
        verbose_name="TikTok Connection",
    )

    # User-facing input
    caption = models.TextField(blank=True, default="", verbose_name="캡션 (마크업/해시태그 포함)")
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.PULL_FROM_URL,
    )
    video_url = models.URLField(
        blank=True, default="",
        help_text="PULL_FROM_URL 방식의 원본 영상 URL. TikTok이 직접 fetch.",
    )
    video_file_path = models.CharField(
        max_length=500, blank=True, default="",
        help_text="FILE_UPLOAD 방식의 서버 로컬/스토리지 경로",
    )
    video_size_bytes = models.BigIntegerField(default=0)

    requested_privacy = models.CharField(
        max_length=30, choices=Privacy.choices, default=Privacy.SELF_ONLY,
    )
    effective_privacy = models.CharField(
        max_length=30, choices=Privacy.choices, default=Privacy.SELF_ONLY,
        help_text="실제 TikTok에 전달된 privacy. 미감사 시 SELF_ONLY로 강제됨.",
    )
    disable_duet = models.BooleanField(default=False)
    disable_comment = models.BooleanField(default=False)
    disable_stitch = models.BooleanField(default=False)
    auto_add_music = models.BooleanField(default=False)

    # TikTok-side identifiers
    publish_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    upload_url = models.URLField(blank=True, default="")
    tiktok_video_id = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True,
    )
    fail_reason = models.TextField(blank=True, default="")
    api_response = models.JSONField(default=dict, blank=True)

    # Retry tracking
    retry_count = models.IntegerField(default=0)
    next_check_at = models.DateTimeField(null=True, blank=True)

    # Lifecycle timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    initiated_at = models.DateTimeField(null=True, blank=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"TikTokPost({self.id}, {self.status})"

    def mark_failed(self, reason: str, response: dict = None):
        self.status = self.Status.FAILED
        self.fail_reason = reason[:2000]
        if response is not None:
            self.api_response = response
        self.save(update_fields=["status", "fail_reason", "api_response", "updated_at"])

    def mark_published(self, tiktok_video_id: str = "", response: dict = None):
        self.status = self.Status.PUBLISHED
        if tiktok_video_id:
            self.tiktok_video_id = tiktok_video_id
        if response is not None:
            self.api_response = response
        self.published_at = timezone.now()
        self.save(
            update_fields=[
                "status", "tiktok_video_id", "api_response", "published_at", "updated_at",
            ]
        )


# ─────────────────────────────────────────────────────────────────────────────
# Comment moderation (Phase 1: heuristic detection + Mock; Phase 2: Business API)
# ─────────────────────────────────────────────────────────────────────────────

class TikTokSpamFilterConfig(models.Model):
    """Per-connection rule knobs for comment spam detection."""

    class Status(models.TextChoices):
        ACTIVE = "active", "활성"
        INACTIVE = "inactive", "비활성"

    class Action(models.TextChoices):
        REVIEW = "review", "검토 큐로"
        HIDE = "hide", "숨김"
        # NOTE: TikTok organic API does **not** allow deleting fan comments.
        # ``delete`` here is mapped to ``hide`` at moderation time.

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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def __str__(self):
        return f"TikTokSpamFilter({self.connection_id}, {self.status})"


class TikTokCommentLog(models.Model):
    """A cached comment + moderation outcome."""

    class Status(models.TextChoices):
        PENDING = "pending", "대기"
        CLEAN = "clean", "정상"
        DETECTED = "detected", "스팸 감지"
        HIDDEN = "hidden", "숨김 완료"
        REVIEW = "review", "검토 대기"
        FAILED = "failed", "처리 실패"

    class Meta:
        db_table = "tiktok_comment_logs"
        verbose_name = "TikTok Comment Log"
        verbose_name_plural = "TikTok Comment Logs"
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
                name="uniq_tiktok_comment_per_connection",
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        TikTokAccountConnection,
        on_delete=models.CASCADE,
        related_name="comment_logs",
    )
    external_video_id = models.CharField(max_length=255, db_index=True)
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
