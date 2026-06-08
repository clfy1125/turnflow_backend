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
from rest_framework import serializers

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog

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

    accepted_or_after = agg["accepted"] + agg["delivered"] + agg["read"] + agg["failed_no_trace"]
    confirmed_delivered = agg["delivered"] + agg["read"]

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
            "max_sends_per_hour",
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
            "keyword_filter",
            "keyword_mode",
            "message_template",
            "opening_message_template",
            "public_reply_enabled",
            "public_reply_template",
            "public_reply_templates",
            "public_reply_batch_size",
            "public_reply_batch_pause_seconds",
            "follow_gate_enabled",
            "follow_gate_prompt",
            "follow_gate_button_label",
            "follow_gate_retry_message",
            "reward_message_template",
            "gate_trigger_keywords",
            "max_sends_per_hour",
            "total_sent",
            "total_failed",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
            "stats",
        ]
        read_only_fields = fields

    def get_stats(self, obj: AutoDMCampaign) -> dict:
        return _build_stats(obj.dm_logs.all())


# ===== DM 로그 =====


class AdminDMLogListSerializer(serializers.ModelSerializer):
    """DM 발송 로그 목록 (cross-workspace) — 요약."""

    campaign = _CampaignMiniSerializer(read_only=True, help_text="이 로그가 속한 캠페인 (id/name).")

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
        ]
        read_only_fields = fields


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
