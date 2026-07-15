"""
Instagram integration serializers
"""

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .dm_status_groups import GROUP_DISPLAY, status_group
from .models import (
    BUTTON_TEMPLATE_TEXT_MAX,
    DM_TEXT_MAX_BYTES,
    AutoDMCampaign,
    IGAccountConnection,
    SentDMLog,
    SpamCommentLog,
    SpamFilterConfig,
)


def _dm_body_length_errors(
    *,
    follow_gate_enabled: bool,
    follow_gate_prompt: str,
    follow_gate_retry_message: str,
    reward_message_template: str,
    opening_message: str,
    link_button_url: str,
    link_buttons=None,
    opening_message_templates=None,
    follow_gate_prompt_templates=None,
) -> dict:
    """우리가 보내는 DM 본문 포맷별 Meta 글자수 한도 검증(상황에 맞는 한도).

    버튼(postback/web_url)이 붙는 문구 → **button template text 640자**.
    버튼이 없는 일반 텍스트 DM → **UTF-8 1000 바이트**(한글 ≈ 333자).
    버튼 부착 여부:
      - follow_gate_enabled: follow_gate_prompt·follow_gate_retry_message 는 항상 버튼(팔로우 postback);
        reward 는 링크 버튼(link_buttons 목록 또는 legacy link_button_url)이 있을 때만 버튼(링크).
      - 비게이트: opening 은 링크 버튼이 있을 때만 버튼(링크), 없으면 일반 텍스트.
    링크 버튼 개수(1~3)는 640자 text 한도에 영향을 주지 않는다(버튼 title 은 별도 20자 한도).
    회전 목록(opening_message_templates / follow_gate_prompt_templates)의 각 항목도 동일 한도로 검증.
    반환: {필드명: 에러메시지} (없으면 {}).
    """
    has_link = bool(link_buttons) or bool((link_button_url or "").strip())
    errors: dict = {}

    def _too_long(text: str, buttoned: bool) -> str | None:
        t = (text or "").strip()
        if buttoned:
            if len(t) > BUTTON_TEMPLATE_TEXT_MAX:
                return (
                    f"버튼이 붙어 Meta 버튼 카드 한도({BUTTON_TEMPLATE_TEXT_MAX}자)를 초과할 수 "
                    f"없습니다 (현재 {len(t)}자)."
                )
        else:
            nbytes = len(t.encode("utf-8"))
            if nbytes > DM_TEXT_MAX_BYTES:
                approx = DM_TEXT_MAX_BYTES // 3
                return (
                    f"Meta 텍스트 메시지 한도(UTF-8 {DM_TEXT_MAX_BYTES}바이트, 한글 약 {approx}자)를 "
                    f"초과할 수 없습니다 (현재 {nbytes}바이트)."
                )
        return None

    def _check(field: str, label: str, text: str, buttoned: bool) -> None:
        msg = _too_long(text, buttoned)
        if msg:
            errors[field] = f"{label}에는 {msg}"

    def _check_list(field: str, label: str, items, buttoned: bool) -> None:
        for i, item in enumerate(items or []):
            msg = _too_long(str(item), buttoned)
            if msg:
                errors[field] = f"{label} {i + 1}번째 문구에는 {msg}"
                break

    if follow_gate_enabled:
        _check("follow_gate_prompt", "팔로우 게이트 안내 DM", follow_gate_prompt, buttoned=True)
        _check(
            "follow_gate_retry_message",
            "팔로우 재안내 DM",
            follow_gate_retry_message,
            buttoned=True,
        )
        _check("reward_message_template", "리워드 DM", reward_message_template, buttoned=has_link)
    else:
        _check("opening_message_template", "오프닝 DM", opening_message, buttoned=has_link)

    # 회전 변형 목록도 각 항목 검증(모드 무관 — 저장된 값 자체가 한도를 넘으면 안 됨).
    # follow_gate_prompt_templates: 게이트 버튼 항상 → 640자. opening_message_templates: 링크 있으면 640, 없으면 1000바이트.
    _check_list(
        "follow_gate_prompt_templates",
        "팔로우 게이트 안내 DM 변형",
        follow_gate_prompt_templates,
        buttoned=True,
    )
    _check_list(
        "opening_message_templates", "오프닝 DM 변형", opening_message_templates, buttoned=has_link
    )
    return errors


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
            "is_active",
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
            "is_active",
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


class LinkButtonItemSerializer(serializers.Serializer):
    """link_buttons 항목 — 발송 DM 버튼 카드의 web_url 버튼 1개."""

    url = serializers.URLField(max_length=2048, help_text="버튼이 여는 URL (http/https 만 허용).")
    label = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=20,
        help_text="버튼 글자 (Meta 한도 20자). 비우면 '자세히 보기'.",
    )

    class Meta:
        ref_name = "AutoDMLinkButtonItem"

    def validate_url(self, value):
        v = (value or "").strip()
        # Django URLValidator 는 ftp/ftps 도 허용하므로 스킴을 명시적으로 좁힌다.
        if not (v.startswith("http://") or v.startswith("https://")):
            raise serializers.ValidationError("http:// 또는 https:// URL 만 허용됩니다.")
        return v


def _validate_link_buttons_len(value):
    """link_buttons 최대 3개(Meta button template 한도) 검증 공통 헬퍼."""
    if value and len(value) > 3:
        raise serializers.ValidationError("링크 버튼은 최대 3개까지 가능합니다 (Meta 한도).")
    return value


