"""
Instagram integration serializers
"""

from rest_framework import serializers

from .models import AutoDMCampaign, IGAccountConnection, SentDMLog, SpamCommentLog, SpamFilterConfig


class IGAccountConnectionSerializer(serializers.ModelSerializer):
    """Serializer for Instagram Account Connection"""

    workspace_id = serializers.UUIDField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = IGAccountConnection
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "external_account_id",
            "username",
            "account_type",
            "token_expires_at",
            "scopes",
            "status",
            "last_verified_at",
            "error_message",
            "is_expired",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "external_account_id",
            "username",
            "account_type",
            "token_expires_at",
            "scopes",
            "status",
            "last_verified_at",
            "error_message",
            "created_at",
            "updated_at",
        ]

    def get_is_expired(self, obj):
        """Check if token is expired"""
        return obj.is_token_expired()


class ConnectionStartResponseSerializer(serializers.Serializer):
    """Response for connection start endpoint"""

    authorization_url = serializers.URLField()
    state = serializers.CharField()
    mode = serializers.CharField()


class DisconnectResponseSerializer(serializers.Serializer):
    """Instagram 연동 해제 응답 (v3.8)"""

    success = serializers.BooleanField(help_text="요청 처리 성공 여부")
    ig_connection_id = serializers.UUIDField(help_text="해제된 IG 연동 ID")
    username = serializers.CharField(help_text="해제된 IG 계정 username")
    status = serializers.CharField(help_text="처리 후 status (revoked)")
    campaigns_paused = serializers.IntegerField(help_text="일시정지된 활성 캠페인 수")
    logs_cancelled = serializers.IntegerField(
        help_text="진행 중이던(in-flight) DM 로그 중 SKIPPED 처리된 수"
    )
    webhook_unsubscribed = serializers.BooleanField(
        help_text="Meta webhook 구독 해제 성공 여부 (best-effort)"
    )
    webhook_error = serializers.CharField(
        allow_null=True, allow_blank=True,
        help_text="webhook 해제 실패 시 에러 메시지 (실패해도 disconnect 는 진행됨)"
    )
    reason = serializers.CharField(help_text="해제 이유 (user_requested / meta_deauth / token_expired 등)")


class ConnectionCallbackResponseSerializer(serializers.Serializer):
    """Response for connection callback endpoint"""

    success = serializers.BooleanField()
    message = serializers.CharField()
    connection = IGAccountConnectionSerializer(required=False)


class AutoDMCampaignSerializer(serializers.ModelSerializer):
    """Serializer for Auto DM Campaign (v3.3 — 트리거/키워드/공개답글/Follow-gate 포함)"""

    ig_connection_id = serializers.UUIDField(source="ig_connection.id", read_only=True)
    ig_username = serializers.CharField(source="ig_connection.username", read_only=True)
    is_active = serializers.SerializerMethodField()
    can_send = serializers.SerializerMethodField()

    class Meta:
        model = AutoDMCampaign
        fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            # 트리거
            "trigger_type",
            "media_id",
            "media_url",
            # 키워드
            "keyword_filter",
            "keyword_mode",
            # 메타
            "name",
            "description",
            # 메시지
            "message_template",  # legacy
            "opening_message_template",
            # 공개 답글 (v3.5)
            "public_reply_enabled",
            "public_reply_template",  # legacy 단일
            "public_reply_templates",  # 신규 리스트
            "public_reply_batch_size",
            "public_reply_batch_pause_seconds",
            # Follow-gate (deprecated)
            "follow_gate_enabled",
            "follow_gate_prompt",
            "reward_message_template",
            "gate_trigger_keywords",
            # 운영
            "status",
            "max_sends_per_hour",
            "total_sent",
            "total_failed",
            "is_active",
            "can_send",
            # timestamps
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
        ]
        read_only_fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            "total_sent",
            "total_failed",
            "is_active",
            "can_send",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
        ]

    def get_is_active(self, obj) -> bool:
        return obj.is_active()

    def get_can_send(self, obj) -> bool:
        return obj.can_send_more()


