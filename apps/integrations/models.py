"""
Instagram Account Connection models
"""

import uuid

from django.db import IntegrityError, models, transaction
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
        constraints = [
            # P5: 재연동 시 중복 행 생성 방지 → 캠페인 고아화 차단.
            # (워크스페이스 + IG 계정)당 연동은 정확히 1행. 재연동은 그 행을 in-place 갱신.
            models.UniqueConstraint(
                fields=["workspace", "external_account_id"],
                name="uq_igconn_ws_account",
            )
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
    name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Display Name",
        help_text="IG /me 응답의 name 필드 (사람이 읽는 표시명)",
    )
    account_type = models.CharField(
        max_length=50, verbose_name="Account Type", blank=True
    )  # BUSINESS, CREATOR

    # Profile picture — IG CDN URL 은 서명된 일시 URL 로 만료될 수 있으므로
    # 우리 스토리지(R2/로컬)에 사본을 보관하여 안정 URL 제공.
    profile_picture_url = models.URLField(
        max_length=1024,
        blank=True,
        default="",
        verbose_name="Cached Profile Picture URL",
        help_text="R2/로컬에 캐싱된 안정 URL — 프론트에 노출",
    )
    profile_picture_source_url = models.TextField(
        blank=True,
        default="",
        verbose_name="IG Source Profile Picture URL",
        help_text="IG /me 가 준 원본 URL — 변경 감지/디버그용 (내부)",
    )
    profile_picture_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Profile Picture Last Synced At",
    )

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

    def disconnect(self, reason: str = "user_requested") -> dict:
        """
        IG 계정 연동 해제 — 모든 후속 자동화를 안전하게 정지하고 토큰 폐기.

        호출 시점:
            - 사용자 자발적 연동 해제 (POST /instagram/{id}/disconnect/)
            - Meta Deauthorize Callback 수신 (사용자가 IG 설정에서 앱 제거)
            - 토큰 만료/회수 감지 시 운영자 결정

        수행 작업:
            1. webhook 구독 해제 시도 (best-effort, 실패해도 진행)
            2. 이 계정 소유의 활성 캠페인 모두 PAUSED 로 전환
            3. ACCEPTED/QUEUED/SUBMITTING 상태의 in-flight SentDMLog 를 SKIPPED 로 정리
            4. status = REVOKED + 토큰 폐기

        Returns:
            {
                "campaigns_paused": int,
                "logs_cancelled":   int,
                "webhook_unsubscribed": bool,
                "webhook_error": str | None,
            }
        """
        # 순환 import 방지를 위해 함수 내부 import
        from .services import InstagramOAuthService

        report = {
            "campaigns_paused": 0,
            "logs_cancelled": 0,
            "webhook_unsubscribed": False,
            "webhook_error": None,
        }

        # 1) Webhook 구독 해제 (best-effort)
        if self.external_account_id and self.status == self.Status.ACTIVE:
            try:
                InstagramOAuthService.unsubscribe_webhooks(
                    ig_user_id=self.external_account_id,
                    access_token=self.access_token,
                )
                report["webhook_unsubscribed"] = True
            except Exception as e:
                # 실패해도 disconnect 흐름은 계속 — 토큰이 이미 무효일 수도 있음
                report["webhook_error"] = str(e)

        # 2) 활성 캠페인 PAUSED 전환
        from .models import AutoDMCampaign, SentDMLog  # 로컬 import (circular 방지)

        paused = AutoDMCampaign.objects.filter(
            ig_connection=self,
            status=AutoDMCampaign.Status.ACTIVE,
        ).update(
            status=AutoDMCampaign.Status.PAUSED,
            updated_at=timezone.now(),
        )
        report["campaigns_paused"] = paused

        # 3) in-flight SentDMLog 정리 (이미 발송된 DELIVERED/READ 는 건드리지 않음)
        in_flight_statuses = (
            SentDMLog.Status.QUEUED,
            SentDMLog.Status.SUBMITTING,
            SentDMLog.Status.ACCEPTED,
            SentDMLog.Status.RATE_LIMITED,
        )
        cancelled = SentDMLog.objects.filter(
            campaign__ig_connection=self,
            status__in=in_flight_statuses,
        ).update(
            status=SentDMLog.Status.SKIPPED,
            error_message=f"IG connection disconnected ({reason})",
        )
        report["logs_cancelled"] = cancelled

        # 4) status REVOKED + 토큰 폐기
        self.status = self.Status.REVOKED
        self.error_message = f"Disconnected: {reason}"
        # 암호화된 토큰 컬럼을 빈 문자열로 — descriptor 가 자동 처리
        self.access_token = ""
        self.save(
            update_fields=[
                "status",
                "error_message",
                "_encrypted_access_token",
                "updated_at",
            ]
        )

        return report

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

    Follow-gate (v3.8 — is_user_follow_business 기반 silent verify):
        opening DM 에 quick_reply 버튼 ("팔로우했어요") 첨부 → 사용자 클릭 시
        messaging_postbacks 웹훅 → IGSID 로 is_user_follow_business 호출 →
            true  → reward DM 발송 (PASSED)
            false → 재안내 메시지 + 버튼 재첨부 (PENDING 유지)
        gate_trigger_keywords 는 postback 미수신·구버전 클라이언트용 fallback 으로 유지.
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
        STORY_REPLY = "story_reply", "특정 스토리 답장"

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

    # Follow-gate (v3.8 — is_user_follow_business silent verify)
    follow_gate_enabled = models.BooleanField(
        default=False,
        verbose_name="Follow-gate 사용",
        help_text=(
            "활성화 시 opening DM 에 버튼이 추가되고, 사용자가 버튼 클릭 시 "
            "reward_message_template 을 발송한다. 버튼 클릭 시 팔로우 검증 여부는 "
            "gate_verify_follow 로 제어한다 (true=검증 후 발송 / false=즉시 발송)."
        ),
    )
    gate_verify_follow = models.BooleanField(
        default=True,
        verbose_name="팔로우 여부 검증",
        help_text=(
            "follow_gate_enabled=true 일 때만 의미 있음. "
            "true(기본): 버튼 클릭 시 IG Profile API(is_user_follow_business)로 "
            "팔로우 여부를 검증한 뒤 통과 시에만 reward 발송 (기존 follow-gate 동작). "
            "false: 검증을 건너뛰고 버튼 클릭 즉시 reward 를 발송 "
            "(button-only 모드 — 팔로우 확인 없음). "
            "follow_gate_enabled=false 면 이 값과 무관하게 게이트 미사용."
        ),
    )
    follow_gate_prompt = models.TextField(
        blank=True,
        default="",
        verbose_name="Follow-gate 안내 문구",
        help_text=(
            "Opening DM 본문에 들어갈 follow 요청 문구. "
            "예: '댓글 남겨주셔서 감사해요! 팔로우도 하셨나요? 버튼을 눌러주세요!'"
        ),
    )
    follow_gate_button_label = models.CharField(
        max_length=20,
        blank=True,
        default="팔로우했어요",
        verbose_name="Follow-gate 버튼 텍스트",
        help_text="quick_reply 버튼 라벨 (Meta 한도 20자). 비우면 '팔로우했어요' 사용.",
    )
    follow_gate_retry_message = models.TextField(
        blank=True,
        default="",
        verbose_name="Follow 미확인 시 재안내 메시지",
        help_text=(
            "팔로우 체크가 false 로 돌아왔을 때 발송할 안내 문구. 비우면 기본 문구 사용.\n"
            "예: '앗! 팔로우 확인이 안됐어요 😣 프로필에서 팔로우 후 다시 버튼을 눌러주세요!'"
        ),
    )
    reward_message_template = models.TextField(
        blank=True,
        default="",
        verbose_name="Reward DM 템플릿",
        help_text="follow_gate 통과 후 발송할 본 DM (링크 등 포함)",
    )

    # 링크 버튼 (web_url) — 발송되는 DM 카드에 "라벨 달린 링크 버튼"으로 첨부된다.
    # - 단순 DM(STANDALONE): opening/단발 DM 에 첨부
    # - follow-gate(버튼클릭 즉시 / 팔로우 검증 후): reward DM 에 첨부
    # URL 본문에 직접 박는 대신 Meta generic-template 의 web_url 버튼으로 보내 깔끔하고
    # 정책상 안전(첫 DM 텍스트 URL 스팸 판정 회피). 비우면 버튼 미첨부.
    link_button_url = models.URLField(
        max_length=2048,
        blank=True,
        default="",
        verbose_name="링크 버튼 URL",
        help_text="DM 카드에 붙일 링크 버튼이 여는 URL (http/https). 비우면 버튼 없음.",
    )
    link_button_label = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name="링크 버튼 라벨",
        help_text="링크 버튼 글자 (Meta 한도 20자). link_button_url 있고 비우면 '자세히 보기'.",
    )

    gate_trigger_keywords = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Gate 통과 키워드 (fallback)",
        help_text=(
            "postback 을 못 받는 구버전 클라이언트 fallback. "
            "이 키워드로 답장 시 gate 통과로 간주. 예: ['GO', 'YES', '네']"
        ),
    )

    # 발송 옵션
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, verbose_name="상태"
    )

    # 발송 제한
    max_sends_per_hour = models.IntegerField(
        default=200,
        verbose_name="시간당 최대 발송 수 (deprecated)",
        help_text=(
            "(deprecated v4.3 — 더 이상 강제되지 않음) 발송 페이싱은 dm_pacer 가 계정 단위 "
            "자동 조절로 대체했다. API 하위호환을 위해 필드만 유지하며 값은 무시된다."
        ),
    )

    # 통계
    total_sent = models.IntegerField(default=0, verbose_name="총 발송 수")
    total_failed = models.IntegerField(default=0, verbose_name="총 실패 수")
    total_unconfirmed = models.IntegerField(
        default=0,
        verbose_name="총 도착미확인 수",
        help_text=(
            "FAILED_NO_TRACE (200 접수됐으나 35분 내 도착 미확인). "
            "'실패'가 아니라 '미확인' 이므로 total_failed / success_rate 와 분리 집계."
        ),
    )

    # ===== 예약 발송 (DM scheduling — 활성 기간 한정 + 자동 종료) =====
    # scheduled_* 는 "계획"(이 기간에만 발송)이고, started_at/ended_at 은 "실제 기록"이다.
    # 둘 다 비우면 기존처럼 status 만으로 수동 운영(always_on).
    scheduled_start_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="예약 시작일시",
        help_text=(
            "이 시각 이후부터 발송 시작 (status=active 여도 이 시각 전에는 발송하지 않음). "
            "비우면 즉시 시작. ISO8601 타임존 포함 권장 (예: 2026-07-01T09:00:00+09:00)."
        ),
    )
    scheduled_end_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="예약 종료일시",
        help_text=(
            "이 시각 이후 캠페인 자동 종료(status=completed, 발송 중단). "
            "비우면 수동 종료 전까지 무기한. scheduled_start_at 이 있으면 그보다 미래여야 함."
        ),
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일시")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="시작일시")
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="종료일시")

    def __str__(self):
        target = self.media_id or self.trigger_type
        return f"{self.name} ({target})"

    def is_active(self) -> bool:
        """캠페인이 활성 상태인지 확인 (status 만 본다 — 예약 창은 is_runnable_now 참고)"""
        return self.status == self.Status.ACTIVE

    # 특정 게시물/스토리 한 개를 "점유"하는 트리거 (= 같은 media_id 에 활성 캠페인 1개 제한 대상).
    # any_media 는 계정 전체를 대상으로 하므로 특정 게시물을 점유하지 않고,
    # next_media 는 attach 전에는 media_id 가 비어 있어 점유하지 않는다(attach 되면
    # specific_media 로 전환되며 그때부터 점유 대상).
    MEDIA_BOUND_TRIGGERS = (TriggerType.SPECIFIC_MEDIA, TriggerType.STORY_REPLY)

    def occupies_media_slot(self) -> bool:
        """이 캠페인이 특정 게시물(media_id) 한 개를 점유하는지 여부."""
        return self.trigger_type in self.MEDIA_BOUND_TRIGGERS and bool(
            (self.media_id or "").strip()
        )

    @classmethod
    def find_active_conflict(
        cls,
        *,
        ig_connection_id,
        media_id: str,
        trigger_type=None,
        exclude_id=None,
    ) -> "AutoDMCampaign | None":
        """같은 IG 게시물(media_id)에 이미 활성(ACTIVE) 캠페인이 있으면 그 캠페인을 반환.

        한 게시물에 활성 캠페인이 둘 이상 생기는 것을 막기 위한 중복 검사의 단일 진실원천.
        생성/활성화 변경(수정·재개·예약 활성화) 경로에서 호출한다.

        반환:
            충돌하는 활성 캠페인 인스턴스, 없으면 ``None``.

        규칙:
            - ``media_id`` 가 비어 있으면(any_media / 미부착 next_media) 특정 게시물을
              점유하지 않으므로 항상 ``None``.
            - ``trigger_type`` 이 주어졌고 media 점유 트리거가 아니면(any_media 등)
              ``None`` — any_media 캠페인이 stray media_id 를 들고 있어도 오탐하지 않는다.
            - ``exclude_id`` 로 자기 자신은 제외(수정/재개 시 본인과 충돌 처리 방지).
            - 비교는 같은 ``ig_connection`` 범위 안에서만 수행(멀티테넌시/멀티 IG 안전).

        NOTE: next_media 자동 attach(webhook/폴링)는 같은 신규 게시물에 여러 캠페인을
        한 번에 붙이는 게 **의도된 동작**이므로 이 검사를 거치지 않는다(tasks.py).
        이 검사는 사용자가 특정 게시물을 직접 지정/활성화하는 경로에만 적용된다.
        """
        media_id = (media_id or "").strip()
        if not media_id:
            return None
        if trigger_type is not None and trigger_type not in cls.MEDIA_BOUND_TRIGGERS:
            return None
        qs = cls.objects.filter(
            ig_connection_id=ig_connection_id,
            media_id=media_id,
            status=cls.Status.ACTIVE,
        )
        if exclude_id is not None:
            qs = qs.exclude(id=exclude_id)
        return qs.first()

    def copy(self, new_name: str | None = None) -> "AutoDMCampaign":
        """이 캠페인을 비활성(INACTIVE) 복사본으로 복제해 저장 후 반환.

        설정 필드는 전부 복사(트리거·키워드·메시지·공개답글·Follow-gate·예약 기간 포함),
        통계(total_sent/total_failed)·실행기록(started_at/ended_at)·타임스탬프는 초기화한다.
        SentDMLog 등 자식 로그는 복사하지 않는다.

        _meta.fields 인트로스펙션 + EXCLUDE denylist 방식이라, 향후 설정 필드가 추가돼도
        자동으로 복사된다(통계/타임스탬프만 명시적으로 제외).
        """
        EXCLUDE = {
            "id",
            "name",
            "status",
            "total_sent",
            "total_failed",
            "total_unconfirmed",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
        }
        data = {}
        for f in self._meta.fields:
            if f.name in EXCLUDE:
                continue
            # FK 는 *_id(attname)로 복사 (ig_connection → ig_connection_id)
            data[f.attname] = getattr(self, f.attname)
        return AutoDMCampaign.objects.create(
            name=(new_name or f"{self.name} 복사")[:255],
            status=self.Status.INACTIVE,
            **data,
        )

    # ===== 예약 발송 윈도우 =====

    @staticmethod
    def schedule_window_q(now=None):
        """현재 예약 창(window) 안에 있는 캠페인을 고르는 Q 필터.

        scheduled_start_at 이 없거나 이미 지났고(시작됨),
        scheduled_end_at 이 없거나 아직 안 지난(미종료) 캠페인.
        발송 경로에서 status=ACTIVE 필터와 함께 ``.filter()`` 에 적용한다.
        """
        from django.db.models import Q

        if now is None:
            now = timezone.now()
        started = Q(scheduled_start_at__isnull=True) | Q(scheduled_start_at__lte=now)
        not_ended = Q(scheduled_end_at__isnull=True) | Q(scheduled_end_at__gt=now)
        return started & not_ended

    def is_within_schedule(self, now=None) -> bool:
        """현재 시각이 이 캠페인의 예약 창 안에 있는지 (status 무관)."""
        if now is None:
            now = timezone.now()
        if self.scheduled_start_at and now < self.scheduled_start_at:
            return False
        if self.scheduled_end_at and now >= self.scheduled_end_at:
            return False
        return True

    def is_runnable_now(self, now=None) -> bool:
        """지금 실제로 DM 을 발송할 수 있는지 = status=ACTIVE 이고 예약 창 안."""
        return self.is_active() and self.is_within_schedule(now)

    def schedule_state(self, now=None) -> str:
        """예약 창 기준 UX 상태 (status 와 별개로 프론트 배지용).

        - "always_on": 시작/종료 예약 둘 다 없음 (수동 운영)
        - "scheduled": 시작 예약이 아직 안 됨 (대기 중)
        - "running":   예약 창 진행 중
        - "ended":     종료 예약 시각 경과 (자동 종료 대상)
        """
        if now is None:
            now = timezone.now()
        if self.scheduled_end_at and now >= self.scheduled_end_at:
            return "ended"
        if self.scheduled_start_at and now < self.scheduled_start_at:
            return "scheduled"
        if not self.scheduled_start_at and not self.scheduled_end_at:
            return "always_on"
        return "running"

    def can_send_more(self) -> bool:
        """(deprecated v4.3) 시간당 한도(max_sends_per_hour)는 더 이상 강제되지 않는다.

        발송 페이싱은 dm_pacer(계정 단위 지터 슬롯)가 대체했다. 시리얼라이저의
        can_send 표시 호환을 위해 '캠페인 활성 여부'만 반환한다.
        """
        return self.is_active()

    def increment_sent(self):
        """발송 카운트 증가 (원자적 — 고동시성에서 lost update 방지)."""
        from django.db.models import F

        type(self).objects.filter(pk=self.pk).update(
            total_sent=F("total_sent") + 1, updated_at=timezone.now()
        )

    def increment_failed(self):
        """실패 카운트 증가 (원자적)."""
        from django.db.models import F

        type(self).objects.filter(pk=self.pk).update(
            total_failed=F("total_failed") + 1, updated_at=timezone.now()
        )

    def increment_unconfirmed(self):
        """도착미확인(FAILED_NO_TRACE) 카운트 증가 (원자적).

        200 접수됐으나 echo/Conversations API 로 도착을 확인 못 한 건.
        '실패'가 아니라 '미확인' 이므로 total_failed 와 분리해 success_rate 를 깎지 않는다.
        """
        from django.db.models import F

        type(self).objects.filter(pk=self.pk).update(
            total_unconfirmed=F("total_unconfirmed") + 1, updated_at=timezone.now()
        )

    # ===== 신규 로직 헬퍼 =====

    def get_opening_message(self) -> str:
        """Opening DM 본문.

        follow_gate_enabled 인 경우 follow_gate_prompt 가 우선 사용된다
        (= 화면상 "팔로우도 하셨나요?" 안내). 비어 있으면 기본 템플릿으로 fallback.
        gate 미사용 캠페인은 기존 동작 (opening_message_template / message_template) 유지.
        """
        if self.follow_gate_enabled:
            prompt = (self.follow_gate_prompt or "").strip()
            if prompt:
                return prompt
        return self.opening_message_template or self.message_template or ""

    # Follow-gate 기본 문구 (필드가 비어있을 때 fallback)
    DEFAULT_FOLLOW_GATE_PROMPT = "댓글 남겨주셔서 감사해요!\n팔로우도 하셨나요? 버튼을 눌러주세요!"
    DEFAULT_FOLLOW_GATE_BUTTON = "팔로우했어요"
    DEFAULT_FOLLOW_GATE_RETRY = (
        "앗! 팔로우 확인이 안됐어요. 😣\n"
        "프로필로 이동해 팔로우를 해주시고 다시 버튼을 눌러주세요!"
    )

    def get_follow_gate_button_label(self) -> str:
        return (self.follow_gate_button_label or "").strip() or self.DEFAULT_FOLLOW_GATE_BUTTON

    def get_follow_gate_retry_message(self) -> str:
        """Follow 미확인 시 재안내 문구. 캠페인 설정값 우선, 비면 기본 문구."""
        return (self.follow_gate_retry_message or "").strip() or self.DEFAULT_FOLLOW_GATE_RETRY

    # 링크 버튼 (web_url) 기본 라벨 — URL 만 있고 라벨이 비었을 때 fallback
    DEFAULT_LINK_BUTTON_LABEL = "자세히 보기"

    def get_link_buttons(self) -> list | None:
        """발송 DM 에 첨부할 web_url 링크 버튼 1개를 Meta 버튼 형태로 반환.

        link_button_url 이 설정돼 있을 때만 ``[{"type":"web_url","title","url"}]`` 반환.
        없으면 None (버튼 미첨부). 단순 DM / reward DM 발송 시 send 태스크가 사용한다.
        """
        url = (self.link_button_url or "").strip()
        if not url:
            return None
        label = (self.link_button_label or "").strip() or self.DEFAULT_LINK_BUTTON_LABEL
        return [{"type": "web_url", "title": label[:20], "url": url}]

    def pick_public_reply_template(self) -> str:
        """공개 답글 템플릿 1개 선택.

        public_reply_templates 리스트에서 무작위 선택. 비어있으면 legacy
        public_reply_template 사용.
        """
        import random

        candidates = [
            t.strip() for t in (self.public_reply_templates or []) if t and str(t).strip()
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
        """
        이 캠페인이 해당 media_id 트리거(=댓글 webhook)에 매칭되는지.

        STORY_REPLY 캠페인은 댓글 webhook 으로 발화되지 않으므로 항상 False.
        Story 매칭은 matches_story() 사용.
        """
        if self.trigger_type == self.TriggerType.STORY_REPLY:
            return False
        if self.trigger_type == self.TriggerType.ANY_MEDIA:
            return True
        # specific_media (또는 attach 된 next_media) — media_id 일치 필요
        return bool(self.media_id) and self.media_id == media_id

    def matches_story(self, story_id: str) -> bool:
        """
        이 캠페인이 messages webhook 의 reply_to.story.id 에 매칭되는지.

        STORY_REPLY 트리거에서만 True. media_id 에 Story ID 가 저장돼 있어야 함.
        """
        if self.trigger_type != self.TriggerType.STORY_REPLY:
            return False
        return bool(self.media_id) and self.media_id == story_id

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

    # 되살릴 수 있는 종결 상태 (P1 — 무손실 하드닝):
    # 토큰 만료(일시적, 재연동/갱신으로 해소)·스케줄 스킵(창이 다시 열림)은 '일시적 원인'이라
    # 같은 row 를 QUEUED 로 되돌려 재발송할 수 있다(같은 idempotency_key 재사용 → 중복 INSERT 불가).
    # 제외: FAILED_WINDOW(정당한 메시징 윈도우 만료), FAILED_PARAM(잘못된 데이터),
    #       FAILED_NO_TRACE(이미 ACCEPTED=발송됨, 되살리면 중복 발송).
    REVIVABLE_STATUSES = (
        Status.FAILED_TOKEN,
        Status.SKIPPED,
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
            # P2a: echo fallback 매칭(recipient_user_id + status, 최근 accepted 순) seqscan 방지.
            # 0019 마이그레이션에서 CREATE INDEX CONCURRENTLY 로 생성.
            models.Index(
                fields=["recipient_user_id", "status", "-accepted_at"],
                name="dm_log_recipient_status_idx",
            ),
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
    echo_mid = models.CharField(max_length=255, blank=True, default="", verbose_name="Echo MID")

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

    @classmethod
    def create_idempotent(
        cls, *, idempotency_key: str, **fields
    ) -> "tuple[SentDMLog | None, bool]":
        """전역 멱등 INSERT — ``SentDMLog.idempotency_key`` 전역 UNIQUE 로 "정확히 한 번" 보증.

        반환: ``(log, created)``. ``created=True`` 면 신규 발송 대상, ``False`` 면 중복
        (이미 같은 ``idempotency_key`` 가 발송됨 → ``log`` 은 기존 row, 아카이브됐으면 None).

        ★ 무손실 하드닝: ``idempotency_key`` UNIQUE 충돌만 '중복'으로 본다. INSERT 가 *키 충돌이 아닌*
        IntegrityError(NOT NULL·FK·CHECK 등)로 실패하면 — 같은 키 row 가 없으므로 — '중복'으로 둔갑시키지
        않고 **그대로 전파**한다(트랜잭션째 롤백 → 부분 row 없음). 발송 누락이 silent drop 대신 에러/재시도로
        가시화된다.

        (SentDMLog 는 배치 아카이브로 운영 — 파티셔닝 안 함 — 이라 전역 UNIQUE 가 그대로 단일 보증.
        DMDedupKey 레저는 §15.8 에서 redundant 로 제거됨. 향후 파티셔닝 시 레저 재도입 검토.)

        호출부는 기존 ``try/except IntegrityError`` 인라인을 대체한다(=동작 동일, 보증만 강화).
        """
        try:
            with transaction.atomic():
                log = cls.objects.create(idempotency_key=idempotency_key, **fields)
            return log, True
        except IntegrityError:
            existing = cls.objects.filter(idempotency_key=idempotency_key).first()
            if existing is None:
                # 같은 키 row 가 없다 = UNIQUE 충돌이 아닌 다른 제약 위반 → '중복' 아님 → 전파.
                raise
            return existing, False

    # ===== 상태 전이 헬퍼 =====

    def is_terminal(self) -> bool:
        """더 이상 워커가 변경하지 않는 종결 상태인지"""
        return self.status in self.TERMINAL_STATUSES

    def is_delivered(self) -> bool:
        """사용자에게 '도착함'이라 보고 가능한지"""
        return self.status in self.DELIVERED_STATUSES

    def messaging_window(self):
        """이 로그가 발송 가능한 메시징 윈도우 (comment Private Reply 7일 / user_id DM 24h).

        rate-limit/한도로 오래 defer 되거나, 되살림(revive) 시점이 윈도우 밖이면
        어차피 Meta 가 거부하므로 이 경계를 단일 판단 기준으로 쓴다.
        """
        from datetime import timedelta

        return timedelta(days=7) if self.comment_id else timedelta(hours=24)

    def revive(self, reason: str = "", enqueue: bool = True) -> bool:
        """실패 종결 로그를 '제자리에서' QUEUED 로 되살림 (P1 — 무손실 하드닝).

        같은 row·같은 idempotency_key 를 재사용하므로 중복 INSERT(duplicate) 문제가 원천 차단된다.
        대상은 REVIVABLE_STATUSES(FAILED_TOKEN/SKIPPED) 뿐이며, 메시징 윈도우가 이미
        지났으면(어차피 Meta 거부) 되살리지 않는다.

        Args:
            reason: verification_log 에 남길 사유.
            enqueue: True 면 send_dm_task 를 즉시 재투입.
        Returns:
            되살렸으면 True, 대상 아님/윈도우 만료면 False.
        """
        if self.status not in self.REVIVABLE_STATUSES:
            return False
        if timezone.now() - self.created_at >= self.messaging_window():
            return False

        old_status = self.status
        self.status = self.Status.QUEUED
        self.next_retry_at = None
        self.save(update_fields=["status", "next_retry_at"])
        self.append_verification_log(
            {"path": "revive", "reason": reason or "manual", "from": old_status}
        )
        if enqueue:
            from .tasks import send_dm_task

            send_dm_task.delay(str(self.id))
        return True

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
        self.save(update_fields=["status", "verified_via", "delivered_at", "echo_mid"])

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

    # 자동 숨김 — 스팸 감지 시 Meta API 로 즉시 숨길지 여부.
    # False(기본)면 감지만 DB 에 기록하고 유저가 수동으로 숨김한다(감지→수동 흐름).
    # ⚠️ 계정 전체 검사로 전환되므로 기본 OFF 로 두어 대량 자동 숨김을 방지한다.
    auto_hide_enabled = models.BooleanField(
        default=False,
        verbose_name="자동 숨김",
        help_text="스팸 감지 시 자동으로 댓글을 숨김 처리 (off면 감지 기록만 → 수동 숨김 대기)",
    )

    # LLM 판정 사용 여부 — off면 규칙(키워드/URL)만으로 판정(gemma 롤아웃 kill-switch).
    use_llm = models.BooleanField(
        default=True,
        verbose_name="LLM 판정 사용",
        help_text="off면 gemma LLM 없이 키워드/URL 규칙만으로 스팸 판정",
    )

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
        CLEAN = "clean", "정상"  # 스팸 아님 — 멱등 장부용(짧은 TTL 후 정리), 통계 제외
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
            models.Index(fields=["hidden_at"], name="spam_log_hidden_at_idx"),
        ]
        constraints = [
            # 멱등성: 계정(spam_filter)당 comment_id 는 1행. 동시 중복 웹훅이
            # get_or_create 로 이 제약에 경합 → 최초 1회만 분류/숨김 수행.
            # (SeenComment 의 UNIQUE(ig_connection, comment_id) 선례와 동일)
            models.UniqueConstraint(
                fields=["spam_filter", "comment_id"],
                name="uq_spam_log_filter_comment",
            )
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

    # LLM/하이브리드 판정 결과 (모더레이션 UI·디버그용)
    confidence = models.FloatField(
        null=True, blank=True, verbose_name="스팸 신뢰도", help_text="0.0~1.0 (LLM 판정 시)"
    )
    spam_category = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="스팸 분류",
        help_text="rule/scam/adult/phishing/promo/abuse 등",
    )
    engine = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="판정 엔진",
        help_text="rule / llm / llm_failopen / rule_trivial / rule_only 등",
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


class EventInbox(models.Model):
    """웹훅 이벤트 멱등성 장부 (P2b).

    Meta 는 동일 웹훅을 재전송할 수 있고, 3-tier 전환으로 webhook 동시성이 올라가면
    echo/read 이벤트가 여러 워커에 동시 도달해 같은 SentDMLog row 를 락 없이 UPDATE 하는
    레이스가 발생한다. 이 테이블에 ``event_key`` 를 UNIQUE 로 멱등 INSERT(ON CONFLICT DO NOTHING)
    해서 "최초 1회"만 후속 처리(process_messaging_event)를 enqueue 한다.

    event_key 포맷: ``"{event_type}:{mid}"``  (예: ``"echo:abc123"``, ``"read:abc123"``)
    """

    EVENT_ECHO = "echo"
    EVENT_READ = "read"

    event_key = models.CharField(max_length=255, unique=True, verbose_name="이벤트 키")
    event_type = models.CharField(max_length=32, verbose_name="이벤트 타입")
    payload = models.JSONField(default=dict, blank=True, verbose_name="이벤트 페이로드")
    received_at = models.DateTimeField(auto_now_add=True, verbose_name="수신 시각")
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name="처리 완료 시각")

    class Meta:
        db_table = "webhook_event_inbox"
        verbose_name = "Webhook Event Inbox"
        verbose_name_plural = "Webhook Event Inbox"
        indexes = [
            models.Index(fields=["processed_at"], name="webhook_eve_process_idx"),
            models.Index(fields=["received_at"], name="webhook_eve_receive_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_key} ({'done' if self.processed_at else 'pending'})"


class SeenComment(models.Model):
    """댓글 관측 장부 (웹훅 누락 보정용) — 본문(text) 저장 안 함.

    목적:
        Instagram ``comments`` 웹훅은 전달이 보장되지 않아(36h 후 드롭) 트리거 댓글이
        유실될 수 있다. 시간당 폴링이 댓글 edge 를 재조회해 누락분을 찾는데, 이때
        "이미 본 댓글"을 빠르게 건너뛰고(앵커) 페이지네이션을 끊기 위한 최소 장부다.

    중요 — 정확성의 하드 보증이 아니다:
        DM "정확히 한 번"의 하드 보증은 ``SentDMLog.idempotency_key`` UNIQUE 제약이다.
        이 장부는 폴링 앵커/관측을 위한 최적화 레이어이며, TTL(``expires_at``)로 만료돼
        ``integrations.cleanup_comment_ledger`` 가 주기 삭제한다. 장부가 비어 있어도
        idempotency_key 가 중복 발송을 막으므로 정확성에는 영향이 없다.

    생성 경로:
        - 웹훅: ``process_comment_and_send_dm`` 가 수신 즉시 기록(source=webhook).
          웹훅 payload(value.id / value.media.id)만으로 생성 가능 — 별도 Meta API 호출 불필요.
        - 폴링: ``poll_missed_comments`` 가 댓글 edge 응답으로 기록(source=poll).
        UNIQUE(ig_connection, comment_id) 라서 ``get_or_create`` 의 created 플래그가 곧 앵커 판정.
    """

    class Source(models.TextChoices):
        WEBHOOK = "webhook", "웹훅"
        POLL = "poll", "폴링"

    ig_connection = models.ForeignKey(
        IGAccountConnection,
        on_delete=models.CASCADE,
        related_name="seen_comments",
        verbose_name="Instagram Connection",
    )
    comment_id = models.CharField(max_length=255, verbose_name="댓글 ID")
    media_id = models.CharField(
        max_length=255,
        db_index=True,
        blank=True,
        default="",
        verbose_name="Media ID",
    )
    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.WEBHOOK,
        verbose_name="관측 경로",
    )
    triggered = models.BooleanField(
        default=False,
        verbose_name="DM 트리거됨",
        help_text="매칭되어 DM enqueue 했는지 (관측/디버그용)",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="최초 관측 시각")
    expires_at = models.DateTimeField(db_index=True, verbose_name="만료 시각(TTL)")

    class Meta:
        db_table = "seen_comments"
        verbose_name = "Seen Comment"
        verbose_name_plural = "Seen Comments"
        constraints = [
            models.UniqueConstraint(
                fields=["ig_connection", "comment_id"],
                name="uq_seen_comment_conn_comment",
            ),
        ]
        indexes = [
            models.Index(fields=["ig_connection", "media_id"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        return f"SeenComment({self.comment_id} via {self.source})"


class DMAccountBlock(models.Model):
    """IG 계정별 Action Block(Meta code 368 등) 쿨다운의 **내구 저장소**.

    rate_governor 의 Action Block 상태는 원래 Redis(`dm:ab:cooldown/level:*`)에만 있어
    Redis flush / DR failover 시 소실 → 차단됐던 계정이 재개되어 Meta 차단을 '연장' 시키는
    위험이 있었다. 이 테이블을 진실로 두고 Redis 는 fast-path 캐시로 쓴다:

    - ``trip_action_block`` 가 캐시 + 이 행을 함께 upsert (듀얼라이트)
    - ``action_block_cooldown_remaining`` 는 캐시 miss 시 이 행으로 폴백 + 캐시 재프라임
    - ``rate_governor.rehydrate_from_db`` 가 Redis 손실 후 이 행들로 캐시 재시드

    키는 IG 계정 단위(``external_account_id``) — Meta 한도가 계정당이므로(거버너 키와 동일).
    설계: DR_IMPLEMENTATION_PLAN.md §7.2.
    """

    external_account_id = models.CharField(
        max_length=255, unique=True, db_index=True, verbose_name="Instagram Account ID"
    )
    cooldown_until = models.DateTimeField(
        null=True, blank=True, verbose_name="쿨다운 종료 시각(이 시각까지 발송 차단)"
    )
    level = models.IntegerField(default=0, verbose_name="에스컬레이션 레벨(반복 차단 횟수)")
    last_tripped_at = models.DateTimeField(null=True, blank=True, verbose_name="마지막 트립 시각")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dm_account_block"
        verbose_name = "DM Account Block"
        verbose_name_plural = "DM Account Blocks"
        indexes = [models.Index(fields=["cooldown_until"], name="dm_acct_block_cooldown_idx")]

    def __str__(self) -> str:
        return (
            f"DMAccountBlock({self.external_account_id} until={self.cooldown_until} L{self.level})"
        )