class AutoDMCampaignSerializer(serializers.ModelSerializer):
    """Serializer for Auto DM Campaign (v3.3 — 트리거/키워드/공개답글/Follow-gate 포함)"""

    ig_connection_id = serializers.UUIDField(source="ig_connection.id", read_only=True)
    ig_username = serializers.CharField(source="ig_connection.username", read_only=True)
    is_active = serializers.SerializerMethodField()
    can_send = serializers.SerializerMethodField()
    # 예약 발송: 창 기준 UX 상태 (always_on / scheduled / running / ended)
    schedule_state = serializers.SerializerMethodField()
    is_runnable_now = serializers.SerializerMethodField()
    # 웹훅 누락 시 자동 보정 가능 여부 + 위험 안내 (프론트 고지용)
    miss_recovery = serializers.SerializerMethodField()
    # 실패 DM 복구가 이 캠페인 소유자 플랜에서 실제로 동작하는지 (프로 전용).
    # false 면 recovery_reply_enabled 가 true 여도 안내 대댓글이 게시되지 않는다 → 프론트에서 토글 잠금.
    recovery_reply_available = serializers.SerializerMethodField()
    # 공개 답글 상한 도달 여부 (읽기 전용 — 프론트 UX 표시용)
    public_reply_limit_reached = serializers.SerializerMethodField()
    # 링크 버튼 목록 (쓰기 가능, list 우선, 최대 3개). writable nested → update() 에서 별도 처리.
    link_buttons = LinkButtonItemSerializer(many=True, required=False)
    # 실제 발송 시 첨부될 버튼(link_buttons 우선, 없으면 legacy fallback) — 읽기 전용 편의값.
    effective_link_buttons = serializers.SerializerMethodField()

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
            "opening_message_templates",  # 오프닝 변형 회전 풀
            # 공개 답글 (v3.5)
            "public_reply_enabled",
            "public_reply_template",  # legacy 단일
            "public_reply_templates",  # 신규 리스트
            "public_reply_batch_size",
            "public_reply_batch_pause_seconds",
            "public_reply_limit",  # 공개 답글 누적 상한 (0=무제한, 기본 200)
            "public_reply_posted_count",  # 게시 누계 (read-only)
            "public_reply_limit_reached",  # 상한 도달 여부 (read-only)
            # Follow-gate (v3.8 — is_user_follow_business silent verify)
            "follow_gate_enabled",
            "gate_verify_follow",
            "follow_gate_prompt",
            "follow_gate_prompt_templates",  # 게이트 오프닝 변형 회전 풀
            "follow_gate_button_label",
            "follow_gate_retry_message",
            "reward_message_template",
            "gate_trigger_keywords",
            # 실패 DM 복구 (recovery) — 비팔로워 2534025 실패 시 안내 대댓글 + 인바운드 재전송
            "recovery_reply_enabled",
            "recovery_reply_templates",
            "recovery_keyword",
            "recovery_ttl_seconds",
            "recovery_reply_available",
            # 링크 버튼 (web_url — DM 카드에 라벨 달린 링크 버튼으로 첨부)
            "link_button_url",  # legacy 단일
            "link_button_label",  # legacy 단일
            "link_buttons",  # 신규 리스트 (최대 3개, list 우선)
            "effective_link_buttons",  # 실제 첨부될 버튼 (read-only)
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
            "miss_recovery",
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
            "public_reply_posted_count",
            "public_reply_limit_reached",
            "effective_link_buttons",
            "is_active",
            "can_send",
            "schedule_state",
            "is_runnable_now",
            "miss_recovery",
            "recovery_reply_available",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
        ]

    def get_recovery_reply_available(self, obj) -> bool:
        """이 캠페인 소유자 플랜이 실패 DM 복구(dm_recovery, 프로 전용)를 보유하는지.

        런타임 게이트(_maybe_enter_recovery)와 동일한 owner_has_feature 판정이라,
        false 면 recovery_reply_enabled 가 true 여도 실제 안내 대댓글은 게시되지 않는다.
        스태프(어드민)는 항상 true 로 노출한다.

        목록(list) 직렬화는 자식 serializer 인스턴스를 항목마다 재사용하므로 owner_id 기준으로
        메모이즈해 owner_has_feature 의 항목당 플랜 조회(N+1)를 막는다.
        """
        from apps.billing.subscription_utils import owner_has_feature

        from .campaign_stats import is_admin_user

        request = self.context.get("request")
        if request is not None and is_admin_user(request.user):
            return True
        try:
            workspace = obj.ig_connection.workspace
            owner_id = workspace.owner_id
        except Exception:  # noqa: BLE001 - 조회 실패는 미보유 취급
            return False
        cache = self.__dict__.setdefault("_recovery_avail_cache", {})
        if owner_id not in cache:
            try:
                cache[owner_id] = owner_has_feature(workspace, "dm_recovery")
            except Exception:  # noqa: BLE001 - 조회 실패는 미보유 취급
                cache[owner_id] = False
        return cache[owner_id]

    def validate_link_buttons(self, value):
        return _validate_link_buttons_len(value)

    def update(self, instance, validated_data):
        # link_buttons 는 writable nested serializer 라 기본 ModelSerializer.update()의
        # 중첩쓰기 가드에 걸린다 → pop 후 super().update() 를 호출하고 직접 대입한다.
        link_buttons = validated_data.pop("link_buttons", None)
        instance = super().update(instance, validated_data)
        if link_buttons is not None:
            instance.link_buttons = link_buttons
            instance.save(update_fields=["link_buttons", "updated_at"])
        return instance

    @extend_schema_field(LinkButtonItemSerializer(many=True))
    def get_effective_link_buttons(self, obj) -> list:
        """실제 발송 시 첨부될 버튼 목록 (get_link_buttons 결과를 {url,label} 형태로)."""
        return [{"url": b["url"], "label": b["title"]} for b in (obj.get_link_buttons() or [])]

    def get_public_reply_limit_reached(self, obj) -> bool:
        return obj.public_reply_limit_reached()

    def get_is_active(self, obj) -> bool:
        return obj.is_active()

    def get_can_send(self, obj) -> bool:
        return obj.can_send_more()

    def get_schedule_state(self, obj) -> str:
        return obj.schedule_state()

    def get_is_runnable_now(self, obj) -> bool:
        return obj.is_runnable_now()

    def get_miss_recovery(self, obj) -> dict:
        """웹훅 누락 시 자동 보정(poll_missed_comments) 가능 여부 + 위험 안내.

        specific_media / next_media(attach 후 specific 로 전환)만 시간당 폴링으로 누락이 보정된다.
        any_media(폴링 비용)·story_reply(메시지 기반이라 재조회할 댓글 소스가 없음)는 보정망이 없어,
        인스타 웹훅이 누락되면 해당 DM 이 발송되지 않을 수 있다 → 프론트에서 사용자에게 고지.
        """
        safe = obj.trigger_type in (
            AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            AutoDMCampaign.TriggerType.NEXT_MEDIA,
        )
        return {
            "auto_recovery_supported": safe,
            "warning": (
                None
                if safe
                else (
                    "이 트리거 유형(모든 게시물/스토리 답장)은 인스타그램 웹훅이 누락되면 "
                    "자동 보정·재발송이 되지 않습니다. 누락 없는 발송이 중요하면 "
                    "'특정 게시물'(specific_media) 트리거를 권장합니다."
                )
            ),
        }

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
        help_text=(
            "첫 인사 DM 본문 (Private Reply via comment_id). 글자수 한도는 상황에 따라 다름: "
            "링크 버튼(link_button_url)을 붙이면 버튼 카드로 나가 **640자**, 버튼이 없으면 "
            "일반 텍스트라 **UTF-8 1000바이트(한글 약 333자)**. 초과 시 400."
        ),
    )
    message_template = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="legacy 별칭 — opening_message_template 미사용 시 이 값 사용 (동일한 글자수 한도 적용).",
    )
    opening_message_templates = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
        help_text=(
            "오프닝 DM 변형 목록. 비어있지 않으면 발송 시마다 무작위 1개 선택(동일 메시지 대량발송 "
            "스팸판정 회피). 각 항목은 오프닝과 동일 한도(링크 버튼 있으면 640자, 없으면 1000바이트). "
            "diversify-opening API 로 생성한 변형을 여기에 저장한다."
        ),
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
    public_reply_limit = serializers.IntegerField(
        default=200,
        min_value=0,
        max_value=1_000_000,
        help_text=(
            "공개 답글(대댓글) 누적 상한 (기본 200, 0=무제한). 도달 시 이후 공개 답글을 "
            "게시하지 않는다(DM 발송엔 영향 없음). 복구 안내 대댓글은 상한 차단·집계에서 제외."
        ),
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
            "버튼(팔로우)이 항상 붙는 버튼 카드라 **640자** 한도(초과 시 400). "
            "예: '댓글 남겨주셔서 감사해요! 팔로우도 하셨나요? 버튼을 눌러주세요!'"
        ),
    )
    follow_gate_prompt_templates = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
        help_text=(
            "게이트 오프닝(follow_gate_prompt) 변형 목록. 비어있지 않으면 발송 시마다 무작위 1개 선택. "
            "각 항목 640자 이내(팔로우 버튼 카드). diversify-opening API 결과를 저장."
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
            "재안내 메시지에도 같은 '팔로우했어요' 버튼이 자동 첨부되는 버튼 카드라 **640자** 한도."
        ),
    )
    reward_message_template = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "팔로우 통과 후 보내는 본 DM. 링크 버튼(link_button_url)을 붙이면 버튼 카드로 나가 "
            "**640자**, 버튼이 없으면 일반 텍스트라 **UTF-8 1000바이트(한글 약 333자)** 한도."
        ),
    )

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
        help_text="[legacy] 단일 링크 버튼 글자 (Meta 한도 20자). 비우면 '자세히 보기'.",
    )
    link_buttons = LinkButtonItemSerializer(
        many=True,
        required=False,
        default=list,
        help_text=(
            "링크 버튼 목록 (최대 3개, Meta button template 한도). 비어있지 않으면 legacy "
            "link_button_url/link_button_label 보다 우선한다. 하나라도 있으면 DM 은 버튼 "
            '카드(본문 640자 한도)로 발송된다. 각 항목: {"url": "https://...", "label": "받기"}.'
        ),
    )

    gate_trigger_keywords = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        default=list,
        help_text="postback 미수신 구버전 클라이언트 fallback. 이 키워드 답장도 통과로 간주.",
    )

    # 실패 DM 복구 (recovery) — v2(2026-07-14): 인바운드 DM 트리거 폐기 → 재댓글 방식
    recovery_reply_enabled = serializers.BooleanField(
        required=False,
        default=True,
        help_text=(
            "opening 이 2534025(비팔로워 채널 미개설)로 **확정** 실패하면 댓글에 'DM이 숨겨진 "
            "요청/스팸함으로 갔어요 — 수락 후 다시 댓글 달아주세요' 안내를 게시한다. 사용자가 "
            "다시 댓글을 달면 일반 발송 경로로 재발송되고, 성공 시 이전 실패 건은 "
            "recovery_delivered 로 자동 승격된다. 기본값 true. "
            "프로 전용 — 미보유 플랜은 켜도 동작하지 않는다(recovery_reply_available 로 확인)."
        ),
    )
    recovery_reply_templates = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text=(
            "복구 안내 대댓글 변형 목록(무작위 1개 사용). 비우면 서버 조합 생성기가 매번 새 문구를 "
            "만든다(권장 — 봇 검사에 가장 강함). 추천 문구는 recovery-reply-suggestions API 참고."
        ),
    )
    recovery_keyword = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=255,
        help_text=(
            "(deprecated v2 — 인바운드 DM 트리거 폐기로 값은 무시된다. 하위호환용으로 수용만 함.)"
        ),
    )
    recovery_ttl_seconds = serializers.IntegerField(
        required=False,
        default=604800,
        min_value=3600,
        max_value=2592000,
        help_text=(
            "복구 대기 유효기간(초). 이 시간 내 같은 사용자의 재댓글 발송 성공이 없으면 "
            "recovery_expired 로 만료. 기본 7일(604800), 범위 1시간~30일."
        ),
    )

    # 운영 — (deprecated v4.3) 값은 수용하되 무시됨: 페이싱은 dm_pacer(계정 단위 지터 슬롯)가 담당.
    # 하위호환(기존 프론트가 보내는 값 400 방지)을 위해 필드는 유지한다.
    max_sends_per_hour = serializers.IntegerField(
        default=200,
        min_value=1,
        max_value=500,
        help_text="(deprecated v4.3 — 강제되지 않음) 발송 페이싱은 계정 단위 자동 조절로 대체됨.",
    )

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

    def validate_link_buttons(self, value):
        return _validate_link_buttons_len(value)

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
        # 복구: 템플릿은 선택 — 비우면 서버 기본 세트(DEFAULT_RECOVERY_REPLY_TEMPLATES)를
        # 무작위로 사용하므로 recovery_reply_enabled=true 여도 필수 아님.
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
        # 우리가 보내는 DM 본문 포맷별 Meta 글자수 한도 검증(버튼 640자 / 일반 텍스트 1000바이트).
        length_errors = _dm_body_length_errors(
            follow_gate_enabled=bool(attrs.get("follow_gate_enabled")),
            follow_gate_prompt=attrs.get("follow_gate_prompt") or "",
            follow_gate_retry_message=attrs.get("follow_gate_retry_message") or "",
            reward_message_template=attrs.get("reward_message_template") or "",
            opening_message=(
                attrs.get("opening_message_template") or attrs.get("message_template") or ""
            ),
            link_button_url=attrs.get("link_button_url") or "",
            link_buttons=attrs.get("link_buttons") or [],
            opening_message_templates=attrs.get("opening_message_templates") or [],
            follow_gate_prompt_templates=attrs.get("follow_gate_prompt_templates") or [],
        )
        if length_errors:
            raise serializers.ValidationError(length_errors)
        return attrs


