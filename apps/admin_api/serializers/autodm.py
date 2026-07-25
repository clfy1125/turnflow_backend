"""apps/admin_api/serializers/autodm.py — 자동 DM 모니터링(도메인 F) 시리얼라이저.

``/api/v1/admin/auto-dm/`` 및 관련 백오피스 엔드포인트에서 사용하는 cross-workspace
(전역) 읽기 전용 시리얼라이저 모음. 모든 접근은 ``IsAdminUser``(is_staff=True) 권한으로만
허용되며, 워크스페이스 경계를 넘어 전체 캠페인/DM 로그/IG 연동을 조회한다.

보안:
- IG ``access_token`` 등 비밀값은 절대 직렬화하지 않는다. 토큰은 상태/만료/마지막 검증 시각만 노출.

원본 도메인 모델: ``apps.integrations.models`` (IGAccountConnection / AutoDMCampaign / SentDMLog).
캠페인/계정 통계는 ``apps.integrations.serializers.DMVerificationStatsSerializer`` 와 동일한
집계 dict 형태(키 셋)를 따른다.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.integrations.dm_status_groups import GROUP_DISPLAY
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import is_instagram_permalink

# ===== 공용 mini 시리얼라이저 =====


class _OwnerSerializer(serializers.Serializer):
    """워크스페이스 소유자(User) 요약 — id/email 만 노출."""

    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)

    class Meta:
        # 다른 admin 도메인의 동명 _OwnerSerializer 와 OpenAPI 컴포넌트 충돌 방지.
        ref_name = "AdminAutoDMOwner"


class _WorkspaceMiniSerializer(serializers.Serializer):
    """워크스페이스 요약 — id/name."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)

    class Meta:
        ref_name = "AdminAutoDMWorkspaceMini"


