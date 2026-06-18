"""
Instagram integration serializers
"""

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
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
            "name",
            "account_type",
            "profile_picture_url",
            "profile_picture_synced_at",
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
            "name",
            "account_type",
            "profile_picture_url",
            "profile_picture_synced_at",
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
        allow_null=True,
        allow_blank=True,
        help_text="webhook 해제 실패 시 에러 메시지 (실패해도 disconnect 는 진행됨)",
    )
    reason = serializers.CharField(
        help_text="해제 이유 (user_requested / meta_deauth / token_expired 등)"
    )


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
    # 예약 발송: 창 기준 UX 상태 (always_on / scheduled / running / ended)
    schedule_state = serializers.SerializerMethodField()
    is_runnable_now = serializers.SerializerMethodField()

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
            # Follow-gate (v3.8 — is_user_follow_business silent verify)
            "follow_gate_enabled",
            "gate_verify_follow",
            "follow_gate_prompt",
            "follow_gate_button_label",
            "follow_gate_retry_message",
            "reward_message_template",
            "gate_trigger_keywords",
            # 링크 버튼 (web_url — DM 카드에 라벨 달린 링크 버튼으로 첨부)
            "link_button_url",
            "link_button_label",
            # 운영
            "status",
            "max_sends_per_hour",
            "total_sent",
            "total_failed",
            "is_active",
            "can_send",
            # 예약 발송 (활성 기간 + 자동 종료)
            "scheduled_start_at",
            "scheduled_end_at",
            "schedule_state",
            "is_runnable_now",
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
            "schedule_state",
            "is_runnable_now",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
        ]

    def get_is_active(self, obj) -> bool:
        return obj.is_active()

    def get_can_send(self, obj) -> bool:
        return obj.can_send_more()

    def get_schedule_state(self, obj) -> str:
        return obj.schedule_state()

    def get_is_runnable_now(self, obj) -> bool:
        return obj.is_runnable_now()

    def validate(self, attrs):
        """예약 창 정합성 검증 (PATCH/PUT 경로 — 이 시리얼라이저가 update 에 쓰임).

        부분 수정 시 누락된 값은 기존 인스턴스 값으로 보완해 종료>시작 관계를 확인한다.
        """
        start = attrs.get("scheduled_start_at", getattr(self.instance, "scheduled_start_at", None))
        end = attrs.get("scheduled_end_at", getattr(self.instance, "scheduled_end_at", None))
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"scheduled_end_at": "scheduled_end_at 은 scheduled_start_at 보다 미래여야 합니다."}
            )
        # create/schedule 엔드포인트와 동일 규칙: 과거 종료일은 거부(즉시 종료되는 footgun 방지).
        # 단, 사용자가 이번 요청에서 scheduled_end_at 을 새로 보낸 경우에만 검사한다
        # (기존에 지나간 종료일을 그대로 둔 채 다른 필드만 PATCH 하는 건 막지 않음).
        if "scheduled_end_at" in attrs and end and end <= timezone.now():
            raise serializers.ValidationError(
                {
                    "scheduled_end_at": "scheduled_end_at 은 현재 시각보다 미래여야 합니다 (과거면 즉시 종료됨)."
                }
            )
        return attrs


