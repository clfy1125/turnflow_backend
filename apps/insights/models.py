"""
Instagram Insights 모델

설계 요약 (API 호출량 절감 — 가장 중요한 제약):
    - IG Graph API 는 워크스페이스/계정 단위 rate limit 이 있어 매 요청마다
      Meta 를 호출하면 즉시 throttle 됨.
    - 따라서 미디어 메타데이터 + 인사이트 수치를 모두 DB 에 캐싱하고,
      프론트는 DB 에서만 읽는다. Meta 호출은 Celery 동기화 태스크 1곳으로 집약.
    - 인사이트는 "신선도" 가 시간 경과에 따라 다르게 요구되므로 stale TTL 차등화.

엔티티:
    IGMedia          — IG 미디어 메타데이터 (영구 캐싱, 변하지 않음)
    IGMediaInsight   — 미디어별 인사이트 수치 (organic + paid, 1:1)
    MediaSyncJob     — 사용자 트리거 동기화 작업 상태
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


class MediaProductType(models.TextChoices):
    """
    IG Graph API 의 media_product_type 분류.
    분석/통계 UI 에서 포맷 뱃지(릴스/캐러셀/이미지) 분기 기준.
    """

    FEED = "FEED", "피드"
    REELS = "REELS", "릴스"
    STORY = "STORY", "스토리"
    AD = "AD", "광고"


class MediaType(models.TextChoices):
    """IG Graph API 의 media_type. 캐러셀 여부 판별에 사용."""

    IMAGE = "IMAGE", "이미지"
    VIDEO = "VIDEO", "동영상"
    CAROUSEL_ALBUM = "CAROUSEL_ALBUM", "캐러셀"


class IGMedia(models.Model):
    """
    Instagram 미디어 메타데이터 캐시.

    `external_media_id` (IG ID) 를 동일 계정 안에서 유니크 키로 사용.
    Meta 호출 1회 이후 메타데이터는 거의 불변(캡션 수정만 가능)이므로
    `metadata_last_synced_at` 기준 24h 이상 지나면 재동기화.
    """

    class Meta:
        db_table = "ig_media"
        verbose_name = "Instagram Media"
        verbose_name_plural = "Instagram Media"
        ordering = ["-published_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "external_media_id"],
                name="uniq_ig_media_per_account",
            )
        ]
        indexes = [
            models.Index(fields=["workspace", "-published_at"]),
            models.Index(fields=["account", "-published_at"]),
            models.Index(fields=["workspace", "media_product_type", "-published_at"]),
            models.Index(fields=["external_media_id"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="ig_media",
        verbose_name="Workspace",
    )
    account = models.ForeignKey(
        "integrations.IGAccountConnection",
        on_delete=models.CASCADE,
        related_name="media",
        verbose_name="Instagram Account",
    )

    # IG 원본 식별자
    external_media_id = models.CharField(
        max_length=64,
        verbose_name="Instagram Media ID",
        db_index=True,
        help_text="Meta Graph API 의 미디어 ID",
    )

    # 포맷
    media_type = models.CharField(
        max_length=20,
        choices=MediaType.choices,
        default=MediaType.IMAGE,
        verbose_name="미디어 타입",
    )
    media_product_type = models.CharField(
        max_length=20,
        choices=MediaProductType.choices,
        default=MediaProductType.FEED,
        verbose_name="미디어 제품 타입",
        help_text="FEED / REELS / STORY / AD",
    )

    # 콘텐츠 본문
    caption = models.TextField(blank=True, default="", verbose_name="캡션")
    permalink = models.URLField(max_length=512, blank=True, default="", verbose_name="원본 URL")
    media_url = models.TextField(
        blank=True,
        default="",
        verbose_name="미디어 URL",
        help_text=(
            "IG CDN 의 서명된 일시 URL — 만료될 수 있음. 쿼리스트링 서명이 길어 "
            "VARCHAR 로는 부족 (1500~2000자), TextField 사용."
        ),
    )
    thumbnail_url = models.TextField(
        blank=True, default="", verbose_name="썸네일 URL"
    )

    # 동영상 메타
    duration_seconds = models.FloatField(
        null=True,
        blank=True,
        verbose_name="영상 길이 (초)",
        help_text="REELS / VIDEO 만 값 존재. 평균 시청 완료율 계산에 사용",
    )

    # 캐러셀 자식 (자식 미디어 ID 만 저장 — Meta 가 자식별 인사이트를 제공하지 않음)
    children = models.JSONField(
        default=list,
        blank=True,
        verbose_name="캐러셀 자식 미디어",
        help_text=(
            "캐러셀의 경우 자식 미디어 [{id, media_type, media_url, thumbnail_url}] 리스트. "
            "Meta API 한계로 자식별 좋아요/저장 분리는 불가능. "
            "자식 url 도 IG CDN 서명 쿼리스트링 포함이라 매우 길 수 있음 — JSON 이라 길이 제한 없음."
        ),
    )

    # 게시 시각 (IG 원본 timestamp)
    published_at = models.DateTimeField(
        verbose_name="게시 시각", db_index=True, help_text="IG 가 발급한 timestamp"
    )

    # 동기화 상태
    metadata_last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="메타데이터 마지막 동기화 시각",
    )
    insights_last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="인사이트 마지막 동기화 시각",
    )
    insights_sync_error = models.TextField(
        blank=True,
        default="",
        verbose_name="인사이트 동기화 마지막 에러",
        help_text="403/100 등 에러 분류 — 권한 누락 안내용",
    )

    # 원본 페이로드 (디버깅 / 추가 필드 보존)
    raw_metadata = models.JSONField(default=dict, blank=True, verbose_name="원본 메타데이터")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.media_product_type} {self.external_media_id} ({self.account_id})"

    # ===== 캐싱 정책 =====

    # 인사이트 stale TTL — 게시 후 경과 시간별 차등
    INSIGHT_STALE_TTL_RECENT = timedelta(minutes=15)  # 24h 이내
    INSIGHT_STALE_TTL_WEEK = timedelta(hours=1)  # 7일 이내
    INSIGHT_STALE_TTL_OLD = timedelta(hours=24)  # 7일 이상

    def insight_stale_ttl(self) -> timedelta:
        """미디어 게시 후 경과 시간에 따라 적용할 TTL."""
        age = timezone.now() - self.published_at
        if age <= timedelta(hours=24):
            return self.INSIGHT_STALE_TTL_RECENT
        if age <= timedelta(days=7):
            return self.INSIGHT_STALE_TTL_WEEK
        return self.INSIGHT_STALE_TTL_OLD

    def is_insights_fresh(self) -> bool:
        """현재 시점에서 캐시된 인사이트가 신선한지 (= 재동기화 불필요)."""
        if not self.insights_last_synced_at:
            return False
        return (timezone.now() - self.insights_last_synced_at) < self.insight_stale_ttl()


class IGMediaInsight(models.Model):
    """
    미디어별 인사이트 수치 (organic + paid).

    Meta IG Insights API (`/{media-id}/insights`) 응답을 정규화.
    v25 기준 미디어 타입별 사용 가능 메트릭이 달라 nullable 필드가 많다.

    paid_* 필드는 광고로 부스팅된 게시물에 한해 채워짐. Marketing API 연동이
    별도로 필요하며, 현재 단계에서는 nullable 컬럼만 확보해 둔다.
    """

    class Meta:
        db_table = "ig_media_insights"
        verbose_name = "Instagram Media Insight"
        verbose_name_plural = "Instagram Media Insights"
        indexes = [
            models.Index(fields=["-fetched_at"]),
            models.Index(fields=["-engagement_rate"]),
            models.Index(fields=["-viral_score"]),
        ]

    media = models.OneToOneField(
        IGMedia,
        on_delete=models.CASCADE,
        related_name="insight",
        primary_key=True,
        verbose_name="미디어",
    )

    # ===== Organic (모든 미디어 타입 공통 또는 일부) =====
    reach = models.BigIntegerField(default=0, verbose_name="도달")
    likes = models.BigIntegerField(default=0, verbose_name="좋아요")
    comments = models.BigIntegerField(default=0, verbose_name="댓글")
    shares = models.BigIntegerField(default=0, verbose_name="공유")
    saved = models.BigIntegerField(default=0, verbose_name="저장")
    total_interactions = models.BigIntegerField(
        default=0, verbose_name="총 상호작용", help_text="likes+comments+shares+saved 합계 (Meta 제공)"
    )

    # FEED / REELS 일부
    views = models.BigIntegerField(
        null=True,
        blank=True,
        verbose_name="조회수",
        help_text="REELS=재생 횟수, FEED 이미지=노출 수와 유사",
    )
    impressions = models.BigIntegerField(
        null=True, blank=True, verbose_name="노출 (FEED only)"
    )

    # FEED 전용 — 프로필/팔로우 영향
    follows = models.BigIntegerField(null=True, blank=True, verbose_name="팔로우 전환")
    profile_visits = models.BigIntegerField(null=True, blank=True, verbose_name="프로필 방문")
    profile_activity = models.BigIntegerField(
        null=True,
        blank=True,
        verbose_name="프로필 액션",
        help_text="프로필에서 발생한 클릭 등",
    )

    # ===== REELS 전용 =====
    ig_reels_avg_watch_time_ms = models.BigIntegerField(
        null=True, blank=True, verbose_name="릴스 평균 시청시간 (ms)"
    )
    ig_reels_video_view_total_time_ms = models.BigIntegerField(
        null=True, blank=True, verbose_name="릴스 누적 시청시간 (ms)"
    )
    reels_skip_rate = models.FloatField(
        null=True,
        blank=True,
        verbose_name="릴스 스킵률 (%)",
        help_text="시청자가 첫 3초 이내 이탈한 비율 — '3초 훅' 진단 지표",
    )

    # ===== Paid (광고 부스팅 — Marketing API 연동 후 채워짐) =====
    paid_reach = models.BigIntegerField(null=True, blank=True, verbose_name="유료 도달")
    paid_impressions = models.BigIntegerField(null=True, blank=True, verbose_name="유료 노출")
    paid_link_clicks = models.BigIntegerField(null=True, blank=True, verbose_name="유료 링크 클릭")
    paid_website_visits = models.BigIntegerField(
        null=True, blank=True, verbose_name="유료 웹사이트 방문"
    )
    paid_spend = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="유료 지출 (원)",
    )
    paid_currency = models.CharField(
        max_length=8, blank=True, default="", verbose_name="통화"
    )

    # ===== 가공 지표 (서버 사이드 캐시 — list 쿼리에서 정렬/필터에 사용) =====
    engagement_rate = models.FloatField(
        null=True,
        blank=True,
        verbose_name="인게이지먼트율 (%)",
        help_text="(likes+comments+saved+shares) / reach * 100",
    )
    viral_score = models.FloatField(
        null=True,
        blank=True,
        verbose_name="저장/공유 비율 (%)",
        help_text="(saved+shares) / reach * 100",
    )

    # 원본 페이로드
    raw_payload = models.JSONField(default=dict, blank=True)

    fetched_at = models.DateTimeField(auto_now=True, verbose_name="마지막 fetch 시각")

    def __str__(self) -> str:
        return f"Insight({self.media_id}) reach={self.reach}"

    # ===== 가공 지표 계산 =====

    def recompute_derived(self) -> None:
        """engagement_rate / viral_score 재계산. save() 직전 호출."""
        reach = self.reach or 0
        if reach <= 0:
            self.engagement_rate = None
            self.viral_score = None
            return
        eng = (self.likes or 0) + (self.comments or 0) + (self.saved or 0) + (self.shares or 0)
        viral = (self.saved or 0) + (self.shares or 0)
        self.engagement_rate = round(eng / reach * 100, 2)
        self.viral_score = round(viral / reach * 100, 2)

    def save(self, *args, **kwargs):
        self.recompute_derived()
        super().save(*args, **kwargs)

    @property
    def has_paid_data(self) -> bool:
        """광고 데이터가 1건이라도 채워졌는지 — 토글 노출 분기 기준."""
        return any(
            v is not None
            for v in (
                self.paid_reach,
                self.paid_impressions,
                self.paid_link_clicks,
                self.paid_spend,
            )
        )

    @property
    def organic_reach(self) -> int:
        """오가닉 토글용 — 전체 reach 에서 paid_reach 차감 (없으면 reach 전체)."""
        if self.paid_reach is None:
            return self.reach
        return max(self.reach - self.paid_reach, 0)

    @property
    def watch_completion_rate(self) -> float | None:
        """평균 시청 완료율 — REELS 약점 진단용. (avg_watch / duration)"""
        if not self.ig_reels_avg_watch_time_ms or not self.media.duration_seconds:
            return None
        avg_sec = self.ig_reels_avg_watch_time_ms / 1000.0
        return round(avg_sec / self.media.duration_seconds * 100, 2)


class IGAccountInsight(models.Model):
    """
    계정 단위 인사이트 (follow_type breakdown).

    Meta v25 에서 **미디어 단위로는** follower/non-follower 도달을 쪼갤 수 없지만,
    **계정 단위(/{ig-user-id}/insights)** 에서는 `breakdown=follow_type` 으로
    FOLLOWER / NON_FOLLOWER / UNKNOWN 으로 분리된 reach 를 받을 수 있다.

    이 모델은 한 계정의 "최근 N일 도달 follower 비중" 을 캐싱한다.
    "고인물 콘텐츠" 진단 룰의 근거 데이터로 사용 — 게시물별 정확도는 떨어지지만
    계정 차원에서 신규 유저 노출이 부족한지 판단할 수 있다.

    period_days 별로 (보통 30일) 하나의 row 만 유지 (update_or_create).
    """

    class Meta:
        db_table = "ig_account_insights"
        verbose_name = "Instagram Account Insight"
        verbose_name_plural = "Instagram Account Insights"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "period_days"],
                name="uniq_ig_account_insight_period",
            )
        ]
        indexes = [models.Index(fields=["account", "-fetched_at"])]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "integrations.IGAccountConnection",
        on_delete=models.CASCADE,
        related_name="audience_insights",
    )
    period_days = models.IntegerField(
        default=30,
        verbose_name="기간 (일)",
        help_text="이 인사이트가 커버하는 일수 (기본 30일 — Meta 가 허용하는 최대 90일까지)",
    )
    period_start = models.DateField(verbose_name="기간 시작")
    period_end = models.DateField(verbose_name="기간 종료")

    # reach breakdown by follow_type
    follower_reach = models.BigIntegerField(default=0, verbose_name="팔로워 도달")
    non_follower_reach = models.BigIntegerField(default=0, verbose_name="비팔로워 도달")
    unknown_reach = models.BigIntegerField(default=0, verbose_name="알 수 없음")

    raw_payload = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return (
            f"AccountInsight({self.account_id}, {self.period_days}d) "
            f"f={self.follower_reach} nf={self.non_follower_reach}"
        )

    @property
    def total_reach(self) -> int:
        return (self.follower_reach or 0) + (self.non_follower_reach or 0) + (self.unknown_reach or 0)

    @property
    def follower_share_pct(self) -> float | None:
        """전체 도달 중 팔로워가 차지하는 비율 (%)."""
        total = self.total_reach
        if total <= 0:
            return None
        return round(self.follower_reach / total * 100, 1)

    @property
    def non_follower_share_pct(self) -> float | None:
        total = self.total_reach
        if total <= 0:
            return None
        return round(self.non_follower_reach / total * 100, 1)


class MediaSyncJob(models.Model):
    """
    사용자 트리거 동기화 작업 (강제 새로고침 버튼 등).

    Celery beat 자동 동기화와 분리해서, 사용자가 "지금 당장" 새로고침을 요청한
    이력 + 결과를 추적. 동일 계정 안에서는 진행 중인 job 이 1개만 허용 (rate 보호).
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "대기"
        RUNNING = "running", "실행 중"
        SUCCEEDED = "succeeded", "성공"
        FAILED = "failed", "실패"

    class Scope(models.TextChoices):
        METADATA_ONLY = "metadata_only", "신규 미디어 메타데이터만"
        INSIGHTS_RECENT = "insights_recent", "최근 7일 인사이트"
        INSIGHTS_ALL = "insights_all", "전체 인사이트"

    class Meta:
        db_table = "ig_media_sync_jobs"
        verbose_name = "Media Sync Job"
        verbose_name_plural = "Media Sync Jobs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspace.Workspace", on_delete=models.CASCADE, related_name="ig_sync_jobs"
    )
    account = models.ForeignKey(
        "integrations.IGAccountConnection",
        on_delete=models.CASCADE,
        related_name="sync_jobs",
    )
    triggered_by = models.ForeignKey(
        "authentication.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.INSIGHTS_RECENT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)

    # 진행률 / 결과
    total = models.IntegerField(default=0, verbose_name="처리 대상 수")
    processed = models.IntegerField(default=0, verbose_name="처리 완료 수")
    error_count = models.IntegerField(default=0, verbose_name="에러 수")
    error_message = models.TextField(blank=True, default="")

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"SyncJob({self.scope}) {self.status} {self.processed}/{self.total}"