class AutoDMCampaignUpdateSerializer(serializers.ModelSerializer):
    """Auto DM Campaign 수정 (Swagger/edit form 용 — v3.3)"""

    link_buttons = LinkButtonItemSerializer(many=True, required=False)

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
            "opening_message_templates",
            # 공개 답글 (v3.5)
            "public_reply_enabled",
            "public_reply_template",
            "public_reply_templates",
            "public_reply_batch_size",
            "public_reply_batch_pause_seconds",
            "public_reply_limit",
            # Follow-gate (v3.8)
            "follow_gate_enabled",
            "gate_verify_follow",
            "follow_gate_prompt",
            "follow_gate_prompt_templates",  # 게이트 오프닝 변형 회전 풀
            "follow_gate_button_label",
            "follow_gate_retry_message",
            "reward_message_template",
            "gate_trigger_keywords",
            # 실패 DM 복구 (recovery)
            "recovery_reply_enabled",
            "recovery_reply_templates",
            "recovery_keyword",
            "recovery_ttl_seconds",
            # 링크 버튼 (web_url)
            "link_button_url",
            "link_button_label",
            "link_buttons",
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
            "opening_message_templates": {"required": False},
            "public_reply_enabled": {"required": False},
            "public_reply_template": {"required": False, "allow_blank": True},
            "public_reply_templates": {"required": False},
            "public_reply_batch_size": {"required": False},
            "public_reply_batch_pause_seconds": {"required": False},
            "public_reply_limit": {"required": False},
            "follow_gate_enabled": {"required": False},
            "gate_verify_follow": {"required": False},
            "follow_gate_prompt": {"required": False, "allow_blank": True},
            "follow_gate_prompt_templates": {"required": False},
            "follow_gate_button_label": {"required": False, "allow_blank": True},
            "follow_gate_retry_message": {"required": False, "allow_blank": True},
            "reward_message_template": {"required": False, "allow_blank": True},
            "gate_trigger_keywords": {"required": False},
            "recovery_reply_enabled": {"required": False},
            "recovery_reply_templates": {"required": False},
            "recovery_keyword": {"required": False, "allow_blank": True},
            "recovery_ttl_seconds": {"required": False},
        }

    def validate(self, attrs):
        # 부분 수정(PATCH)이라 attrs 에 없는 필드는 기존 인스턴스 값으로 병합해 판정한다.
        def _resolve(field, default=""):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, default)

        # trigger_type 정합성: media_id 는 이 serializer 로 편집할 수 없다(fields 에 없음).
        # 따라서 SPECIFIC_MEDIA/STORY_REPLY 로의 **변경**은 유효한 media_id/story_id 재지정이
        # 불가능해 matches_media()가 영구 False 인 무효 상태를 만든다(예: ANY→SPECIFIC 이면
        # media_id="" 로 bool("")=False, →STORY_REPLY 는 댓글 웹훅에서 항상 False). 이 상태의
        # 캠페인에 남은 RECOVERY_PENDING 은 재댓글이 와도 스코핑 게이트에서 영구 탈락한다.
        # ANY_MEDIA 로의 변경만 허용하고(update() 에서 stray media_id 클리어), 나머지 전환은
        # 새 캠페인 생성으로 유도한다. trigger 를 안 바꾸는 수정(이름/문구/상태)은 영향 없음.
        if self.instance is not None:
            new_trigger = attrs.get("trigger_type")
            if new_trigger is not None and new_trigger != self.instance.trigger_type:
                if new_trigger in (
                    AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
                    AutoDMCampaign.TriggerType.STORY_REPLY,
                ):
                    raise serializers.ValidationError(
                        {
                            "trigger_type": (
                                "specific_media/story_reply 로의 변경은 지원하지 않습니다 "
                                "(대상 게시물/스토리를 바꾸려면 새 캠페인을 만들어 주세요)."
                            )
                        }
                    )

        length_errors = _dm_body_length_errors(
            follow_gate_enabled=bool(_resolve("follow_gate_enabled", False)),
            follow_gate_prompt=_resolve("follow_gate_prompt", "") or "",
            follow_gate_retry_message=_resolve("follow_gate_retry_message", "") or "",
            reward_message_template=_resolve("reward_message_template", "") or "",
            opening_message=(
                _resolve("opening_message_template", "") or _resolve("message_template", "") or ""
            ),
            link_button_url=_resolve("link_button_url", "") or "",
            link_buttons=_resolve("link_buttons", []) or [],
            opening_message_templates=_resolve("opening_message_templates", []) or [],
            follow_gate_prompt_templates=_resolve("follow_gate_prompt_templates", []) or [],
        )
        if length_errors:
            raise serializers.ValidationError(length_errors)
        return attrs

    def validate_link_buttons(self, value):
        return _validate_link_buttons_len(value)

    def update(self, instance, validated_data):
        # ANY_MEDIA 로 전환 시 남아있는 media_id(직전 SPECIFIC/STORY 잔재)를 클리어한다.
        # stray media_id 를 든 ANY_MEDIA 캠페인은 복구 재댓글 스레드의 media 추정
        # (poll_recovery_recomments)을 오염시켜 형제 SPECIFIC 캠페인의 정당한 재댓글을
        # 오필터할 수 있다(적대 리뷰 발견). media_id 는 fields 에 없어 super().update() 가
        # 건드리지 않으므로 여기서 명시 클리어 후 저장한다.
        # link_buttons 는 writable nested → 중첩쓰기 가드 회피 위해 pop 후 직접 대입.
        link_buttons = validated_data.pop("link_buttons", None)
        if validated_data.get("trigger_type") == AutoDMCampaign.TriggerType.ANY_MEDIA:
            instance.media_id = ""
        instance = super().update(instance, validated_data)
        if link_buttons is not None:
            instance.link_buttons = link_buttons
            instance.save(update_fields=["link_buttons", "updated_at"])
        return instance


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
    # 코스 상태 그룹 (유저 콘솔 탭/배지 단일 소스 — dm_status_groups). 프론트가 status 를
    # 다시 분류하지 않고 이 값으로 바로 탭 필터/배지를 그린다.
    status_group = serializers.SerializerMethodField()
    status_group_display = serializers.SerializerMethodField()
    # 복구 대기(recovery_pending) 여부 — 숨겨진 요청·스팸 배지에 "복구 대기" 보조 칩 표시용.
    is_recovering = serializers.SerializerMethodField()
    frontend_action = serializers.SerializerMethodField()
    # v3.8: 캠페인 로그 1행 = opening 1건 기준. 그 흐름에서 팔로우 전환됐는지를 한눈에.
    follow_passed = serializers.SerializerMethodField()
    # 이 행의 플로우 내 역할. 재안내(retry)는 quick_reply 재첨부를 위해 dm_kind=opening 으로
    # 저장되므로 dm_kind 만으로는 오프닝과 구분 불가 — 프론트는 이 필드로 라벨링할 것.
    flow_role = serializers.SerializerMethodField()

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
            "status_group",
            "status_group_display",
            "is_recovering",
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
            # 복구 안내 대댓글(숨겨진 요청·스팸 → "수락 후 재댓글" 안내). public_reply_* 와
            # 대칭으로 노출 → 프론트가 숨겨진 요청·스팸 탭에서 "복구 안내 댓글 게시됨"을 표시 가능.
            "recovery_reply_id",
            "recovery_pending_at",
            # v3.8: 팔로우 전환 여부 (opening 1행만 보여줄 때 핵심 지표)
            "follow_passed",
            # 플로우 내 역할: opening / retry / reward / standalone (표시용)
            "flow_role",
        ]
        read_only_fields = fields  # 모두 읽기 전용

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Story 답장 등으로 username 미해석(빈 값)이면 표시용 폴백 user_{IGSID}.
        # DB 컬럼은 빈 채 유지 → 윈도우 내 재열람 시 실제 핸들로 채워질 여지 보존.
        if not data.get("recipient_username"):
            data["recipient_username"] = f"user_{instance.recipient_user_id}"
        return data

    def get_is_delivered(self, obj) -> bool:
        return obj.is_delivered()

    def get_is_terminal(self, obj) -> bool:
        return obj.is_terminal()

    def get_display_status(self, obj) -> str:
        """프론트 표시용 사용자 친화적 상태 (세분화 라벨).

        failed_param 은 subcode=2534025(숨김함) 면 '숨겨진 요청 · 스팸' 으로 분기한다.
        코스 탭/배지는 status_group / status_group_display 를 쓰고, 이 값은 상세 라벨용.
        """
        if obj.status == "failed_param" and str(obj.error_subcode or "").strip() == "2534025":
            return "숨겨진 요청 · 스팸"
        return _STATUS_DISPLAY.get(obj.status, obj.status)

    @extend_schema_field(serializers.ChoiceField(choices=list(GROUP_DISPLAY.keys())))
    def get_status_group(self, obj) -> str:
        """코스 상태 그룹 머신값 (waiting/sent/read/hidden_spam/attention)."""
        return status_group(obj.status, obj.error_subcode)

    def get_status_group_display(self, obj) -> str:
        """코스 상태 그룹 표시명 (대기중/전송됨/읽음/숨겨진 요청·스팸/확인 필요)."""
        return GROUP_DISPLAY[status_group(obj.status, obj.error_subcode)]

    def get_is_recovering(self, obj) -> bool:
        """복구 대기(recovery_pending) 여부 — '복구 대기' 보조 칩 표시 판단용."""
        return obj.status == SentDMLog.Status.RECOVERY_PENDING

    def get_frontend_action(self, obj) -> dict:
        """v3.2 — 상태별 프론트엔드 표시/체크리스트/CTA 가이드 (failed_param 은 subcode 분기)."""
        from .dm_frontend_actions import build_frontend_action

        return build_frontend_action(obj.status, obj.error_subcode)

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

    @extend_schema_field(
        serializers.ChoiceField(choices=["opening", "retry", "reward", "standalone"])
    )
    def get_flow_role(self, obj) -> str:
        """플로우 내 역할 (include_children=true 전체 행 표시 시 행 라벨용).

        - opening    : 루트 오프닝 DM (댓글 1건 = 1행)
        - retry      : 팔로우 미확인 재안내 DM (dm_kind 는 opening 이지만 child 행)
        - reward     : 게이트 통과 보상 DM
        - standalone : 게이트 미사용 단발 DM
        """
        if obj.dm_kind == SentDMLog.DMKind.REWARD:
            return "reward"
        if obj.dm_kind == SentDMLog.DMKind.OPENING:
            return "retry" if obj.parent_log_id is not None else "opening"
        return "standalone"


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
    "recovery_pending": "복구 대기 (요청함 수락·재댓글 대기)",
    "recovery_delivered": "복구 성공 (재댓글 발송 도착)",
    "recovery_expired": "복구 대기 만료",
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

    # ── v4.2 — 사람(수신자 Instagram ID) 단위 지표 (마케팅용) ──────────────
    # 위 필드는 모두 "발송 이벤트" 단위라 follow-gate 캠페인(1명=DM 2건)에서 부풀려 보인다.
    # 아래 unique_* 는 수신자 계정 기준 중복 제거한 "사람 수" 지표.
    unique_recipients = serializers.IntegerField(
        help_text="DM 로그가 1건이라도 있는 고유 수신자 수 (도달 인원)"
    )
    unique_sent = serializers.IntegerField(
        help_text="DM 이 실제 발송된 고유 수신자 수 (CTR 분모). accepted 이상 상태 기준"
    )
    unique_delivered = serializers.IntegerField(
        help_text="도착(delivered/read) 경험이 있는 고유 수신자 수"
    )
    unique_read = serializers.IntegerField(help_text="읽음 확인된 고유 수신자 수")
    unique_followers = serializers.IntegerField(
        help_text="follow-gate 통과(팔로우 확인)된 고유 수신자 수"
    )
    unique_delivery_rate = serializers.FloatField(
        help_text="unique_delivered / unique_sent (사람 단위 도착률, 0~1)"
    )

    # ── v4.4 — 사람 단위 처리 현황 (루트 DM 기준 — queue-state.people 과 동일 정의) ──
    unique_targets = serializers.IntegerField(
        help_text=(
            "전체 대상 사람 수 — 루트 DM(오프닝/단독, 리워드·재안내 제외) 기준 고유 수신자. "
            "실패 포함 모수이며 항등 '루트 발송 + unique_waiting + unique_failed' 이 항상 성립. "
            "unique_sent 는 리워드/재안내 수신자도 포함(전체 로그 기준)이라, 부모 오프닝이 집계 "
            "구간(기본 30일) 밖인 드문 경우 unique_targets 와 미세하게 다를 수 있음"
        )
    )
    unique_waiting = serializers.IntegerField(
        help_text="아직 발송 대기/발송 중인 사람 수 (루트 DM 기준)"
    )
    unique_failed = serializers.IntegerField(
        help_text=(
            "아무것도 받지 못한 사람 수 — 하드실패(failed_*)·복구 대기/만료(recovery_*)·"
            "한도 스킵(skipped) 포함. '확인 필요' 카드 = unique_failed + unique_unconfirmed"
        )
    )
    unique_unconfirmed = serializers.IntegerField(
        help_text=(
            "발송은 됐으나 도착 미확인(failed_no_trace)만 있는 사람 수. "
            "unique_failed 와 서로소(합산 시 중복 없음)"
        )
    )
    unique_reach_rate = serializers.FloatField(
        help_text="unique_delivered / unique_targets — 전체 대상 대비 실제 도달률 ([0,1] 클램프)"
    )
    unique_sent_rate = serializers.FloatField(
        help_text=(
            "unique_sent / unique_targets — **전체 대상 대비 전송된 비율 ([0,1] 클램프)**. "
            "유저 콘솔 헤드라인('N% 메시지가 성공적으로 전송됐어요')용. delivery_rate 는 "
            "Meta 접수건만 분모라 하드실패가 빠져 100%로 부풀 수 있으니, 헤드라인엔 이 값을 쓸 것."
        )
    )

    # ── v4.5 — 숨겨진 요청 · 스팸 / 확인 필요 분리 (유저 콘솔 카드용) ──────────
    # '확인 필요'(unique_failed + unique_unconfirmed) 중 '숨겨진 요청/스팸함' 케이스를
    # 별도 카드로 분리한다(비팔로워 채널 미개설 2534025 — 가장 잦은 사유). dm_status_groups 참조.
    unique_hidden_spam = serializers.IntegerField(
        help_text=(
            "'숨겨진 요청 · 스팸' 인원 — 비팔로워라 채널 미개설(2534025)로 첫 DM 이 상대 "
            "숨겨진 요청/스팸함으로 간 사람 (복구 대기·만료 recovery_* + 복구 OFF 시 "
            "failed_param@2534025). unique_failed 의 부분집합 (아무것도 못 받은 사람)."
        )
    )
    unique_needs_attention = serializers.IntegerField(
        help_text=(
            "기존 '확인 필요' 총합 = unique_failed + unique_unconfirmed (하위호환·다른 위치 배치용). "
            "숨겨진 요청·스팸을 포함한 전체 조치 대상 인원."
        )
    )
    unique_needs_attention_excl_hidden = serializers.IntegerField(
        help_text=(
            "숨겨진 요청·스팸을 뺀 '확인 필요' 인원 "
            "(= unique_needs_attention − unique_hidden_spam). 새 '확인 필요' 카드용."
        )
    )

    # ── v4.2 — CTR(참여율) ────────────────────────────────────────────────
    ctr = serializers.FloatField(
        help_text=(
            "참여율 = ctr_interacted / unique_sent (0~1). "
            "게이트형 캠페인은 '버튼 클릭', 비게이트형은 '읽음' 을 참여로 본다."
        )
    )
    ctr_basis = serializers.ChoiceField(
        choices=["click", "read", "mixed"],
        help_text=(
            "CTR 참여 판정 기준. click=버튼 클릭(게이트형), read=읽음(비게이트형), "
            "mixed=집계 범위에 두 타입이 섞임(campaign_id 로 필터하면 click/read 로 확정)."
        ),
    )
    ctr_interacted = serializers.IntegerField(help_text="상호작용한 고유 수신자 수 (CTR 분자)")
    ctr_denominator = serializers.IntegerField(help_text="CTR 분모 (= unique_sent)")


