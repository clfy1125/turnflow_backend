"""
Instagram Account Connection models
"""

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

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

    # ===== v3.4 next_media 폴링 추적 =====

    # 마지막으로 폴링/감지한 미디어 (이 시점 이전 게시물은 "다음 게시물"이 아님)
    last_seen_media_id = models.CharField(
        max_length=255, blank=True, default="", verbose_name="마지막 감지 미디어 ID"
    )
    last_seen_media_at = models.DateTimeField(
        null=True, blank=True, verbose_name="마지막 감지 미디어 timestamp"
    )
    last_polled_at = models.DateTimeField(
        null=True, blank=True, verbose_name="마지막 미디어 폴링 시각"
    )

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


class IGOAuthState(models.Model):
    """
    Temporary storage for Instagram OAuth state tokens when frontend opens popup
    This avoids relying on Django session cookies for popup-based flows.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=255, unique=True, db_index=True)
    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="ig_oauth_states",
        verbose_name="Workspace",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "ig_oauth_states"
        verbose_name = "Instagram OAuth State"
        verbose_name_plural = "Instagram OAuth States"

    def is_expired(self):
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"IGOAuthState(state={self.state}, workspace={self.workspace_id})"


class AutoDMCampaign(models.Model):
    """
    자동 DM 발송 캠페인 (v3.3 — 트리거/키워드/공개답글/Follow-gate 확장).

    트리거 모드:
        SPECIFIC_MEDIA — 특정 게시물 ID 매칭 (기본, 기존 호환)
        ANY_MEDIA      — 계정의 모든 게시물에 트리거
        NEXT_MEDIA     — 캠페인 활성 후 첫 신규 게시물에 자동 attach
                         (Beat 폴링이 발견 시 media_id 자동 업데이트하고
                          trigger_type 을 SPECIFIC_MEDIA 로 전환)

    키워드 매칭:
        keyword_filter 가 비어 있으면 모든 댓글 매칭.
        있으면 keyword_mode (any/all/exact) 에 따라 평가.

    Follow-gate (Meta API 한계로 silent verify 불가):
        opening DM 에서 "팔로우 후 'GO' 답장" 안내 → 사용자가 답장 시 reward DM 발송.
        gate_trigger_keywords 매칭으로만 검증 가능.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "활성"
        PAUSED = "paused", "일시정지"
        COMPLETED = "completed", "완료"
        INACTIVE = "inactive", "비활성"

    class TriggerType(models.TextChoices):
        SPECIFIC_MEDIA = "specific_media", "특정 게시물"
        ANY_MEDIA = "any_media", "모든 게시물"
        NEXT_MEDIA = "next_media", "다음 게시물"

    class KeywordMode(models.TextChoices):
        ANY = "any", "키워드 중 하나라도 포함"
        ALL = "all", "모든 키워드 포함"
        EXACT = "exact", "댓글 전체 일치"

    class Meta:
        db_table = "auto_dm_campaigns"
        verbose_name = "Auto DM Campaign"
        verbose_name_plural = "Auto DM Campaigns"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ig_connection", "status"]),
            models.Index(fields=["media_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["ig_connection", "trigger_type", "status"]),
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

    # 트리거 정보
    trigger_type = models.CharField(
        max_length=20,
        choices=TriggerType.choices,
        default=TriggerType.SPECIFIC_MEDIA,
        verbose_name="트리거 종류",
        help_text=(
            "specific_media: 특정 게시물 (media_id 필수) / "
            "any_media: 모든 피드 게시물·릴스 / "
            "next_media: 캠페인 활성 후 새로 올리는 게시물 1개에만 자동 적용"
            "(한번 적용되면 specific_media로 자동 전환됨, 다음 게시물부터는 미적용 — "
            "추가 게시물에도 적용하려면 새 캠페인 생성 필요). "
            "전체 가이드는 GET /api/v1/integrations/auto-dm-campaigns/guide/ 참고."
        ),
    )
    media_id = models.CharField(
        max_length=255,
        verbose_name="Instagram Media ID",
        db_index=True,
        blank=True,
        default="",
        help_text="specific_media 트리거에서만 사용 (any/next 모드에선 빈 문자열)",
    )
    media_url = models.URLField(
        blank=True, null=True, verbose_name="Media URL", help_text="게시물 URL (참고용)"
    )

    # 키워드 필터
    keyword_filter = models.JSONField(
        default=list,
        blank=True,
        verbose_name="키워드 필터",
        help_text="비워두면 모든 댓글 매칭. 예: ['info', '가격', '구매']",
    )
    keyword_mode = models.CharField(
        max_length=10,
        choices=KeywordMode.choices,
        default=KeywordMode.ANY,
        verbose_name="키워드 매칭 방식",
    )

    # 캠페인 정보
    name = models.CharField(max_length=255, verbose_name="캠페인 이름")
    description = models.TextField(blank=True, verbose_name="설명")

    # DM 메시지 (legacy: message_template = opening_message_template 의 별칭)
    message_template = models.TextField(
        verbose_name="DM 메시지 템플릿 (legacy)",
        help_text="legacy: opening_message_template 와 동일 의미. 신규 코드는 opening_* 사용.",
        blank=True,
    )
    opening_message_template = models.TextField(
        verbose_name="Opening DM 템플릿",
        help_text="댓글 작성자에게 발송될 첫 DM (Private Reply via comment_id)",
        blank=True,
    )

    # 댓글에 공개 답글
    public_reply_enabled = models.BooleanField(
        default=False,
        verbose_name="공개 답글 게시",
        help_text="DM 발송 시 댓글에 답글도 함께 게시 (예: 'DM 보내드렸습니다!')",
    )
    public_reply_template = models.TextField(
        blank=True,
        default="",
        verbose_name="공개 답글 템플릿 (legacy 단일)",
        help_text=(
            "[deprecated] 단일 템플릿. 새 캠페인은 public_reply_templates 리스트 사용. "
            "기존 데이터 호환을 위해 유지."
        ),
    )
    public_reply_templates = models.JSONField(
        default=list,
        blank=True,
        verbose_name="공개 답글 템플릿 목록",
        help_text=(
            "댓글마다 무작위로 1개씩 골라 답글로 게시. 봇 검사 회피를 위해 "
            "최소 3개 이상 다양한 문구 권장. 예: ['DM 드렸어요!', '확인 부탁드려요 :)', "
            "'안내 보내드렸습니다 🎁']"
        ),
    )
    public_reply_batch_size = models.IntegerField(
        default=10,
        verbose_name="공개 답글 배치 크기",
        help_text="이 개수만큼 답글 게시 후 쿨다운(public_reply_batch_pause_seconds) 적용",
    )
    public_reply_batch_pause_seconds = models.IntegerField(
        default=300,
        verbose_name="공개 답글 배치 쿨다운 (초)",
        help_text="배치 크기 도달 시 다음 답글까지 대기 시간 (Instagram 봇 검사 회피)",
    )

    # Follow-gate (deprecated — Meta API 한계로 silent verify 불가, 답글 신뢰만 가능)
    # 코드 레벨에선 비활성화. 기존 데이터 호환을 위해 컬럼만 유지.
    follow_gate_enabled = models.BooleanField(
        default=False,
        verbose_name="Follow-gate 사용 (deprecated)",
        help_text="[deprecated] Meta API 한계로 silent 검증 불가능 — 코드 무시됨.",
    )
    follow_gate_prompt = models.TextField(
        blank=True,
        default="",
        verbose_name="Follow-gate 안내 문구",
        help_text=(
            "Opening DM에 추가될 follow 요청 문구. "
            "예: '@우리계정 팔로우 후 GO 라고 답장해주세요'"
        ),
    )
    reward_message_template = models.TextField(
        blank=True,
        default="",
        verbose_name="Reward DM 템플릿",
        help_text="follow_gate 통과 후 발송할 본 DM (링크 등 포함)",
    )
    gate_trigger_keywords = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Gate 통과 키워드",
        help_text="이 키워드로 답장 시 gate 통과로 간주. 예: ['GO', 'YES', '네']",
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
        target = self.media_id or self.trigger_type
        return f"{self.name} ({target})"

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

    # ===== 신규 로직 헬퍼 =====

    def get_opening_message(self) -> str:
        """DM 본문 — legacy message_template과 호환.

        Follow-gate prompt 첨부는 deprecated (Meta 한계로 검증 불가).
        """
        return self.opening_message_template or self.message_template or ""

    def pick_public_reply_template(self) -> str:
        """공개 답글 템플릿 1개 선택.

        public_reply_templates 리스트에서 무작위 선택. 비어있으면 legacy
        public_reply_template 사용.
        """
        import random

        candidates = [
            t.strip()
            for t in (self.public_reply_templates or [])
            if t and str(t).strip()
        ]
        if candidates:
            return random.choice(candidates)
        return (self.public_reply_template or "").strip()

    def matches_keyword(self, comment_text: str) -> bool:
        """댓글이 키워드 필터에 매칭되는지"""
        keywords = [k.strip() for k in (self.keyword_filter or []) if k and str(k).strip()]
        if not keywords:
            return True  # 필터 없음 = 모든 댓글 매칭

        text_lower = (comment_text or "").lower()
        keywords_lower = [k.lower() for k in keywords]

        if self.keyword_mode == self.KeywordMode.EXACT:
            return text_lower.strip() in keywords_lower
        if self.keyword_mode == self.KeywordMode.ALL:
            return all(k in text_lower for k in keywords_lower)
        # 기본 ANY
        return any(k in text_lower for k in keywords_lower)

    def matches_media(self, media_id: str) -> bool:
        """이 캠페인이 해당 media_id 트리거에 매칭되는지"""
        if self.trigger_type == self.TriggerType.ANY_MEDIA:
            return True
        # specific_media (또는 attach 된 next_media) — media_id 일치 필요
        return bool(self.media_id) and self.media_id == media_id

    def matches_gate_keyword(self, message_text: str) -> bool:
        """답장 텍스트가 gate 통과 키워드와 매칭되는지"""
        kws = [k.strip() for k in (self.gate_trigger_keywords or []) if k and str(k).strip()]
        if not kws:
            # 키워드 미설정이면 어떤 답장이든 통과 (느슨한 모드)
            return bool(message_text and message_text.strip())
        text_lower = (message_text or "").strip().lower()
        return any(k.lower() == text_lower or k.lower() in text_lower for k in kws)


class SentDMLog(models.Model):
    """
    DM 발송 로그 (99.9% 발송 보증 시스템)

    상태머신:
        QUEUED -> SUBMITTING -> ACCEPTED -> DELIVERED -> READ
                                     |
                                     +-> FAILED_API / FAILED_TOKEN /
                                         FAILED_WINDOW / FAILED_PARAM /
                                         FAILED_NO_TRACE / SKIPPED

    검증 신호:
        1) Meta API 200 + message_id   -> ACCEPTED
        2) messages 웹훅 + is_echo:true -> DELIVERED (1차)
        3) GET /{message_id} 능동 조회   -> DELIVERED (2차 안전망)
        4) messaging_seen 웹훅           -> READ (부가)
    """

    class Status(models.TextChoices):
        # 정상 흐름
        QUEUED = "queued", "큐 대기"
        SUBMITTING = "submitting", "API 호출 중"
        ACCEPTED = "accepted", "Meta 접수됨"
        DELIVERED = "delivered", "도착 확인"
        READ = "read", "읽음 확인"

        # 호환용 (구 데이터)
        PENDING = "pending", "대기중(legacy)"
        SENT = "sent", "발송완료(legacy)"
        FAILED = "failed", "발송실패(legacy)"
        SKIPPED = "skipped", "건너뜀"

        # 분류된 실패 (v3.2 — 명시적 에러 코드)
        FAILED_TOKEN = "failed_token", "토큰 만료/세션 무효"
        FAILED_WINDOW = "failed_window", "24h 윈도우 만료"
        FAILED_PARAM = "failed_param", "파라미터 오류"
        RATE_LIMITED = "rate_limited", "Meta 응답 대기 (지연)"

        # 원인 불명 (200 응답 받았으나 35분 내 도착 미확인 OR 분류 불가 4xx)
        FAILED_NO_TRACE = "failed_no_trace", "도착 미확인 (자가 점검 필요)"

        # legacy alias — 0007 이전 코드/외부 통합과 호환 위해 유지
        FAILED_API = "failed_api", "API 실패(legacy)"

    class VerifiedVia(models.TextChoices):
        ECHO = "echo", "is_echo 웹훅"
        CONV_API = "conv_api", "Conversations API"
        BOTH = "both", "echo+conv_api"

    class DMKind(models.TextChoices):
        STANDALONE = "standalone", "단발 DM (gate 미사용)"
        OPENING = "opening", "Opening DM (인사/Follow 안내)"
        REWARD = "reward", "Reward DM (Gate 통과 후 본 DM)"

    class GateStatus(models.TextChoices):
        NONE = "none", "미적용"
        PENDING = "pending", "Gate 답장 대기"
        PASSED = "passed", "Gate 통과 (reward 발송됨)"
        EXPIRED = "expired", "Gate 답장 24h 만료"

    # 종결 상태 (이 상태가 되면 더 이상 워커가 손대지 않음)
    TERMINAL_STATUSES = (
        Status.DELIVERED,
        Status.READ,
        Status.FAILED_TOKEN,
        Status.FAILED_WINDOW,
        Status.FAILED_PARAM,
        Status.FAILED_NO_TRACE,
        Status.SKIPPED,
        Status.SENT,  # legacy compatibility
        Status.FAILED,  # legacy compatibility
    )

    # 사용자에게 "도착함"이라고 보고할 수 있는 상태
    DELIVERED_STATUSES = (
        Status.DELIVERED,
        Status.READ,
        Status.SENT,  # legacy
    )

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
            models.Index(fields=["status", "accepted_at"]),  # reconcile worker
            models.Index(fields=["meta_message_id"]),  # echo 매칭
            models.Index(fields=["next_retry_at"]),  # 재시도 워커
        ]
        # idempotency_key uniqueness는 필드 자체의 unique=True 로 표현

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
        max_length=20, choices=Status.choices, default=Status.QUEUED, verbose_name="상태"
    )

    # ===== 99.9% 보증 시스템 신규 필드 =====

    # 멱등성 키 (sha256(workspace_id:ig_user_id:comment_id:campaign_id))
    idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="Idempotency Key",
        help_text="중복 발송 차단용 sha256 해시",
    )

    # Meta가 발급한 message_id (POST 응답)
    meta_message_id = models.CharField(
        max_length=255, blank=True, default="", verbose_name="Meta Message ID"
    )

    # 웹훅 echo의 mid (검증용, 보통 meta_message_id와 동일)
    echo_mid = models.CharField(
        max_length=255, blank=True, default="", verbose_name="Echo MID"
    )

    # 검증 경로
    verified_via = models.CharField(
        max_length=16,
        choices=VerifiedVia.choices,
        blank=True,
        default="",
        verbose_name="도착 확인 경로",
    )

    # 에러 분류
    error_code = models.CharField(max_length=50, blank=True, verbose_name="에러 코드")
    error_subcode = models.CharField(max_length=50, blank=True, verbose_name="에러 서브코드")
    error_message = models.TextField(blank=True, verbose_name="에러 메시지")

    # 재시도 관리
    retry_count = models.IntegerField(default=0, verbose_name="재시도 횟수")
    next_retry_at = models.DateTimeField(null=True, blank=True, verbose_name="다음 재시도 시각")

    # 단계별 타임스탬프
    submitted_at = models.DateTimeField(null=True, blank=True, verbose_name="API 호출 시각")
    accepted_at = models.DateTimeField(null=True, blank=True, verbose_name="Meta 접수 시각")
    delivered_at = models.DateTimeField(null=True, blank=True, verbose_name="도착 확인 시각")
    read_at = models.DateTimeField(null=True, blank=True, verbose_name="읽음 시각")

    # 메타데이터
    webhook_payload = models.JSONField(default=dict, blank=True, verbose_name="Webhook 원본 데이터")
    api_response = models.JSONField(default=dict, blank=True, verbose_name="API 응답 데이터")
    verification_log = models.JSONField(
        default=list,
        blank=True,
        verbose_name="검증 로그",
        help_text="능동 조회/echo 매칭 등 검증 시도 이력",
    )

    # ===== v3.3 캠페인 고도화 =====

    # DM 유형 (opening / reward / standalone)
    dm_kind = models.CharField(
        max_length=12,
        choices=DMKind.choices,
        default=DMKind.STANDALONE,
        db_index=True,
        verbose_name="DM 유형",
    )

    # Follow-gate 상태
    gate_status = models.CharField(
        max_length=10,
        choices=GateStatus.choices,
        default=GateStatus.NONE,
        db_index=True,
        verbose_name="Follow-gate 상태",
    )

    # 부모 opening 로그 (reward DM 의 경우 어떤 opening 에서 비롯됐는지)
    parent_log = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_logs",
        verbose_name="부모 로그 (gate 통과 시 opening DM)",
    )

    # 공개 답글 발송 결과 (선택)
    public_reply_posted_at = models.DateTimeField(
        null=True, blank=True, verbose_name="공개 답글 게시 시각"
    )
    public_reply_id = models.CharField(
        max_length=255, blank=True, default="", verbose_name="공개 답글 ID"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="발송일시(legacy)")

    def __str__(self):
        return f"{self.recipient_username} - {self.status}"

    # ===== 상태 전이 헬퍼 =====

    def is_terminal(self) -> bool:
        """더 이상 워커가 변경하지 않는 종결 상태인지"""
        return self.status in self.TERMINAL_STATUSES

    def is_delivered(self) -> bool:
        """사용자에게 '도착함'이라 보고 가능한지"""
        return self.status in self.DELIVERED_STATUSES

    def mark_submitting(self):
        """API 호출 시작"""
        self.status = self.Status.SUBMITTING
        self.submitted_at = timezone.now()
        self.save(update_fields=["status", "submitted_at"])

    def mark_accepted(self, message_id: str, api_response: dict = None):
        """Meta가 200 + message_id 응답"""
        self.status = self.Status.ACCEPTED
        self.meta_message_id = message_id
        self.accepted_at = timezone.now()
        if api_response:
            self.api_response = api_response
        # legacy 호환: sent_at도 기록
        self.sent_at = self.accepted_at
        self.save(
            update_fields=[
                "status",
                "meta_message_id",
                "accepted_at",
                "sent_at",
                "api_response",
            ]
        )

    def mark_delivered(self, via: str, mid: str = ""):
        """도착 확정 (echo 또는 conv_api)"""
        # 이미 더 강한 상태(READ)면 변경하지 않음
        if self.status == self.Status.READ:
            return
        # 이미 DELIVERED인데 다른 경로로 또 들어오면 BOTH로 승격
        if self.status == self.Status.DELIVERED and self.verified_via and self.verified_via != via:
            self.verified_via = self.VerifiedVia.BOTH
        else:
            self.verified_via = via

        self.status = self.Status.DELIVERED
        self.delivered_at = self.delivered_at or timezone.now()
        if mid:
            self.echo_mid = mid
        self.save(
            update_fields=["status", "verified_via", "delivered_at", "echo_mid"]
        )

    def mark_read(self):
        """messaging_seen 수신"""
        self.status = self.Status.READ
        self.read_at = timezone.now()
        if not self.delivered_at:
            self.delivered_at = self.read_at
        self.save(update_fields=["status", "read_at", "delivered_at"])

    def mark_failed(
        self,
        status: str,
        error_message: str,
        error_code: str = "",
        error_subcode: str = "",
        api_response: dict = None,
    ):
        """분류된 실패 처리"""
        self.status = status
        self.error_message = error_message
        self.error_code = error_code
        self.error_subcode = error_subcode
        if api_response:
            self.api_response = api_response
        self.save()

    def mark_skipped(self, reason: str):
        """건너뛰기"""
        self.status = self.Status.SKIPPED
        self.error_message = reason
        self.save(update_fields=["status", "error_message"])

    def append_verification_log(self, entry: dict):
        """검증 시도 로그 추가"""
        log = list(self.verification_log or [])
        entry = dict(entry)
        entry.setdefault("ts", timezone.now().isoformat())
        log.append(entry)
        self.verification_log = log
        self.save(update_fields=["verification_log"])

    # ===== Legacy compatibility =====

    def mark_as_sent(self, api_response: dict = None):
        """Legacy: 호출 시 새 상태머신의 ACCEPTED로 매핑"""
        msg_id = ""
        if api_response and isinstance(api_response, dict):
            msg_id = api_response.get("message_id", "") or ""
        self.mark_accepted(msg_id, api_response)

    def mark_as_failed(self, error_message: str, error_code: str = "", api_response: dict = None):
        """Legacy: 분류 없이 FAILED_API로 매핑"""
        self.mark_failed(
            status=self.Status.FAILED_API,
            error_message=error_message,
            error_code=error_code,
            api_response=api_response,
        )

    def mark_as_skipped(self, reason: str):
        """Legacy alias"""
        self.mark_skipped(reason)


