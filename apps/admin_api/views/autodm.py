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

from django.db.models import Count, Min
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog
from apps.admin_api.serializers.autodm import (
    AdminCampaignDetailSerializer,
    AdminCampaignListSerializer,
    AdminDMLogDetailSerializer,
    AdminDMLogListSerializer,
    AdminIGConnectionListSerializer,
    _build_stats,
)
from apps.integrations.dm_exceptions import DMSendError, DMTransientError
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.serializers import DMVerificationStatsSerializer
from apps.integrations.services import InstagramMessagingService

logger = logging.getLogger(__name__)

TAG = "admin-auto-dm"


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
    ordering_fields = ["created_at", "started_at", "total_sent"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        ig_connection_id = self.request.query_params.get("ig_connection_id")
        owner = self.request.query_params.get("owner")
        if ig_connection_id:
            qs = qs.filter(ig_connection_id=ig_connection_id)
        if owner:
            qs = qs.filter(ig_connection__workspace__owner_id=owner)
        return qs

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
- 발송량(total_sent) 기준 상위 캠페인 추적

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 조회 — request.user 의 워크스페이스로 필터링하지 않습니다.
- N+1 방지를 위해 ig_connection / workspace / owner 를 select_related 합니다.
- 기본 정렬 `-created_at`, 표준 PageNumberPagination(page_size=20) 적용 → `{count,next,previous,results}`.

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
                description="정렬 (created_at/started_at/total_sent, `-` 접두 내림차순).",
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
                            "total_sent": 1280,
                            "total_failed": 3,
                            "max_sends_per_hour": 200,
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
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        if recipient:
            qs = qs.filter(recipient_username__icontains=recipient)
        if ig_connection_id:
            qs = qs.filter(campaign__ig_connection_id=ig_connection_id)
        if since:
            qs = qs.filter(created_at__gte=since)
        return qs

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
- `since` 는 ISO datetime (예: `2026-05-01T00:00:00Z`).
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
                description="수신자 username 부분일치 검색.",
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
                            "recipient_username": "buyer01",
                            "status": "delivered",
                            "dm_kind": "opening",
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
- `meta_message_id` 가 없으면 400.
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
            400: OpenApiResponse(description="message_id 없음 — 재검증 불가"),
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