class DMRecipientRollupSerializer(serializers.Serializer):
    """캠페인 DM 로그 — 수신자(사람) 1명 단위 롤업 (v4.2).

    개별 발송 이벤트가 아니라 recipient_user_id 로 묶은 최신 상태.
    """

    recipient_user_id = serializers.CharField(help_text="수신자 Instagram ID (묶음 키)")
    recipient_username = serializers.CharField(
        allow_blank=True, help_text="수신자 username (최신값 best-effort)"
    )
    sent = serializers.BooleanField(help_text="DM 실제 발송됨 (accepted 이상)")
    delivered = serializers.BooleanField(help_text="도착 확인됨 (delivered/read)")
    read = serializers.BooleanField(help_text="읽음 확인됨")
    follower_status = serializers.ChoiceField(
        choices=["verified_follower", "clicked_unverified", "not_followed", "unknown"],
        help_text=(
            "팔로우/참여 상태. verified_follower=게이트 통과+팔로우 검증됨, "
            "clicked_unverified=버튼만 누름(미검증), not_followed=클릭/통과 안 함, "
            "unknown=게이트 미사용 캠페인(팔로우 여부 알 수 없음)."
        ),
    )
    dm_count = serializers.IntegerField(help_text="이 사람에게 나간 총 DM 이벤트 수")
    needs_attention = serializers.BooleanField(
        help_text=(
            "조치가 필요한 상태(= status_group == attention '확인 필요')인가. "
            "**success-aware**: 발송/도착/읽음/복구 성공이 하나라도 있으면 false — 과거 실패 "
            "로그(복구 전 no_trace 등)가 남아 있어도 결국 전송/복구됐으면 '확인 필요'로 보지 않는다. "
            "숨겨진 요청·스팸(hidden_spam)은 별도이므로 여기서 false(→ status_group 로 표시)."
        )
    )
    # 코스 상태 그룹 (한 사람 = 1개 그룹, 우선순위 read>sent>waiting>hidden_spam>attention).
    # 프론트 탭/배지의 단일 소스 — 클라이언트에서 sent/delivered/read 불리언으로 재분류할 필요 없음.
    status_group = serializers.ChoiceField(
        choices=list(GROUP_DISPLAY.keys()),
        help_text=(
            "이 수신자의 코스 상태 그룹. "
            "waiting(대기중)/sent(전송됨)/read(읽음)/hidden_spam(숨겨진 요청·스팸)/attention(확인 필요). "
            "?status_group= 로 서버 필터 가능."
        ),
    )
    status_group_display = serializers.CharField(
        help_text="status_group 의 한국어 표시명 (대기중/전송됨/읽음/숨겨진 요청 · 스팸/확인 필요)"
    )
    is_recovering = serializers.BooleanField(
        help_text=(
            "복구 대기(recovery_pending)인 DM 이 있음. hidden_spam 배지에 '복구 대기' 보조 칩을 "
            "붙일지 판단용 (복구 ON → true, 복구 OFF/만료 → false)."
        )
    )
    last_activity_at = serializers.DateTimeField(allow_null=True, help_text="마지막 활동 시각")


