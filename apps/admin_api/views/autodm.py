"""apps/admin_api/views/autodm.py — 자동 DM 모니터링(도메인 F) 백오피스 뷰.

``/api/v1/admin/auto-dm/`` 및 관련 엔드포인트. 모든 뷰는 ``IsAdminUser``(is_staff=True)
권한으로만 접근 가능하며, **cross-workspace 전역 범위**로 동작한다 (request.user 의 워크스페이스로
필터링하지 않는다).

제공 기능:
- 캠페인 목록/상세/일시중지/재개
- DM 발송 로그 목록/상세/강제 재시도/수동 재검증
- 전역 DM 발송 검증 통계
- IG 계정 연동 목록 (비밀값 미노출)

재시도/재검증 로직은 ``apps.integrations.verification_views`` 의 retry/reverify 를 충실히
복제하되 워크스페이스 필터를 제거한 전역 버전이다. 모든 mutation 은 성공 후
``log_admin_action`` 으로 감사 로그를 남긴다.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count, Max, Min, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.audit import log_admin_action
from apps.admin_api.dm_error_catalog import INVESTIGATE as POLICY_INVESTIGATE
from apps.admin_api.dm_error_catalog import NORMAL as POLICY_NORMAL
from apps.admin_api.dm_error_catalog import reason_policy_map
from apps.admin_api.dm_error_filters import (
    SCOPE_ALL,
    SCOPES,
    is_known_reason,
    policy_q,
    reason_q,
    scope_q,
)
from apps.admin_api.dm_policy_rollup import (
    AXES,
    AXIS_FOLLOW_UP,
    AXIS_OPENING,
    FOLLOWUP_NOT_SENT_HAVING,
    OPENING_NOT_SENT_HAVING,
    followup_not_sent_annotations,
    opening_not_sent_annotations,
    person_rep_map,
    rep_log_qs,
)
from apps.admin_api.models import AdminActionLog
from apps.admin_api.serializers.autodm import (
    AdminCampaignDetailSerializer,
    AdminCampaignListSerializer,
    AdminDMLogDetailSerializer,
    AdminDMLogListSerializer,
    AdminDMRecipientSerializer,
    AdminIGConnectionListSerializer,
    _build_stats,
)
from apps.integrations.campaign_stats import (
    QUEUE_WAITING_STATUSES,
    SENT_FOR_QUOTA_STATUSES,
    TIMESERIES_RANGES,
    annotate_campaign_people,
    annotate_campaign_stats,
    latest_followup_rows,
    new_requester_timeseries,
)
from apps.integrations.dm_exceptions import DMSendError, DMTransientError
from apps.integrations.dm_status_groups import (
    ATTENTION,
    GROUP_DISPLAY,
    HIDDEN_SPAM,
    READ,
    SENT,
    WAITING,
    status_group_q,
)
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.queue_state import build_queue_state_payload
from apps.integrations.serializers import (
    CampaignTimeseriesSerializer,
    DMQueueStateSerializer,
    DMVerificationStatsSerializer,
)
from apps.integrations.services import InstagramMessagingService

logger = logging.getLogger(__name__)

TAG = "admin-auto-dm"


# ===== 오류 분류 필터 (DM-14 / DM-15) =====
#
# `?error_policy=` · `?error_reason=` · `?error_scope=` 는 로그 목록(이벤트 단위)과
# 수신자 목록(사람 단위)에서 **같은 어휘**로 동작해야 한다 — 운영 대시보드 팝업의
# `보러가기` 가 두 화면 어디로든 착지하기 때문이다. 그래서 검증·Q 조립을 여기 모은다.
#
# 판정은 `dm_error_filters` 가 사전을 SQL 로 컴파일한 것이라 **상한이 없다**
# (11차의 500쌍 OR 체인 → 폐기, DM-15). 마이그레이션도 없다.


def _bad_request(message: str, *, field: str, allowed=None, code: int = 400) -> Response:
    """공통 오류 포맷 (apps/core/exceptions 규약과 동일한 모양)."""
    details: dict = {"field": field}
    if allowed is not None:
        details["allowed"] = allowed
    return Response(
        {"success": False, "error": {"code": code, "message": message, "details": details}},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _classification_error(params) -> Response | None:
    """분류 필터 파라미터 검증 — 잘못된 값이면 400 Response, 정상이면 None.

    허용값을 응답 `details.allowed` 로 함께 준다(프론트가 목록을 하드코딩하지 않도록).
    """
    scope = params.get("error_scope") or SCOPE_ALL
    if scope not in SCOPES:
        return _bad_request(
            f"error_scope 값이 올바르지 않습니다: {scope!r}",
            field="error_scope",
            allowed=list(SCOPES),
        )
    policy = params.get("error_policy")
    if policy and policy != "all" and policy not in (POLICY_INVESTIGATE, POLICY_NORMAL):
        return _bad_request(
            "error_policy 값이 올바르지 않습니다. (all/investigate/normal)",
            field="error_policy",
            allowed=["all", POLICY_INVESTIGATE, POLICY_NORMAL],
        )
    reason = params.get("error_reason")
    if reason and reason != "all" and not is_known_reason(reason):
        return _bad_request(
            f"error_reason 값이 올바르지 않습니다: {reason!r}. "
            "값은 dm_quality.failure_breakdown[].reason / skipped_breakdown[].reason 에서 옵니다.",
            field="error_reason",
            allowed=sorted(reason_policy_map()),
        )
    return None


def _classification_q(params) -> Q | None:
    """분류 필터 파라미터 → 로그 1건에 대한 Q. 지정된 게 없으면 None.

    ``error_reason`` 은 사유 키 자체가 스코프를 담고 있다(오류 사유 vs 건너뜀 사유).
    ``error_policy`` 의 모수는 **오류 8종 + 건너뜀**이며 ``error_scope`` 로 한쪽만 볼 수 있다.
    """
    scope = params.get("error_scope") or SCOPE_ALL
    policy = params.get("error_policy")
    reason = params.get("error_reason")

    parts: list[Q] = []
    if scope != SCOPE_ALL:
        parts.append(scope_q(scope))
    if reason and reason != "all":
        parts.append(reason_q(reason))
    if policy and policy != "all":
        parts.append(policy_q(policy, scope=scope))
    if not parts:
        return None
    combined = parts[0]
    for part in parts[1:]:
        combined &= part
    return combined


# ===== 캠페인 =====


class AdminCampaignListView(generics.ListAPIView):
    """자동 DM 캠페인 목록 (cross-workspace)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminCampaignListSerializer
    queryset = AutoDMCampaign.objects.select_related(
        "ig_connection",
        "ig_connection__workspace",
        "ig_connection__workspace__owner",
    )
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "trigger_type"]
    search_fields = ["name", "ig_connection__username"]
    # DM-1-c: 목록이 **인원**을 보여주므로 인원 기준 정렬을 제공한다 (annotate 필드 재사용).
    # total_sent(발송 이벤트 수)는 하위호환으로 남겨둔다 — 값과 정렬 축을 맞추려면
    # 프론트는 '발송 인원' 컬럼에 people_sent 를 쓸 것.
    ordering_fields = [
        "created_at",
        "started_at",
        "total_sent",
        "people_targets",
        "people_sent",
    ]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        ig_connection_id = self.request.query_params.get("ig_connection_id")
        owner = self.request.query_params.get("owner")
        if ig_connection_id:
            qs = qs.filter(ig_connection_id=ig_connection_id)
        if owner:
            qs = qs.filter(ig_connection__workspace__owner_id=owner)
        # DM-1-b/c: 사람 단위 요약 + 이벤트 단위 enrichment 를 한 번의 LEFT JOIN 으로
        # annotate (목록 N+1 제거). 정의는 상세 stats 와 같은 campaign_stats 모듈.
        return annotate_campaign_people(annotate_campaign_stats(qs))

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 캠페인 목록",
        description="""
## 개요
전체 워크스페이스의 자동 DM 캠페인을 한 곳에서 모니터링합니다. 운영자가 캠페인의 상태/트리거/
누적 발송·실패 수를 빠르게 훑어볼 수 있는 cross-workspace 목록입니다.

## 사용 시나리오
- 운영 대시보드에서 활성/일시정지 캠페인 현황 파악
- 특정 IG 계정 또는 특정 소유자(User)의 캠페인만 필터링하여 점검
- 발송 **인원**(`people.sent`) 기준 상위 캠페인 추적

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 조회 — request.user 의 워크스페이스로 필터링하지 않습니다.
- N+1 방지를 위해 ig_connection / workspace / owner 를 select_related 합니다.
- 기본 정렬 `-created_at`, 표준 PageNumberPagination(page_size=20) 적용 → `{count,next,previous,results}`.

### 사람(인원) 단위 요약 `people` (DM-1-b)
각 항목의 `people` 은 **상세 `stats` 의 `unique_*` 와 같은 정의·같은 계산**입니다
(`apps.integrations.campaign_stats`) — 목록의 `people.sent` 와 상세의 `unique_sent` 는
항상 일치합니다.

| 키 | 상세 stats 대응 | 의미 |
|---|---|---|
| `targets` | `unique_targets` | 전체 대상 인원 (루트 DM 기준) |
| `sent` | `unique_sent` | 실제 발송된 인원 (Meta 접수 이상) |
| `waiting` | `unique_waiting` | 발송 대기/발송 중 인원 |
| `failed` | `unique_failed` | 아무것도 받지 못한 인원 |
| `unconfirmed` | `unique_unconfirmed` | 발송됐으나 도착 미확인 인원 |
| `hidden_spam` | `unique_hidden_spam` | 숨겨진 요청·스팸 인원 (`failed` 의 부분집합) |
| `needs_attention` | `unique_needs_attention_excl_hidden` | 숨김함 제외 '확인 필요' 인원 |
| `sent_rate` | `unique_sent_rate` | `sent / targets` (0~1) |

불변식 `targets == sent + waiting + failed`.

- **`total_sent` / `total_failed` / `total_unconfirmed` 는 발송 *이벤트* 수**(모델
  비정규화 카운터)라 follow-gate 캠페인에서 인원보다 큽니다(1명 = 오프닝+리워드 2건 이상).
  하위호환 폴백용으로 유지하며, 화면 표기는 `people.*` 를 쓰세요.
- `delivered_count` / `delivery_rate` / `last_sent_at` 은 이벤트 단위 배송 지표입니다.
  `delivery_rate` 는 하드실패가 분모에서 빠져 100% 로 부풀 수 있으니 헤드라인에는
  `people.sent_rate` 를 쓰세요.

## 주의사항
- `owner` 는 IG 계정이 속한 워크스페이스의 소유자(User) PK 로 필터합니다.
- IG access_token 등 비밀값은 노출되지 않습니다.
        """,
        parameters=[
            OpenApiParameter(
                "status",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="캠페인 상태 (active/paused/completed/inactive).",
            ),
            OpenApiParameter(
                "trigger_type",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="트리거 종류 (specific_media/any_media/next_media/story_reply).",
            ),
            OpenApiParameter(
                "ig_connection_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="특정 IG 계정 연동(UUID)의 캠페인만 필터.",
            ),
            OpenApiParameter(
                "owner",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="워크스페이스 소유자(User) PK 로 필터.",
            ),
            OpenApiParameter(
                "search",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="캠페인 이름 또는 IG username 부분일치 검색.",
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="정렬 — created_at/started_at/**people_targets**/**people_sent**"
                "/total_sent (`-` 접두 내림차순). '발송 인원' 컬럼 정렬은 `people_sent`, "
                "'전체 대상' 은 `people_targets` 를 쓰세요. total_sent 는 발송 **이벤트** "
                "수 기준이라 인원 표기와 축이 다릅니다(하위호환용).",
            ),
            OpenApiParameter(
                "page",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="페이지 번호 (page_size=20).",
            ),
        ],
        responses={
            200: AdminCampaignListSerializer(many=True),
            400: OpenApiResponse(description="잘못된 쿼리 파라미터"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "목록 응답 예시",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": "8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa",
                            "name": "신상 런칭 자동 DM",
                            "ig_username": "my_brand",
                            "owner": {"id": 7, "email": "owner@example.com"},
                            "status": "active",
                            "trigger_type": "specific_media",
                            "people": {
                                "targets": 827,
                                "sent": 696,
                                "waiting": 0,
                                "failed": 131,
                                "unconfirmed": 0,
                                "hidden_spam": 98,
                                "needs_attention": 33,
                                "sent_rate": 0.8416,
                            },
                            "delivered_count": 696,
                            "delivery_rate": 0.9993,
                            "last_sent_at": "2026-07-27T18:22:00+09:00",
                            "total_sent": 1280,
                            "total_failed": 3,
                            "total_unconfirmed": 0,
                            "created_at": "2026-05-01T09:00:00+09:00",
                            "started_at": "2026-05-01T09:05:00+09:00",
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdminCampaignDetailView(generics.RetrieveAPIView):
    """자동 DM 캠페인 상세 (cross-workspace)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminCampaignDetailSerializer
    queryset = AutoDMCampaign.objects.select_related(
        "ig_connection",
        "ig_connection__workspace",
        "ig_connection__workspace__owner",
    )

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 캠페인 상세",
        description="""
## 개요
단일 자동 DM 캠페인의 전체 설정과 누적 발송 통계(stats)를 반환합니다.

## 사용 시나리오
- 운영자가 특정 캠페인의 키워드/Follow-gate/공개답글 설정을 검토할 때
- 캠페인의 도착률(delivery_rate)·gate 통과율 등 품질 지표 확인

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 조회 — 워크스페이스 멤버십을 검사하지 않습니다.
- `stats` 는 이 캠페인의 dm_logs 를 DMVerificationStatsSerializer 와 동일 형태로 집계합니다.

## 주의사항
- IG access_token 등 비밀값은 노출되지 않습니다.
- `media_url` 은 참고용이며 IG CDN 서명 URL 특성상 만료될 수 있습니다.
        """,
        parameters=[
            OpenApiParameter(
                "pk",
                str,
                OpenApiParameter.PATH,
                description="캠페인 UUID.",
            ),
        ],
        responses={
            200: AdminCampaignDetailSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="캠페인 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdminCampaignPauseView(APIView):
    """캠페인 일시중지 (ACTIVE → PAUSED)."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 캠페인 일시중지",
        description="""
## 개요
활성(active) 상태의 캠페인을 일시중지(paused) 합니다.

## 사용 시나리오
- 스팸 신고/정책 위반 의심 캠페인을 운영자가 즉시 멈출 때
- 사용자 요청/장애 대응으로 발송을 중단해야 할 때

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 현재 상태가 `active` 가 아니면 409 를 반환합니다 (멱등성/오작동 방지).
- 성공 시 status=paused 로 저장하고 `campaign.pause` 감사 로그를 남깁니다.
- 본문(request body) 없음.

## 주의사항
- 일시중지는 신규 트리거 발송만 멈춥니다. 이미 큐에 들어간 in-flight 로그는 별도입니다.
- 재개는 `POST .../resume/` 사용.
        """,
        request=None,
        responses={
            200: OpenApiResponse(description="일시중지 완료"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="캠페인 없음"),
            409: OpenApiResponse(description="활성 상태가 아니어서 일시중지 불가"),
        },
        examples=[
            OpenApiExample(
                "성공 응답",
                value={"id": "8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa", "status": "paused"},
                response_only=True,
            )
        ],
    )
    def post(self, request, pk):
        campaign = get_object_or_404(AutoDMCampaign, pk=pk)
        if campaign.status != AutoDMCampaign.Status.ACTIVE:
            return Response(
                {"detail": "활성 상태 캠페인만 일시중지할 수 있습니다."},
                status=status.HTTP_409_CONFLICT,
            )

        before = campaign.status
        campaign.status = AutoDMCampaign.Status.PAUSED
        campaign.save(update_fields=["status", "updated_at"])

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.CAMPAIGN_PAUSE,
            target_type="campaign",
            target_id=campaign.pk,
            target_repr=campaign.name,
            changes={"status": {"before": before, "after": campaign.status}},
        )
        logger.info(
            "[admin-auto-dm] req=%s campaign paused id=%s",
            getattr(request, "id", ""),
            campaign.pk,
        )
        return Response({"id": str(campaign.id), "status": campaign.status})