class AutoDMCampaignCreateSerializer(serializers.Serializer):
    """Auto DM Campaign 생성 (v3.3)"""

    # 트리거
    trigger_type = serializers.ChoiceField(
        choices=AutoDMCampaign.TriggerType.choices,
        default=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        help_text=(
            "specific_media: 특정 게시물 (media_id 필수) / "
            "any_media: 모든 게시물 / "
            "next_media: 캠페인 활성 후 첫 신규 게시물 1개에만 자동 적용. "
            "전체 안내는 GET /api/v1/integrations/auto-dm-campaigns/guide/"
        ),
    )
    media_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="trigger_type=specific_media 일 때 필수. any/next 일 때는 빈 문자열",
    )
    media_url = serializers.URLField(
        required=False, allow_blank=True, allow_null=True, default=None,
        help_text="게시물 URL (참고용)",
    )

    # 키워드
    keyword_filter = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=128),
        required=False,
        default=list,
        help_text="비어있으면 모든 댓글 매칭. 예: ['info', '가격']",
    )
    keyword_mode = serializers.ChoiceField(
        choices=AutoDMCampaign.KeywordMode.choices,
        default=AutoDMCampaign.KeywordMode.ANY,
    )

    # 메타
    name = serializers.CharField(required=True, max_length=255, help_text="캠페인 이름")
    description = serializers.CharField(required=False, allow_blank=True, default="")

    # 메시지 (둘 중 하나는 필수)
    opening_message_template = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="첫 인사 DM 본문 (Private Reply via comment_id)",
    )
    message_template = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="legacy 별칭 — opening_message_template 미사용 시 이 값 사용",
    )

    # 공개 답글 (v3.5)
    public_reply_enabled = serializers.BooleanField(default=False)
    public_reply_template = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="[deprecated] 단일 템플릿. 새 캠페인은 public_reply_templates 사용 권장.",
    )
    public_reply_templates = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=2200),
        required=False,
        default=list,
        help_text=(
            "공개 답글 템플릿 목록 (1개 이상). 매 답글마다 무작위로 1개 선택. "
            "Instagram 봇 검사 회피를 위해 최소 3개 이상 다양한 문구 권장."
        ),
    )
    public_reply_batch_size = serializers.IntegerField(
        default=10, min_value=1, max_value=200,
        help_text="이 개수만큼 답글 게시 후 쿨다운 적용 (기본 10)",
    )
    public_reply_batch_pause_seconds = serializers.IntegerField(
        default=300, min_value=30, max_value=3600,
        help_text="배치 도달 후 다음 답글까지 대기 시간 (초, 기본 300)",
    )

    # Follow-gate (deprecated — Meta 한계로 silent 검증 불가)
    follow_gate_enabled = serializers.BooleanField(default=False)
    follow_gate_prompt = serializers.CharField(required=False, allow_blank=True, default="")
    reward_message_template = serializers.CharField(required=False, allow_blank=True, default="")
    gate_trigger_keywords = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        default=list,
        help_text="[deprecated] Meta API 가 silent 검증을 지원하지 않아 무시됨.",
    )

    # 운영
    max_sends_per_hour = serializers.IntegerField(
        default=200, min_value=1, max_value=500
    )

    def validate(self, attrs):
        trigger = attrs.get("trigger_type", AutoDMCampaign.TriggerType.SPECIFIC_MEDIA)
        media_id = (attrs.get("media_id") or "").strip()
        if trigger == AutoDMCampaign.TriggerType.SPECIFIC_MEDIA and not media_id:
            raise serializers.ValidationError(
                {"media_id": "trigger_type=specific_media 일 때 media_id 는 필수입니다."}
            )
        # v3.7: STORY_REPLY 트리거는 media_id 에 Story ID 가 필수
        if trigger == AutoDMCampaign.TriggerType.STORY_REPLY and not media_id:
            raise serializers.ValidationError(
                {"media_id": "trigger_type=story_reply 일 때 media_id 에 대상 Story ID 가 필수입니다."}
            )
        # Story 답장 캠페인은 공개 답글 불가능 (Story 에는 댓글 자체가 없음)
        if (
            trigger == AutoDMCampaign.TriggerType.STORY_REPLY
            and attrs.get("public_reply_enabled")
        ):
            raise serializers.ValidationError(
                {"public_reply_enabled": "Story 답장 캠페인은 공개 답글을 사용할 수 없습니다 (Story 에 댓글 기능이 없음)."}
            )
        opening = (attrs.get("opening_message_template") or "").strip()
        legacy_msg = (attrs.get("message_template") or "").strip()
        if not opening and not legacy_msg:
            raise serializers.ValidationError(
                {"opening_message_template": "opening_message_template 또는 message_template 중 하나는 필수입니다."}
            )
        # Follow-gate 는 deprecated — 검증만 약하게 유지 (실제 동작 안 함)
        if attrs.get("follow_gate_enabled"):
            if not (attrs.get("reward_message_template") or "").strip():
                raise serializers.ValidationError(
                    {"reward_message_template": "Follow-gate 사용 시 reward_message_template 필수 (단 현재 비활성화 상태)"}
                )
        # public_reply: templates(list) 또는 legacy template 중 하나는 비어있지 않아야 함
        if attrs.get("public_reply_enabled"):
            tmpls = [t for t in (attrs.get("public_reply_templates") or []) if t and str(t).strip()]
            legacy = (attrs.get("public_reply_template") or "").strip()
            if not tmpls and not legacy:
                raise serializers.ValidationError(
                    {
                        "public_reply_templates": (
                            "public_reply_enabled=true 시 "
                            "public_reply_templates (또는 legacy public_reply_template) 중 "
                            "하나 이상 필수"
                        )
                    }
                )
        return attrs


