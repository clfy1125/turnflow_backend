"""
Insights API 시리얼라이저.

3개 화면별 시리얼라이저:
    MediaListItemSerializer   — 화면 1 통합 테이블 한 행
    MediaDetailSerializer     — 화면 2 상세 (organic/paid 분리 포함)
    AggregateRequestSerializer / AggregateResponseSerializer — 묶어보기
    InsightItemSerializer     — 화면 2/3 진단 카드
    SyncJobSerializer         — 강제 동기화 작업 상태
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import IGAccountInsight, IGMedia, IGMediaInsight, MediaProductType, MediaSyncJob

# Meta IG Insights 정책: 게시 후 ~28일이 지나면 media insights 가 비공개로 전환된다.
# 정확한 컷오프는 Meta 가 운영적으로 조정 — 안내용 분류에는 28일 기준이 충분.
META_INSIGHTS_WINDOW_DAYS = 28

_METRICS_UNAVAILABLE_REASONS = (
    "meta_28d_window",
    "permission_error",
    "api_error",
    "not_synced",
)


def _derive_metrics_unavailable_reason(media: IGMedia) -> str | None:
    """metrics 가 비어있을 때 그 원인을 enum 코드로 분류.

    프론트가 "왜 도달/좋아요가 -로 보이는지" 를 사용자에게 안내할 수 있도록 한다.
    실제 수치가 하나라도 채워져 있으면 None 반환.
    """
    insight = getattr(media, "insight", None)
    if insight and (insight.reach or insight.likes or insight.views or insight.total_interactions):
        return None

    err = (media.insights_sync_error or "").strip()
    if err.startswith("permission:"):
        if media.published_at and (
            timezone.now() - media.published_at
        ) > timedelta(days=META_INSIGHTS_WINDOW_DAYS):
            return "meta_28d_window"
        return "permission_error"
    if err.startswith("api:"):
        return "api_error"

    if not media.insights_last_synced_at:
        return "not_synced"

    # sync 는 끝났는데 수치가 비어있는 케이스 — 28일 정책일 가능성이 가장 큼
    if media.published_at and (
        timezone.now() - media.published_at
    ) > timedelta(days=META_INSIGHTS_WINDOW_DAYS):
        return "meta_28d_window"
    return None


# ─────────────────────────────────────────────────────────────
# Media 목록 (화면 1)
# ─────────────────────────────────────────────────────────────


class MediaInsightInlineSerializer(serializers.ModelSerializer):
    """리스트 한 행에 직접 박혀나가는 핵심 수치 (NULL 없는 정수만)."""

    class Meta:
        model = IGMediaInsight
        fields = (
            "reach",
            "likes",
            "comments",
            "shares",
            "saved",
            "total_interactions",
            "views",
            "follows",
            "profile_visits",
            "engagement_rate",
            "viral_score",
        )


class MediaListItemSerializer(serializers.ModelSerializer):
    """화면 1 통합 테이블 한 행 — 썸네일 + 포맷 뱃지 + 가공 지표."""

    account_username = serializers.CharField(source="account.username", read_only=True)
    metrics = MediaInsightInlineSerializer(source="insight", read_only=True)
    is_insights_fresh = serializers.SerializerMethodField()
    has_paid_data = serializers.SerializerMethodField()
    metrics_unavailable_reason = serializers.SerializerMethodField()

    class Meta:
        model = IGMedia
        fields = (
            "id",
            "external_media_id",
            "account_username",
            "media_type",
            "media_product_type",
            "permalink",
            "thumbnail_url",
            "media_url",
            "caption",
            "duration_seconds",
            "published_at",
            "insights_last_synced_at",
            "is_insights_fresh",
            "has_paid_data",
            "metrics",
            "metrics_unavailable_reason",
        )
        read_only_fields = fields

    def get_is_insights_fresh(self, obj: IGMedia) -> bool:
        return obj.is_insights_fresh()

    def get_has_paid_data(self, obj: IGMedia) -> bool:
        ins = getattr(obj, "insight", None)
        return bool(ins and ins.has_paid_data)

    @extend_schema_field(
        serializers.ChoiceField(choices=list(_METRICS_UNAVAILABLE_REASONS), allow_null=True)
    )
    def get_metrics_unavailable_reason(self, obj: IGMedia) -> str | None:
        return _derive_metrics_unavailable_reason(obj)


# ─────────────────────────────────────────────────────────────
# Media 상세 (화면 2)
# ─────────────────────────────────────────────────────────────


class OrganicMetricsSerializer(serializers.Serializer):
    """오가닉 토글 시 노출되는 수치."""

    reach = serializers.IntegerField()
    likes = serializers.IntegerField()
    comments = serializers.IntegerField()
    shares = serializers.IntegerField()
    saved = serializers.IntegerField()
    total_interactions = serializers.IntegerField()
    views = serializers.IntegerField(allow_null=True)
    impressions = serializers.IntegerField(allow_null=True)
    follows = serializers.IntegerField(allow_null=True)
    profile_visits = serializers.IntegerField(allow_null=True)
    profile_activity = serializers.IntegerField(allow_null=True)
    engagement_rate = serializers.FloatField(allow_null=True)
    viral_score = serializers.FloatField(allow_null=True)


class PaidMetricsSerializer(serializers.Serializer):
    """광고 토글 시 노출되는 수치 (광고 연동 미완료면 null)."""

    reach = serializers.IntegerField(allow_null=True)
    impressions = serializers.IntegerField(allow_null=True)
    link_clicks = serializers.IntegerField(allow_null=True)
    website_visits = serializers.IntegerField(allow_null=True)
    spend = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    currency = serializers.CharField(allow_blank=True)
    cost_per_website_visit = serializers.FloatField(allow_null=True)


class ReelsAnalysisSerializer(serializers.Serializer):
    """릴스 약점 진단용 raw 수치."""

    skip_rate = serializers.FloatField(allow_null=True)
    avg_watch_time_seconds = serializers.FloatField(allow_null=True)
    total_view_time_seconds = serializers.FloatField(allow_null=True)
    watch_completion_rate = serializers.FloatField(allow_null=True)


class CarouselChildSerializer(serializers.Serializer):
    """캐러셀 자식 미디어 — Meta 가 자식별 인사이트 미제공이라 메타데이터만."""

    id = serializers.CharField()
    media_type = serializers.CharField()
    media_url = serializers.CharField(allow_blank=True)
    thumbnail_url = serializers.CharField(allow_blank=True)


class MediaDetailSerializer(serializers.ModelSerializer):
    """화면 2 상세 페이지 페이로드."""

    account_username = serializers.CharField(source="account.username", read_only=True)
    organic = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    paid = serializers.SerializerMethodField()
    reels = serializers.SerializerMethodField()
    carousel_children = serializers.SerializerMethodField()
    paid_available = serializers.SerializerMethodField()
    metrics_unavailable_reason = serializers.SerializerMethodField()

    class Meta:
        model = IGMedia
        fields = (
            "id",
            "external_media_id",
            "account_username",
            "media_type",
            "media_product_type",
            "caption",
            "permalink",
            "thumbnail_url",
            "media_url",
            "duration_seconds",
            "published_at",
            "insights_last_synced_at",
            "metrics_unavailable_reason",
            "paid_available",
            "total",
            "organic",
            "paid",
            "reels",
            "carousel_children",
        )
        read_only_fields = fields

    @extend_schema_field(
        serializers.ChoiceField(choices=list(_METRICS_UNAVAILABLE_REASONS), allow_null=True)
    )
    def get_metrics_unavailable_reason(self, obj: IGMedia) -> str | None:
        return _derive_metrics_unavailable_reason(obj)

    # ----- helpers -----

    def _insight(self, obj: IGMedia):
        return getattr(obj, "insight", None)

    def get_paid_available(self, obj: IGMedia) -> bool:
        ins = self._insight(obj)
        return bool(ins and ins.has_paid_data)

    @extend_schema_field(OrganicMetricsSerializer(allow_null=True))
    def get_total(self, obj: IGMedia):
        ins = self._insight(obj)
        if ins is None:
            return None
        return OrganicMetricsSerializer(
            {
                "reach": ins.reach,
                "likes": ins.likes,
                "comments": ins.comments,
                "shares": ins.shares,
                "saved": ins.saved,
                "total_interactions": ins.total_interactions,
                "views": ins.views,
                "impressions": ins.impressions,
                "follows": ins.follows,
                "profile_visits": ins.profile_visits,
                "profile_activity": ins.profile_activity,
                "engagement_rate": ins.engagement_rate,
                "viral_score": ins.viral_score,
            }
        ).data

    @extend_schema_field(OrganicMetricsSerializer(allow_null=True))
    def get_organic(self, obj: IGMedia):
        ins = self._insight(obj)
        if ins is None:
            return None
        # 오가닉 = 전체 - paid (paid 데이터 없으면 전체와 동일)
        return OrganicMetricsSerializer(
            {
                "reach": ins.organic_reach,
                "likes": ins.likes,
                "comments": ins.comments,
                "shares": ins.shares,
                "saved": ins.saved,
                "total_interactions": ins.total_interactions,
                "views": ins.views,
                "impressions": ins.impressions,
                "follows": ins.follows,
                "profile_visits": ins.profile_visits,
                "profile_activity": ins.profile_activity,
                "engagement_rate": ins.engagement_rate,
                "viral_score": ins.viral_score,
            }
        ).data

    @extend_schema_field(PaidMetricsSerializer(allow_null=True))
    def get_paid(self, obj: IGMedia):
        ins = self._insight(obj)
        if ins is None or not ins.has_paid_data:
            return None
        spend = float(ins.paid_spend) if ins.paid_spend is not None else None
        visits = ins.paid_website_visits
        cpv = (spend / visits) if (spend and visits) else None
        return PaidMetricsSerializer(
            {
                "reach": ins.paid_reach,
                "impressions": ins.paid_impressions,
                "link_clicks": ins.paid_link_clicks,
                "website_visits": visits,
                "spend": ins.paid_spend,
                "currency": ins.paid_currency or "KRW",
                "cost_per_website_visit": cpv,
            }
        ).data

    @extend_schema_field(ReelsAnalysisSerializer(allow_null=True))
    def get_reels(self, obj: IGMedia):
        if obj.media_product_type != MediaProductType.REELS:
            return None
        ins = self._insight(obj)
        if ins is None:
            return None
        avg_sec = (
            ins.ig_reels_avg_watch_time_ms / 1000.0
            if ins.ig_reels_avg_watch_time_ms
            else None
        )
        total_sec = (
            ins.ig_reels_video_view_total_time_ms / 1000.0
            if ins.ig_reels_video_view_total_time_ms
            else None
        )
        return ReelsAnalysisSerializer(
            {
                "skip_rate": ins.reels_skip_rate,
                "avg_watch_time_seconds": avg_sec,
                "total_view_time_seconds": total_sec,
                "watch_completion_rate": ins.watch_completion_rate,
            }
        ).data

    @extend_schema_field(CarouselChildSerializer(many=True, allow_null=True))
    def get_carousel_children(self, obj: IGMedia):
        if obj.media_type != "CAROUSEL_ALBUM":
            return None
        return CarouselChildSerializer(obj.children or [], many=True).data


# ─────────────────────────────────────────────────────────────
# Insight 진단 카드 (화면 2/3 공통)
# ─────────────────────────────────────────────────────────────


class InsightItemSerializer(serializers.Serializer):
    """diagnosis.Insight dataclass 와 동일 필드."""

    id = serializers.CharField()
    icon = serializers.CharField()
    severity = serializers.ChoiceField(choices=["info", "warning", "critical"])
    title = serializers.CharField()
    message = serializers.CharField()
    metric = serializers.DictField()


# ─────────────────────────────────────────────────────────────
# 묶어보기 (Aggregate)
# ─────────────────────────────────────────────────────────────


class AggregateRequestSerializer(serializers.Serializer):
    """프론트의 체크박스 다중 선택 → 서버 합산 요청."""

    media_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=200,
        help_text="합산할 IGMedia.id (UUID) 리스트. 최대 200건.",
    )


class AggregatePaidSerializer(serializers.Serializer):
    spend = serializers.FloatField(allow_null=True)
    reach = serializers.IntegerField(allow_null=True)
    link_clicks = serializers.IntegerField(allow_null=True)


class AggregateResponseSerializer(serializers.Serializer):
    media_count = serializers.IntegerField()
    reach_sum = serializers.IntegerField()
    reach_disclaimer = serializers.CharField()
    likes = serializers.IntegerField()
    comments = serializers.IntegerField()
    shares = serializers.IntegerField()
    saved = serializers.IntegerField()
    total_interactions = serializers.IntegerField()
    views = serializers.IntegerField(allow_null=True)
    impressions = serializers.IntegerField(allow_null=True)
    follows = serializers.IntegerField(allow_null=True)
    profile_visits = serializers.IntegerField(allow_null=True)
    avg_engagement_rate = serializers.FloatField()
    avg_viral_score = serializers.FloatField()
    paid = AggregatePaidSerializer()


# ─────────────────────────────────────────────────────────────
# Sync Job
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# Account Audience Insight (follow_type breakdown)
# ─────────────────────────────────────────────────────────────


class AudienceInsightSerializer(serializers.ModelSerializer):
    """계정 단위 follow_type 도달 분포 + 진단 카드."""

    total_reach = serializers.IntegerField(read_only=True)
    follower_share_pct = serializers.FloatField(read_only=True, allow_null=True)
    non_follower_share_pct = serializers.FloatField(read_only=True, allow_null=True)
    cards = serializers.SerializerMethodField()

    class Meta:
        model = IGAccountInsight
        fields = (
            "account",
            "period_days",
            "period_start",
            "period_end",
            "follower_reach",
            "non_follower_reach",
            "unknown_reach",
            "total_reach",
            "follower_share_pct",
            "non_follower_share_pct",
            "fetched_at",
            "cards",
        )
        read_only_fields = fields

    @extend_schema_field(InsightItemSerializer(many=True))
    def get_cards(self, obj: IGAccountInsight):
        from .diagnosis import diagnose_account

        cards = diagnose_account(obj)
        return InsightItemSerializer(
            [
                {
                    "id": c.id,
                    "icon": c.icon,
                    "severity": c.severity,
                    "title": c.title,
                    "message": c.message,
                    "metric": c.metric,
                }
                for c in cards
            ],
            many=True,
        ).data


class SyncJobCreateSerializer(serializers.Serializer):
    account_id = serializers.UUIDField(help_text="IGAccountConnection.id")
    scope = serializers.ChoiceField(
        choices=MediaSyncJob.Scope.choices,
        default=MediaSyncJob.Scope.INSIGHTS_RECENT,
        help_text=(
            "metadata_only: 신규 미디어 메타데이터만 / "
            "insights_recent: 최근 7일 인사이트 새로고침 / "
            "insights_all: 전체 미디어 인사이트 (느림, IG quota 소모 큼)"
        ),
    )


class SyncJobSerializer(serializers.ModelSerializer):
    progress_pct = serializers.SerializerMethodField()

    class Meta:
        model = MediaSyncJob
        fields = (
            "id",
            "account",
            "scope",
            "status",
            "total",
            "processed",
            "error_count",
            "error_message",
            "progress_pct",
            "started_at",
            "finished_at",
            "created_at",
        )
        read_only_fields = fields

    def get_progress_pct(self, obj: MediaSyncJob) -> float:
        if not obj.total:
            return 0.0 if obj.status != MediaSyncJob.Status.SUCCEEDED else 100.0
        return round(obj.processed / obj.total * 100, 1)