class _CampaignMiniSerializer(serializers.Serializer):
    """캠페인 요약 — id/name (DM 로그 목록의 nested 표시용)."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)

    class Meta:
        ref_name = "AdminAutoDMCampaignMini"


# ===== 통계 집계 헬퍼 =====


def _build_stats(queryset) -> dict:
    """``SentDMLog`` 쿼리셋을 ``DMVerificationStatsSerializer`` 와 동일한 dict 로 집계.

    verification_views.stats 의 집계 로직을 그대로 따른다 (전역 범위에서 재사용).
    """
    delivered_or_read = Q(status="delivered") | Q(status="read")
    agg = queryset.aggregate(
        total=Count("id"),
        queued=Count("id", filter=Q(status="queued")),
        submitting=Count("id", filter=Q(status="submitting")),
        accepted=Count("id", filter=Q(status="accepted")),
        delivered=Count("id", filter=Q(status="delivered")),
        read=Count("id", filter=Q(status="read")),
        rate_limited=Count("id", filter=Q(status="rate_limited")),
        failed_token=Count("id", filter=Q(status="failed_token")),
        failed_window=Count("id", filter=Q(status="failed_window")),
        failed_param=Count("id", filter=Q(status="failed_param")),
        failed_no_trace=Count("id", filter=Q(status="failed_no_trace")),
        skipped=Count("id", filter=Q(status="skipped")),
        recovery_pending=Count("id", filter=Q(status="recovery_pending")),
        recovery_delivered=Count("id", filter=Q(status="recovery_delivered")),
        recovery_expired=Count("id", filter=Q(status="recovery_expired")),
        legacy_sent=Count("id", filter=Q(status="sent")),
        legacy_failed=Count("id", filter=Q(status="failed")),
        legacy_failed_api=Count("id", filter=Q(status="failed_api")),
        standalone_total=Count("id", filter=Q(dm_kind="standalone")),
        opening_total=Count("id", filter=Q(dm_kind="opening")),
        opening_delivered=Count("id", filter=Q(dm_kind="opening") & delivered_or_read),
        reward_total=Count("id", filter=Q(dm_kind="reward")),
        reward_delivered=Count("id", filter=Q(dm_kind="reward") & delivered_or_read),
        gate_pending=Count("id", filter=Q(gate_status="pending")),
        gate_passed=Count("id", filter=Q(gate_status="passed")),
        gate_expired=Count("id", filter=Q(gate_status="expired")),
        public_replies_posted=Count("id", filter=~Q(public_reply_id="")),
    )

    accepted_or_after = (
        agg["accepted"]
        + agg["delivered"]
        + agg["read"]
        + agg["failed_no_trace"]
        + agg["recovery_delivered"]  # 복구 재전송 성공 = 실제 도착 (분모·분자 포함)
    )
    confirmed_delivered = agg["delivered"] + agg["read"] + agg["recovery_delivered"]

    delivery_rate = confirmed_delivered / accepted_or_after if accepted_or_after else 0.0
    read_rate = agg["read"] / confirmed_delivered if confirmed_delivered else 0.0
    gate_passthrough_rate = (
        agg["gate_passed"] / agg["opening_delivered"] if agg["opening_delivered"] else 0.0
    )

    agg["delivery_rate"] = round(delivery_rate, 4)
    agg["read_rate"] = round(read_rate, 4)
    agg["gate_passthrough_rate"] = round(gate_passthrough_rate, 4)
    return agg


# ===== 캠페인 =====


class AdminCampaignListSerializer(serializers.ModelSerializer):
    """캠페인 목록 (cross-workspace) — 운영 모니터링용 요약."""

    ig_username = serializers.CharField(
        source="ig_connection.username",
        read_only=True,
        help_text="이 캠페인이 연결된 IG 계정 username.",
    )
    owner = _OwnerSerializer(
        source="ig_connection.workspace.owner",
        read_only=True,
        help_text="IG 계정이 속한 워크스페이스의 소유자(User).",
    )

    class Meta:
        model = AutoDMCampaign
        fields = [
            "id",
            "name",
            "ig_username",
            "owner",
            "status",
            "trigger_type",
            "total_sent",
            "total_failed",
            "total_unconfirmed",
            "created_at",
            "started_at",
        ]
        read_only_fields = fields


class AdminCampaignDetailSerializer(serializers.ModelSerializer):
    """캠페인 상세 (cross-workspace) — 전체 설정 + 누적 발송 통계."""

    ig_connection_id = serializers.UUIDField(
        source="ig_connection.id",
        read_only=True,
        help_text="연결된 IGAccountConnection PK.",
    )
    ig_username = serializers.CharField(source="ig_connection.username", read_only=True)
    owner = _OwnerSerializer(source="ig_connection.workspace.owner", read_only=True)
    media_permalink = serializers.SerializerMethodField(
        help_text=(
            "인스타그램 게시물 영구 링크 — media_url 이 instagram.com permalink 면 그 값, "
            "아니면 빈 문자열. specific_media 캠페인은 생성 시 permalink 를 백필하지만(best-effort), "
            "레거시/조회 실패 캠페인은 빈 값일 수 있음(프론트는 이때 media_id 만 표시). "
            "media_url 은 CDN 이미지 URL 등 permalink 가 아닐 수 있어 이 필드로 분리."
        )
    )
    stats = serializers.SerializerMethodField(
        help_text=(
            "이 캠페인의 dm_logs 를 DMVerificationStatsSerializer 와 동일한 형태로 집계한 dict "
            "(delivery_rate/read_rate/gate_passthrough_rate 포함)."
        )
    )

    class Meta:
        model = AutoDMCampaign
        fields = [
            "id",
            "name",
            "description",
            "ig_connection_id",
            "ig_username",
            "owner",
            "status",
            "trigger_type",
            "media_id",
            "media_url",
            "media_permalink",
            "keyword_filter",
            "keyword_mode",
            "message_template",
            "opening_message_template",
            "public_reply_enabled",
            "public_reply_template",
            "public_reply_templates",
            "public_reply_batch_size",
            "public_reply_batch_pause_seconds",
            "public_reply_limit",
            "public_reply_posted_count",
            "follow_gate_enabled",
            "gate_verify_follow",
            "follow_gate_prompt",
            "follow_gate_button_label",
            "follow_gate_retry_message",
            "reward_message_template",
            "gate_trigger_keywords",
            "link_button_url",
            "link_button_label",
            "link_buttons",
            "total_sent",
            "total_failed",
            "total_unconfirmed",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
            "stats",
        ]
        read_only_fields = fields

    def get_stats(self, obj: AutoDMCampaign) -> dict:
        return _build_stats(obj.dm_logs.all())

    def get_media_permalink(self, obj: AutoDMCampaign) -> str:
        url = obj.media_url or ""
        return url if is_instagram_permalink(url) else ""


# ===== DM 로그 =====


class AdminDMLogListSerializer(serializers.ModelSerializer):
    """DM 발송 로그 목록 (cross-workspace) — 요약."""

    campaign = _CampaignMiniSerializer(read_only=True, help_text="이 로그가 속한 캠페인 (id/name).")
    flow_role = serializers.SerializerMethodField(
        help_text=(
            "플로우 내 역할 (표시용) — opening/retry/reward/standalone. "
            "재안내(retry)는 quick_reply 재첨부를 위해 dm_kind=opening 으로 저장되므로 "
            "dm_kind 만으로는 오프닝과 구분되지 않는다(parent_log 유무로 판정). "
            "수신자 타임라인에서 '왜 이 사람에게 여러 건이 갔나'를 라벨링하는 데 사용."
        )
    )

    class Meta:
        model = SentDMLog
        fields = [
            "id",
            "campaign",
            # 수신자 행(recipients 목록)과 로그를 잇는 키 — recipients → 이 필드로 exact 필터.
            "recipient_user_id",
            "recipient_username",
            "status",
            "dm_kind",
            "flow_role",
            "gate_status",
            "error_code",
            "created_at",
            "delivered_at",
        ]
        read_only_fields = fields

    @extend_schema_field(
        serializers.ChoiceField(choices=["opening", "retry", "reward", "standalone"])
    )
    def get_flow_role(self, obj: SentDMLog) -> str:
        # 단일 소스: apps.integrations.serializers.SentDMLogSerializer.get_flow_role 와 동일 판정.
        if obj.dm_kind == SentDMLog.DMKind.REWARD:
            return "reward"
        if obj.dm_kind == SentDMLog.DMKind.OPENING:
            return "retry" if obj.parent_log_id is not None else "opening"
        return "standalone"


class AdminDMLogDetailSerializer(serializers.ModelSerializer):
    """DM 발송 로그 상세 (cross-workspace) — 디버깅/검증 이력 포함."""

    campaign = _CampaignMiniSerializer(read_only=True)

    class Meta:
        model = SentDMLog
        fields = [
            "id",
            "campaign",
            "recipient_username",
            "status",
            "dm_kind",
            "gate_status",
            "error_code",
            "created_at",
            "delivered_at",
            # 상세 전용
            "comment_id",
            "comment_text",
            "message_sent",
            "error_message",
            "verified_via",
            "retry_count",
            "verification_log",
        ]
        read_only_fields = fields


# ===== DM 수신자(사람) 단위 롤업 =====


class AdminDMRecipientSerializer(serializers.Serializer):
    """수신자(사람) 1명 단위 롤업 — (campaign, recipient_user_id) 그룹당 1행.

    사용자용 ``DMRecipientRollupSerializer`` (integrations) 와 **동일한 status_group 판정**을
    따르되, 어드민 교차-워크스페이스 식별용 nested(campaign/ig/owner)와 오프닝/상호작용
    카운트를 추가한다. 개별 발송 이벤트는 ``GET /admin/auto-dm/logs/?recipient_user_id=`` 로
    펼친다(타임라인).

    행 키는 ``recipient_user_id`` 우선, 비어 있으면(스토리 답장 초기 등) 표시상
    ``recipient_username`` 폴백 — 사용자용 프론트의 ``recipient_user_id || recipient_username``
    폴백과 동일하다.
    """

    recipient_user_id = serializers.CharField(
        allow_blank=True,
        help_text="수신자 Instagram ID (묶음 키). 비어 있을 수 있음(스토리 답장 초기).",
    )
    recipient_username = serializers.CharField(
        allow_blank=True,
        help_text="수신자 username (최신값 best-effort, 미해석 시 user_{IGSID} 폴백).",
    )
    campaign = _CampaignMiniSerializer(help_text="이 수신자가 속한 캠페인 (id/name).")
    ig_connection_id = serializers.UUIDField(help_text="캠페인이 연결된 IGAccountConnection PK.")
    ig_username = serializers.CharField(allow_blank=True, help_text="IG 계정 username.")
    owner = _OwnerSerializer(help_text="캠페인이 속한 워크스페이스 소유자(User).")
    status_group = serializers.ChoiceField(
        choices=list(GROUP_DISPLAY.keys()),
        help_text=(
            "이 수신자의 코스 상태 그룹 — 사용자용 recipients 와 동일 어휘·판정 "
            "(waiting/sent/read/hidden_spam/attention). ?status_group= 로 서버 필터 가능."
        ),
    )
    status_group_display = serializers.CharField(
        help_text="status_group 한국어 표시명 (대기중/전송됨/읽음/숨겨진 요청 · 스팸/확인 필요)."
    )
    sent = serializers.BooleanField(help_text="DM 이 실제 발송됨 (Meta 접수 이상)")
    delivered = serializers.BooleanField(help_text="도착 확인됨 (delivered/read)")
    read = serializers.BooleanField(help_text="읽음 확인됨")
    needs_attention = serializers.BooleanField(
        help_text=(
            "조치 필요(status_group == attention)인가. success-aware — 발송/도착/읽음/복구 성공이 "
            "하나라도 있으면 false. 숨겨진 요청·스팸은 별도 그룹이므로 여기서 false."
        )
    )
    dm_count = serializers.IntegerField(
        help_text="이 사람에게 나간 총 DM 이벤트 수 (오프닝+상호작용+재시도)"
    )
    opening_count = serializers.IntegerField(
        help_text="오프닝 DM 수 (dm_kind ∈ {opening, standalone} — 트리거로 나간 첫 DM)."
    )
    interaction_count = serializers.IntegerField(
        help_text="상호작용 DM 수 (dm_kind = reward — 오프닝 이후 같은 DM창 후속 발송)."
    )
    latest_status = serializers.CharField(
        allow_blank=True, help_text="가장 최근 발송의 SentDMLog.status (배지 보조용)."
    )
    first_sent_at = serializers.DateTimeField(
        allow_null=True, help_text="이 수신자의 첫 DM 로그 생성(발송 시작) 시각."
    )
    last_activity_at = serializers.DateTimeField(allow_null=True, help_text="마지막 활동 시각.")

    class Meta:
        ref_name = "AdminDMRecipient"


# ===== IG 연동 =====


class AdminIGConnectionListSerializer(serializers.ModelSerializer):
    """IG 계정 연동 목록 (cross-workspace).

    보안: ``access_token`` 등 비밀값은 절대 노출하지 않는다. 토큰은 상태/만료/마지막
    검증 시각만 제공한다.
    """

    workspace = _WorkspaceMiniSerializer(
        read_only=True, help_text="이 IG 계정이 속한 워크스페이스 (id/name)."
    )
    owner = _OwnerSerializer(
        source="workspace.owner",
        read_only=True,
        help_text="워크스페이스 소유자(User).",
    )
    campaigns_count = serializers.SerializerMethodField(
        help_text="이 계정에 연결된 자동 DM 캠페인 총 수 (dm_campaigns)."
    )
    recent_delivery_rate_24h = serializers.SerializerMethodField(
        help_text=(
            "최근 24시간 ACCEPTED 진입 건 중 DELIVERED+READ 비율 (0~1). " "집계 대상 없으면 null."
        )
    )

    class Meta:
        model = IGAccountConnection
        fields = [
            "id",
            "username",
            "workspace",
            "owner",
            "status",
            "token_expires_at",
            "last_verified_at",
            "error_message",
            "campaigns_count",
            "recent_delivery_rate_24h",
        ]
        read_only_fields = fields

    def get_campaigns_count(self, obj: IGAccountConnection) -> int:
        return obj.dm_campaigns.count()

    def get_recent_delivery_rate_24h(self, obj: IGAccountConnection) -> float | None:
        since = timezone.now() - timedelta(hours=24)
        agg = SentDMLog.objects.filter(
            campaign__ig_connection=obj, created_at__gte=since
        ).aggregate(
            accepted=Count("id", filter=Q(status="accepted")),
            delivered=Count("id", filter=Q(status="delivered")),
            read=Count("id", filter=Q(status="read")),
            no_trace=Count("id", filter=Q(status="failed_no_trace")),
        )
        denom = agg["accepted"] + agg["delivered"] + agg["read"] + agg["no_trace"]
        num = agg["delivered"] + agg["read"]
        return round(num / denom, 4) if denom else None