class AutoDMCampaignUpdateSerializer(serializers.ModelSerializer):
    """Auto DM Campaign 수정 (Swagger/edit form 용 — v3.3)"""

    class Meta:
        model = AutoDMCampaign
        fields = [
            "trigger_type",
            "media_url",
            "keyword_filter",
            "keyword_mode",
            "name",
            "description",
            "message_template",
            "opening_message_template",
            # 공개 답글 (v3.5)
            "public_reply_enabled",
            "public_reply_template",
            "public_reply_templates",
            "public_reply_batch_size",
            "public_reply_batch_pause_seconds",
            # Follow-gate (deprecated)
            "follow_gate_enabled",
            "follow_gate_prompt",
            "reward_message_template",
            "gate_trigger_keywords",
            "max_sends_per_hour",
            "status",
        ]
        extra_kwargs = {
            "media_url": {"required": False, "allow_null": True, "allow_blank": True},
            "description": {"required": False, "allow_blank": True},
            "max_sends_per_hour": {"required": False},
            "status": {"required": False},
            "trigger_type": {"required": False},
            "keyword_filter": {"required": False},
            "keyword_mode": {"required": False},
            "message_template": {"required": False, "allow_blank": True},
            "opening_message_template": {"required": False, "allow_blank": True},
            "public_reply_enabled": {"required": False},
            "public_reply_template": {"required": False, "allow_blank": True},
            "public_reply_templates": {"required": False},
            "public_reply_batch_size": {"required": False},
            "public_reply_batch_pause_seconds": {"required": False},
            "follow_gate_enabled": {"required": False},
            "follow_gate_prompt": {"required": False, "allow_blank": True},
            "reward_message_template": {"required": False, "allow_blank": True},
            "gate_trigger_keywords": {"required": False},
        }