class DMQueueGaugeSerializer(serializers.Serializer):
    """큐-상태 게이지 카운트 (v4.3 — 프론트 게이지 = sent / total)."""

    sent = serializers.IntegerField(help_text="발송 완료 (Meta 접수 이상 — 월 쿼터와 동일 정의)")
    waiting = serializers.IntegerField(help_text="발송 대기 (QUEUED — 페이서 슬롯/재시도 대기)")
    in_flight = serializers.IntegerField(help_text="발송 중 (SUBMITTING — 순간값)")
    failed = serializers.IntegerField(
        help_text="하드 실패 (토큰/윈도우/파라미터 — 게이지 분모(total) 제외, 별도 표기용)"
    )
    total = serializers.IntegerField(help_text="sent + waiting + in_flight (정상 큐는 100% 도달)")


class DMQueuePeopleGaugeSerializer(serializers.Serializer):
    """큐-상태 사람(수신자) 단위 게이지 (v4.4 — 유저 콘솔 "N명" 표기용).

    루트 DM(오프닝/단독 — 리워드·재안내 제외) 기준 고유 수신자 수.
    follow-gate 캠페인에서 이벤트 단위 gauge 는 1명=2건 이상으로 부풀므로,
    "전체 대상 N명 / 처리 완료" UI 는 이 블록을 쓴다. 진행바 = processed / total.
    """

    total = serializers.IntegerField(
        help_text="전체 대상 사람 수 (실패 포함 — sent+waiting+failed)"
    )
    sent = serializers.IntegerField(help_text="DM 이 실제 발송된 사람 (Meta 접수 이상)")
    waiting = serializers.IntegerField(help_text="발송 차례 대기/발송 중인 사람")
    failed = serializers.IntegerField(
        help_text=(
            "아무것도 받지 못하고 종결·정체된 사람 "
            "(하드실패·복구 대기/만료·한도 스킵 — '확인 필요' 성격)"
        )
    )
    processed = serializers.IntegerField(help_text="처리 완료 = sent + failed (진행바 분자)")


