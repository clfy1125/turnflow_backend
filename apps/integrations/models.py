"""
Instagram Account Connection models
"""

from django.db import models
from django.utils import timezone
from datetime import timedelta
import uuid
from .encryption import EncryptedTextField


class IGAccountConnection(models.Model):
    """
    Instagram Business Account Connection
    Stores OAuth tokens and account information
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        ERROR = "error", "Error"

    class Meta:
        db_table = "ig_account_connections"
        verbose_name = "Instagram Account Connection"
        verbose_name_plural = "Instagram Account Connections"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "status"]),
            models.Index(fields=["external_account_id"]),
            models.Index(fields=["status"]),
        ]

    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Relations
    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="ig_connections",
        verbose_name="Workspace",
    )

    # Instagram Account Info
    external_account_id = models.CharField(
        max_length=255, verbose_name="Instagram Account ID", db_index=True
    )
    username = models.CharField(max_length=255, verbose_name="Instagram Username", blank=True)
    account_type = models.CharField(
        max_length=50, verbose_name="Account Type", blank=True
    )  # BUSINESS, CREATOR

    # OAuth Tokens (encrypted)
    _encrypted_access_token = models.TextField(verbose_name="Encrypted Access Token")
    access_token = EncryptedTextField("_encrypted_access_token")

    # Token metadata
    token_expires_at = models.DateTimeField(verbose_name="Token Expires At", null=True, blank=True)
    scopes = models.JSONField(
        default=list, verbose_name="OAuth Scopes", help_text="List of granted permissions"
    )

    # Connection status
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, verbose_name="Status"
    )
    last_verified_at = models.DateTimeField(null=True, blank=True, verbose_name="Last Verified At")
    error_message = models.TextField(blank=True, verbose_name="Last Error Message")

    # Additional metadata
    metadata = models.JSONField(default=dict, blank=True, verbose_name="Additional Metadata")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created At")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Updated At")

    def __str__(self):
        return f"{self.username or self.external_account_id} ({self.workspace.name})"

    def is_token_expired(self) -> bool:
        """Check if access token is expired"""
        if not self.token_expires_at:
            return False
        return timezone.now() >= self.token_expires_at

    def refresh_token_if_needed(self):
        """
        Check if token needs refresh and attempt to refresh
        For long-lived tokens, they typically last 60 days
        """
        if self.is_token_expired():
            self.status = self.Status.EXPIRED
            self.save()
            return False
        return True

    def mark_as_verified(self):
        """Mark connection as recently verified"""
        self.last_verified_at = timezone.now()
        self.status = self.Status.ACTIVE
        self.error_message = ""
        self.save(update_fields=["last_verified_at", "status", "error_message", "updated_at"])

    def mark_as_error(self, error_message: str):
        """Mark connection as having an error"""
        self.status = self.Status.ERROR
        self.error_message = error_message
        self.save(update_fields=["status", "error_message", "updated_at"])

    @classmethod
    def get_active_connection(cls, workspace):
        """Get active connection for workspace"""
        return cls.objects.filter(workspace=workspace, status=cls.Status.ACTIVE).first()


class AutoDMCampaign(models.Model):
    """
    자동 DM 발송 캠페인
    특정 게시물에 댓글 달린 사람들에게 자동으로 DM 발송
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "활성"
        PAUSED = "paused", "일시정지"
        COMPLETED = "completed", "완료"
        INACTIVE = "inactive", "비활성"

    class Meta:
        db_table = "auto_dm_campaigns"
        verbose_name = "Auto DM Campaign"
        verbose_name_plural = "Auto DM Campaigns"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ig_connection", "status"]),
            models.Index(fields=["media_id"]),
            models.Index(fields=["status"]),
        ]

    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Relations
    ig_connection = models.ForeignKey(
        IGAccountConnection,
        on_delete=models.CASCADE,
        related_name="dm_campaigns",
        verbose_name="Instagram Connection",
    )

    # 대상 게시물 정보
    media_id = models.CharField(
        max_length=255,
        verbose_name="Instagram Media ID",
        db_index=True,
        help_text="댓글을 감지할 Instagram 게시물 ID",
    )
    media_url = models.URLField(
        blank=True, null=True, verbose_name="Media URL", help_text="게시물 URL (참고용)"
    )

    # 캠페인 정보
    name = models.CharField(max_length=255, verbose_name="캠페인 이름")
    description = models.TextField(blank=True, verbose_name="설명")

    # DM 메시지 템플릿
    message_template = models.TextField(
        verbose_name="DM 메시지 템플릿", help_text="댓글 작성자에게 전송될 DM 내용"
    )

    # 발송 옵션
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, verbose_name="상태"
    )

    # 발송 제한
    max_sends_per_hour = models.IntegerField(
        default=200,
        verbose_name="시간당 최대 발송 수",
        help_text="스팸 방지를 위한 시간당 발송 제한",
    )

    # 통계
    total_sent = models.IntegerField(default=0, verbose_name="총 발송 수")
    total_failed = models.IntegerField(default=0, verbose_name="총 실패 수")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일시")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="시작일시")
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="종료일시")

    def __str__(self):
        return f"{self.name} ({self.media_id})"

    def is_active(self) -> bool:
        """캠페인이 활성 상태인지 확인"""
        return self.status == self.Status.ACTIVE

    def can_send_more(self) -> bool:
        """더 많은 DM을 보낼 수 있는지 확인 (시간당 제한 체크)"""
        if not self.is_active():
            return False

        # 최근 1시간 동안 발송된 DM 개수 체크
        one_hour_ago = timezone.now() - timedelta(hours=1)
        recent_sends = self.dm_logs.filter(created_at__gte=one_hour_ago).count()

        return recent_sends < self.max_sends_per_hour

    def increment_sent(self):
        """발송 카운트 증가"""
        self.total_sent += 1
        self.save(update_fields=["total_sent", "updated_at"])

    def increment_failed(self):
        """실패 카운트 증가"""
        self.total_failed += 1
        self.save(update_fields=["total_failed", "updated_at"])