class SpamFilterConfig(models.Model):
    """
    Instagram 계정별 스팸 필터 설정
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "활성"
        INACTIVE = "inactive", "비활성"

    class Meta:
        db_table = "spam_filter_configs"
        verbose_name = "Spam Filter Config"
        verbose_name_plural = "Spam Filter Configs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ig_connection", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["ig_connection"], name="unique_spam_filter_per_ig_connection"
            )
        ]

    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Relations
    ig_connection = models.OneToOneField(
        IGAccountConnection,
        on_delete=models.CASCADE,
        related_name="spam_filter",
        verbose_name="Instagram Connection",
    )

    # 상태
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.INACTIVE, verbose_name="상태"
    )

    # 스팸 키워드 리스트
    spam_keywords = models.JSONField(
        default=list,
        verbose_name="스팸 키워드",
        help_text="검사할 스팸 키워드 목록 (예: ['아이돌', '주소창', '사건'])",
    )

    # URL 차단 설정
    block_urls = models.BooleanField(default=True, verbose_name="URL 차단")

    # 통계
    total_spam_detected = models.IntegerField(default=0, verbose_name="총 스팸 감지 수")
    total_hidden = models.IntegerField(default=0, verbose_name="총 숨김 처리 수")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일시")

    def __str__(self):
        return f"{self.ig_connection.username} - {self.status}"

    def is_active(self) -> bool:
        """스팸 필터가 활성화되어 있는지"""
        return self.status == self.Status.ACTIVE

    def increment_spam_detected(self):
        """스팸 감지 카운트 증가"""
        self.total_spam_detected += 1
        self.save(update_fields=["total_spam_detected", "updated_at"])

    def increment_hidden(self):
        """숨김 처리 카운트 증가"""
        self.total_hidden += 1
        self.save(update_fields=["total_hidden", "updated_at"])


class SpamCommentLog(models.Model):
    """
    스팸 댓글 탐지 및 처리 로그
    """

    class Status(models.TextChoices):
        DETECTED = "detected", "감지됨"
        HIDDEN = "hidden", "숨김처리"
        FAILED = "failed", "처리실패"

    class Meta:
        db_table = "spam_comment_logs"
        verbose_name = "Spam Comment Log"
        verbose_name_plural = "Spam Comment Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["spam_filter", "status"]),
            models.Index(fields=["comment_id"]),
            models.Index(fields=["created_at"]),
        ]

    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Relations
    spam_filter = models.ForeignKey(
        SpamFilterConfig,
        on_delete=models.CASCADE,
        related_name="spam_logs",
        verbose_name="스팸 필터",
    )

    # 댓글 정보
    comment_id = models.CharField(max_length=255, verbose_name="댓글 ID", db_index=True)
    comment_text = models.TextField(verbose_name="댓글 내용")
    commenter_user_id = models.CharField(max_length=255, verbose_name="작성자 Instagram ID")
    commenter_username = models.CharField(max_length=255, verbose_name="작성자 Username")

    # 미디어 정보
    media_id = models.CharField(max_length=255, verbose_name="미디어 ID", blank=True)

    # 스팸 탐지 정보
    spam_reasons = models.JSONField(
        default=list,
        verbose_name="스팸 판정 이유",
        help_text="스팸으로 판단한 이유 목록 (예: ['contains_url', 'keyword:아이돌'])",
    )

    # 처리 상태
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DETECTED, verbose_name="상태"
    )

    # 에러 정보
    error_message = models.TextField(blank=True, verbose_name="에러 메시지")

    # 메타데이터
    webhook_payload = models.JSONField(default=dict, blank=True, verbose_name="Webhook 원본 데이터")
    api_response = models.JSONField(default=dict, blank=True, verbose_name="API 응답 데이터")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="감지일시")
    hidden_at = models.DateTimeField(null=True, blank=True, verbose_name="숨김처리일시")

    def __str__(self):
        return f"Spam: {self.commenter_username} - {self.status}"

    def mark_as_hidden(self, api_response: dict = None):
        """숨김 처리 완료"""
        self.status = self.Status.HIDDEN
        self.hidden_at = timezone.now()
        if api_response:
            self.api_response = api_response
        self.save(update_fields=["status", "hidden_at", "api_response"])

    def mark_as_failed(self, error_message: str, api_response: dict = None):
        """숨김 처리 실패"""
        self.status = self.Status.FAILED
        self.error_message = error_message
        if api_response:
            self.api_response = api_response
        self.save(update_fields=["status", "error_message", "api_response"])