class AutoDMCampaignListSerializer(AutoDMCampaignSerializer):
    """목록/요약/토글 응답용 — 기본 캠페인 필드 + per-item 통계 enrichment (조회 고도화 v4.1).

    delivery_rate / needs_attention_count / delivered_count / last_sent_at / thumbnail_url 을
    read-only 로 추가해, 프론트가 항목마다 stats 를 따로 호출하던 N+1 을 제거한다.
    목록 쿼리는 annotate_campaign_stats 로 한 번에 집계되며, 단건(pause/resume 등) 은
    compute_campaign_enrichment 가 즉석 집계한다.
    """

    delivered_count = serializers.SerializerMethodField()
    delivery_rate = serializers.SerializerMethodField()
    needs_attention_count = serializers.SerializerMethodField()
    last_sent_at = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    class Meta(AutoDMCampaignSerializer.Meta):
        fields = AutoDMCampaignSerializer.Meta.fields + [
            "delivered_count",
            "delivery_rate",
            "needs_attention_count",
            "last_sent_at",
            "thumbnail_url",
        ]

    def _enrich(self, obj):
        cache = getattr(obj, "_enrichment_cache", None)
        if cache is None:
            from .campaign_stats import compute_campaign_enrichment

            cache = compute_campaign_enrichment(obj)
            obj._enrichment_cache = cache
        return cache

    def get_delivered_count(self, obj) -> int:
        return self._enrich(obj)["delivered_count"]

    def get_delivery_rate(self, obj) -> float:
        return self._enrich(obj)["delivery_rate"]

    def get_needs_attention_count(self, obj) -> int:
        return self._enrich(obj)["needs_attention_count"]

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_sent_at(self, obj):
        return self._enrich(obj)["last_sent_at"]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_thumbnail_url(self, obj):
        return self._enrich(obj)["thumbnail_url"]


class CampaignSummaryCountsSerializer(serializers.Serializer):
    """상태별 캠페인 개수."""

    active = serializers.IntegerField()
    paused = serializers.IntegerField()
    completed = serializers.IntegerField()
    inactive = serializers.IntegerField()
    total = serializers.IntegerField()


class CampaignSummaryUsageSerializer(serializers.Serializer):
    """이번 달 DM 사용량 + 한도 (워크스페이스 단위)."""

    sent_this_month = serializers.IntegerField(help_text="이번 캘린더월에 발송(접수)된 DM 수")
    monthly_free_limit = serializers.IntegerField(
        help_text="플랜 월 DM 한도 (starter 100 / pro 1000 / enterprise -1=무제한)"
    )
    remaining_this_month = serializers.IntegerField(
        allow_null=True, help_text="남은 발송 가능 수. 무제한이면 null"
    )
    is_over_limit = serializers.BooleanField(
        help_text="한도 도달/초과 여부 (무제한이면 항상 false)"
    )
    period_start = serializers.DateTimeField(help_text="집계 기간 시작 (해당 월 1일 00:00 KST)")
    period_end = serializers.DateTimeField(help_text="집계 기간 끝 (다음 달 1일 00:00 KST, 미포함)")


class CampaignSummaryDeliverySerializer(serializers.Serializer):
    """발송 품질 요약 (목록 범위 전체 합산)."""

    total_sent = serializers.IntegerField(help_text="도착/읽음 확인된 DM 합")
    delivery_rate = serializers.FloatField(help_text="ACCEPTED 진입 건 중 도착확인 비율 (0~1)")
    success_rate = serializers.FloatField(
        help_text="전체 로그 중 도착(또는 legacy sent) 비율 (0~1)"
    )
    needs_attention_total = serializers.IntegerField(
        help_text="사용자 조치 필요 로그 합 (토큰만료/윈도우만료/파라미터오류/도착미확인)"
    )


class AutoDMCampaignSummarySerializer(serializers.Serializer):
    """캠페인 요약 응답 (GET .../auto-dm-campaigns/summary/)."""

    counts = CampaignSummaryCountsSerializer()
    usage = CampaignSummaryUsageSerializer()
    delivery = CampaignSummaryDeliverySerializer()
    last_activity_at = serializers.DateTimeField(
        allow_null=True, help_text="가장 최근 DM 로그 생성 시각 (없으면 null)"
    )


class CampaignBulkActionRequestSerializer(serializers.Serializer):
    """벌크 액션 요청 — 캠페인 id 배열."""

    ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=200,
        help_text="대상 캠페인 UUID 배열 (최대 200개).",
    )


class CampaignBulkFailureSerializer(serializers.Serializer):
    """벌크 액션 실패 항목."""

    id = serializers.CharField(help_text="실패한 캠페인 id (또는 잘못된 입력값)")
    reason = serializers.CharField(help_text="실패 사유 코드 (not_found 등)")