class AdminCampaignResumeView(APIView):
    """캠페인 재개 (PAUSED → ACTIVE)."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 캠페인 재개",
        description="""
## 개요
일시중지(paused) 상태의 캠페인을 다시 활성(active) 으로 전환합니다.

## 사용 시나리오
- 점검/장애 대응이 끝난 캠페인의 발송을 재개할 때

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 현재 상태가 `paused` 가 아니면 409 를 반환합니다.
- 성공 시 status=active 로 저장하고 `campaign.resume` 감사 로그를 남깁니다.
- 본문(request body) 없음.

## 주의사항
- completed/inactive 상태에서는 재개할 수 없습니다 (409).
        """,
        request=None,
        responses={
            200: OpenApiResponse(description="재개 완료"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="캠페인 없음"),
            409: OpenApiResponse(description="일시중지 상태가 아니어서 재개 불가"),
        },
        examples=[
            OpenApiExample(
                "성공 응답",
                value={"id": "8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa", "status": "active"},
                response_only=True,
            )
        ],
    )
    def post(self, request, pk):
        campaign = get_object_or_404(AutoDMCampaign, pk=pk)
        if campaign.status != AutoDMCampaign.Status.PAUSED:
            return Response(
                {"detail": "일시정지 상태 캠페인만 재개할 수 있습니다."},
                status=status.HTTP_409_CONFLICT,
            )

        # ★ '한 게시물 = 활성 캠페인 1개' 불변식 — 사용자 경로(create/resume)와 동일하게
        #   관리자 재개도 같은 게시물에 이미 활성 캠페인이 있으면 거부한다(중복 opening DM 방지).
        conflict = AutoDMCampaign.find_active_conflict(
            ig_connection_id=campaign.ig_connection_id,
            media_id=campaign.media_id,
            trigger_type=campaign.trigger_type,
            exclude_id=campaign.id,
        )
        if conflict is not None:
            return Response(
                {
                    "detail": "같은 게시물에 이미 활성 캠페인이 있어 재개할 수 없습니다. "
                    "기존 활성 캠페인을 먼저 일시정지하세요.",
                    "code": "duplicate_active_campaign",
                    "conflict_campaign_id": str(conflict.id),
                },
                status=status.HTTP_409_CONFLICT,
            )

        before = campaign.status
        campaign.status = AutoDMCampaign.Status.ACTIVE
        campaign.save(update_fields=["status", "updated_at"])

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.CAMPAIGN_RESUME,
            target_type="campaign",
            target_id=campaign.pk,
            target_repr=campaign.name,
            changes={"status": {"before": before, "after": campaign.status}},
        )
        logger.info(
            "[admin-auto-dm] req=%s campaign resumed id=%s",
            getattr(request, "id", ""),
            campaign.pk,
        )
        return Response({"id": str(campaign.id), "status": campaign.status})


class AdminCampaignQueueStateView(APIView):
    """캠페인 순차 발송 큐 현황 (cross-workspace, DM-3)."""

    permission_classes = [IsAdminUser]
    serializer_class = DMQueueStateSerializer

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 캠페인 큐 현황",
        description="""
