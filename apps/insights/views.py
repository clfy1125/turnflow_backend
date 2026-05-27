"""
Instagram Insights API 뷰.

화면별 엔드포인트:

    화면 1 (통합 게시물 테이블)
        GET  /api/v1/insights/workspaces/{workspace_id}/media/

    화면 2 (상세 + 약점 진단)
        GET  /api/v1/insights/workspaces/{workspace_id}/media/{media_id}/
        GET  /api/v1/insights/workspaces/{workspace_id}/media/{media_id}/diagnosis/

    화면 1 보조 (체크박스 다중 선택 → 합산)
        POST /api/v1/insights/workspaces/{workspace_id}/aggregate/

    강제 동기화 (rate-limited, throttle 적용)
        POST /api/v1/insights/workspaces/{workspace_id}/sync-jobs/
        GET  /api/v1/insights/workspaces/{workspace_id}/sync-jobs/{job_id}/

API 호출량 절감:
    - 목록/상세는 모두 DB 캐시(IGMedia + IGMediaInsight) 에서 읽음 → Meta 호출 0
    - 강제 동기화만 사용자 트리거 — 시간당 5회로 throttle
    - 자동 동기화는 Celery beat (apps.insights.tasks)
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.integrations.models import IGAccountConnection
from apps.workspace.models import Membership, Workspace

from .diagnosis import diagnose_media
from .filters import IGMediaFilter
from .models import IGAccountInsight, IGMedia, MediaSyncJob
from .serializers import (
    AggregateRequestSerializer,
    AggregateResponseSerializer,
    AudienceInsightSerializer,
    InsightItemSerializer,
    MediaDetailSerializer,
    MediaListItemSerializer,
    SyncJobCreateSerializer,
    SyncJobSerializer,
)
from .services import aggregate_media_insights, sync_account_audience_insight
from .tasks import run_sync_job


# ─────────────────────────────────────────────────────────────
# 공통 — Workspace 권한 체크
# ─────────────────────────────────────────────────────────────


def _get_workspace_or_403(request, workspace_id):
    """Workspace 조회 + Membership 체크. 미가입 시 403."""
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise NotFound(detail="워크스페이스를 찾을 수 없습니다.")
    if not Membership.objects.filter(workspace=workspace, user=request.user).exists():
        raise PermissionDenied(detail="이 워크스페이스에 접근 권한이 없습니다.")
    return workspace


WORKSPACE_PATH_PARAM = OpenApiParameter(
    name="workspace_id",
    type=str,
    location=OpenApiParameter.PATH,
    description="Workspace UUID. 멤버십 보유자만 접근 가능.",
    required=True,
)

COMMON_ERROR_RESPONSES = {
    401: OpenApiExample(
        "Unauthorized",
        value={
            "success": False,
            "error": {"code": 401, "message": "Authentication credentials were not provided.", "details": {}},
        },
        response_only=True,
        status_codes=["401"],
    ),
    403: OpenApiExample(
        "Forbidden",
        value={
            "success": False,
            "error": {"code": 403, "message": "이 워크스페이스에 접근 권한이 없습니다.", "details": {}},
        },
        response_only=True,
        status_codes=["403"],
    ),
    404: OpenApiExample(
        "Not Found",
        value={
            "success": False,
            "error": {"code": 404, "message": "워크스페이스를 찾을 수 없습니다.", "details": {}},
        },
        response_only=True,
        status_codes=["404"],
    ),
}


# ─────────────────────────────────────────────────────────────
# 화면 1 — 통합 게시물 테이블
# ─────────────────────────────────────────────────────────────


@extend_schema(
    summary="통합 게시물 테이블 조회",
    description=(
        "**화면 1 — 통합 게시물 테이블**\n\n"
        "워크스페이스 내 모든 IG 계정의 게시물(릴스/캐러셀/이미지)을 단일 리스트로 반환합니다. "
        "Meta Graph API 를 직접 호출하지 않고 서버 DB 캐시에서 응답하기 때문에 IG rate limit 영향이 없습니다.\n\n"
        "**호출 시점**: 마케터 로그인 직후 첫 화면. 페이지네이션은 DRF 표준(?page=).\n\n"
        "**가공 지표**: 각 행의 `metrics.engagement_rate`, `metrics.viral_score` 는 서버에서 미리 계산해 둔 값입니다.\n\n"
        "**썸네일/포맷 뱃지**: `media_product_type` 가 FEED/REELS/STORY/AD 중 하나. 프론트에서 아이콘 매핑.\n\n"
        "**필터**: `media_product_type`, `account_id`, `published_after/before`, `has_paid`, `min_reach`, `min_er`.\n"
        "**정렬**: `ordering=-published_at` (기본) / `-insight__engagement_rate` / `-insight__viral_score` / `-insight__reach` 등.\n\n"
        "**신선도**: 각 미디어에 `is_insights_fresh` 가 false 면 곧 자동 동기화 대상. "
        "사용자가 강제 새로고침을 원하면 POST `/sync-jobs/` 호출.\n\n"
        "**수치 결측 사유 (`metrics_unavailable_reason`)**: `metrics.*` 가 모두 비어있을 때 그 원인을 enum 으로 내려준다.\n"
        "- `meta_28d_window`: Meta IG Insights 정책상 게시 후 약 28일이 지나면 비공개 — 영구적, 재시도 무의미\n"
        "- `permission_error`: IG 토큰 권한 부족 (재연동 안내)\n"
        "- `api_error`: 일시적 Graph API 에러 (다음 sync 에서 재시도)\n"
        "- `not_synced`: 아직 한 번도 sync 되지 않음\n"
        "- `null`: 정상 (수치가 채워져 있음)"
    ),
    parameters=[
        WORKSPACE_PATH_PARAM,
        OpenApiParameter(
            name="ordering",
            type=str,
            location=OpenApiParameter.QUERY,
            description="정렬 키. 예: `-published_at`, `-insight__engagement_rate`, `-insight__reach`",
        ),
    ],
    responses={
        200: MediaListItemSerializer(many=True),
        401: dict,
        403: dict,
        404: dict,
    },
    examples=[
        OpenApiExample(
            "응답 예시",
            value={
                "count": 124,
                "next": "https://api/.../media/?page=2",
                "previous": None,
                "results": [
                    {
                        "id": "8f3b5c0e-...",
                        "external_media_id": "17900000000000000",
                        "account_username": "brand_official",
                        "media_type": "VIDEO",
                        "media_product_type": "REELS",
                        "permalink": "https://www.instagram.com/reel/Cxxxxxxx/",
                        "thumbnail_url": "https://...",
                        "media_url": "https://...",
                        "caption": "신제품 출시 🚀",
                        "duration_seconds": 15.0,
                        "published_at": "2026-05-12T09:30:00+09:00",
                        "insights_last_synced_at": "2026-05-14T08:15:00+09:00",
                        "is_insights_fresh": True,
                        "has_paid_data": False,
                        "metrics_unavailable_reason": None,
                        "metrics": {
                            "reach": 34160,
                            "likes": 1250,
                            "comments": 88,
                            "shares": 42,
                            "saved": 240,
                            "total_interactions": 1620,
                            "views": 51230,
                            "follows": 18,
                            "profile_visits": 320,
                            "engagement_rate": 4.74,
                            "viral_score": 0.83,
                        },
                    }
                ],
            },
            response_only=True,
            status_codes=["200"],
        ),
    ],
    tags=["Insights"],
)
class MediaListView(ListAPIView):
    serializer_class = MediaListItemSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_class = IGMediaFilter
    queryset = IGMedia.objects.none()  # spectacular 스키마 추출용 (실제는 get_queryset 사용)
    ordering_fields = (
        "published_at",
        "insight__reach",
        "insight__engagement_rate",
        "insight__viral_score",
        "insight__likes",
        "insight__total_interactions",
    )
    ordering = ("-published_at",)
    search_fields = ("caption", "external_media_id")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return IGMedia.objects.none()
        workspace = _get_workspace_or_403(self.request, self.kwargs["workspace_id"])
        return (
            IGMedia.objects.filter(workspace=workspace)
            .select_related("account", "insight")
        )


# ─────────────────────────────────────────────────────────────
# 화면 2 — 상세 + 진단
# ─────────────────────────────────────────────────────────────


@extend_schema(
    summary="개별 게시물 상세 조회",
    description=(
        "**화면 2 — 개별 게시물 상세**\n\n"
        "특정 게시물의 organic / paid / total 인사이트를 동시에 반환합니다. 프론트는 "
        "[전체] / [오가닉] / [광고] 토글에 따라 동일 응답의 다른 섹션을 노출하면 됩니다.\n\n"
        "**필드 구성**:\n"
        "- `total`: 전체 (= organic + paid)\n"
        "- `organic`: 광고 부스팅 도달을 차감한 순수 수치. 광고 미연동 시 `total` 과 동일.\n"
        "- `paid`: 광고 데이터. 광고 미연동/미부스팅 게시물은 `null`. `paid_available` 로 토글 노출 여부 판단.\n"
        "- `reels`: media_product_type=REELS 일 때만 채워짐 (skip_rate, avg_watch_time 등).\n"
        "- `carousel_children`: CAROUSEL_ALBUM 일 때만. Meta 가 자식 미디어별 인사이트를 제공하지 않으므로 메타데이터만.\n\n"
        "**약점 진단**: 별도 엔드포인트 `/diagnosis/` 사용 (룰 카드 목록 반환)."
    ),
    parameters=[
        WORKSPACE_PATH_PARAM,
        OpenApiParameter(
            name="media_id",
            type=str,
            location=OpenApiParameter.PATH,
            description="IGMedia.id (UUID, 외부 IG media ID 가 아님)",
            required=True,
        ),
    ],
    responses={200: MediaDetailSerializer, 401: dict, 403: dict, 404: dict},
    tags=["Insights"],
)
class MediaDetailView(RetrieveAPIView):
    serializer_class = MediaDetailSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = IGMedia.objects.none()
    lookup_url_kwarg = "media_id"
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return IGMedia.objects.none()
        workspace = _get_workspace_or_403(self.request, self.kwargs["workspace_id"])
        return (
            IGMedia.objects.filter(workspace=workspace)
            .select_related("account", "insight")
        )


@extend_schema(
    summary="게시물 약점 진단 (화면 2/3)",
    description=(
        "**화면 2 약점 진단 + 화면 3 인사이트 매트릭스 통합 엔드포인트**\n\n"
        "지정 게시물에 룰 기반 진단을 실행하여 매칭된 인사이트 카드를 배열로 반환합니다. "
        "LLM 호출 없이 결정론적 룰만 사용하므로 응답이 즉시 반환되며 비용이 발생하지 않습니다.\n\n"
        "**룰 카탈로그**:\n"
        "- `reels_skip_critical` / `reels_skip_warning` — 릴스 3초 훅 (skip_rate)\n"
        "- `reels_low_watch_completion` / `reels_high_watch_completion` — 중반 이탈 / 시청 완료율 우수\n"
        "- `useful_but_heavy` — 높은 저장 + 낮은 공유 (정보성 콘텐츠)\n"
        "- `viral_meme` — 높은 도달 + 낮은 팔로우 전환 (브랜드 연결 약함)\n"
        "- `followers_only` — 도달 정체 + 신규 유저 노출 부족 (proxy: home/explore breakdown 은 v25 미디어 API 미제공)\n"
        "- `top_performer` — ER 5% 이상 (광고 부스팅 후보)\n"
        "- `paid_efficient` — 광고 CTR > 오가닉 ER (예산 증액 권장)\n\n"
        "각 카드 `severity` 는 `info` / `warning` / `critical` 중 하나. 프론트는 색상 매핑."
    ),
    parameters=[
        WORKSPACE_PATH_PARAM,
        OpenApiParameter(
            name="media_id", type=str, location=OpenApiParameter.PATH, required=True
        ),
    ],
    responses={200: InsightItemSerializer(many=True), 401: dict, 403: dict, 404: dict},
    examples=[
        OpenApiExample(
            "응답 예시",
            value=[
                {
                    "id": "reels_skip_critical",
                    "icon": "🛑",
                    "severity": "critical",
                    "title": "초반 3초에서 절반 이상 이탈",
                    "message": "초반 3초 이내에 시청자의 58%가 이탈했습니다. ...",
                    "metric": {"reels_skip_rate": 58.0},
                },
                {
                    "id": "top_performer",
                    "icon": "🚀",
                    "severity": "info",
                    "title": "상위 성과 — 광고 부스팅 후보",
                    "message": "인게이지먼트율이 5.4%로 평균을 크게 상회합니다. ...",
                    "metric": {"engagement_rate": 5.4},
                },
            ],
            response_only=True,
            status_codes=["200"],
        )
    ],
    tags=["Insights"],
)
class MediaDiagnosisView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, workspace_id, media_id):
        workspace = _get_workspace_or_403(request, workspace_id)
        try:
            media = (
                IGMedia.objects.select_related("insight")
                .get(workspace=workspace, id=media_id)
            )
        except IGMedia.DoesNotExist:
            raise NotFound(detail="게시물을 찾을 수 없습니다.")

        insights = diagnose_media(media, getattr(media, "insight", None))
        data = InsightItemSerializer(
            [
                {
                    "id": i.id,
                    "icon": i.icon,
                    "severity": i.severity,
                    "title": i.title,
                    "message": i.message,
                    "metric": i.metric,
                }
                for i in insights
            ],
            many=True,
        ).data
        return Response(data)


# ─────────────────────────────────────────────────────────────
# 화면 1 — 묶어보기 (Aggregate)
# ─────────────────────────────────────────────────────────────


@extend_schema(
    summary="게시물 다중 선택 합산",
    description=(
        "**화면 1 — 캠페인 그룹핑(묶어보기)**\n\n"
        "테이블에서 체크박스로 선택한 게시물들의 인사이트를 서버에서 합산해 반환합니다. "
        "Meta 호출 없이 DB 단일 쿼리로 처리됩니다 (최대 200건).\n\n"
        "**합산 규칙**:\n"
        "- 노출/반응/팔로우/조회는 단순합(Sum).\n"
        "- 도달(reach)은 단순합. 사용자 중복으로 실제 유니크 도달보다 높게 측정될 수 있으며 "
        "`reach_disclaimer` 에 안내 문구가 동봉됩니다 (프론트 툴팁에 그대로 사용).\n"
        "- ER / Viral Score 는 단순 평균 (게시물별 비율의 평균).\n\n"
        "**호출 제한**: media_ids 는 1~200건. 그 이상은 별도 분석 화면을 권장."
    ),
    parameters=[WORKSPACE_PATH_PARAM],
    request=AggregateRequestSerializer,
    responses={200: AggregateResponseSerializer, 400: dict, 401: dict, 403: dict, 404: dict},
    examples=[
        OpenApiExample(
            "요청 예시",
            value={
                "media_ids": [
                    "8f3b5c0e-1111-2222-3333-444455556666",
                    "9a4c6d1f-aaaa-bbbb-cccc-ddddeeeeffff",
                ]
            },
            request_only=True,
        ),
        OpenApiExample(
            "응답 예시",
            value={
                "media_count": 2,
                "reach_sum": 51000,
                "reach_disclaimer": (
                    "도달수는 사용자 중복이 발생할 수 있어, 단순 합산 수치는 실제 유니크 도달보다 높게 측정될 수 있습니다."
                ),
                "likes": 2400,
                "comments": 150,
                "shares": 80,
                "saved": 420,
                "total_interactions": 3050,
                "views": 78000,
                "impressions": None,
                "follows": 35,
                "profile_visits": 510,
                "avg_engagement_rate": 4.96,
                "avg_viral_score": 0.98,
                "paid": {"spend": None, "reach": None, "link_clicks": None},
            },
            response_only=True,
            status_codes=["200"],
        ),
    ],
    tags=["Insights"],
)
class MediaAggregateView(GenericAPIView):
    serializer_class = AggregateRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, workspace_id):
        workspace = _get_workspace_or_403(request, workspace_id)
        req = self.get_serializer(data=request.data)
        req.is_valid(raise_exception=True)
        media_ids = req.validated_data["media_ids"]
        qs = IGMedia.objects.filter(workspace=workspace, id__in=media_ids)
        agg = aggregate_media_insights(qs)
        return Response(AggregateResponseSerializer(agg).data)


# ─────────────────────────────────────────────────────────────
# 강제 동기화 작업
# ─────────────────────────────────────────────────────────────


@extend_schema(
    summary="강제 동기화 작업 생성 (사용자 트리거)",
    description=(
        "**프론트 '지금 새로고침' 버튼이 호출**.\n\n"
        "선택 IG 계정에 대해 Celery 동기화 작업을 생성하고 비동기로 실행합니다. "
        "작업 진행률은 `GET /sync-jobs/{job_id}/` 로 폴링.\n\n"
        "**Scope**:\n"
        "- `metadata_only` — 신규 미디어만 발견 (가장 가벼움)\n"
        "- `insights_recent` — 최근 7일 미디어 인사이트 (기본)\n"
        "- `insights_all` — 전체 미디어 인사이트 (IG quota 다량 소모)\n\n"
        "**Throttle**: 사용자별 시간당 5회. 초과 시 429 응답.\n\n"
        "**중복 방지**: 동일 계정에 진행 중(running/queued) job 이 있으면 그 job 을 그대로 반환."
    ),
    parameters=[WORKSPACE_PATH_PARAM],
    request=SyncJobCreateSerializer,
    responses={201: SyncJobSerializer, 400: dict, 401: dict, 403: dict, 404: dict, 429: dict},
    examples=[
        OpenApiExample(
            "요청 예시",
            value={
                "account_id": "f1e2d3c4-b5a6-7788-99aa-bbccddeeff00",
                "scope": "insights_recent",
            },
            request_only=True,
        )
    ],
    tags=["Insights"],
)
class SyncJobCreateView(GenericAPIView):
    serializer_class = SyncJobCreateSerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "insights_sync"

    def post(self, request, workspace_id):
        workspace = _get_workspace_or_403(request, workspace_id)
        s = self.get_serializer(data=request.data)
        s.is_valid(raise_exception=True)

        try:
            account = IGAccountConnection.objects.get(
                workspace=workspace, id=s.validated_data["account_id"]
            )
        except IGAccountConnection.DoesNotExist:
            raise NotFound(detail="IG 계정 연결을 찾을 수 없습니다.")

        # 중복 방지: 진행 중 job 이 있으면 그것을 반환
        existing = MediaSyncJob.objects.filter(
            account=account, status__in=(MediaSyncJob.Status.QUEUED, MediaSyncJob.Status.RUNNING)
        ).first()
        if existing:
            return Response(SyncJobSerializer(existing).data, status=status.HTTP_200_OK)

        job = MediaSyncJob.objects.create(
            workspace=workspace,
            account=account,
            triggered_by=request.user,
            scope=s.validated_data["scope"],
        )
        run_sync_job.delay(str(job.id))
        return Response(SyncJobSerializer(job).data, status=status.HTTP_201_CREATED)


@extend_schema(
    summary="동기화 작업 상태 조회",
    description=(
        "POST 로 생성한 동기화 작업의 진행률/상태/에러를 폴링.\n\n"
        "**권장 폴링 주기**: 2~5초. 작업이 끝나면 `status` 가 `succeeded` 또는 `failed` 로 전이.\n\n"
        "`progress_pct` 는 processed/total 비율(%). total 이 0 이면 succeeded 시점에 100 으로 채워짐."
    ),
    parameters=[
        WORKSPACE_PATH_PARAM,
        OpenApiParameter(
            name="job_id",
            type=str,
            location=OpenApiParameter.PATH,
            description="MediaSyncJob.id",
            required=True,
        ),
    ],
    responses={200: SyncJobSerializer, 401: dict, 403: dict, 404: dict},
    tags=["Insights"],
)
class SyncJobDetailView(RetrieveAPIView):
    serializer_class = SyncJobSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = MediaSyncJob.objects.none()
    lookup_url_kwarg = "job_id"
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return MediaSyncJob.objects.none()
        workspace = _get_workspace_or_403(self.request, self.kwargs["workspace_id"])
        return MediaSyncJob.objects.filter(workspace=workspace)


# ─────────────────────────────────────────────────────────────
# 계정 단위 청중 인사이트 (follow_type breakdown)
# ─────────────────────────────────────────────────────────────


@extend_schema(
    summary="계정 청중 인사이트 (follow_type breakdown)",
    description=(
        "**화면 3 보조 — 계정 단위 청중 인사이트**\n\n"
        "지정 IG 계정의 최근 N일 도달을 **FOLLOWER / NON_FOLLOWER / UNKNOWN** 으로 쪼개 반환합니다. "
        "Meta v25 의 `breakdown=follow_type` 을 그대로 매핑한 정확한 데이터입니다 (proxy 가 아님).\n\n"
        "**왜 별도 엔드포인트인가**: 동일한 follow_type breakdown 을 게시물 단위로는 Meta 가 제공하지 않습니다. "
        "따라서 화면 3 사이드바에서는 (계정 단위) 이 카드와 (게시물 단위) `/media/{id}/diagnosis/` 카드를 함께 노출하면 "
        "마케터가 '내 계정 전반의 신규 유저 노출 수준' 과 '이 게시물 개별 성과' 를 동시에 볼 수 있습니다.\n\n"
        "**캐싱**: DB 캐시에서 응답 (Celery beat `insights.refresh_account_audience_insights` 가 일 1회 갱신). "
        "`?force=1` 쿼리로 즉시 새로고침 (IG API 1회 호출).\n\n"
        "**카드(cards)**:\n"
        "- `account_followers_dominant` — follower 비중 85% 이상 (고인물 경고)\n"
        "- `account_healthy_acquisition` — 비팔로워 비중 30% 이상 (신규 유입 양호)"
    ),
    parameters=[
        WORKSPACE_PATH_PARAM,
        OpenApiParameter(
            name="account_id",
            type=str,
            location=OpenApiParameter.PATH,
            description="IGAccountConnection.id",
            required=True,
        ),
        OpenApiParameter(
            name="period_days",
            type=int,
            location=OpenApiParameter.QUERY,
            description="조회 기간 (일). 기본 30, 최대 90.",
        ),
        OpenApiParameter(
            name="force",
            type=int,
            location=OpenApiParameter.QUERY,
            description="1 이면 캐시 무시하고 즉시 Meta 호출 (IG quota 소모, 사용자 throttle 권장)",
        ),
    ],
    responses={200: AudienceInsightSerializer, 401: dict, 403: dict, 404: dict},
    examples=[
        OpenApiExample(
            "응답 예시",
            value={
                "account": "f1e2d3c4-b5a6-7788-99aa-bbccddeeff00",
                "period_days": 30,
                "period_start": "2026-04-15",
                "period_end": "2026-05-15",
                "follower_reach": 84000,
                "non_follower_reach": 14000,
                "unknown_reach": 2000,
                "total_reach": 100000,
                "follower_share_pct": 84.0,
                "non_follower_share_pct": 14.0,
                "fetched_at": "2026-05-15T03:00:00+09:00",
                "cards": [
                    {
                        "id": "account_followers_dominant",
                        "icon": "🔍",
                        "severity": "warning",
                        "title": "최근 30일 도달의 84%가 팔로워",
                        "message": "...신규 유저 노출이 부족합니다...",
                        "metric": {
                            "period_days": 30,
                            "total_reach": 100000,
                            "follower_share_pct": 84.0,
                            "non_follower_share_pct": 14.0,
                        },
                    }
                ],
            },
            response_only=True,
            status_codes=["200"],
        )
    ],
    tags=["Insights"],
)
class AccountAudienceInsightView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, workspace_id, account_id):
        workspace = _get_workspace_or_403(request, workspace_id)
        try:
            account = IGAccountConnection.objects.get(workspace=workspace, id=account_id)
        except IGAccountConnection.DoesNotExist:
            raise NotFound(detail="IG 계정 연결을 찾을 수 없습니다.")

        period_days = int(request.query_params.get("period_days") or 30)
        period_days = max(1, min(period_days, 90))
        force = request.query_params.get("force") in ("1", "true", "True")

        ai = (
            IGAccountInsight.objects.filter(account=account, period_days=period_days)
            .order_by("-fetched_at")
            .first()
        )
        if force or ai is None:
            ai = sync_account_audience_insight(account, period_days=period_days) or ai

        if ai is None:
            raise NotFound(
                detail=(
                    "계정 청중 인사이트 데이터가 아직 없습니다. "
                    "Meta 권한(`instagram_business_manage_insights`)이 부여됐는지 확인하세요."
                )
            )
        return Response(AudienceInsightSerializer(ai).data)
