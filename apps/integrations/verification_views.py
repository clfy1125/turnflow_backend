"""
DM 발송 검증 API (99.9% 보증 시스템 — 프론트엔드용 엔드포인트).

기존 `views.py` 의 댓글/캠페인 ViewSet 과 분리. 프론트가 ACCEPTED ↔ DELIVERED ↔
READ 의 차이를 표시하고, 실패 사유별 후속 액션(재연동/재시도)을 처리할 수 있도록
조회·통계·수동 재검증·강제 재시도 엔드포인트를 제공한다.

엔드포인트 (모두 `/api/v1/integrations/dm-verification/` 하위):

    GET    /                                DM 로그 목록 (캠페인/상태별 필터)
    GET    /{id}/                           단건 상세
    GET    /{id}/checklist/                 단건의 자가 점검 체크리스트 + 액션 가이드
    POST   /{id}/reverify/                  해당 로그 즉시 능동 재검증
    POST   /{id}/retry/                     transient 실패 건 강제 재발송 큐 등록
    GET    /stats/?campaign_id=...          캠페인 단위 발송 통계 (이벤트 + 사람 단위 + CTR)
    GET    /recipients/?campaign_id=...      수신자(사람) 단위로 묶은 로그 롤업
    GET    /queue-state/?campaign_id=...     순차 발송 큐 현황 (게이지 + ETA) — v4.3 페이서
    GET    /lookup/?message_id=...          meta_message_id 또는 idempotency_key 조회
    GET    /health/?ig_connection_id=...    해당 계정의 최근 발송 보증 헬스체크
    GET    /self_check_guide/               자가 점검 체크리스트 (상태 무관, 인증 불필요)

인증: 모든 엔드포인트 JWT (IsAuthenticated) + Workspace 멤버십 확인.
    예외: /self_check_guide/ — AllowAny (정적 가이드라인이라 공개)
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Min, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .campaign_stats import NEEDS_ATTENTION_STATUSES, SENT_FOR_QUOTA_STATUSES
from .dm_exceptions import DMSendError, DMTransientError
from .dm_frontend_actions import SELF_CHECK_CHECKLIST, build_frontend_action
from .models import AutoDMCampaign, IGAccountConnection, SentDMLog
from .serializers import (
    DMLookupResponseSerializer,
    DMQueueStateSerializer,
    DMRecipientRollupSerializer,
    DMReverifyResponseSerializer,
    DMVerificationStatsSerializer,
    SentDMLogSerializer,
)
from .services import InstagramMessagingService


def _user_workspaces(request):
    """현재 유저가 속한 워크스페이스 ID 집합"""
    return request.user.memberships.values_list("workspace_id", flat=True)


def _recipient_username_display(recipient_user_id, recipient_username) -> str:
    """수신자 표시용 username — 미해석(빈 값)이면 user_{IGSID} 폴백.

    DB 컬럼(recipient_username)은 빈 채로 두고 응답에서만 폴백을 적용한다
    (윈도우 내 재열람 시 실제 핸들로 채워질 여지 보존 · Max 롤업 무손상)."""
    return recipient_username or f"user_{recipient_user_id}"


def _maybe_enqueue_username_resolution(campaign_id, recipient_user_ids) -> None:
    """수신자 목록 열람 시 빈 username 을 지연 해석하도록 Celery 태스크 enqueue (fire-and-forget).

    - 설정 DM_RESOLVE_RECIPIENT_USERNAME=False 면 no-op.
    - 폴링 storm 방지: IGSID 별 pending 플래그(cache.add, TTL 60s)로 재-enqueue 억제.
    - 뷰에서 동기 외부 API 호출 금지(CLAUDE.md §5.3) — 실제 IG 호출은 태스크가 수행하고,
      엔드포인트는 현재 데이터를 즉시 반환한다(핸들은 다음 로드 때 반영).
    """
    if not campaign_id:
        return
    if not getattr(settings, "DM_RESOLVE_RECIPIENT_USERNAME", True):
        return

    to_resolve = [
        rid
        for rid in {str(x) for x in recipient_user_ids if x}
        if cache.add(f"dm:uname:pending:{campaign_id}:{rid}", 1, 60)
    ]
    if not to_resolve:
        return

    from .tasks import resolve_recipient_usernames_for_campaign

    resolve_recipient_usernames_for_campaign.delay(str(campaign_id), to_resolve)


def _get_log_for_user(request, log_id: str) -> SentDMLog:
    """워크스페이스 멤버십 검증 후 SentDMLog 반환"""
    try:
        log = SentDMLog.objects.select_related("campaign__ig_connection__workspace").get(id=log_id)
    except SentDMLog.DoesNotExist as e:
        raise NotFound("DM 로그를 찾을 수 없습니다.") from e
    workspace = log.campaign.ig_connection.workspace
    if not workspace.memberships.filter(user=request.user).exists():
        raise PermissionDenied("이 로그가 속한 워크스페이스의 멤버가 아닙니다.")
    return log


class DMVerificationViewSet(viewsets.ViewSet):
    """
    DM 발송 검증 ViewSet — 프론트엔드 전용.

    "Meta 접수(ACCEPTED)" 와 "도착 확인(DELIVERED)" 을 분리해서 노출하고,
    실패 사유별 후속 처리를 지원한다.
    """

    permission_classes = [IsAuthenticated]
    # drf-spectacular 의 자동 스키마 추론을 위해 기본 serializer 명시
    # (실제 응답은 각 @extend_schema(responses=...) 가 우선)
    serializer_class = SentDMLogSerializer

    # ===== 목록 / 상세 =====

    @extend_schema(
        summary="DM 발송 로그 목록 조회",
        description="""
        ## 목적
        현재 사용자가 속한 워크스페이스의 모든 DM 발송 로그를 조회합니다.
        ACCEPTED / DELIVERED / READ 등 99.9% 보증 시스템의 모든 상태가 노출됩니다.

        ## 사용 시나리오
        - 캠페인 상세 화면에서 DM 발송 이력 표 출력
        - 실패 건 디버깅 (에러 코드 + 검증 로그 확인)
        - "도착 확인(DELIVERED)" 만 필터링해서 진짜 발송 성공률 표시

        ## 인증
        Bearer JWT 필수.

        ## 쿼리 파라미터
        - `campaign_id` (UUID, 선택): 특정 캠페인만 필터
        - `status` (str, 선택): 특정 상태만 필터
            (queued, submitting, accepted, delivered, read,
             failed_api, failed_token, failed_window, failed_param, failed_no_trace, skipped)
        - `since` (ISO datetime, 선택): 이 시각 이후 생성된 로그만
        - `recipient_username` (str, 선택): 수신자 username 부분일치
        - `recipient_user_id` (str, 선택): 수신자 Instagram ID **정확일치**.
            `recipients/` 목록에서 한 계정을 클릭했을 때, 그 사람에게 간 모든 개별
            DM(opening/reward/재안내)의 타임라인을 조회하는 "자세히보기" 용도.
            보통 `campaign_id` 와 함께 사용합니다.
        - `page` (int): 페이지 번호 (PageNumberPagination, page_size=20)

        ## 비즈니스 로직
        프론트는 `display_status` 필드로 한국어 표시명을, `is_delivered` 로 진짜 도착
        여부를, `verified_via` 로 검증 경로(echo/conv_api)를 확인할 수 있습니다.

        ## 참고
        이 목록은 **개별 발송 이벤트** 단위입니다. 수신자(사람) 단위로 묶어 보려면
        `GET /dm-verification/recipients/?campaign_id=...` 를 사용하세요.
        """,
        parameters=[
            OpenApiParameter("campaign_id", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("status", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("since", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("recipient_username", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("recipient_user_id", str, OpenApiParameter.QUERY, required=False),
        ],
        responses={
            200: SentDMLogSerializer(many=True),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
        },
        tags=["DM Verification"],
    )
    def list(self, request):
        qs = SentDMLog.objects.filter(
            campaign__ig_connection__workspace_id__in=_user_workspaces(request)
        ).select_related("campaign")

        campaign_id = request.query_params.get("campaign_id")
        status_filter = request.query_params.get("status")
        since = request.query_params.get("since")
        recipient_username = request.query_params.get("recipient_username")
        recipient_user_id = request.query_params.get("recipient_user_id")

        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        if status_filter:
            qs = qs.filter(status=status_filter)
        if since:
            qs = qs.filter(created_at__gte=since)
        if recipient_username:
            qs = qs.filter(recipient_username__icontains=recipient_username)
        if recipient_user_id:
            # 계정별 "자세히보기": 한 수신자에게 간 모든 개별 DM(opening/reward/재안내) 타임라인.
            qs = qs.filter(recipient_user_id=recipient_user_id)

        qs = qs.order_by("-created_at")

        # 간단 페이지네이션
        try:
            page = int(request.query_params.get("page", 1))
        except ValueError:
            page = 1
        page_size = 20
        offset = (page - 1) * page_size
        total = qs.count()
        items = list(qs[offset : offset + page_size])

        # 지연 username 해석: 캠페인 컨텍스트가 있을 때만(태스크가 토큰을 캠페인에서 얻음).
        if campaign_id:
            _maybe_enqueue_username_resolution(
                campaign_id,
                [i.recipient_user_id for i in items if not i.recipient_username],
            )

        return Response(
            {
                "count": total,
                "page": page,
                "page_size": page_size,
                "results": SentDMLogSerializer(items, many=True).data,
            }
        )

    @extend_schema(
        summary="DM 발송 로그 단건 조회",
        description="""
        ## 목적
        특정 DM 로그의 상세 정보를 조회합니다.
        verification_log 필드에 능동 검증 시도 이력이 모두 들어있습니다.

        ## 인증
        Bearer JWT + 해당 로그가 속한 워크스페이스 멤버십.
        """,
        responses={
            200: SentDMLogSerializer,
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="로그 없음"),
        },
        tags=["DM Verification"],
    )
    def retrieve(self, request, pk=None):
        log = _get_log_for_user(request, pk)
        return Response(SentDMLogSerializer(log).data)

    # ===== 수동 재검증 =====

    @extend_schema(
        summary="DM 수동 재검증 (Conversations API 즉시 호출)",
        description="""
        ## 목적
        ACCEPTED 상태에서 echo 웹훅이 누락됐다고 의심될 때, 프론트에서 버튼 한 번으로
        `GET /v25.0/{message_id}` 를 즉시 호출해 도착 여부를 확정한다.

        ## 사용 시나리오
        - 사용자가 "DM이 진짜 갔는지 다시 확인" 버튼을 누름
        - 운영자가 의심스러운 ACCEPTED 건을 강제 검증

        ## 동작
        1. 로그가 ACCEPTED 상태가 아니거나 message_id 가 없으면 400 반환
        2. Meta Graph API `GET /{message_id}` 호출
        3. 200 → DELIVERED 로 승격, 검증 경로는 conv_api
        4. 404 → 검증 로그에 not_found 기록 (상태는 변경 안 함)
        5. 토큰 오류면 IGAccountConnection 을 error 로 마킹

        ## 인증
        Bearer JWT + 워크스페이스 멤버십 + 토큰 유효.

        ## 응답 예시 (DMReverifyResponseSerializer)
        ```json
        {
            "log_id": "uuid",
            "previous_status": "accepted",
            "new_status": "delivered",
            "verified_via": "conv_api",
            "found_in_meta": true,
            "detail": "메시지가 Meta DB에 존재합니다."
        }
        ```
        """,
        responses={
            200: DMReverifyResponseSerializer,
            400: OpenApiResponse(description="재검증 불가능한 상태"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="로그 없음"),
            502: OpenApiResponse(description="Meta API 호출 실패"),
        },
        tags=["DM Verification"],
    )
    @action(detail=True, methods=["post"], url_path="reverify")
    def reverify(self, request, pk=None):
        log = _get_log_for_user(request, pk)
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

        if not log.meta_message_id:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "이 로그는 Meta message_id 가 없어 재검증할 수 없습니다.",
                        "details": {"status": log.status},
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        ig_conn = log.campaign.ig_connection

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
                    "trigger": "manual",
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
                {"path": "conv_api", "result": "api_error", "error": str(e), "trigger": "manual"}
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
                    "trigger": "manual",
                    "message_id": message.get("id"),
                }
            )
            log.mark_delivered(via=SentDMLog.VerifiedVia.CONV_API)
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
            {"path": "conv_api", "result": "not_found", "trigger": "manual"}
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

    # ===== 수동 재발송 (transient 실패용) =====

    @extend_schema(
        summary="DM 강제 재발송 큐 등록",
        description="""
        ## 목적
        `failed_api` 상태(주로 5xx/rate-limit) 의 로그를 운영자가 즉시 다시 큐에 넣는다.

        ## 제약 (재시도 가능 조건)
        - 현재 상태가 `failed_api`, `queued`, `submitting` 중 하나
        - **`failed_token`/`failed_window`/`failed_param`은 재시도 불가** (정책 위반)
          → 토큰 재연동 / 캠페인 룰 변경이 선행되어야 함

        ## 동작
        1. 상태를 QUEUED 로 되돌림
        2. retry_count += 1
        3. send_dm_task 즉시 enqueue
        4. 멱등성 키는 그대로 유지 → 중복 발송 안 됨 (이미 ACCEPTED면 task가 skip)

        ## 인증
        Bearer JWT + 워크스페이스 멤버십.
        """,
        responses={
            202: OpenApiResponse(
                description="재발송 큐 등록됨",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={"log_id": "uuid", "status": "queued", "retry_count": 2},
                    )
                ],
            ),
            400: OpenApiResponse(description="재시도 불가 상태"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="로그 없음"),
        },
        tags=["DM Verification"],
    )
    @action(detail=True, methods=["post"], url_path="retry")
    def retry(self, request, pk=None):
        from .tasks import send_dm_task

        log = _get_log_for_user(request, pk)

        # v3.2: RATE_LIMITED 만 자동/수동 재시도 가능
        # (다른 모든 FAILED_* 는 Dead Letter — 정책/토큰/파라미터 문제라 재시도 무의미)
        retriable_statuses = {
            SentDMLog.Status.RATE_LIMITED,
            SentDMLog.Status.QUEUED,
            SentDMLog.Status.SUBMITTING,
            # legacy 호환
            SentDMLog.Status.FAILED_API,
        }
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
                                "failed_token: 재연동 필요, "
                                "failed_window: 댓글이 24시간 윈도우 내에 있어야 함, "
                                "failed_param: 댓글이 7일 초과되었을 가능성, "
                                "failed_no_trace: 자가 점검 체크리스트 확인 필요"
                            ),
                        },
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 예약 발송: 캠페인 활성 기간이 끝났으면(또는 아직 시작 전이면) 수동 재시도도 막는다.
        # (send_dm_task 가 실행 시점에 한 번 더 차단하지만, 운영자에게 명확한 사유를 즉시 반환)
        if not log.campaign.is_within_schedule():
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 409,
                        "message": "캠페인이 활성 발송 기간(예약 창) 밖이라 재시도할 수 없습니다.",
                        "details": {
                            "schedule_state": log.campaign.schedule_state(),
                            "scheduled_start_at": (
                                log.campaign.scheduled_start_at.isoformat()
                                if log.campaign.scheduled_start_at
                                else None
                            ),
                            "scheduled_end_at": (
                                log.campaign.scheduled_end_at.isoformat()
                                if log.campaign.scheduled_end_at
                                else None
                            ),
                            "hint": "기간을 연장하려면 캠페인 schedule API 로 종료일을 변경하세요.",
                        },
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )

        log.status = SentDMLog.Status.QUEUED
        log.retry_count += 1
        log.next_retry_at = None
        log.save(update_fields=["status", "retry_count", "next_retry_at"])

        send_dm_task.delay(str(log.id))

        return Response(
            {
                "log_id": str(log.id),
                "status": log.status,
                "retry_count": log.retry_count,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    # ===== 통계 =====

    @extend_schema(
        summary="DM 발송 통계 (캠페인/계정 단위)",
        description="""
        ## 목적
        사용자에게 "Meta 접수 vs 진짜 도착" 비율을 보여주기 위한 집계.

        ## 쿼리 파라미터
        - `campaign_id` (UUID, 선택): 특정 캠페인만 집계
        - `ig_connection_id` (UUID, 선택): 특정 계정 전체 캠페인 합산
        - `since` (ISO datetime, 선택, 기본: 30일 전)

        ## 지표 두 계열 (중요)
        응답에는 **① 발송 이벤트 단위** 지표와 **② 사람(수신자) 단위** 지표가 함께 옵니다.
        follow-gate 캠페인은 1명 = DM 2건(opening+reward)이라 이벤트 단위는 부풀려 보이므로,
        마케팅 대시보드에는 사람 단위(`unique_*`, `ctr`) 를 쓰는 것을 권장합니다.

        ### ① 발송 이벤트 단위 (배송 신뢰성/디버깅)
        - `delivery_rate`: ACCEPTED 진입 건 중 DELIVERED+READ 비율 (0~1).
          이 값이 0.999 이상이면 99.9% 보증 달성.
        - `read_rate`: DELIVERED 건 중 READ 비율.
        - `total`, `delivered`, `read`, `gate_passed` 등: 로그(이벤트) 개수.

        ### ② 사람(수신자 Instagram ID) 단위 — v4.2 (마케팅)
        - `unique_recipients`: 도달한 고유 수신자 수.
        - `unique_sent`: DM 이 실제 발송된 고유 수신자 수 (CTR 분모).
        - `unique_delivered` / `unique_read` / `unique_followers`: 각 단계 도달 사람 수.
        - `unique_delivery_rate`: unique_delivered / unique_sent.
        - `ctr`: **참여율** = 상호작용한 사람 / 발송된 사람 (0~1).
          - 게이트형 캠페인(`follow_gate_enabled=true`): "버튼 클릭" 을 참여로 봄.
          - 비게이트형(링크 즉시 발송): "읽음(READ)" 을 참여로 봄.
          - `ctr_basis` 로 어느 기준인지 확인 (`click`/`read`/`mixed`).
          - **주의**: `read` 는 messaging_seen 웹훅에 의존하는 best-effort 신호라
            비게이트형 CTR 은 실제보다 낮게 나올 수 있음(과소집계).

        ## 인증
        Bearer JWT + 워크스페이스 멤버십.
        """,
        parameters=[
            OpenApiParameter("campaign_id", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("ig_connection_id", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("since", str, OpenApiParameter.QUERY, required=False),
        ],
        responses={
            200: DMVerificationStatsSerializer,
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "게이트형 캠페인 통계 (사람 단위 + CTR)",
                value={
                    "total": 1240,
                    "delivered": 610,
                    "read": 402,
                    "gate_passed": 388,
                    "delivery_rate": 0.984,
                    "unique_recipients": 620,
                    "unique_sent": 620,
                    "unique_delivered": 610,
                    "unique_read": 402,
                    "unique_followers": 388,
                    "unique_delivery_rate": 0.9839,
                    "ctr": 0.6258,
                    "ctr_basis": "click",
                    "ctr_interacted": 388,
                    "ctr_denominator": 620,
                },
                response_only=True,
            )
        ],
        tags=["DM Verification"],
    )
    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        qs = SentDMLog.objects.filter(
            campaign__ig_connection__workspace_id__in=_user_workspaces(request)
        )

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

        delivered_or_read = Q(status="delivered") | Q(status="read")
        agg = qs.aggregate(
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
            # v3.3 — DM 종류별
            standalone_total=Count("id", filter=Q(dm_kind="standalone")),
            opening_total=Count("id", filter=Q(dm_kind="opening")),
            opening_delivered=Count("id", filter=Q(dm_kind="opening") & delivered_or_read),
            reward_total=Count("id", filter=Q(dm_kind="reward")),
            reward_delivered=Count("id", filter=Q(dm_kind="reward") & delivered_or_read),
            # v3.3 — Follow-gate
            gate_pending=Count("id", filter=Q(gate_status="pending")),
            gate_passed=Count("id", filter=Q(gate_status="passed")),
            gate_expired=Count("id", filter=Q(gate_status="expired")),
            # 공개 답글
            public_replies_posted=Count("id", filter=~Q(public_reply_id="")),
        )

        # ACCEPTED 진입 건 = accepted + delivered + read + failed_no_trace
        # (DELIVERED/READ는 ACCEPTED를 거쳐 갔고, no_trace 도 ACCEPTED 후 종결)
        accepted_or_after = (
            agg["accepted"]
            + agg["delivered"]
            + agg["read"]
            + agg["failed_no_trace"]
            + agg["recovery_delivered"]  # 복구 재전송 성공 = 실제 도착
        )
        confirmed_delivered = agg["delivered"] + agg["read"] + agg["recovery_delivered"]

        delivery_rate = confirmed_delivered / accepted_or_after if accepted_or_after else 0.0
        read_rate = agg["read"] / confirmed_delivered if confirmed_delivered else 0.0

        # Gate 통과율 = gate_passed / opening_delivered
        # (opening DELIVERED 중 사용자가 응답해서 통과한 비율)
        gate_passthrough_rate = (
            agg["gate_passed"] / agg["opening_delivered"] if agg["opening_delivered"] else 0.0
        )

        agg["delivery_rate"] = round(delivery_rate, 4)
        agg["read_rate"] = round(read_rate, 4)
        agg["gate_passthrough_rate"] = round(gate_passthrough_rate, 4)

        # ─────────────────────────────────────────────────────────────
        # v4.2 — 사람(수신자 Instagram ID) 단위 지표 + CTR (마케팅 API)
        #
        # 위의 total/delivered/... 는 "발송 이벤트" 단위(디버깅·배송 신뢰성용)라
        # follow-gate 캠페인에서 1명 = DM 2건(opening+reward)으로 부풀려 보인다.
        # 마케터가 보는 "몇 명에게 닿았나 / 몇 명이 반응했나" 는 아래 unique_* 로 제공한다.
        # (기존 이벤트 필드는 하위호환 위해 그대로 둔다.)
        # ─────────────────────────────────────────────────────────────
        def _uniq(**flt) -> int:
            base = qs.filter(**flt) if flt else qs
            return base.values("recipient_user_id").distinct().count()

        unique_recipients = _uniq()
        unique_sent = (
            qs.filter(status__in=SENT_FOR_QUOTA_STATUSES)
            .values("recipient_user_id")
            .distinct()
            .count()
        )
        unique_delivered = (
            qs.filter(status__in=["delivered", "read"])
            .values("recipient_user_id")
            .distinct()
            .count()
        )
        unique_read = _uniq(status="read")
        unique_followers = _uniq(gate_status="passed")

        # CTR = 상호작용한 고유 수신자 / 발송된 고유 수신자.
        #  - 게이트형 캠페인(follow_gate_enabled=True): 버튼 1회라도 클릭 = child 로그 존재
        #    (reward=통과 / retry=클릭했으나 미통과 둘 다 parent_log 로 opening 에 묶인다).
        #  - 비게이트형 캠페인: 상호작용 단계가 없으므로 "읽음(READ)" 을 참여로 본다.
        # 두 조건은 캠페인 타입으로 자연히 배타적이라 OR 하나로 집계된다.
        ctr_interacted = (
            qs.filter(
                Q(campaign__follow_gate_enabled=True, parent_log__isnull=False)
                | Q(campaign__follow_gate_enabled=False, status="read")
            )
            .values("recipient_user_id")
            .distinct()
            .count()
        )
        ctr = ctr_interacted / unique_sent if unique_sent else 0.0

        gate_flags = set(qs.values_list("campaign__follow_gate_enabled", flat=True).distinct())
        if gate_flags == {True}:
            ctr_basis = "click"
        elif gate_flags == {False} or not gate_flags:
            ctr_basis = "read"
        else:
            ctr_basis = "mixed"

        unique_delivery_rate = unique_delivered / unique_sent if unique_sent else 0.0

        agg.update(
            {
                "unique_recipients": unique_recipients,
                "unique_sent": unique_sent,
                "unique_delivered": unique_delivered,
                "unique_read": unique_read,
                "unique_followers": unique_followers,
                "unique_delivery_rate": round(unique_delivery_rate, 4),
                "ctr": round(ctr, 4),
                "ctr_basis": ctr_basis,
                "ctr_interacted": ctr_interacted,
                "ctr_denominator": unique_sent,
            }
        )
        return Response(agg)

    # ===== 수신자(사람) 단위 로그 =====

    @extend_schema(
        summary="캠페인 DM 로그 — 수신자(사람) 단위 묶음",
        description="""
        ## 목적
        캠페인의 DM 발송 로그를 **수신자 Instagram ID 기준으로 1행씩 묶어** 반환합니다.
        기존 목록(`GET /`)이 개별 발송 이벤트(한 사람이 opening+reward 를 받으면 2행)인 반면,
        이 엔드포인트는 **한 사람 = 1행**으로 최신 상태만 롤업해서 보여줍니다.
        마케터용 "이 캠페인으로 몇 명에게 닿았고 각자 어디까지 진행됐나" 화면에 사용합니다.

        ## 사용 시나리오
        - 캠페인 상세의 "수신자 목록" 표 (발송/도착/읽음/팔로우 상태 컬럼)
        - 한 행 클릭 → `GET /?campaign_id=...&recipient_user_id=<id>` 로 그 사람의
          개별 DM 타임라인("자세히보기") 조회

        ## 인증
        Bearer JWT + 해당 캠페인이 속한 워크스페이스 멤버십.

        ## 쿼리 파라미터
        - `campaign_id` (UUID, **필수**): 대상 캠페인
        - `recipient_username` (str, 선택): username 부분일치 검색
        - `category` (str, 선택): 상태별 사람 단위 필터 —
          `all`(기본)/`delivered`(성공)/`read`(읽음)/`attention`(확인 필요).
          **페이지네이션 이전**에 적용되며 `count` 는 필터 후 총 인원이다.
          `delivered` 는 정의상 `read` 를 포함한다. 잘못된 값은 400.
        - `page` (int): 페이지 번호 (page_size=20)

        ## 각 행 필드
        - `recipient_user_id` / `recipient_username`: 수신자 (username 은 최신값 best-effort)
        - `sent` (bool): DM 이 실제 발송됨 (accepted 이상)
        - `delivered` (bool): 도착 확인됨 (delivered/read)
        - `read` (bool): 읽음 확인됨
        - `follower_status`: 팔로우/참여 상태 (아래 표 참고)
        - `dm_count` (int): 이 사람에게 나간 총 DM 이벤트 수 (opening+reward+재안내 합)
        - `needs_attention` (bool): 실패 등 사용자 조치가 필요한 로그가 있음
        - `last_activity_at`: 마지막 활동 시각

        ## follower_status 값
        | 값 | 의미 |
        |---|---|
        | `verified_follower` | follow-gate 통과 + 팔로우 검증 완료 (확인 시점 팔로워) |
        | `clicked_unverified` | 버튼은 눌렀으나 팔로우 미검증 (button-only 모드 or 검증 실패) |
        | `not_followed` | 아직 버튼 클릭/통과 안 함 (pending/expired) |
        | `unknown` | 게이트 미사용 캠페인 — 팔로우 여부를 알 수 없음 |

        주의: 우리는 "확인 시점의 팔로우 여부" 만 알 수 있어 "원래 팔로워 vs 이 캠페인으로
        전환" 은 구분하지 못합니다. verified_follower = 검증 시점에 팔로워였음.
        """,
        parameters=[
            OpenApiParameter("campaign_id", str, OpenApiParameter.QUERY, required=True),
            OpenApiParameter("recipient_username", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                "category",
                str,
                OpenApiParameter.QUERY,
                required=False,
                enum=["all", "delivered", "read", "attention"],
                description="상태별 사람 단위 필터 (페이지네이션 이전 적용, 기본 all).",
            ),
        ],
        responses={
            200: DMRecipientRollupSerializer(many=True),
            400: OpenApiResponse(description="campaign_id 누락 또는 category 값 오류"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="캠페인 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "수신자 롤업 목록",
                value={
                    "count": 2,
                    "page": 1,
                    "page_size": 20,
                    "campaign_id": "b1e2c3d4-0000-0000-0000-000000000000",
                    "results": [
                        {
                            "recipient_user_id": "17841400000000001",
                            "recipient_username": "buyer_a",
                            "sent": True,
                            "delivered": True,
                            "read": True,
                            "follower_status": "verified_follower",
                            "dm_count": 2,
                            "needs_attention": False,
                            "last_activity_at": "2026-07-09T13:20:11+09:00",
                        },
                        {
                            "recipient_user_id": "17841400000000002",
                            "recipient_username": "user_17841400000000002",
                            "sent": True,
                            "delivered": True,
                            "read": False,
                            "follower_status": "not_followed",
                            "dm_count": 1,
                            "needs_attention": False,
                            "last_activity_at": "2026-07-09T13:19:02+09:00",
                        },
                    ],
                },
                response_only=True,
            )
        ],
        tags=["DM Verification"],
    )
    @action(detail=False, methods=["get"], url_path="recipients")
    def recipients(self, request):
        campaign_id = request.query_params.get("campaign_id")
        if not campaign_id:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "campaign_id 는 필수입니다.",
                        "details": {"field": "campaign_id"},
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            campaign = AutoDMCampaign.objects.select_related("ig_connection__workspace").get(
                id=campaign_id
            )
        except (AutoDMCampaign.DoesNotExist, ValueError, TypeError) as e:
            raise NotFound("캠페인을 찾을 수 없습니다.") from e
        workspace = campaign.ig_connection.workspace
        if not workspace.memberships.filter(user=request.user).exists():
            raise PermissionDenied("이 캠페인이 속한 워크스페이스의 멤버가 아닙니다.")

        logs = SentDMLog.objects.filter(campaign=campaign)
        recipient_username = request.query_params.get("recipient_username")
        if recipient_username:
            logs = logs.filter(recipient_username__icontains=recipient_username)

        # 수신자별 롤업 — 한 번의 group-by 조건부 집계.
        rows = logs.values("recipient_user_id").annotate(
            latest_username=Max("recipient_username"),
            dm_count=Count("id"),
            last_activity_at=Max("created_at"),
            sent_n=Count("id", filter=Q(status__in=SENT_FOR_QUOTA_STATUSES)),
            delivered_n=Count("id", filter=Q(status__in=["delivered", "read"])),
            read_n=Count("id", filter=Q(status="read")),
            passed_n=Count("id", filter=Q(gate_status="passed")),
            clicks_n=Count("id", filter=Q(parent_log__isnull=False)),
            needs_n=Count("id", filter=Q(status__in=NEEDS_ATTENTION_STATUSES)),
        )

        # 사람 단위 카테고리 필터 (페이지네이션 이전 = HAVING 절).
        # 값(delivered/read/attention)은 응답 boolean 필드와 동일 정의라 일관적이다.
        # 원시 logs 가 아니라 annotate 결과에 filter 를 걸어야(HAVING) 롤업 카운트가
        # 오염되지 않고, total=rows.count() 도 자동으로 필터 후 인원으로 맞는다.
        category = request.query_params.get("category")
        category_having = {
            "delivered": Q(delivered_n__gt=0),  # 성공 (delivered=true, read 포함)
            "read": Q(read_n__gt=0),  # 읽음 (read=true)
            "attention": Q(needs_n__gt=0),  # 확인 필요 (needs_attention=true)
        }
        if category and category != "all":
            if category not in category_having:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 400,
                            "message": "category 값이 올바르지 않습니다. (all/delivered/read/attention)",
                            "details": {
                                "field": "category",
                                "allowed": ["all", *category_having.keys()],
                            },
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(category_having[category])

        # 안정 정렬: 마지막 활동 desc + tie-break recipient_user_id
        # (페이지 이동 시 동률로 인한 중복/누락 방지)
        rows = rows.order_by("-last_activity_at", "recipient_user_id")

        try:
            page = int(request.query_params.get("page", 1))
        except ValueError:
            page = 1
        page_size = 20
        offset = (page - 1) * page_size
        total = rows.count()
        page_rows = list(rows[offset : offset + page_size])

        # 지연 username 해석: 현재 페이지 중 미해석(빈 값) 수신자만 백그라운드로 해석 트리거.
        _maybe_enqueue_username_resolution(
            campaign.id,
            [r["recipient_user_id"] for r in page_rows if not r["latest_username"]],
        )

        gated = campaign.follow_gate_enabled
        verify = campaign.gate_verify_follow

        def _follower_status(row) -> str:
            if not gated:
                return "unknown"
            if row["passed_n"] > 0:
                return "verified_follower" if verify else "clicked_unverified"
            if row["clicks_n"] > 0:
                return "clicked_unverified"
            return "not_followed"

        results = [
            {
                "recipient_user_id": r["recipient_user_id"],
                "recipient_username": _recipient_username_display(
                    r["recipient_user_id"], r["latest_username"]
                ),
                "sent": r["sent_n"] > 0,
                "delivered": r["delivered_n"] > 0,
                "read": r["read_n"] > 0,
                "follower_status": _follower_status(r),
                "dm_count": r["dm_count"],
                "needs_attention": r["needs_n"] > 0,
                "last_activity_at": r["last_activity_at"],
            }
            for r in page_rows
        ]

        return Response(
            {
                "count": total,
                "page": page,
                "page_size": page_size,
                "campaign_id": str(campaign.id),
                "results": DMRecipientRollupSerializer(results, many=True).data,
            }
        )

    # ===== 큐 현황 (게이지 + ETA) =====

    @extend_schema(
        summary="DM 순차 발송 큐 현황 (게이지 + ETA)",
        description="""
        ## 목적
        Instagram 정책상 DM 은 계정당 순차 발송(오프닝 평균 ~5.0초/건, 리워드류 ~2초/건)됩니다.
        이 엔드포인트는 **현재 발송 대기 중인 DM 수 / 발송 완료 수 / 예상 완료 시각(ETA)** 을
        계산해 프론트가 게이지 UI 로 사용자에게 안내할 수 있게 합니다.
        캠페인 생성을 막거나 예약을 강제하지 않습니다 — 순수 정보 제공(read-only)입니다.

        ## 사용 시나리오
        - 캠페인 상세: "발송 512 / 대기 138 — 약 12분 후 완료 예상" 게이지 + 카운트다운
        - 계정 대시보드: 계정 전체(모든 캠페인 합산) 발송 큐 현황
        - 대기가 많을 때 "다른 캠페인 N건이 앞서 대기 중" 안내 (`ahead_of_this_campaign`)

        ## 인증
        Bearer JWT + 해당 리소스가 속한 워크스페이스 멤버십.

        ## 쿼리 파라미터 (둘 중 정확히 1개 필수)
        - `campaign_id` (UUID): 캠페인 스코프 게이지. ETA 는 계정 공유 대기열(타 캠페인
          백로그 포함)을 반영해 계산됩니다.
        - `ig_connection_id` (UUID): 계정 스코프 — 그 계정의 모든 캠페인 합산.

        ## 비즈니스 로직 (v4.3 페이서)
        - 발송은 계정 단위 **지터 슬롯**으로 직렬화됩니다(봇 지문 회피를 위해 간격이 매번
          랜덤). 대기 건 대부분은 확정 슬롯 시각을 갖고 있어 ETA 가 정확하며, 아직 슬롯을
          받지 않은 건은 평균 간격으로 추정합니다(`eta_is_estimate=true`).
        - `blocking_reason`:
          - `action_block_cooldown` — Instagram 이 일시적으로 계정 발송을 제한
            (`action_block_cooldown_seconds` 후 자동 재개, ETA 에 반영됨)
          - `monthly_quota_reached` — 플랜 월 DM 한도 도달 (업그레이드 전까지 신규 발송 skip)
          - `null` — 정상 (페이싱 진행 중)
        - 게이지: `gauge.sent / gauge.total` (= sent+waiting+in_flight). `failed` 는 분모
          제외 — 별도 세그먼트로 표기하세요.

        ## 폴링 가이드
        5~10초 폴링 권장. `generated_at` 기준으로 클라이언트에서 카운트다운 보간.
        """,
        parameters=[
            OpenApiParameter("campaign_id", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("ig_connection_id", str, OpenApiParameter.QUERY, required=False),
        ],
        responses={
            200: DMQueueStateSerializer,
            400: OpenApiResponse(
                description="campaign_id / ig_connection_id 둘 다 없거나 둘 다 지정"
            ),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="캠페인/연동 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "campaign 스코프 조회",
                value={
                    "scope": "campaign",
                    "campaign_id": "b1e2c3d4-0000-0000-0000-000000000000",
                    "ig_connection_id": "a0c1b2d3-0000-0000-0000-000000000000",
                    "external_account_id": "17841400000000000",
                    "ig_username": "turnflow_official",
                    "gauge": {
                        "sent": 512,
                        "waiting": 138,
                        "in_flight": 2,
                        "failed": 4,
                        "total": 652,
                    },
                    "pacing": {
                        "private_reply_avg_gap_s": 5.0,
                        "send_api_avg_gap_s": 2.0,
                        "hourly_backstop_cap": 740,
                    },
                    "account_waiting": 190,
                    "ahead_of_this_campaign": 52,
                    "blocking_reason": None,
                    "action_block_cooldown_seconds": 0,
                    "eta_seconds": 1045.0,
                    "eta_finish_at": "2026-07-09T13:25:40+09:00",
                    "eta_is_estimate": False,
                    "generated_at": "2026-07-09T13:08:15+09:00",
                },
                response_only=True,
            )
        ],
        tags=["DM Verification"],
    )
    @action(detail=False, methods=["get"], url_path="queue-state")
    def queue_state(self, request):
        import time as _time

        from . import dm_pacer
        from .rate_governor import PRIVATE_REPLY_HOURLY_CAP, action_block_cooldown_remaining

        campaign_id = request.query_params.get("campaign_id")
        ig_connection_id = request.query_params.get("ig_connection_id")
        if bool(campaign_id) == bool(ig_connection_id):  # 둘 다 or 둘 다 아님
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "campaign_id 또는 ig_connection_id 중 정확히 1개를 지정하세요.",
                        "details": {"fields": ["campaign_id", "ig_connection_id"]},
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        campaign = None
        if campaign_id:
            try:
                campaign = AutoDMCampaign.objects.select_related("ig_connection__workspace").get(
                    id=campaign_id
                )
            except (AutoDMCampaign.DoesNotExist, ValueError, TypeError) as e:
                raise NotFound("캠페인을 찾을 수 없습니다.") from e
            ig_conn = campaign.ig_connection
        else:
            try:
                ig_conn = IGAccountConnection.objects.select_related("workspace").get(
                    id=ig_connection_id
                )
            except (IGAccountConnection.DoesNotExist, ValueError, TypeError) as e:
                raise NotFound("IG 연동을 찾을 수 없습니다.") from e

        workspace = ig_conn.workspace
        if not workspace.memberships.filter(user=request.user).exists():
            raise PermissionDenied("이 리소스가 속한 워크스페이스의 멤버가 아닙니다.")

        ext = str(ig_conn.external_account_id)
        account_qs = SentDMLog.objects.filter(campaign__ig_connection=ig_conn)
        scope_qs = account_qs.filter(campaign=campaign) if campaign else account_qs

        hard_failed = [
            SentDMLog.Status.FAILED_TOKEN,
            SentDMLog.Status.FAILED_WINDOW,
            SentDMLog.Status.FAILED_PARAM,
            SentDMLog.Status.FAILED,  # legacy
        ]
        agg = scope_qs.aggregate(
            sent=Count("id", filter=Q(status__in=SENT_FOR_QUOTA_STATUSES)),
            waiting=Count("id", filter=Q(status=SentDMLog.Status.QUEUED)),
            in_flight=Count("id", filter=Q(status=SentDMLog.Status.SUBMITTING)),
            failed=Count("id", filter=Q(status__in=hard_failed)),
        )
        gauge = {
            "sent": agg["sent"],
            "waiting": agg["waiting"],
            "in_flight": agg["in_flight"],
            "failed": agg["failed"],
            "total": agg["sent"] + agg["waiting"] + agg["in_flight"],
        }

        account_waiting_qs = account_qs.filter(status=SentDMLog.Status.QUEUED)
        account_waiting = account_waiting_qs.count()
        ahead = 0
        if campaign:
            my_oldest = scope_qs.filter(status=SentDMLog.Status.QUEUED).aggregate(
                m=Min("created_at")
            )["m"]
            if my_oldest:
                ahead = account_waiting_qs.filter(created_at__lt=my_oldest).count()

        # ── ETA (v4.3): 대기 건 대부분은 확정 슬롯(next_retry_at)을 보유. 버킷별로
        #    max(확정 슬롯) 과 (포인터 + 미클레임 × 평균 간격) 추정을 합성한다.
        #    미클레임은 계정 공유 포인터를 소비하므로 **계정 단위**로 센다.
        now_ts = _time.time()
        finish_ts = now_ts
        is_estimate = False

        bucket_filters = {
            dm_pacer.BUCKET_PRIVATE_REPLY: (
                dm_pacer.bucket_q(dm_pacer.BUCKET_PRIVATE_REPLY),
                dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_PRIVATE_REPLY),
            ),
            dm_pacer.BUCKET_SEND_API: (
                dm_pacer.bucket_q(dm_pacer.BUCKET_SEND_API),
                dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_SEND_API),
            ),
        }
        scope_waiting_qs = scope_qs.filter(status=SentDMLog.Status.QUEUED)
        for bucket, (bucket_q, avg_gap) in bucket_filters.items():
            scope_bucket = scope_waiting_qs.filter(bucket_q)
            if not scope_bucket.exists():
                continue
            claimed_max = scope_bucket.aggregate(m=Max("next_retry_at"))["m"]
            if claimed_max:
                finish_ts = max(finish_ts, claimed_max.timestamp())
            # 미클레임(슬롯 미예약) — 계정 전체가 같은 포인터를 소비하므로 계정 단위 추정
            unclaimed_account = account_waiting_qs.filter(
                bucket_q, next_retry_at__isnull=True
            ).count()
            if unclaimed_account and scope_bucket.filter(next_retry_at__isnull=True).exists():
                pointer = dm_pacer.peek_next_slot(ext, bucket) or now_ts
                finish_ts = max(finish_ts, max(pointer, now_ts) + unclaimed_account * avg_gap)
                is_estimate = True

        # ── 차단 요인 ──
        ab_remaining = action_block_cooldown_remaining(ext)
        blocking_reason = None
        if ab_remaining > 0:
            blocking_reason = "action_block_cooldown"
            finish_ts = max(finish_ts, now_ts + ab_remaining) + 0  # 쿨다운 후 재개
            is_estimate = True
        else:
            from apps.billing.dm_limits import check_dm_quota

            try:
                quota_ok, _, _ = check_dm_quota(workspace.owner)
            except Exception:  # noqa: BLE001 — 쿼터 조회 실패는 표시만 정상 취급
                quota_ok = True
            if not quota_ok and gauge["waiting"] > 0:
                blocking_reason = "monthly_quota_reached"
                is_estimate = True

        eta_seconds = max(0.0, round(finish_ts - now_ts, 1)) if gauge["waiting"] else 0.0
        eta_finish_at = (
            datetime.fromtimestamp(finish_ts, tz=timezone.get_current_timezone())
            if gauge["waiting"]
            else None
        )

        cap = getattr(settings, "IG_PRIVATE_REPLY_HOURLY_CAP", PRIVATE_REPLY_HOURLY_CAP)
        payload = {
            "scope": "campaign" if campaign else "account",
            "campaign_id": str(campaign.id) if campaign else None,
            "ig_connection_id": str(ig_conn.id),
            "external_account_id": ext,
            "ig_username": ig_conn.username or "",
            "gauge": gauge,
            "pacing": {
                "private_reply_avg_gap_s": dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_PRIVATE_REPLY),
                "send_api_avg_gap_s": dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_SEND_API),
                "hourly_backstop_cap": int(cap or 0),
            },
            "account_waiting": account_waiting,
            "ahead_of_this_campaign": ahead,
            "blocking_reason": blocking_reason,
            "action_block_cooldown_seconds": int(ab_remaining),
            "eta_seconds": eta_seconds,
            "eta_finish_at": eta_finish_at,
            "eta_is_estimate": bool(is_estimate),
            "generated_at": timezone.now(),
        }
        return Response(DMQueueStateSerializer(payload).data)

    # ===== 룩업 (message_id / idempotency_key) =====

    @extend_schema(
        summary="DM 로그 룩업 (메시지 ID / 멱등성 키)",
        description="""
        ## 목적
        Meta 가 발급한 `message_id` 또는 우리가 발급한 `idempotency_key` 로 단건 조회.

        ## 사용 시나리오
        - 운영자가 Meta 대시보드에서 본 `message_id` 로 우리 로그 추적
        - 댓글/캠페인 단위로 중복 발송이 막혔는지 확인 (idempotency_key 검증)

        ## 쿼리 파라미터 (둘 중 하나 필수)
        - `message_id`: Meta 가 발급한 message_id 또는 echo mid
        - `idempotency_key`: sha256 해시

        ## 인증
        Bearer JWT + 워크스페이스 멤버십 (다른 워크스페이스 로그면 found=false 처리).
        """,
        parameters=[
            OpenApiParameter("message_id", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("idempotency_key", str, OpenApiParameter.QUERY, required=False),
        ],
        responses={
            200: DMLookupResponseSerializer,
            400: OpenApiResponse(description="파라미터 누락"),
        },
        tags=["DM Verification"],
    )
    @action(detail=False, methods=["get"], url_path="lookup")
    def lookup(self, request):
        message_id = request.query_params.get("message_id")
        idem = request.query_params.get("idempotency_key")

        if not (message_id or idem):
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "message_id 또는 idempotency_key 중 하나는 필수입니다.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = SentDMLog.objects.filter(
            campaign__ig_connection__workspace_id__in=_user_workspaces(request)
        ).select_related("campaign")

        if message_id:
            log = qs.filter(Q(meta_message_id=message_id) | Q(echo_mid=message_id)).first()
        else:
            log = qs.filter(idempotency_key=idem).first()

        if not log:
            return Response({"found": False, "log": None})
        return Response({"found": True, "log": SentDMLogSerializer(log).data})

    # ===== 헬스체크 =====

    @extend_schema(
        summary="DM 발송 보증 헬스체크",
        description="""
        ## 목적
        특정 IG 계정에 대해 최근 1시간/24시간 동안의 발송 보증 상태를 한 번에 본다.

        ## 응답 필드
        - `accepted_pending_verification`: ACCEPTED 인데 아직 도착 미확정 (워커가 처리 중)
        - `stuck_submitting`: SUBMITTING 60초+ 정체 (드물면 무시, 많으면 인프라 점검)
        - `recent_delivery_rate_1h`: 최근 1시간 ACCEPTED 진입 건 중 DELIVERED+READ 비율
        - `recent_delivery_rate_24h`: 최근 24시간 동일 지표
        - `token_expired`: IG 계정 토큰 만료 여부
        - `connection_status`: IG 계정 연결 상태

        ## 권장 사용
        대시보드 메인에서 5분마다 폴링 → 0.999 미만이면 경고 배지 노출.

        ## 쿼리 파라미터
        - `ig_connection_id` (UUID, 필수)
        """,
        parameters=[
            OpenApiParameter("ig_connection_id", str, OpenApiParameter.QUERY, required=True),
        ],
        responses={
            200: OpenApiResponse(description="헬스체크 응답"),
            400: OpenApiResponse(description="ig_connection_id 누락"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="IG 계정 없음"),
        },
        tags=["DM Verification"],
    )
    @action(detail=False, methods=["get"], url_path="health")
    def health(self, request):
        ig_connection_id = request.query_params.get("ig_connection_id")
        if not ig_connection_id:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "ig_connection_id 가 필요합니다.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            ig_conn = IGAccountConnection.objects.select_related("workspace").get(
                id=ig_connection_id
            )
        except IGAccountConnection.DoesNotExist as e:
            raise NotFound("IG 계정을 찾을 수 없습니다.") from e

        if not ig_conn.workspace.memberships.filter(user=request.user).exists():
            raise PermissionDenied("이 IG 계정이 속한 워크스페이스의 멤버가 아닙니다.")

        now = timezone.now()
        h1 = now - timedelta(hours=1)
        d1 = now - timedelta(hours=24)

        base = SentDMLog.objects.filter(campaign__ig_connection=ig_conn)

        def _delivery_rate(since):
            agg = base.filter(created_at__gte=since).aggregate(
                accepted=Count("id", filter=Q(status="accepted")),
                delivered=Count("id", filter=Q(status="delivered")),
                read=Count("id", filter=Q(status="read")),
                no_trace=Count("id", filter=Q(status="failed_no_trace")),
            )
            denom = agg["accepted"] + agg["delivered"] + agg["read"] + agg["no_trace"]
            num = agg["delivered"] + agg["read"]
            return round(num / denom, 4) if denom else None

        accepted_pending = base.filter(status=SentDMLog.Status.ACCEPTED).count()
        stuck = base.filter(
            status=SentDMLog.Status.SUBMITTING,
            submitted_at__lte=now - timedelta(seconds=60),
        ).count()

        # next_media 폴링 상태 (v3.4)
        next_media_pending = AutoDMCampaign.objects.filter(
            ig_connection=ig_conn,
            trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
            status=AutoDMCampaign.Status.ACTIVE,
            media_id="",
        ).count()

        last_polled = ig_conn.last_polled_at
        seconds_since_poll = (now - last_polled).total_seconds() if last_polled else None

        if next_media_pending == 0:
            polling_status = "not_needed"
        elif last_polled is None:
            polling_status = "never"
        elif seconds_since_poll is not None and seconds_since_poll > 15 * 60:
            polling_status = "stale"  # 15분 넘게 폴링 안 됨 → Beat/워커 점검 필요
        else:
            polling_status = "ok"

        return Response(
            {
                "ig_connection_id": str(ig_conn.id),
                "ig_username": ig_conn.username,
                "connection_status": ig_conn.status,
                "token_expired": ig_conn.is_token_expired(),
                "accepted_pending_verification": accepted_pending,
                "stuck_submitting": stuck,
                "recent_delivery_rate_1h": _delivery_rate(h1),
                "recent_delivery_rate_24h": _delivery_rate(d1),
                "campaigns_active": AutoDMCampaign.objects.filter(
                    ig_connection=ig_conn, status=AutoDMCampaign.Status.ACTIVE
                ).count(),
                # next_media 폴링 모니터링 (v3.4)
                "next_media_pending_count": next_media_pending,
                "last_polled_at": (last_polled.isoformat() if last_polled else None),
                "seconds_since_last_poll": (
                    int(seconds_since_poll) if seconds_since_poll is not None else None
                ),
                "polling_status": polling_status,
                "last_seen_media_id": ig_conn.last_seen_media_id or None,
                "last_seen_media_at": (
                    ig_conn.last_seen_media_at.isoformat() if ig_conn.last_seen_media_at else None
                ),
            }
        )

    # ===== v3.2 — 자가 점검 체크리스트 =====

    @extend_schema(
        summary="DM 로그의 프론트엔드 액션/체크리스트 조회",
        description="""
        ## 목적
        v3.2 단순화 정책에 따라, 단건 DM 로그의 현재 상태에 매핑되는
        프론트엔드 표시 가이드(타이틀/설명/체크리스트/CTA)를 한 번에 받아간다.

        프론트는 이 응답을 그대로 모달/배지/토스트에 렌더링하면 된다.

        ## 응답 형식
        ```json
        {
          "log_id": "uuid",
          "status": "failed_no_trace",
          "frontend_action": {
            "type": "checklist",
            "title": "도착 미확인 — 다음 설정을 확인해주세요",
            "description": "...",
            "checklist": [
              {"id": "message_access_allowed", "title": "...", "description": "..."},
              ...
            ],
            "cta": {"label": "재검증 시도", "action": "reverify"},
            "severity": "warning"
          }
        }
        ```

        `frontend_action.type`:
            - `success`    : 도착/읽음 성공
            - `wait`       : 처리 중 (queued/submitting/accepted/rate_limited)
            - `reconnect`  : 토큰 만료 — 재연동 CTA
            - `info`       : 정책 위반 안내 (window/param)
            - `checklist`  : 자가 점검 (failed_no_trace)

        ## 인증
        Bearer JWT + 워크스페이스 멤버십.
        """,
        responses={
            200: OpenApiResponse(description="frontend_action 포함 응답"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="로그 없음"),
        },
        tags=["DM Verification"],
    )
    @action(detail=True, methods=["get"], url_path="checklist")
    def checklist(self, request, pk=None):
        log = _get_log_for_user(request, pk)
        return Response(
            {
                "log_id": str(log.id),
                "status": log.status,
                "frontend_action": build_frontend_action(log.status),
            }
        )

    @extend_schema(
        summary="자가 점검 체크리스트 (정적, 인증 불필요)",
        description="""
        ## 목적
        FAILED_NO_TRACE 또는 도착 미확인 상황에서 사용자에게 노출할 수 있는
        4가지 자가 점검 항목을 정적으로 제공한다.

        프론트가 별도 로그 ID 없이 도움말 페이지/팁 모달에서 조회할 수 있도록
        인증 없이 제공한다 (정적 가이드).

        ## 응답 항목
        - `message_access_allowed`: Instagram 메시지 액세스 허용 여부
        - `default_routing_app`:    기본 대화 라우팅 앱 설정
        - `restricted_content`:     제한된 컨텐츠 여부
        - `recipient_account`:      수신자 계정 문제

        ## 응답 예시
        ```json
        {
          "version": "v3.2",
          "checklist": [
            {"id": "message_access_allowed", "title": "메시지 액세스 허용 여부", "description": "..."},
            ...
          ]
        }
        ```
        """,
        responses={200: OpenApiResponse(description="체크리스트 응답")},
        tags=["DM Verification"],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="self_check_guide",
        permission_classes=[AllowAny],
    )
    def self_check_guide(self, request):
        return Response(
            {
                "version": "v3.2",
                "checklist": SELF_CHECK_CHECKLIST,
            }
        )