class SentDMLog(models.Model):
    """
    DM 발송 로그
    중복 발송 방지 및 발송 이력 추적
    """

    class Status(models.TextChoices):
        PENDING = "pending", "대기중"
        SENT = "sent", "발송완료"
        FAILED = "failed", "발송실패"
        SKIPPED = "skipped", "건너뜀"

    class Meta:
        db_table = "sent_dm_logs"
        verbose_name = "Sent DM Log"
        verbose_name_plural = "Sent DM Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["campaign", "comment_id"]),
            models.Index(fields=["campaign", "recipient_username"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            # 같은 캠페인에서 같은 댓글에 대해 중복 발송 방지
            models.UniqueConstraint(
                fields=["campaign", "comment_id"], name="unique_campaign_comment"
            )
        ]

    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Relations
    campaign = models.ForeignKey(
        AutoDMCampaign, on_delete=models.CASCADE, related_name="dm_logs", verbose_name="캠페인"
    )

    # 댓글 정보
    comment_id = models.CharField(max_length=255, verbose_name="댓글 ID", db_index=True)
    comment_text = models.TextField(blank=True, verbose_name="댓글 내용")

    # 수신자 정보
    recipient_user_id = models.CharField(max_length=255, verbose_name="수신자 Instagram ID")
    recipient_username = models.CharField(max_length=255, verbose_name="수신자 Username")

    # 발송 내용
    message_sent = models.TextField(verbose_name="발송된 메시지")

    # 발송 상태
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, verbose_name="상태"
    )

    # 에러 정보
    error_message = models.TextField(blank=True, verbose_name="에러 메시지")
    error_code = models.CharField(max_length=50, blank=True, verbose_name="에러 코드")

    # 메타데이터
    webhook_payload = models.JSONField(default=dict, blank=True, verbose_name="Webhook 원본 데이터")
    api_response = models.JSONField(default=dict, blank=True, verbose_name="API 응답 데이터")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="발송일시")

    def __str__(self):
        return f"{self.recipient_username} - {self.status}"

    def mark_as_sent(self, api_response: dict = None):
        """발송 완료 처리"""
        self.status = self.Status.SENT
        self.sent_at = timezone.now()
        if api_response:
            self.api_response = api_response
        self.save(update_fields=["status", "sent_at", "api_response"])

    def mark_as_failed(self, error_message: str, error_code: str = "", api_response: dict = None):
        """발송 실패 처리"""
        self.status = self.Status.FAILED
        self.error_message = error_message
        self.error_code = error_code
        if api_response:
            self.api_response = api_response
        self.save(update_fields=["status", "error_message", "error_code", "api_response"])

    def mark_as_skipped(self, reason: str):
        """건너뛰기 처리"""
        self.status = self.Status.SKIPPED
        self.error_message = reason
        self.save(update_fields=["status", "error_message"])