## 개요
"지금 이 캠페인이 얼마나 대기 중인지"를 어드민에서 봅니다. 유저 콘솔의
`GET /integrations/dm-verification/queue-state/?campaign_id=` 와 **응답 스키마·집계가
완전히 동일**하며(`apps.integrations.queue_state.build_queue_state_payload` 단일 소스),
워크스페이스 멤버십 대신 `IsAdminUser` 로만 게이트합니다 — 어드민은 교차-워크스페이스로
봐야 하므로 유저 경로를 그대로 부를 수 없습니다.

## 사용 시나리오
- 캠페인 상세에서 대기 인원/ETA 확인 (5~10초 폴링)
- **`blocking_reason` 감시** — 고객 문의 전에 정지 상태를 먼저 발견:
  - `action_block_cooldown`: Meta Action Block 쿨다운 중
    (`action_block_cooldown_seconds` 로 잔여 시간)
  - `monthly_quota_reached`: 월 DM 한도 소진 (대기 건이 있는데 못 나감 → 업셀 신호)
  - `null`: 차단 없음

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- `gauge` 는 **발송 이벤트** 단위, `people` 은 **사람** 단위입니다. `people` 은
  `campaign_stats.people_rollup` 이라 캠페인 상세 `stats` 의 `unique_*`(DM-1) 와 정의가
  같아 두 화면이 자동으로 정합합니다 (`total = sent + waiting + failed` 항등).
- `account_waiting` / `ahead_of_this_campaign` 은 **계정(IG 연동) 단위** — 같은 계정의 다른
  캠페인 대기 건이 앞에 있으면 그만큼 늦어집니다(페이서가 계정 공유 포인터를 씀).
- `eta_*` 는 확정 슬롯(next_retry_at)과 미클레임 추정의 합성이며
  `eta_is_estimate=true` 면 추정이 섞였다는 뜻입니다. 대기 0 이면 `eta_seconds=0`,
  `eta_finish_at=null`.
- 읽기 전용 — AdminActionLog 감사 기록 없음.

## 주의사항
- 캠페인 UUID 가 없으면 404.
        """,
        parameters=[
            OpenApiParameter("pk", str, OpenApiParameter.PATH, description="캠페인 UUID."),
        ],
        responses={
            200: DMQueueStateSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="캠페인 없음"),
        },
    )
    def get(self, request, pk):
        campaign = get_object_or_404(
            AutoDMCampaign.objects.select_related("ig_connection__workspace__owner"), pk=pk
        )
        payload = build_queue_state_payload(campaign.ig_connection, campaign)
        return Response(DMQueueStateSerializer(payload).data)


class AdminCampaignTimeseriesView(APIView):
    """캠페인 신규 요청자 시계열 (cross-workspace, DM-3)."""

    permission_classes = [IsAdminUser]
    serializer_class = CampaignTimeseriesSerializer

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 캠페인 신규 요청자 시계열",
        description="""
## 개요
캠페인의 **신규 요청자**(그 버킷에 처음 트리거한 사람 수) 시계열입니다. 유저 콘솔의
`GET /integrations/auto-dm/campaigns/{id}/timeseries/` 와 **응답 스키마·집계가 동일**
(`campaign_stats.new_requester_timeseries` 단일 소스)하며 워크스페이스 필터만 없습니다.

## 사용 시나리오
- 캠페인 상세의 진행/모멘텀 차트 (아직 유입이 있는지, 언제 꺾였는지)

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- **사람 단위**: 한 사람의 최초 루트 DM 시각을 요청 시점으로 보고, 같은 사람이 여러 번
  댓글을 달아도 최초 1회만 셉니다 → `range=all` 이면
  `Σ series[].new_requesters == totals.lifetime_unique_requesters` 이고 이 값은
  캠페인 상세 `stats.unique_targets` 와 같은 정의입니다.
- 버킷: KST(Asia/Seoul) 벽시계 절단 — `all`·`7d`=일, `24h`=시간. 빈 구간 제로필.
- `history_complete=false` 면 로그 보존정책으로 과거 구간이 잘렸다는 뜻입니다.