class DMQueuePacingSerializer(serializers.Serializer):
    """현재 발송 페이싱 정보 (v4.3 — 계정 단위 지터 슬롯)."""

    private_reply_avg_gap_s = serializers.FloatField(
        help_text="오프닝(사설답장) 평균 발송 간격 초 (지터 범위 중앙값, 기본 5.0)"
    )
    send_api_avg_gap_s = serializers.FloatField(
        help_text="리워드/재안내/스토리답장 평균 발송 간격 초 (기본 2.0)"
    )
    hourly_backstop_cap = serializers.IntegerField(
        help_text="계정당 시간당 백스톱 상한 (기본 740 — 정상 운영에선 걸리지 않는 최후 방어선)"
    )


class DMQueueStateSerializer(serializers.Serializer):
    """DM 순차 발송 큐 현황 (게이지 + ETA) 응답 — v4.3 페이서 기반."""

    scope = serializers.ChoiceField(choices=["campaign", "account"], help_text="집계 범위")
    campaign_id = serializers.UUIDField(allow_null=True, help_text="campaign 스코프일 때만")
    ig_connection_id = serializers.UUIDField(help_text="대상 IG 연동 ID")
    external_account_id = serializers.CharField(help_text="IG 계정 ID")
    ig_username = serializers.CharField(help_text="IG 계정 username")

    gauge = DMQueueGaugeSerializer()
    people = DMQueuePeopleGaugeSerializer()
    pacing = DMQueuePacingSerializer()

    account_waiting = serializers.IntegerField(
        help_text="이 계정 전체(모든 캠페인)의 발송 대기 수 — 계정 단위로 순차 발송되므로 공유 대기열"
    )
    ahead_of_this_campaign = serializers.IntegerField(
        help_text=(
            "이 캠페인의 가장 오래된 대기 건보다 먼저 줄 선 다른 캠페인 대기 수 (FIFO). "
            "account 스코프면 항상 0"
        )
    )

    blocking_reason = serializers.CharField(
        allow_null=True,
        help_text=(
            "발송 정지 사유. null=정상(페이싱 진행 중), "
            "action_block_cooldown=Instagram 일시 제한(자동 재개), "
            "monthly_quota_reached=플랜 월 한도 도달(업그레이드 필요)"
        ),
    )
    action_block_cooldown_seconds = serializers.IntegerField(
        help_text="Action Block 쿨다운 잔여 초 (0=해당 없음)"
    )

    eta_seconds = serializers.FloatField(
        help_text="대기 중인 DM 이 모두 발송 완료될 때까지 예상 초 (0=대기 없음)"
    )
    eta_finish_at = serializers.DateTimeField(
        allow_null=True, help_text="예상 완료 시각 (ISO8601). 대기 없으면 null"
    )
    eta_is_estimate = serializers.BooleanField(
        help_text=(
            "true=추정치(미확정 슬롯/일시정지 포함 — '약 N분' 표기 권장), "
            "false=확정 슬롯 기반(전 건 슬롯 예약 완료)"
        )
    )
    generated_at = serializers.DateTimeField(help_text="집계 시각 (클라이언트 보간 기준점)")


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
            "auto_hide_enabled",
            "use_llm",
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
        fields = ["status", "spam_keywords", "block_urls", "auto_hide_enabled", "use_llm"]

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
            "confidence",
            "spam_category",
            "engine",
            "status",
            "error_message",
            "webhook_payload",
            "api_response",
            "created_at",
            "hidden_at",
        ]
        read_only_fields = fields  # 모두 읽기 전용


