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
    GET    /stats/?campaign_id=...          캠페인 단위 발송 통계
    GET    /lookup/?message_id=...          meta_message_id 또는 idempotency_key 조회
    GET    /health/?ig_connection_id=...    해당 계정의 최근 발송 보증 헬스체크
    GET    /self_check_guide/               자가 점검 체크리스트 (상태 무관, 인증 불필요)

인증: 모든 엔드포인트 JWT (IsAuthenticated) + Workspace 멤버십 확인.
    예외: /self_check_guide/ — AllowAny (정적 가이드라인이라 공개)
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .dm_exceptions import DMSendError, DMTransientError
from .dm_frontend_actions import SELF_CHECK_CHECKLIST, build_frontend_action
from .models import AutoDMCampaign, IGAccountConnection, SentDMLog
from .serializers import (
    DMLookupResponseSerializer,
    DMReverifyResponseSerializer,
    DMVerificationStatsSerializer,
    SentDMLogSerializer,
)
from .services import InstagramMessagingService


def _user_workspaces(request):
    """현재 유저가 속한 워크스페이스 ID 집합"""
    return request.user.memberships.values_list("workspace_id", flat=True)


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
        - `page` (int): 페이지 번호 (PageNumberPagination, page_size=20)

        ## 비즈니스 로직
        프론트는 `display_status` 필드로 한국어 표시명을, `is_delivered` 로 진짜 도착
        여부를, `verified_via` 로 검증 경로(echo/conv_api)를 확인할 수 있습니다.
        """,
        parameters=[
            OpenApiParameter("campaign_id", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("status", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("since", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("recipient_username", str, OpenApiParameter.QUERY, required=False),
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

        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        if status_filter:
            qs = qs.filter(status=status_filter)
        if since:
            qs = qs.filter(created_at__gte=since)
        if recipient_username:
            qs = qs.filter(recipient_username__icontains=recipient_username)

        qs = qs.order_by("-created_at")

        # 간단 페이지네이션
        try:
            page = int(request.query_params.get("page", 1))
        except ValueError:
            page = 1
        page_size = 20
        offset = (page - 1) * page_size
        total = qs.count()
        items = qs[offset : offset + page_size]

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

        ## 핵심 지표
        - `delivery_rate`: ACCEPTED 진입 건 중 DELIVERED+READ 비율 (0~1).
          이 값이 0.999 이상이면 99.9% 보증 달성.
        - `read_rate`: DELIVERED 건 중 READ 비율.

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
        },
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
            agg["accepted"] + agg["delivered"] + agg["read"] + agg["failed_no_trace"]
        )
        confirmed_delivered = agg["delivered"] + agg["read"]

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
        return Response(agg)

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