## 주의사항
- `range` 는 `all`(기본) / `24h` / `7d` 만 허용 — 그 외 값은 **400**.
        """,
        parameters=[
            OpenApiParameter("pk", str, OpenApiParameter.PATH, description="캠페인 UUID."),
            OpenApiParameter(
                "range",
                str,
                OpenApiParameter.QUERY,
                required=False,
                enum=sorted(TIMESERIES_RANGES),
                description="집계 범위 (기본 all). all·7d=일 버킷 / 24h=시간 버킷.",
            ),
        ],
        responses={
            200: CampaignTimeseriesSerializer,
            400: OpenApiResponse(description="range 값 오류 (프로젝트 에러 포맷)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="캠페인 없음"),
        },
    )
    def get(self, request, pk):
        range_key = request.query_params.get("range", "all")
        if range_key not in TIMESERIES_RANGES:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": f"잘못된 range 값입니다: {range_key!r}",
                        "details": {"allowed": sorted(TIMESERIES_RANGES)},
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        campaign = get_object_or_404(AutoDMCampaign, pk=pk)
        data = new_requester_timeseries(campaign, range_key)
        data.update(
            campaign_id=campaign.id,
            campaign_status=campaign.status,
            is_active=campaign.status == AutoDMCampaign.Status.ACTIVE,
        )
        return Response(CampaignTimeseriesSerializer(data).data)


# ===== DM 로그 =====


class AdminDMLogListView(generics.ListAPIView):
    """DM 발송 로그 목록 (cross-workspace)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminDMLogListSerializer
    queryset = SentDMLog.objects.select_related("campaign")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "dm_kind", "gate_status"]
    ordering_fields = ["created_at", "delivered_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        campaign_id = params.get("campaign_id")
        recipient = params.get("recipient")
        ig_connection_id = params.get("ig_connection_id")
        since = params.get("since")
        # 수신자 타임라인 펼침용 exact 필터 — recipients 목록의 한 행을 클릭했을 때
        # 그 사람에게 간 모든 발송을 시간순으로 조회. recipient(부분일치)와 별개 파라미터.
        recipient_user_id = params.get("recipient_user_id")
        recipient_username = params.get("recipient_username")
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        if recipient:
            qs = qs.filter(recipient_username__icontains=recipient)
        if recipient_user_id:
            qs = qs.filter(recipient_user_id=recipient_user_id)
        if recipient_username:
            qs = qs.filter(recipient_username=recipient_username)
        if ig_connection_id:
            qs = qs.filter(campaign__ig_connection_id=ig_connection_id)
        if since:
            qs = qs.filter(created_at__gte=since)
        # DM-14/15 — 운영 대시보드 팝업의 `보러가기` 착지점. 이벤트 단위라
        # failure_breakdown[].count / skipped_breakdown[].count 와 그대로 대응한다.
        classification = _classification_q(params)
        if classification is not None:
            qs = qs.filter(classification)
        return qs

    def list(self, request, *args, **kwargs):
        # ListAPIView 는 get_queryset 에서 400 을 낼 수 없어 여기서 먼저 검증한다
        # (잘못된 값을 조용히 무시하면 "필터했는데 전체가 나온다"가 된다).
        invalid = _classification_error(request.query_params)
        if invalid is not None:
            return invalid
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=[TAG],
        summary="[관리자] DM 로그 목록",
        description="""
## 개요
전체 워크스페이스의 DM 발송 로그를 조회합니다. ACCEPTED/DELIVERED/READ 및 각종 실패 상태를
모두 노출하는 cross-workspace 모니터링 목록입니다.

## 사용 시나리오
- 발송 실패(에러 코드별) 건 디버깅
- 특정 수신자/캠페인/IG 계정의 발송 이력 추적
- 특정 시각 이후 발송된 로그만 조회

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 조회 — 워크스페이스로 필터링하지 않습니다.
- campaign 을 select_related 하여 N+1 을 방지합니다.
- 기본 정렬 `-created_at`, 표준 PageNumberPagination(page_size=20).

## 주의사항
- `recipient` 는 수신자 username 부분일치(icontains) 입니다.
- `recipient_user_id` / `recipient_username` 는 **정확일치(exact)** 로, 수신자 목록
  (`GET /admin/auto-dm/recipients/`)의 한 행을 클릭해 그 사람의 발송 타임라인을 펼칠 때
  사용합니다 (부분일치 `recipient` 와 별개 파라미터). `recipient_user_id` 가 1순위 키이며
  비어 있는 수신자는 `recipient_username` exact 로 폴백하세요.
- `since` 는 ISO datetime (예: `2026-05-01T00:00:00Z`).

## 오류 분류 드릴다운 (DM-14 / DM-15)
운영 대시보드 팝업의 `보러가기` 착지점입니다. 이 목록은 **이벤트(발송 1건) 단위**라
`dm_quality` 의 두 표와 그대로 대응합니다.

| 파라미터 | 값의 출처 | 대응 |
|---|---|---|
| `?error_reason=` | `failure_breakdown[].reason` · `skipped_breakdown[].reason` | 그 행(들)의 `count` 합 |
| `?error_policy=` | `investigate` / `normal` | Σ 같은 policy 행들의 `count` |
| `?error_scope=` | `all`(기본) / `error` / `skipped` | 오류 8종만 / 건너뜀만 |

- **상한이 없습니다.** 사유·분류 판정을 SQL 로 컴파일하므로 전사 범위에서 ⚪ 전체를 훑어도
  400 이 나지 않습니다 (11차의 500쌍 상한은 폐기).
- `error_policy` 의 모수는 **오류 8종 + 건너뜀**입니다. 성공·진행 중 로그는 어느 쪽에도
  들어가지 않습니다 (`normal` 을 눌렀을 때 도착한 DM 전부가 딸려 나오면 무의미하므로).
  팝업에 건너뜀 표를 그리지 않는다면 `&error_scope=error` 로 모수를 맞추세요.
- `error_reason` 은 사유 키 자체가 스코프를 담습니다 — 건너뜀 사유를 주면 자동으로
  `status=skipped` 안에서만 찾습니다.
- 셋을 함께 주면 AND 입니다. 잘못된 값은 조용히 무시하지 않고 **400**(`details.field`).
        """,
        parameters=[
            OpenApiParameter(
                "status",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="발송 상태 (queued/submitting/accepted/delivered/read/failed_* 등).",
            ),
            OpenApiParameter(
                "error_reason",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="DM-14 — 사유 머신 키로 필터. 값은 `dm_quality.failure_breakdown[].reason` "
                "또는 `skipped_breakdown[].reason` 을 그대로 싣는다(한국어 문구·code 로 필터하지 말 것 — "
                "문구는 다듬어지고, 한 사유가 code 여러 조합으로 온다). 미등록 값은 400.",
            ),
            OpenApiParameter(
                "error_policy",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="DM-15 — 분류로 필터: all(기본)/investigate(🔴 조사 필요)/normal(⚪ 자동 처리). "
                "모수는 오류 8종 + 건너뜀이며 **상한 없음**.",
            ),
            OpenApiParameter(
                "error_scope",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="분류 필터의 모수: all(기본 — 오류+건너뜀)/error(오류 8종 = "
                "failure_breakdown 모수)/skipped(건너뜀 = skipped_breakdown 모수).",
            ),
            OpenApiParameter(
                "dm_kind",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="DM 유형 (standalone/opening/reward).",
            ),
            OpenApiParameter(
                "gate_status",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="Follow-gate 상태 (none/pending/passed/expired).",
            ),
            OpenApiParameter(
                "campaign_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="특정 캠페인(UUID)의 로그만 필터.",
            ),
            OpenApiParameter(
                "recipient",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="수신자 username 부분일치(icontains) 검색.",
            ),
            OpenApiParameter(
                "recipient_user_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="수신자 Instagram ID 정확일치 — 수신자 타임라인 펼침용 1순위 키.",
            ),
            OpenApiParameter(
                "recipient_username",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="수신자 username 정확일치(exact) — recipient_user_id 가 빈 수신자 폴백. "
                "부분일치 검색은 `recipient` 사용.",
            ),
            OpenApiParameter(
                "ig_connection_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="특정 IG 계정 연동(UUID)의 로그만 필터.",
            ),
            OpenApiParameter(
                "since",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="이 ISO datetime 이후 생성된 로그만.",
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="정렬 (created_at/delivered_at, `-` 접두 내림차순).",
            ),
            OpenApiParameter(
                "page",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="페이지 번호 (page_size=20).",
            ),
        ],
        responses={
            200: AdminDMLogListSerializer(many=True),
            400: OpenApiResponse(description="잘못된 쿼리 파라미터"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "목록 응답 예시",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": "f0e1d2c3-2222-4b3c-8d4e-bbbbbbbbbbbb",
                            "campaign": {
                                "id": "8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa",
                                "name": "신상 런칭 자동 DM",
                            },
                            "recipient_user_id": "17841400000000001",
                            "recipient_username": "buyer01",
                            "status": "delivered",
                            "dm_kind": "opening",
                            "flow_role": "opening",
                            "gate_status": "passed",
                            "error_code": "",
                            "created_at": "2026-05-02T10:00:00+09:00",
                            "delivered_at": "2026-05-02T10:00:03+09:00",
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdminDMLogDetailView(generics.RetrieveAPIView):
    """DM 발송 로그 상세 (cross-workspace)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminDMLogDetailSerializer
    queryset = SentDMLog.objects.select_related("campaign")

    @extend_schema(
        tags=[TAG],
        summary="[관리자] DM 로그 상세",
        description="""
## 개요
단일 DM 발송 로그의 상세 정보를 반환합니다. 댓글 내용, 발송 메시지, 에러 메시지,
검증 이력(verification_log)까지 디버깅에 필요한 전체 필드를 포함합니다.

## 사용 시나리오
- 실패 건의 원인(에러 코드/메시지) 정밀 분석
- echo/conv_api 검증 경로 및 재시도 횟수 확인

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 조회 — 워크스페이스 멤버십을 검사하지 않습니다.

### 오류 원인·조치 (DM-2)
`error_subcode` + `error_title` / `error_cause` / `error_action` / `recoverable` 이 함께
내려갑니다. 판정은 운영 대시보드 `failure_breakdown` 과 **같은 서버 사전**
(`dm_error_catalog`, `(code,subcode)` → `(code,status)` → `(code)` → `status` 4단 폴백)이라
로그 1건을 열었을 때 대시보드로 돌아가 코드를 대조할 필요가 없습니다.
사전에 없는 조합은 네 필드가 **빈 문자열**(recoverable=false)이니 프론트 로컬 사전으로
폴백하세요. 새 코드가 나와도 사전이 서버에 있어 프론트 배포는 필요 없습니다.

- `error_subcode` 는 사전 판정 1순위 키입니다 — `100/2534025`(숨김함 유입, **복구 대상**)와
  `100/2534022`(윈도우 만료, 정상 실패)는 조치가 정반대라 subcode 없이는 구분할 수 없습니다.
- `recoverable=true` → 재발송/재검증 버튼 노출 (failed_no_trace·recovery_*·failed_param@2534025).

## 주의사항
- comment_text/message_sent 는 개인정보를 포함할 수 있으므로 취급에 유의하세요.
        """,
        parameters=[
            OpenApiParameter(
                "pk",
                str,
                OpenApiParameter.PATH,
                description="DM 로그 UUID.",
            ),
        ],
        responses={
            200: AdminDMLogDetailSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="로그 없음"),
        },
        examples=[
            OpenApiExample(
                "숨김함 유입(복구 대상) 로그 상세",
                response_only=True,
                value={
                    "id": "5b1f0c2e-0000-4a00-9c00-000000000001",
                    "campaign": {
                        "id": "8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa",
                        "name": "여름 공구 오픈",
                    },
                    "recipient_username": "buyer_a",
                    "status": "recovery_pending",
                    "error_code": "100",
                    "error_subcode": "2534025",
                    "error_title": "숨겨진 요청 · 스팸함 유입",
                    "error_cause": "수신자가 아직 팔로워가 아니라 DM 채널이 열려 있지 않아, "
                    "첫 DM 이 상대의 '숨겨진 요청/스팸함'으로 들어갔습니다. …",
                    "error_action": "실패가 아니라 복구 대상입니다. 해당 워크스페이스의 "
                    "'실패 DM 복구'(프로 전용)를 켜면 …",
                    "recoverable": True,
                    "error_message": "(#100) Param recipient[id] ...",
                    "retry_count": 0,
                },
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdminDMLogRetryView(APIView):
    """transient 실패 DM 로그 강제 재발송 큐 등록."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=[TAG],
        summary="[관리자] DM 강제 재발송",
        description="""
## 개요
재시도 가능한 상태의 DM 로그를 즉시 발송 큐에 다시 넣습니다 (`send_dm_task`).

## 사용 시나리오
- rate_limited/queued/submitting 또는 legacy failed_api 로 멈춘 발송을 운영자가 재시도

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 재시도 가능 상태: `rate_limited`, `queued`, `submitting`, `failed_api`(legacy).
- 그 외 상태는 400 을 반환합니다 (허용 목록 + 사유 힌트 동봉).
  - failed_token: 재연동 필요 / failed_window: 24h 윈도우 / failed_param: 댓글 7일 초과 등.
- 성공 시 status=queued, retry_count+=1, next_retry_at=None 으로 저장하고
  send_dm_task 를 enqueue 한 뒤 202 를 반환합니다. 멱등성 키 유지로 중복 발송은 task 가 차단.
- 성공 후 `dmlog.retry` 감사 로그를 남깁니다. 본문 없음.

## 주의사항
- 이미 ACCEPTED/DELIVERED 인 건은 task 가 skip 합니다.
        """,
        request=None,
        responses={
            202: OpenApiResponse(
                description="재발송 큐 등록됨",
                examples=[
                    OpenApiExample(
                        "성공",
                        value={
                            "log_id": "f0e1d2c3-2222-4b3c-8d4e-bbbbbbbbbbbb",
                            "status": "queued",
                            "retry_count": 2,
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="재시도 불가 상태"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="로그 없음"),
        },
    )
    def post(self, request, pk):
        from apps.integrations.tasks import send_dm_task

        log = get_object_or_404(SentDMLog, pk=pk)

        # transient(즉시 재큐) + revivable(제자리 되살림: failed_token/skipped) 모두 허용.
        transient_statuses = {
            SentDMLog.Status.RATE_LIMITED,
            SentDMLog.Status.QUEUED,
            SentDMLog.Status.SUBMITTING,
            # legacy 호환
            SentDMLog.Status.FAILED_API,
        }
        retriable_statuses = transient_statuses | set(SentDMLog.REVIVABLE_STATUSES)
        if log.status not in retriable_statuses:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": f"상태 {log.status} 는 재시도할 수 없습니다.",
                        "details": {
                            "allowed": list(retriable_statuses),
                            "hint": (
                                "failed_window: 댓글이 24시간/7일 윈도우 내에 있어야 함, "
                                "failed_param: 댓글이 7일 초과되었을 가능성, "
                                "failed_no_trace: 이미 접수된 건(중복 방지 위해 재시도 불가)"
                            ),
                        },
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        before = log.status
        if log.status in SentDMLog.REVIVABLE_STATUSES:
            # failed_token/skipped → 제자리 되살림 (같은 row·같은 key). 윈도우 밖이면 거부.
            revived = log.revive(reason="admin_retry")
            if not revived:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 400,
                            "message": (
                                f"상태 {log.status} 는 메시징 윈도우가 만료되어 되살릴 수 없습니다."
                            ),
                            "details": {"hint": "comment 7일 / user_id 24h 윈도우 경과"},
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            log.status = SentDMLog.Status.QUEUED
            log.retry_count += 1
            log.next_retry_at = None
            log.save(update_fields=["status", "retry_count", "next_retry_at"])
            send_dm_task.delay(str(log.id))

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.DMLOG_RETRY,
            target_type="dmlog",
            target_id=log.pk,
            target_repr=log.recipient_username,
            changes={
                "status": {"before": before, "after": log.status},
                "retry_count": {"before": log.retry_count - 1, "after": log.retry_count},
            },
        )
        logger.info(
            "[admin-auto-dm] req=%s dmlog retry id=%s retry_count=%s",
            getattr(request, "id", ""),
            log.pk,
            log.retry_count,
        )
        return Response(
            {
                "log_id": str(log.id),
                "status": log.status,
                "retry_count": log.retry_count,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class AdminDMLogReverifyView(APIView):
    """DM 로그 수동 재검증 (Conversations API 즉시 호출)."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=[TAG],
        summary="[관리자] DM 수동 재검증",
        description="""
## 개요
ACCEPTED 상태에서 echo 웹훅 누락이 의심될 때, `GET /{message_id}` 를 즉시 호출해 Meta DB 에
메시지가 실존하는지 확인하고 DELIVERED 로 승격합니다.

## 사용 시나리오
- 운영자가 의심스러운 ACCEPTED 건을 강제로 도착 확정/검증할 때

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 이미 도착 확인(DELIVERED/READ) 된 건은 즉시 found_in_meta=true 로 short-circuit 반환.
- `meta_message_id` 가 **있으면** `GET /{message_id}` 로 조회합니다.
- `meta_message_id` 가 **없으면**(성공 ack 유실 건 = failed_no_trace 다수) Conversations API 로
  '이 수신자에게 보낸 흔적'을 조회해 판정합니다 — 창은 로그 생성 시각부터 현재까지입니다.
  흔적 있으면 도착 확정(200/found_in_meta=true), 없으면 상태 변경 없이 200/false,
  조회 불확실이면 상태 변경 없이 200/false + `unverifiable` 검증로그.
  `recipient_user_id` 까지 없는 건만 400 입니다.
- Meta API 일시 오류(DMTransientError)/그 외 API 오류(DMSendError) 는 502.
- 200 + 메시지 존재 → mark_delivered(conv_api) 후 found_in_meta=true.
- 404(미발견) → 검증 로그에 not_found 기록, 상태 변경 없이 found_in_meta=false (200).
- 성공/탐색 후 `dmlog.reverify` 감사 로그를 남깁니다. 본문 없음.

## 주의사항
- 토큰 만료 계정은 502(API 오류)로 떨어질 수 있으며 재연동이 선행되어야 합니다.
        """,
        request=None,
        responses={
            200: OpenApiResponse(
                description="재검증 결과 (도착 확정 또는 미발견)",
                examples=[
                    OpenApiExample(
                        "도착 확정",
                        value={
                            "log_id": "f0e1d2c3-2222-4b3c-8d4e-bbbbbbbbbbbb",
                            "previous_status": "accepted",
                            "new_status": "delivered",
                            "verified_via": "conv_api",
                            "found_in_meta": True,
                            "detail": "메시지가 Meta DB에 존재합니다.",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="message_id·recipient_user_id 둘 다 없음 — 재검증 불가"
            ),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="로그 없음"),
            502: OpenApiResponse(description="Meta API 호출 실패"),
        },
    )
    def post(self, request, pk):
        log = get_object_or_404(SentDMLog.objects.select_related("campaign__ig_connection"), pk=pk)
        prev = log.status

        if log.is_delivered():
            return Response(
                {
                    "log_id": str(log.id),
                    "previous_status": prev,
                    "new_status": log.status,
                    "verified_via": log.verified_via or "",
                    "found_in_meta": True,
                    "detail": "이미 도착 확인됨.",
                }
            )

        ig_conn = log.campaign.ig_connection

        # ★ message_id 가 없는 건도 재검증 가능해야 한다 (2026-07-30).
        #   성공 ack 가 유실된 발송(사설답장 delivered-but-500 → 재시도 2534023 자기충돌)은
        #   **정의상 message_id 가 비어 있다**. 그런데 프론트는 recoverable=true 판정에
        #   failed_no_trace 를 포함해 재검증 버튼을 노출하므로, 운영자가 누르면 100% 400 이
        #   떴다(prod 76건 전부 해당 = 유일한 수동 정리 경로가 막혀 있었음).
        #   message_id 가 없으면 Conversations API 로 '이 수신자에게 최근 보낸 흔적'을
        #   조회해 도착을 확정한다 — send_dm_task 의 승격 게이트와 같은 신호원.
        if not log.meta_message_id:
            if not log.recipient_user_id:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 400,
                            "message": "message_id 와 수신자 IGSID 가 모두 없어 재검증할 수 없습니다.",
                            "details": {"status": log.status},
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # 로그 생성 시점부터 지금까지를 창으로 잡는다(오래된 건도 조회되도록).
            age_s = int((timezone.now() - log.created_at).total_seconds()) + 120
            recent = InstagramMessagingService.has_recent_message_to_recipient(
                ig_user_id=ig_conn.external_account_id,
                recipient_id=log.recipient_user_id,
                access_token=ig_conn.access_token,
                since_seconds=age_s,
            )
            if recent is True:
                log.append_verification_log(
                    {
                        "path": "conv_api",
                        "result": "found_without_mid",
                        "trigger": "admin_manual",
                        "since_seconds": age_s,
                    }
                )
                log.mark_delivered(via=SentDMLog.VerifiedVia.CONV_API)
                log_admin_action(
                    request=request,
                    action=AdminActionLog.Action.DMLOG_REVERIFY,
                    target_type="dmlog",
                    target_id=log.pk,
                    target_repr=log.recipient_username,
                )
                return Response(
                    {
                        "log_id": str(log.id),
                        "previous_status": prev,
                        "new_status": log.status,
                        "verified_via": log.verified_via or "",
                        "found_in_meta": True,
                        "detail": (
                            "message_id 는 없지만 Conversations API 로 이 수신자에게 보낸 "
                            "메시지가 확인돼 도착으로 확정했습니다."
                        ),
                    }
                )
            log.append_verification_log(
                {
                    "path": "conv_api",
                    "result": "not_found_without_mid" if recent is False else "unverifiable",
                    "trigger": "admin_manual",
                    "since_seconds": age_s,
                }
            )
            return Response(
                {
                    "log_id": str(log.id),
                    "previous_status": prev,
                    "new_status": log.status,
                    "verified_via": log.verified_via or "",
                    "found_in_meta": False,
                    "detail": (
                        "발송 흔적을 찾지 못했습니다."
                        if recent is False
                        else "Meta 조회가 불확실해 판정하지 못했습니다(상태 변경 없음)."
                    ),
                }
            )

        try:
            message = InstagramMessagingService.fetch_message(
                message_id=log.meta_message_id,
                access_token=ig_conn.access_token,
            )
        except DMTransientError as e:
            log.append_verification_log(
                {
                    "path": "conv_api",
                    "result": "transient_error",
                    "error": str(e),
                    "trigger": "admin_manual",
                }
            )
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 502,
                        "message": "Meta API 일시적 오류 — 잠시 후 다시 시도해주세요.",
                        "details": {"reason": str(e)},
                    },
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except DMSendError as e:
            log.append_verification_log(
                {
                    "path": "conv_api",
                    "result": "api_error",
                    "error": str(e),
                    "trigger": "admin_manual",
                }
            )
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 502,
                        "message": str(e),
                        "details": e.api_response,
                    },
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if message is not None:
            log.append_verification_log(
                {
                    "path": "conv_api",
                    "result": "found",
                    "trigger": "admin_manual",
                    "message_id": message.get("id"),
                }
            )
            log.mark_delivered(via=SentDMLog.VerifiedVia.CONV_API)
            log_admin_action(
                request=request,
                action=AdminActionLog.Action.DMLOG_REVERIFY,
                target_type="dmlog",
                target_id=log.pk,
                target_repr=log.recipient_username,
                changes={
                    "status": {"before": prev, "after": log.status},
                    "found_in_meta": {"before": None, "after": True},
                },
            )
            logger.info(
                "[admin-auto-dm] req=%s dmlog reverify found id=%s",
                getattr(request, "id", ""),
                log.pk,
            )
            return Response(
                {
                    "log_id": str(log.id),
                    "previous_status": prev,
                    "new_status": log.status,
                    "verified_via": log.verified_via,
                    "found_in_meta": True,
                    "detail": "메시지가 Meta DB에 존재합니다.",
                }
            )

        log.append_verification_log(
            {"path": "conv_api", "result": "not_found", "trigger": "admin_manual"}
        )
        log_admin_action(
            request=request,
            action=AdminActionLog.Action.DMLOG_REVERIFY,
            target_type="dmlog",
            target_id=log.pk,
            target_repr=log.recipient_username,
            changes={
                "status": {"before": prev, "after": log.status},
                "found_in_meta": {"before": None, "after": False},
            },
        )
        logger.info(
            "[admin-auto-dm] req=%s dmlog reverify not_found id=%s",
            getattr(request, "id", ""),
            log.pk,
        )
        return Response(
            {
                "log_id": str(log.id),
                "previous_status": prev,
                "new_status": log.status,
                "verified_via": "",
                "found_in_meta": False,
                "detail": (
                    "Meta DB에서 메시지를 찾을 수 없습니다. " "자동 워커가 1시간까지 재시도합니다."
                ),
            }
        )


# ===== DM 수신자(사람) 단위 목록 =====


class _RecipientPagination(PageNumberPagination):
    """수신자 목록 전용 — 표준 page_size=20, {count,next,previous,results}."""

    page_size = 20


class AdminDMRecipientListView(APIView):
    """DM 수신자(사람) 단위 목록 (cross-workspace).

    ``SentDMLog`` 를 **(campaign_id, recipient_user_id)** 로 묶어 한 사람이 한 캠페인에서
    받은 모든 발송(오프닝+리워드+재시도)을 1행으로 접는다. 사용자용
    ``GET /integrations/dm-verification/recipients/`` 와 **동일한 status_group 판정**을
    쓰되, 어드민 교차-워크스페이스 스코프 + 캠페인/계정/소유자 nested 를 얹었다.
    """

    permission_classes = [IsAdminUser]
    serializer_class = AdminDMRecipientSerializer

    # ordering= 화이트리스트 (annotate 이름). 기본 -last_activity_at.
    ALLOWED_ORDERING = {"-last_activity_at", "last_activity_at", "dm_count", "-dm_count"}

    # 사람 단위 status_group 필터(HAVING) — 사용자용 recipients 뷰와 1:1 로 동일하게 유지.
    #   (parity: apps.integrations.verification_views.DMVerificationViewSet.recipients)
    @staticmethod
    def _status_group_having() -> dict:
        return {
            WAITING: Q(sent_n=0) & Q(waiting_n__gt=0),
            SENT: Q(sent_n__gt=0) & Q(read_n=0),
            READ: Q(read_n__gt=0),
            HIDDEN_SPAM: Q(sent_n=0) & Q(waiting_n=0) & Q(hidden_spam_n__gt=0),
            ATTENTION: Q(sent_n=0) & Q(waiting_n=0) & Q(hidden_spam_n=0),
        }

    @staticmethod
    def _person_status_group(row: dict) -> str:
        """수신자 1명의 코스 상태 그룹 (read > sent > waiting > hidden_spam > attention)."""
        if row["sent_n"] > 0:
            return READ if row["read_n"] > 0 else SENT
        if row["waiting_n"] > 0:
            return WAITING
        if row["hidden_spam_n"] > 0:
            return HIDDEN_SPAM
        return ATTENTION

    @extend_schema(
        tags=[TAG],
        summary="[관리자] DM 수신자(사람) 목록",
        description="""
## 개요
전체 워크스페이스의 DM 발송 로그를 **수신자 1명 = 1행**으로 접어 조회합니다. 그룹핑 키는
**(campaign_id, recipient_user_id)** — 한 사람이 한 캠페인에서 받은 모든 발송(오프닝 + 리워드 +
재시도)이 1행으로 롤업됩니다. 같은 사람이 다른 캠페인에서도 받았으면 캠페인당 별도 행입니다
("수신자는 캠페인당 1개"). 발송 로그 목록(`/admin/auto-dm/logs/`)이 발송 이벤트 단위라 한
사람에게 여러 행이 생기던 문제를, 사용자용 콘솔과 동일한 수신자 단위 UX 로 맞춥니다.

## 사용 시나리오
- 운영자가 "이 사람에게 결국 DM 이 갔나?" 를 한 행에서 확인
- 한 행을 클릭 → `GET /admin/auto-dm/logs/?recipient_user_id=<id>`(+`campaign_id`) 로
  그 사람의 발송 타임라인(오프닝 → 재시도 → 리워드)을 펼침
- `status_group` 탭(대기중/전송됨/읽음/숨겨진 요청·스팸/확인 필요)으로 드릴다운

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 조회 — request.user 워크스페이스로 필터링하지 않습니다.
- **행 키**: `recipient_user_id` (그룹 키). 표시상 username 이 비면 `user_{IGSID}` 폴백.
- **status_group 판정은 사용자용 `/integrations/dm-verification/recipients/` 와 100% 동일**한
  어휘·우선순위(read > sent > waiting > hidden_spam > attention)를 재사용합니다.
- `opening_count` = dm_kind ∈ {opening, standalone}, `interaction_count` = dm_kind = reward
  (오프닝 + 상호작용 = dm_count, 재시도는 원 dm_kind 유지).
- `latest_status` 는 그 사람 가장 최근 발송의 원시 `SentDMLog.status` (현재 페이지 행에 한해 조회).
- 표준 PageNumberPagination(page_size=20) → `{count, next, previous, results}`.
  정렬 tie-break 로 (campaign_id, recipient_user_id) 를 덧붙여 페이지 경계 안정.

## '발송 안 됨' 드릴다운 (DM-13 / DM-14 / DM-15)
캠페인 상세의 카드 4개(오프닝·후속 × 조사 필요·자동 처리)를 눌렀을 때 **그 사람들만** 뜨게
하는 필터입니다. 판정 규칙이 `stats.not_sent` 집계와 같은 코드(`dm_policy_rollup`)에서 나와
**카드 인원 == 목록 `count`** 가 구조적으로 성립합니다.

| 파라미터 | 값 | 의미 |
|---|---|---|
| `?dm_axis=` | `opening` / `follow_up` / `all`(기본) | 축 |
| `?error_policy=` | `investigate` / `normal` / `all` | 분류 |
| `?error_reason=` | `breakdown[].reason` | 사유 1종 |

- **`dm_axis` 는 그 축의 '발송 안 됨' 모수로 좁힙니다** (단순히 판정 근거만 바꾸는 게
  아닙니다) — 그래야 카드 숫자와 행 수가 맞습니다.
  - `opening` = 루트 DM 이 발송/대기 어디에도 못 간 사람 + 도착 미확인으로 끝난 사람
    (= `unique_failed + unique_unconfirmed`)
  - `follow_up` = **마지막** 후속 DM 이 실패인 사람 (= `stats.follow_up.not_sent.total`)
- 보장되는 항등:
  - `stats.not_sent.investigate` == `?dm_axis=opening&error_policy=investigate` 의 `count`
  - `stats.follow_up.not_sent.investigate` == `?dm_axis=follow_up&error_policy=investigate`
  - `not_sent.breakdown[].people` == `?dm_axis=<축>&error_reason=<그 reason>` 의 `count`
- `dm_axis` 를 주면 `error_title` · `error_reason` · `error_policy` 도 **그 축의 대표 로그**
  기준이 됩니다. 안 주면 축을 가리지 않은 '가장 최근 실패·정체 로그' 기준입니다.
- **상한 없음** — 사유·분류를 SQL 로 컴파일하므로 전역 조회도 400 이 나지 않습니다
  (11차의 500쌍 상한은 폐기, DM-15).

## 주의사항
- `campaign_id` 미지정 시 전역 `(campaign, recipient)` 그룹 집계라 대용량에서 무거울 수 있습니다.
  캠페인/계정 진입 동선에서는 `campaign_id` 또는 `ig_connection_id` 로 좁혀 호출하세요.
- IG access_token 등 비밀값은 노출되지 않습니다.

### 요청 예시
```bash
# 캠페인 상세 "오프닝 · 조사 필요 34명" 카드 드릴다운
curl -H "Authorization: Bearer <staff_token>" \\
  "https://api.example.com/api/v1/admin/auto-dm/recipients/\\
?campaign_id=<uuid>&dm_axis=opening&error_policy=investigate"
```
        """,
        parameters=[
            OpenApiParameter(
                "campaign_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="특정 캠페인(UUID)만. 없으면 전역이되 여전히 (campaign, recipient) 그룹.",
            ),
            OpenApiParameter(
                "status_group",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="코스 상태 그룹 필터 — all(기본)/waiting/sent/read/hidden_spam/attention. "
                "사용자용 recipients 와 동일 어휘·판정.",
            ),
            OpenApiParameter(
                "dm_axis",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="DM-13 — 축 필터: all(기본 · 두 축 합침)/opening(루트 DM 기준 '발송 안 됨')/"
                "follow_up(마지막 후속 DM 기준 '발송 안 됨'). error_policy·error_reason 과 조합 가능. "
                "허용값 외는 400(details.field='dm_axis').",
            ),
            OpenApiParameter(
                "error_policy",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="분류 필터 — all(기본)/investigate(🔴 조사 필요)/normal(⚪ 자동 처리). "
                "판정은 그 사람의 **대표 로그** 1건 기준이며 상한이 없다(DM-15).",
            ),
            OpenApiParameter(
                "error_reason",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="DM-14 — 사유 머신 키 필터. 값은 `stats.not_sent.breakdown[].reason` "
                "또는 운영 대시보드 `failure_breakdown[].reason` 을 그대로 싣는다. 미등록 값은 400.",
            ),
            OpenApiParameter(
                "error_scope",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="분류 필터의 모수 — all(기본 · 오류+건너뜀)/error(오류 8종)/skipped(건너뜀).",
            ),
            OpenApiParameter(
                "recipient",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="수신자 username 부분일치(icontains) 검색.",
            ),
            OpenApiParameter(
                "ig_connection_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="특정 IG 계정 연동(UUID)의 수신자만 필터.",
            ),
            OpenApiParameter(
                "owner",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="워크스페이스 소유자(User) PK 로 필터.",
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="정렬 — -last_activity_at(기본)/last_activity_at/dm_count/-dm_count.",
            ),
            OpenApiParameter(
                "page",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="페이지 번호 (page_size=20).",
            ),
        ],
        responses={
            200: AdminDMRecipientSerializer(many=True),
            400: OpenApiResponse(
                description="status_group / dm_axis / error_policy / error_reason / error_scope "
                "값 오류 (프로젝트 에러 포맷 · details.field 로 어느 파라미터인지 지목)"
            ),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "수신자 목록 응답 예시",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "recipient_user_id": "17841400000000000",
                            "recipient_username": "buyer_a",
                            "campaign": {
                                "id": "8b1c0e2a-1111-4a2b-9c3d-aaaaaaaaaaaa",
                                "name": "여름 공구 오픈",
                            },
                            "ig_connection_id": "1a2b3c4d-3333-4c5d-9e6f-cccccccccccc",
                            "ig_username": "brand_official",
                            "owner": {"id": 42, "email": "owner@example.com"},
                            "status_group": "read",
                            "status_group_display": "읽음",
                            "sent": True,
                            "delivered": True,
                            "read": True,
                            "needs_attention": False,
                            "dm_count": 3,
                            "opening_count": 1,
                            "interaction_count": 2,
                            "latest_status": "read",
                            "error_title": "",
                            "first_sent_at": "2026-07-20T10:00:00+09:00",
                            "last_activity_at": "2026-07-20T10:05:00+09:00",
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request):
        params = request.query_params
        invalid = _classification_error(params)
        if invalid is not None:
            return invalid
        axis = params.get("dm_axis")
        if axis and axis != "all":
            if axis not in AXES:
                return _bad_request(
                    f"dm_axis 값이 올바르지 않습니다: {axis!r}",
                    field="dm_axis",
                    allowed=["all", *AXES],
                )
        else:
            axis = None

        qs = SentDMLog.objects.all()

        campaign_id = params.get("campaign_id")
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        recipient = params.get("recipient")
        if recipient:
            qs = qs.filter(recipient_username__icontains=recipient)
        ig_connection_id = params.get("ig_connection_id")
        if ig_connection_id:
            qs = qs.filter(campaign__ig_connection_id=ig_connection_id)
        owner = params.get("owner")
        if owner:
            try:
                qs = qs.filter(campaign__ig_connection__workspace__owner_id=int(owner))
            except (TypeError, ValueError):
                pass  # 숫자 아니면 무시 (빈 결과 대신 필터 미적용 — 기존 목록 뷰 관례)

        opening_kinds = [SentDMLog.DMKind.OPENING, SentDMLog.DMKind.STANDALONE]
        delivered_statuses = [SentDMLog.Status.DELIVERED, SentDMLog.Status.READ]

        rows = qs.values(
            "campaign_id",
            "recipient_user_id",
            "campaign__name",
            "campaign__ig_connection_id",
            "campaign__ig_connection__username",
            "campaign__ig_connection__workspace__owner_id",
            "campaign__ig_connection__workspace__owner__email",
        ).annotate(
            latest_username=Max("recipient_username"),
            dm_count=Count("id"),
            last_activity_at=Max("created_at"),
            first_sent_at=Min("created_at"),
            sent_n=Count("id", filter=Q(status__in=SENT_FOR_QUOTA_STATUSES)),
            delivered_n=Count("id", filter=Q(status__in=delivered_statuses)),
            read_n=Count("id", filter=Q(status=SentDMLog.Status.READ)),
            waiting_n=Count("id", filter=Q(status__in=QUEUE_WAITING_STATUSES)),
            hidden_spam_n=Count("id", filter=status_group_q(HIDDEN_SPAM)),
            opening_count=Count("id", filter=Q(dm_kind__in=opening_kinds)),
            interaction_count=Count("id", filter=Q(dm_kind=SentDMLog.DMKind.REWARD)),
        )

        # status_group 필터 (HAVING — annotate 결과에 걸어야 롤업 카운트 오염 없이 total 도 정확).
        having = self._status_group_having()
        status_group_param = params.get("status_group")
        if status_group_param and status_group_param != "all":
            if status_group_param not in having:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 400,
                            "message": (
                                "status_group 값이 올바르지 않습니다. "
                                "(all/waiting/sent/read/hidden_spam/attention)"
                            ),
                            "details": {
                                "field": "status_group",
                                "allowed": ["all", *having.keys()],
                            },
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(having[status_group_param])

        # DM-13 — 축 필터. 캠페인 상세 카드가 4개(오프닝·후속 × 조사 필요·자동 처리)라
        #   축을 못 가르면 둘이 합쳐진 인원만 나온다. 판정은 `dm_policy_rollup` 의
        #   `not_sent` 집계와 **같은 규칙**을 HAVING 으로 옮긴 것이므로 카드 인원 == 행 수다.
        if axis == AXIS_OPENING:
            rows = rows.annotate(**opening_not_sent_annotations()).filter(OPENING_NOT_SENT_HAVING)
        elif axis == AXIS_FOLLOW_UP:
            rows = rows.annotate(**followup_not_sent_annotations(qs)).filter(
                FOLLOWUP_NOT_SENT_HAVING
            )

        # DM-8/14/15 — policy·사유 서버 필터 (카드/팝업 드릴다운 착지점).
        #   사람 단위이므로 **대표 로그 1건**에 조건을 건다: 대표 로그를 DISTINCT ON
        #   서브쿼리로 뽑아(id 목록) 그룹 안에 그 id 가 있는지로 HAVING 한다.
        #   쌍 OR 체인을 쓰던 11차 방식과 달리 상한이 없다(DM-15).
        classification = _classification_q(params)
        if classification is not None:
            rep_ids = rep_log_qs(qs, axis=axis, group_by_campaign=True).values("id")
            rows = rows.annotate(
                classified_n=Count("id", filter=Q(id__in=rep_ids) & classification)
            ).filter(classified_n__gt=0)

        ordering = params.get("ordering", "-last_activity_at")
        if ordering not in self.ALLOWED_ORDERING:
            ordering = "-last_activity_at"
        # tie-break 로 그룹 키를 덧붙여 페이지 경계 안정(동률 중복/누락 방지).
        rows = rows.order_by(ordering, "campaign_id", "recipient_user_id")

        paginator = _RecipientPagination()
        page_rows = paginator.paginate_queryset(rows, request, view=self)

        # latest_status: 현재 페이지 (campaign, recipient) 쌍의 최신 로그 상태 1건씩
        # (Postgres DISTINCT ON — over-fetch 후 정확 매칭). 페이지 20행이라 부담 없음.
        # 이건 "지금 상태"라서 성공 로그도 후보다 — 사유(error_*)와 근거 로그가 다르다.
        latest_status_map: dict = {}
        latest_followup_map: dict = {}
        rep_map: dict = {}
        if page_rows:
            camp_ids = {r["campaign_id"] for r in page_rows}
            rcpt_ids = {r["recipient_user_id"] for r in page_rows}
            page_scope = SentDMLog.objects.filter(
                campaign_id__in=camp_ids, recipient_user_id__in=rcpt_ids
            )
            for row in (
                page_scope.order_by("campaign_id", "recipient_user_id", "-created_at")
                .distinct("campaign_id", "recipient_user_id")
                .values("campaign_id", "recipient_user_id", "status")
            ):
                latest_status_map[(row["campaign_id"], row["recipient_user_id"])] = row["status"]

            # DM-16 — 사유(error_title/reason)와 분류(error_policy)를 **한 로그**에서 뽑는다:
            # 그 사람의 가장 최근 **실패·정체** 로그(축을 주면 그 축의 대표 로그).
            # 11차까지는 title 만 '최신 로그' 기준이라, 과거에 실패했다가 결국 성공한 사람의
            # 행이 policy 는 🔴 인데 사유 열만 비어 보였다("조사 필요 34명" 목록에 빈칸 행).
            rep_map = person_rep_map(page_scope, axis=axis)

            # DM-8 — 후속 DM 표(캠페인 상세)와 행을 맞추기 위한 '마지막 후속 DM 상태'.
            # latest_status(전체 로그 기준)로는 오프닝이 최신인 사람의 후속 상태를 알 수 없다.
            for row in latest_followup_rows(page_scope, group_by_campaign=True):
                latest_followup_map[(row["campaign_id"], row["recipient_user_id"])] = row["status"]

        results = []
        empty_rep = {"policy": "", "reason": "", "title": ""}
        for r in page_rows:
            grp = self._person_status_group(r)
            rid = r["recipient_user_id"]
            rep = rep_map.get((r["campaign_id"], rid), empty_rep)
            results.append(
                {
                    "recipient_user_id": rid,
                    "recipient_username": r["latest_username"] or (f"user_{rid}" if rid else ""),
                    "campaign": {"id": r["campaign_id"], "name": r["campaign__name"]},
                    "ig_connection_id": r["campaign__ig_connection_id"],
                    "ig_username": r["campaign__ig_connection__username"] or "",
                    "owner": {
                        "id": r["campaign__ig_connection__workspace__owner_id"],
                        "email": r["campaign__ig_connection__workspace__owner__email"] or "",
                    },
                    "status_group": grp,
                    "status_group_display": GROUP_DISPLAY[grp],
                    "sent": r["sent_n"] > 0,
                    "delivered": r["delivered_n"] > 0,
                    "read": r["read_n"] > 0,
                    "needs_attention": grp == ATTENTION,
                    "dm_count": r["dm_count"],
                    "opening_count": r["opening_count"],
                    "interaction_count": r["interaction_count"],
                    "latest_status": latest_status_map.get((r["campaign_id"], rid), ""),
                    "latest_followup_status": latest_followup_map.get((r["campaign_id"], rid), ""),
                    "error_title": rep["title"],
                    "error_reason": rep["reason"],
                    "error_policy": rep["policy"],
                    "first_sent_at": r["first_sent_at"],
                    "last_activity_at": r["last_activity_at"],
                }
            )

        data = AdminDMRecipientSerializer(results, many=True).data
        return paginator.get_paginated_response(data)


# ===== 통계 =====


class AdminDMVerificationStatsView(APIView):
    """전역 DM 발송 검증 통계 (cross-workspace)."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=[TAG],
        summary="[관리자] DM 발송 통계",
        description="""
## 개요
전체 SentDMLog 에 대해 "Meta 접수 vs 진짜 도착" 비율을 비롯한 발송 보증 지표를 집계합니다.

## 사용 시나리오
- 운영 대시보드에서 전사 발송 품질(delivery_rate/read_rate/gate 통과율) 모니터링
- 특정 캠페인/계정/기간으로 좁혀 품질 점검

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 집계 — 워크스페이스로 필터링하지 않습니다.
- `campaign_id` / `ig_connection_id`(campaign__ig_connection_id) / `since` 로 선택 필터.
- `since` 미지정 시 기본 최근 30일.
- delivery_rate = (delivered+read) / (accepted+delivered+read+failed_no_trace).

## 주의사항
- 응답 형식은 DMVerificationStatsSerializer 와 동일합니다.
        """,
        parameters=[
            OpenApiParameter(
                "campaign_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="특정 캠페인(UUID)만 집계.",
            ),
            OpenApiParameter(
                "ig_connection_id",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="특정 IG 계정(UUID)의 전체 캠페인 합산.",
            ),
            OpenApiParameter(
                "since",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="이 ISO datetime 이후 생성된 로그만 (기본: 30일 전).",
            ),
        ],
        responses={
            200: DMVerificationStatsSerializer,
            400: OpenApiResponse(description="잘못된 쿼리 파라미터"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "통계 응답 예시",
                value={
                    "total": 1500,
                    "accepted": 5,
                    "delivered": 1480,
                    "read": 900,
                    "delivery_rate": 0.9993,
                    "read_rate": 0.6081,
                    "gate_passthrough_rate": 0.42,
                },
                response_only=True,
            )
        ],
    )
    def get(self, request):
        qs = SentDMLog.objects.all()

        campaign_id = request.query_params.get("campaign_id")
        ig_connection_id = request.query_params.get("ig_connection_id")
        since = request.query_params.get("since")

        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        if ig_connection_id:
            qs = qs.filter(campaign__ig_connection_id=ig_connection_id)
        if since:
            qs = qs.filter(created_at__gte=since)
        else:
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=30))

        return Response(_build_stats(qs))


# ===== IG 연동 =====


class AdminIGConnectionListView(generics.ListAPIView):
    """IG 계정 연동 목록 (cross-workspace, 비밀값 미노출)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminIGConnectionListSerializer
    queryset = IGAccountConnection.objects.select_related("workspace", "workspace__owner")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status"]
    search_fields = ["username", "workspace__name", "workspace__owner__email"]
    ordering = ["-created_at"]

    @extend_schema(
        tags=[TAG],
        summary="[관리자] IG 연동 목록",
        description="""
## 개요
전체 워크스페이스의 Instagram 계정 연동 현황을 조회합니다. 토큰 만료/검증 상태와 연결된
캠페인 수, 최근 24시간 도착률을 함께 노출합니다.

## 사용 시나리오
- 토큰 만료/오류(status=expired/error/revoked) 계정 일괄 점검
- 발송 품질이 낮은(recent_delivery_rate_24h) 계정 식별

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 조회 — 워크스페이스로 필터링하지 않습니다.
- workspace/owner 를 select_related 하여 N+1 을 방지합니다.
- 기본 정렬 `-created_at`, 표준 PageNumberPagination(page_size=20).

## 주의사항
- 보안상 IG `access_token` 등 비밀값은 절대 노출하지 않습니다 (상태/만료/검증 시각만 제공).
        """,
        parameters=[
            OpenApiParameter(
                "status",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="연동 상태 (active/expired/revoked/error).",
            ),
            OpenApiParameter(
                "search",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="IG username / 워크스페이스 이름 / 소유자 email 부분일치 검색.",
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="정렬 (created_at, `-` 접두 내림차순). 기본 -created_at.",
            ),
            OpenApiParameter(
                "page",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="페이지 번호 (page_size=20).",
            ),
        ],
        responses={
            200: AdminIGConnectionListSerializer(many=True),
            400: OpenApiResponse(description="잘못된 쿼리 파라미터"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "목록 응답 예시",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": "1a2b3c4d-3333-4c5d-9e6f-cccccccccccc",
                            "username": "my_brand",
                            "workspace": {
                                "id": "9f8e7d6c-4444-4d5e-8f6a-dddddddddddd",
                                "name": "My Brand WS",
                            },
                            "owner": {"id": 7, "email": "owner@example.com"},
                            "status": "active",
                            "token_expires_at": "2026-07-01T00:00:00+09:00",
                            "last_verified_at": "2026-06-01T09:00:00+09:00",
                            "error_message": "",
                            "campaigns_count": 3,
                            "recent_delivery_rate_24h": 0.9991,
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


# ===== 백로그/처리량 모니터링 (P7) =====


class AdminDMBacklogView(APIView):
    """DM 발송 백로그·처리량 모니터링 (cross-workspace).

    유입(inflow) > 처리량(throughput)으로 QUEUED 가 쌓이다 메시징 윈도우(7d/24h) 만료로
    손실되는 'E1' 위험을 가시화한다.
    """

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=[TAG],
        summary="[관리자] DM 백로그/처리량",
        description="""
## 개요
QUEUED(대기) 적체 수, 가장 오래된 대기 건의 나이, 메시징 윈도우 만료 임박 건수,
최근 1시간 처리량(throughput)·유입(inflow), 적체 상위 계정을 집계합니다.

## 사용 시나리오
- 바이럴/저플랜 계정에서 발송 대기가 쌓여 댓글 7일 / user_id 24시간 윈도우 만료로
  누락(FAILED_WINDOW)되기 전에 운영자가 인지.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- `window_risk_count`: QUEUED 중 만료까지 `risk_hours`(기본 6h) 이내인 건수(손실 임박).
- `per_account`: QUEUED 적체 상위 20개 계정(대기 수 + 최오래 대기 나이).
        """,
        parameters=[
            OpenApiParameter(
                "risk_hours",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="윈도우 만료 임박 판정 시간(기본 6).",
            ),
        ],
        responses={
            200: OpenApiResponse(description="백로그 요약"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "백로그 응답 예시",
                value={
                    "total_queued": 320,
                    "oldest_queued_age_seconds": 5400,
                    "window_risk_count": 4,
                    "risk_hours": 6,
                    "sent_last_hour": 690,
                    "inflow_last_hour": 920,
                    "per_account": [
                        {
                            "ig_connection_id": "1a2b3c4d-...",
                            "ig_username": "my_brand",
                            "queued": 210,
                            "oldest_age_seconds": 5400,
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request):
        now = timezone.now()
        try:
            risk_hours = int(request.query_params.get("risk_hours", 6))
        except (TypeError, ValueError):
            risk_hours = 6

        queued = SentDMLog.objects.filter(status=SentDMLog.Status.QUEUED)
        total_queued = queued.count()
        oldest_created = queued.order_by("created_at").values_list("created_at", flat=True).first()
        oldest_age_seconds = int((now - oldest_created).total_seconds()) if oldest_created else 0

        # 윈도우 임박 — QUEUED 를 created_at 순으로 스캔(상한 2000). comment 7d / user_id 24h.
        risk_cut = timedelta(hours=risk_hours)
        window_risk = 0
        for cid, created in queued.order_by("created_at").values_list("comment_id", "created_at")[
            :2000
        ]:
            window = timedelta(days=7) if cid else timedelta(hours=24)
            if (created + window) - now <= risk_cut:
                window_risk += 1

        sent_last_hour = SentDMLog.objects.filter(accepted_at__gte=now - timedelta(hours=1)).count()
        inflow_last_hour = SentDMLog.objects.filter(
            created_at__gte=now - timedelta(hours=1)
        ).count()

        per_account = []
        for row in (
            queued.values("campaign__ig_connection_id", "campaign__ig_connection__username")
            .annotate(queued=Count("id"), oldest=Min("created_at"))
            .order_by("-queued")[:20]
        ):
            oldest = row.get("oldest")
            per_account.append(
                {
                    "ig_connection_id": str(row.get("campaign__ig_connection_id")),
                    "ig_username": row.get("campaign__ig_connection__username"),
                    "queued": row.get("queued"),
                    "oldest_age_seconds": int((now - oldest).total_seconds()) if oldest else 0,
                }
            )

        return Response(
            {
                "total_queued": total_queued,
                "oldest_queued_age_seconds": oldest_age_seconds,
                "window_risk_count": window_risk,
                "risk_hours": risk_hours,
                "sent_last_hour": sent_last_hour,
                "inflow_last_hour": inflow_last_hour,
                "per_account": per_account,
            }
        )