class SentDMLogSerializer(serializers.ModelSerializer):
    """Serializer for Sent DM Log (v3.2 — 99.9% 보증 + 프론트 액션 가이드 포함)"""

    campaign_id = serializers.UUIDField(source="campaign.id", read_only=True)
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    is_delivered = serializers.SerializerMethodField()
    is_terminal = serializers.SerializerMethodField()
    display_status = serializers.SerializerMethodField()
    frontend_action = serializers.SerializerMethodField()

    class Meta:
        model = SentDMLog
        fields = [
            "id",
            "campaign_id",
            "campaign_name",
            "comment_id",
            "comment_text",
            "recipient_user_id",
            "recipient_username",
            "message_sent",
            # 상태
            "status",
            "display_status",
            "is_delivered",
            "is_terminal",
            "verified_via",
            # 식별자
            "idempotency_key",
            "meta_message_id",
            "echo_mid",
            # 에러
            "error_message",
            "error_code",
            "error_subcode",
            # 재시도
            "retry_count",
            "next_retry_at",
            # 단계별 타임스탬프
            "created_at",
            "submitted_at",
            "accepted_at",
            "delivered_at",
            "read_at",
            "sent_at",  # legacy
            # 메타
            "webhook_payload",
            "api_response",
            "verification_log",
            # v3.2 프론트엔드 표시 가이드 (체크리스트 포함)
            "frontend_action",
            # v3.3 캠페인 고도화 — opening/reward 분류 + gate 상태 + 공개 답글
            "dm_kind",
            "gate_status",
            "parent_log",
            "public_reply_id",
            "public_reply_posted_at",
        ]
        read_only_fields = fields  # 모두 읽기 전용

    def get_is_delivered(self, obj) -> bool:
        return obj.is_delivered()

    def get_is_terminal(self, obj) -> bool:
        return obj.is_terminal()

    def get_display_status(self, obj) -> str:
        """프론트 표시용 사용자 친화적 상태"""
        return _STATUS_DISPLAY.get(obj.status, obj.status)

    def get_frontend_action(self, obj) -> dict:
        """v3.2 — 상태별 프론트엔드 표시/체크리스트/CTA 가이드"""
        from .dm_frontend_actions import build_frontend_action

        return build_frontend_action(obj.status)


_STATUS_DISPLAY = {
    "queued": "발송 대기",
    "submitting": "발송 중",
    "accepted": "Meta 접수됨 (도착 확인 중)",
    "delivered": "도착 확인",
    "read": "읽음",
    "pending": "대기중",
    "sent": "발송완료",
    "failed": "발송실패",
    "skipped": "건너뜀",
    "failed_token": "토큰 만료 (재연동 필요)",
    "failed_window": "24시간 메시징 윈도우 만료",
    "failed_param": "파라미터 오류 (댓글 만료 가능)",
    "rate_limited": "Meta 응답 대기 중 (지연)",
    "failed_no_trace": "도착 미확인 (자가 점검 필요)",
    "failed_api": "API 오류(legacy)",
}