class CampaignBulkActionResponseSerializer(serializers.Serializer):
    """벌크 액션 응답 — 성공 id 목록 + 실패 상세."""

    succeeded = serializers.ListField(child=serializers.UUIDField())
    failed = CampaignBulkFailureSerializer(many=True)


class AutoDMCampaignCreateSerializer(serializers.Serializer):
    """Auto DM Campaign 생성 (v3.3)"""

    # 멀티 IG 계정: 이 캠페인이 어느 IG connection 에 묶일지 명시.
    # 미지정 시 workspace 의 첫 활성 connection 사용 (backward compat).
    ig_connection_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text=(
            "캠페인을 연결할 IG 계정 connection 의 UUID. 워크스페이스에 여러 IG "
            "계정이 연동된 경우 필수에 준하게 지정해야 의도한 계정에 캠페인이 묶인다. "
            "미지정 시 첫 번째 활성 connection 으로 fallback (backward compat)."
        ),
    )

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
        required=False,
        allow_blank=True,
        allow_null=True,
        default=None,
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
        required=False,
        allow_blank=True,
        default="",
        help_text="첫 인사 DM 본문 (Private Reply via comment_id)",
    )
    message_template = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="legacy 별칭 — opening_message_template 미사용 시 이 값 사용",
    )

    # 공개 답글 (v3.5)
    public_reply_enabled = serializers.BooleanField(default=False)
    public_reply_template = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
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
        default=10,
        min_value=1,
        max_value=200,
        help_text="이 개수만큼 답글 게시 후 쿨다운 적용 (기본 10)",
    )
    public_reply_batch_pause_seconds = serializers.IntegerField(
        default=300,
        min_value=30,
        max_value=3600,
        help_text="배치 도달 후 다음 답글까지 대기 시간 (초, 기본 300)",
    )

    # Follow-gate (v3.8 — is_user_follow_business silent verify)
    follow_gate_enabled = serializers.BooleanField(
        default=False,
        help_text=(
            "true 면 opening DM 에 버튼이 첨부되고, 사용자가 클릭 시 "
            "reward_message_template 을 발송한다. reward_message_template 가 비어 있으면 무시. "
            "버튼 클릭 시 팔로우 검증 여부는 gate_verify_follow 로 제어."
        ),
    )
    gate_verify_follow = serializers.BooleanField(
        default=True,
        help_text=(
            "follow_gate_enabled=true 전제. true(기본): 버튼 클릭 시 IG Profile API 로 "
            "팔로우 여부를 검증한 후 통과 시에만 reward 발송 (기존 follow-gate). "
            "false: 검증을 건너뛰고 버튼 클릭 즉시 reward 발송 (button-only 모드). "
            "button-only 모드에선 follow_gate_button_label / follow_gate_prompt 를 "
            "용도에 맞게 직접 지정 권장 (비우면 follow 용 기본 문구가 노출됨)."
        ),
    )
    follow_gate_prompt = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "Opening DM 본문 (게이트 안내 문구). 비우면 기본 문구 사용. "
            "예: '댓글 남겨주셔서 감사해요! 팔로우도 하셨나요? 버튼을 눌러주세요!'"
        ),
    )
    follow_gate_button_label = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=20,
        default="",
        help_text="버튼 라벨 (Meta 한도 20자). 비우면 '팔로우했어요'.",
    )
    follow_gate_retry_message = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "팔로우 미확인 시 재안내 문구. 비우면 시스템 기본 문구 사용. "
            "재안내 메시지에도 같은 '팔로우했어요' 버튼이 자동 첨부된다."
        ),
    )
    reward_message_template = serializers.CharField(required=False, allow_blank=True, default="")

    # 링크 버튼 (web_url) — 발송 DM 카드에 라벨 달린 링크 버튼으로 첨부.
    # 단순 DM·버튼클릭 즉시 reward·팔로우 검증 후 reward 모두에 적용된다(콘텐츠 전달 DM에 붙음).
    link_button_url = serializers.URLField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2048,
        help_text="발송 DM 에 붙일 링크 버튼 URL (http/https). 비우면 버튼 없음.",
    )
    link_button_label = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=20,
        help_text="링크 버튼 글자 (Meta 한도 20자). link_button_url 있고 비우면 '자세히 보기'.",
    )

    gate_trigger_keywords = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        default=list,
        help_text="postback 미수신 구버전 클라이언트 fallback. 이 키워드 답장도 통과로 간주.",
    )

    # 운영
    max_sends_per_hour = serializers.IntegerField(default=200, min_value=1, max_value=500)

    # 예약 발송 (활성 기간 한정 + 자동 종료) — 둘 다 생략하면 기존처럼 즉시/무기한 운영
    scheduled_start_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "예약 시작일시 (ISO8601, 타임존 포함). 이 시각부터 발송 시작. "
            "비우면 즉시 시작. 예: '2026-07-01T09:00:00+09:00'"
        ),
    )
    scheduled_end_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "예약 종료일시 (ISO8601, 타임존 포함). 이 시각 이후 자동 종료(status=completed). "
            "비우면 무기한. scheduled_start_at 보다 미래여야 함. 예: '2026-07-31T23:59:59+09:00'"
        ),
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
                {
                    "media_id": "trigger_type=story_reply 일 때 media_id 에 대상 Story ID 가 필수입니다."
                }
            )
        # Story 답장 캠페인은 공개 답글 불가능 (Story 에는 댓글 자체가 없음)
        if trigger == AutoDMCampaign.TriggerType.STORY_REPLY and attrs.get("public_reply_enabled"):
            raise serializers.ValidationError(
                {
                    "public_reply_enabled": "Story 답장 캠페인은 공개 답글을 사용할 수 없습니다 (Story 에 댓글 기능이 없음)."
                }
            )
        opening = (attrs.get("opening_message_template") or "").strip()
        legacy_msg = (attrs.get("message_template") or "").strip()
        if not opening and not legacy_msg:
            raise serializers.ValidationError(
                {
                    "opening_message_template": "opening_message_template 또는 message_template 중 하나는 필수입니다."
                }
            )
        # Follow-gate 사용 시 reward_message_template 필수
        if attrs.get("follow_gate_enabled"):
            if not (attrs.get("reward_message_template") or "").strip():
                raise serializers.ValidationError(
                    {
                        "reward_message_template": (
                            "Follow-gate 사용 시 reward_message_template 필수 "
                            "(팔로우 확인 후 발송할 본 DM)"
                        )
                    }
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
        # 예약 발송 창 검증
        start = attrs.get("scheduled_start_at")
        end = attrs.get("scheduled_end_at")
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"scheduled_end_at": "scheduled_end_at 은 scheduled_start_at 보다 미래여야 합니다."}
            )
        if end and end <= timezone.now():
            raise serializers.ValidationError(
                {
                    "scheduled_end_at": "scheduled_end_at 은 현재 시각보다 미래여야 합니다 (과거면 즉시 종료됨)."
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
            # Follow-gate (v3.8)
            "follow_gate_enabled",
            "gate_verify_follow",
            "follow_gate_prompt",
            "follow_gate_button_label",
            "follow_gate_retry_message",
            "reward_message_template",
            "gate_trigger_keywords",
            # 링크 버튼 (web_url)
            "link_button_url",
            "link_button_label",
            "max_sends_per_hour",
            "status",
            # 예약 발송
            "scheduled_start_at",
            "scheduled_end_at",
        ]
        extra_kwargs = {
            "link_button_url": {"required": False, "allow_blank": True},
            "link_button_label": {"required": False, "allow_blank": True},
            "media_url": {"required": False, "allow_null": True, "allow_blank": True},
            "scheduled_start_at": {"required": False, "allow_null": True},
            "scheduled_end_at": {"required": False, "allow_null": True},
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
            "gate_verify_follow": {"required": False},
            "follow_gate_prompt": {"required": False, "allow_blank": True},
            "follow_gate_button_label": {"required": False, "allow_blank": True},
            "follow_gate_retry_message": {"required": False, "allow_blank": True},
            "reward_message_template": {"required": False, "allow_blank": True},
            "gate_trigger_keywords": {"required": False},
        }


class AutoDMCampaignScheduleSerializer(serializers.Serializer):
    """캠페인 예약 발송 창(활성 기간) 설정 — POST .../auto-dm-campaigns/{id}/schedule/.

    예약 창을 **통째로 교체**한다 (PATCH 의 부분 갱신과 달리, 생략한 필드는 null 로 해제).
    예: 시작만 보내면 종료 예약은 해제되어 무기한이 된다.
    """

    scheduled_start_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "발송 시작일시 (ISO8601, 타임존 포함). null/생략이면 즉시 시작. "
            "예: '2026-07-01T09:00:00+09:00'"
        ),
    )
    scheduled_end_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "자동 종료일시 (ISO8601, 타임존 포함). null/생략이면 무기한. "
            "이 시각 이후 status=completed 로 자동 전환. 예: '2026-07-31T23:59:59+09:00'"
        ),
    )
    activate = serializers.BooleanField(
        required=False,
        default=True,
        help_text=(
            "true(기본): 예약 설정과 동시에 status 를 active 로 전환(필요 시 종료 기록 해제). "
            "false: status 는 그대로 두고 예약 창만 갱신."
        ),
    )

    def validate(self, attrs):
        start = attrs.get("scheduled_start_at")
        end = attrs.get("scheduled_end_at")
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"scheduled_end_at": "종료일은 시작일보다 미래여야 합니다."}
            )
        if end and end <= timezone.now():
            raise serializers.ValidationError(
                {"scheduled_end_at": "종료일은 현재 시각보다 미래여야 합니다 (과거면 즉시 종료됨)."}
            )
        return attrs