class SpamDashboardSummarySerializer(serializers.Serializer):
    """스팸 대시보드 요약 카드 (오늘/어제/누적/최근7일)."""

    today_detected = serializers.IntegerField(help_text="오늘 감지한 스팸 수")
    yesterday_detected = serializers.IntegerField(help_text="어제 감지한 스팸 수")
    today_hidden = serializers.IntegerField(help_text="오늘 차단(숨김)한 댓글 수")
    yesterday_hidden = serializers.IntegerField(help_text="어제 차단(숨김)한 댓글 수")
    total_detected = serializers.IntegerField(help_text="총 감지한 스팸 수(누적)")
    last7_detected = serializers.IntegerField(help_text="최근 7일 감지한 스팸 수")
    total_hidden = serializers.IntegerField(help_text="총 차단(숨김)한 댓글 수(누적)")
    last7_hidden = serializers.IntegerField(help_text="최근 7일 차단(숨김)한 댓글 수")


class SpamDashboardChartPointSerializer(serializers.Serializer):
    """14일 차트의 하루치 포인트."""

    date = serializers.CharField(help_text="YYYY-MM-DD (Asia/Seoul)")
    detected = serializers.IntegerField(help_text="해당일 스팸 감지 수")
    hidden = serializers.IntegerField(help_text="해당일 댓글 차단 수")