class DMVerificationStatsSerializer(serializers.Serializer):
    """DM 발송 통계 응답 (캠페인 단위 — v3.3 — gate/kind 분리 포함)"""

    total = serializers.IntegerField(help_text="전체 로그 수")
    queued = serializers.IntegerField(help_text="큐 대기")
    submitting = serializers.IntegerField(help_text="API 호출 중")
    accepted = serializers.IntegerField(help_text="Meta 접수 (도착 확인 중)")
    delivered = serializers.IntegerField(help_text="도착 확인 완료")
    read = serializers.IntegerField(help_text="읽음 확인")
    rate_limited = serializers.IntegerField(help_text="레이트 리밋/대기 중")
    failed_token = serializers.IntegerField()
    failed_window = serializers.IntegerField()
    failed_param = serializers.IntegerField()
    failed_no_trace = serializers.IntegerField()
    skipped = serializers.IntegerField()
    legacy_sent = serializers.IntegerField(help_text="구 데이터(sent)")
    legacy_failed = serializers.IntegerField(help_text="구 데이터(failed)")
    legacy_failed_api = serializers.IntegerField(help_text="구 데이터(failed_api)")
    delivery_rate = serializers.FloatField(
        help_text="ACCEPTED 진입 건 중 DELIVERED+READ 비율 (0~1)"
    )
    read_rate = serializers.FloatField(
        help_text="DELIVERED 건 중 READ 비율 (0~1)"
    )

    # v3.3 — DM 종류별 분리
    standalone_total = serializers.IntegerField(help_text="단발 DM (gate 미사용) 총 수")
    opening_total = serializers.IntegerField(help_text="Opening DM 총 수")
    opening_delivered = serializers.IntegerField(help_text="Opening DM 도착 확인 수")
    reward_total = serializers.IntegerField(help_text="Reward DM 총 수")
    reward_delivered = serializers.IntegerField(help_text="Reward DM 도착 확인 수")

    # v3.3 — Follow-gate 통과율
    gate_pending = serializers.IntegerField(help_text="Gate 답장 대기")
    gate_passed = serializers.IntegerField(help_text="Gate 통과 (reward 발송됨)")
    gate_expired = serializers.IntegerField(help_text="Gate 답장 24h 만료")
    gate_passthrough_rate = serializers.FloatField(
        help_text="Opening DELIVERED 중 gate 통과 비율 (0~1)"
    )

    # 공개 답글
    public_replies_posted = serializers.IntegerField(help_text="공개 답글 게시 성공 건수")


class DMReverifyResponseSerializer(serializers.Serializer):
    """수동 재검증 응답"""

    log_id = serializers.UUIDField()
    previous_status = serializers.CharField()
    new_status = serializers.CharField()
    verified_via = serializers.CharField(allow_blank=True)
    found_in_meta = serializers.BooleanField()
    detail = serializers.CharField()


class DMLookupResponseSerializer(serializers.Serializer):
    """meta_message_id 또는 idempotency_key로 단건 조회 응답"""

    found = serializers.BooleanField()
    log = SentDMLogSerializer(required=False, allow_null=True)


class SpamFilterConfigSerializer(serializers.ModelSerializer):
    """스팸 필터 설정 Serializer"""

    ig_connection_id = serializers.UUIDField(source="ig_connection.id", read_only=True)
    ig_username = serializers.CharField(source="ig_connection.username", read_only=True)
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = SpamFilterConfig
        fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            "status",
            "spam_keywords",
            "block_urls",
            "total_spam_detected",
            "total_hidden",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            "total_spam_detected",
            "total_hidden",
            "created_at",
            "updated_at",
        ]

    def get_is_active(self, obj):
        """스팸 필터 활성화 여부"""
        return obj.is_active()


class SpamFilterConfigUpdateSerializer(serializers.ModelSerializer):
    """스팸 필터 설정 업데이트 Serializer"""

    class Meta:
        model = SpamFilterConfig
        fields = ["status", "spam_keywords", "block_urls"]

    def validate_spam_keywords(self, value):
        """스팸 키워드 검증"""
        if not isinstance(value, list):
            raise serializers.ValidationError("스팸 키워드는 리스트 형식이어야 합니다.")

        if len(value) > 100:
            raise serializers.ValidationError("스팸 키워드는 최대 100개까지 설정할 수 있습니다.")

        return value


class SpamCommentLogSerializer(serializers.ModelSerializer):
    """스팸 댓글 로그 Serializer"""

    spam_filter_id = serializers.UUIDField(source="spam_filter.id", read_only=True)
    ig_username = serializers.CharField(source="spam_filter.ig_connection.username", read_only=True)

    class Meta:
        model = SpamCommentLog
        fields = [
            "id",
            "spam_filter_id",
            "ig_username",
            "comment_id",
            "comment_text",
            "commenter_user_id",
            "commenter_username",
            "media_id",
            "spam_reasons",
            "status",
            "error_message",
            "webhook_payload",
            "api_response",
            "created_at",
            "hidden_at",
        ]
        read_only_fields = fields  # 모두 읽기 전용