class AutoDMCampaignCopySerializer(serializers.Serializer):
    """캠페인 복사 — POST .../auto-dm-campaigns/{id}/copy/.

    원본 캠페인을 비활성(INACTIVE) 복사본으로 복제한다. 이름만 바꿀 수 있고,
    나머지 설정(예약 기간 포함)은 전부 원본과 동일하게 복사된다.
    """

    name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text="복사본 캠페인 이름. 생략/공백이면 '{원본명} 복사' 로 자동 생성.",
    )


class SentDMLogSerializer(serializers.ModelSerializer):
    """Serializer for Sent DM Log (v3.2 — 99.9% 보증 + 프론트 액션 가이드 포함)"""

    campaign_id = serializers.UUIDField(source="campaign.id", read_only=True)
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    is_delivered = serializers.SerializerMethodField()
    is_terminal = serializers.SerializerMethodField()
    display_status = serializers.SerializerMethodField()
    frontend_action = serializers.SerializerMethodField()
    # v3.8: 캠페인 로그 1행 = opening 1건 기준. 그 흐름에서 팔로우 전환됐는지를 한눈에.
    follow_passed = serializers.SerializerMethodField()

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
            # v3.8: 팔로우 전환 여부 (opening 1행만 보여줄 때 핵심 지표)
            "follow_passed",
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

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_follow_passed(self, obj):
        """이 흐름에서 팔로우 전환됐는지 (opening 1행 관점).

        - 게이트 미사용 (dm_kind=standalone)        → null
        - 게이트 사용 + PASSED                       → true  (reward 발송됨)
        - 게이트 사용 + PENDING / EXPIRED            → false (아직 또는 실패)
        - child log (reward/retry) 행 자체           → null (자식 행은 list 에 뜨지 않지만 안전 fallback)
        """
        if obj.parent_log_id is not None:
            return None
        if obj.dm_kind != SentDMLog.DMKind.OPENING:
            return None
        return obj.gate_status == SentDMLog.GateStatus.PASSED


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
    read_rate = serializers.FloatField(help_text="DELIVERED 건 중 READ 비율 (0~1)")

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