class SpamDashboardBiweeklySerializer(serializers.Serializer):
    """최근 2주 평균/최대."""

    avg_detected = serializers.FloatField(help_text="일 평균 스팸 감지")
    avg_hidden = serializers.FloatField(help_text="일 평균 댓글 차단")
    max_detected = serializers.IntegerField(help_text="일 최대 스팸 감지")
    max_hidden = serializers.IntegerField(help_text="일 최대 댓글 차단")


class SpamDashboardSerializer(serializers.Serializer):
    """스팸 필터 대시보드 응답(요약 카드 + 14일 차트 + 2주 통계)."""

    summary = SpamDashboardSummarySerializer()
    chart_14d = SpamDashboardChartPointSerializer(many=True)
    biweekly = SpamDashboardBiweeklySerializer()


# ===== 캠페인 신규 요청자 시계열 (timeseries) =====


class CampaignTimeseriesPointSerializer(serializers.Serializer):
    """시계열 1개 버킷 — 버킷 시작시각(KST) + 그 버킷의 신규 요청자 수."""

    bucket = serializers.DateTimeField(
        help_text="버킷 시작시각 (ISO8601, +09:00). granularity 에 따라 정시/자정 정렬."
    )
    new_requesters = serializers.IntegerField(
        help_text="이 버킷에 '처음' 요청한 사람 수 (사람 단위, 최초 트리거 시점 기준)."
    )


class CampaignTimeseriesTotalsSerializer(serializers.Serializer):
    """시계열 합계 블록."""

    lifetime_unique_requesters = serializers.IntegerField(
        help_text="캠페인 전 기간 고유 요청자 수 (stats people.total 과 동일 정의)."
    )
    window_new_requesters = serializers.IntegerField(
        help_text=(
            "조회 범위(range) 내 신규 요청자 수 = sum(series[].new_requesters). "
            "range=all 이면 lifetime_unique_requesters 와 같다."
        )
    )
    first_request_at = serializers.DateTimeField(
        allow_null=True, help_text="가장 이른 요청 시각 (KST). 요청 없으면 null."
    )
    last_request_at = serializers.DateTimeField(
        allow_null=True,
        help_text="가장 최근 루트 요청 시각 (KST, 반복 댓글 포함 — '진행 여부' 신호). 없으면 null.",
    )


class CampaignTimeseriesSerializer(serializers.Serializer):
    """캠페인 신규 요청자 시계열 응답 (진행 추이 차트용)."""

    campaign_id = serializers.UUIDField(help_text="캠페인 UUID.")
    campaign_status = serializers.CharField(help_text="캠페인 상태 (active/paused/completed 등).")
    is_active = serializers.BooleanField(help_text="현재 active 상태인지.")
    range = serializers.CharField(help_text="조회 범위: all | 24h | 7d.")
    granularity = serializers.CharField(help_text="버킷 단위: day(all·7d) | hour(24h).")
    timezone = serializers.CharField(help_text="버킷 기준 타임존 (Asia/Seoul 고정).")
    totals = CampaignTimeseriesTotalsSerializer()
    series = CampaignTimeseriesPointSerializer(
        many=True,
        help_text="시간순 버킷 배열 (빈 버킷은 0 으로 채워짐). 마지막 버킷은 진행 중(partial).",
    )
    history_complete = serializers.BooleanField(
        help_text=(
            "로그 보존정책이 과거 데이터를 잘라내지 않아 전 기간 집계가 정확한지. "
            "false 면 과거 구간이 불완전할 수 있으니 차트에 안내 배지를 권장."
        )
    )
