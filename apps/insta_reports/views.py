"""인스타 성장 리포트 API.

엔드포인트 4종 (모두 `/api/v1/insta-reports/`):
    GET  targets/        분석 팝업 데이터(계정 목록 + 생성 가능 여부 + 이용 횟수)
    POST .              생성 시작 (202)
    GET  .              내 리포트 목록
    GET  {id}/          진행 상태 폴링 / 완료 결과
    GET  {id}/download/ 리포트 HTML 다운로드 (인증 필수)

프론트 계약의 단일 소스는 이 파일의 `@extend_schema` 다(사내 MCP 문서 서버가 OpenAPI
스키마를 그대로 읽는다). 필드를 바꾸면 여기 설명도 함께 고칠 것.
"""

from __future__ import annotations

import logging

from django.http import FileResponse, Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.models import IGAccountConnection
from apps.workspace.models import Workspace

from . import ig_profile, progress, quota
from .models import InstagramReport, ReportStatus
from .serializers import (
    ReportCreateSerializer,
    ReportListItemSerializer,
    ReportSerializer,
    ReportTargetsResponseSerializer,
)
from .tasks import generate_insta_report

logger = logging.getLogger(__name__)

TAG = "insta-reports"

# 문서용 공통 에러 응답 (표준 포맷: apps/core/exceptions.custom_exception_handler)
ERR_401 = OpenApiResponse(description="인증 실패 — Authorization 헤더 누락/만료")
ERR_403 = OpenApiResponse(
    description=(
        "프로 플랜 전용 기능입니다.\n\n"
        "```json\n"
        '{"success": false, "error": {"code": 403, '
        '"message": "인스타 성장 리포트는 프로 플랜에서 이용할 수 있어요.", '
        '"details": {"code": "PLAN_REQUIRED", "plan_required": "pro"}}}\n'
        "```"
    )
)
ERR_404 = OpenApiResponse(description="대상을 찾을 수 없음(내 워크스페이스 소유가 아닌 경우 포함)")
ERR_500 = OpenApiResponse(description="서버 오류")


def _user_workspaces(user):
    return Workspace.objects.filter(memberships__user=user)


def _my_connections(user):
    """내가 속한 워크스페이스의 IG 연동 (활성 우선 정렬)."""
    return (
        IGAccountConnection.objects.filter(workspace__in=_user_workspaces(user))
        .select_related("workspace")
        .order_by("-is_active", "-updated_at")
        .distinct()
    )


def _my_reports(user):
    return InstagramReport.objects.filter(workspace__in=_user_workspaces(user)).distinct()


class ReportTargetsView(APIView):
    """분석 팝업 데이터."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[TAG],
        summary="분석 가능한 IG 계정 목록",
        description="""
        ## 목적
        "인스타 분석" 팝업을 한 번의 호출로 그릴 수 있게, **연동된 IG 계정 카드 + 분석 버튼 활성
        여부 + 이번 달 남은 횟수**를 함께 준다.

        ## 언제 호출하나
        팝업을 열 때마다. 응답의 `profile_picture_url` / `followers_count` / `media_count` 는
        서버가 6시간 캐시로 관리하며, 만료됐으면 이 호출이 인스타에서 새로 받아 갱신한다
        (실패해도 캐시값으로 응답 — 팝업이 깨지지 않는다).

        ## 인증
        JWT 필수. 응답에는 **내가 속한 워크스페이스의 연동만** 담긴다.

        ## 프론트 렌더 가이드
        - 카드 제목 = `name`, 부제 = `display_line`
          (예: `@reels_drgn · 팔로워 98,293 · 게시물 672개`)
        - 분석 버튼 `disabled = !can_generate`, 비활성 이유 툴팁 = `reason_message`
        - `running_report_id` 가 있으면 버튼 대신 "생성 중" + 그 id 로 바로 폴링 시작
        - 헤더에 "이번 달 남은 리포트 {quota.total_remaining}회" 표기
        - 소요 시간 안내 = `estimated_minutes` (평균 약 15분)

        ## 이용 횟수 정책
        **IG 계정 1개당 캘린더월 1회**(프로). 추가 IG 계정(9,900원)을 붙이면 그 계정 몫이
        1회 더 늘어난다 — 연동 2개면 각 계정 1회씩, 이번 달 총 2회. 실패한 생성은 차감하지 않는다.
        """,
        responses={
            200: ReportTargetsResponseSerializer,
            401: ERR_401,
            500: ERR_500,
        },
        examples=[
            OpenApiExample(
                "프로 · 연동 2개(1개는 이번 달 사용 완료)",
                value={
                    "plan_required": "pro",
                    "has_feature": True,
                    "estimated_minutes": 18,
                    "estimated_seconds": 1100,
                    "quota": {
                        "per_account_limit": 1,
                        "total_limit": 2,
                        "total_used": 1,
                        "total_remaining": 1,
                        "period_end": "2026-08-01T00:00:00+09:00",
                    },
                    "accounts": [
                        {
                            "connection_id": "3f1c…",
                            "username": "reels_drgn",
                            "name": "이지용 | 릴스 드래곤",
                            "profile_picture_url": "https://media.turnflow.clfy.ai.kr/ig/…webp",
                            "followers_count": 98293,
                            "media_count": 672,
                            "display_line": "@reels_drgn · 팔로워 98,293 · 게시물 672개",
                            "is_active": True,
                            "can_generate": True,
                            "reason": None,
                            "reason_message": "",
                            "used": 0,
                            "limit": 1,
                            "remaining": 1,
                            "next_available_at": None,
                            "running_report_id": None,
                            "last_report": None,
                        },
                        {
                            "connection_id": "9ab2…",
                            "username": "mini_ai_",
                            "name": "미니 AI",
                            "profile_picture_url": "",
                            "followers_count": 5120,
                            "media_count": 88,
                            "display_line": "@mini_ai_ · 팔로워 5,120 · 게시물 88개",
                            "is_active": True,
                            "can_generate": False,
                            "reason": "QUOTA_EXCEEDED",
                            "reason_message": (
                                "이번 달 이 계정의 리포트를 이미 사용했어요. "
                                "다음 달 1일에 다시 만들 수 있어요."
                            ),
                            "used": 1,
                            "limit": 1,
                            "remaining": 0,
                            "next_available_at": "2026-08-01T00:00:00+09:00",
                            "running_report_id": None,
                            "last_report": {
                                "id": "c0de…",
                                "created_at": "2026-07-12T10:03:22+09:00",
                                "period_from": "2026-02-03",
                                "period_to": "2026-07-11",
                            },
                        },
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request):
        connections = list(_my_connections(request.user))
        active = [c for c in connections if c.is_active]
        running_by_ws = {}
        accounts = []
        for conn in connections:
            # 팝업 표시용 통계 갱신(6시간 캐시, fail-soft)
            ig_profile.refresh_stats(conn)
            ws_id = conn.workspace_id
            if ws_id not in running_by_ws:
                running_by_ws[ws_id] = quota.running_report(conn.workspace)
            verdict = quota.evaluate(conn, request.user, running=running_by_ws[ws_id])
            last = (
                _my_reports(request.user)
                .filter(ig_connection=conn, status=ReportStatus.SUCCEEDED)
                .order_by("-created_at")
                .first()
            )
            accounts.append(
                {
                    "connection_id": conn.id,
                    "username": conn.username or "",
                    "name": conn.name or conn.username or "",
                    "profile_picture_url": conn.profile_picture_url or "",
                    "followers_count": conn.followers_count,
                    "media_count": conn.media_count,
                    "display_line": ig_profile.display_line(conn),
                    "is_active": conn.is_active,
                    "can_generate": verdict["can_generate"],
                    "reason": verdict["reason"],
                    "reason_message": verdict["reason_message"],
                    "used": verdict["used"],
                    "limit": verdict["limit"],
                    "remaining": verdict["remaining"],
                    "next_available_at": (
                        verdict["period_end"]
                        if verdict["reason"] == quota.REASON_QUOTA_EXCEEDED
                        else None
                    ),
                    "running_report_id": verdict["running_report_id"],
                    "last_report": (
                        {
                            "id": str(last.id),
                            "created_at": last.created_at.isoformat(),
                            "period_from": (
                                last.period_from.isoformat() if last.period_from else None
                            ),
                            "period_to": last.period_to.isoformat() if last.period_to else None,
                        }
                        if last
                        else None
                    ),
                }
            )

        has_feature = (
            any(quota.has_feature(c.workspace, request.user) for c in connections)
            if connections
            else False
        )
        return Response(
            {
                "plan_required": quota.PLAN_REQUIRED,
                "has_feature": has_feature,
                "estimated_minutes": round(progress.AVERAGE_TOTAL_SECONDS / 60),
                "estimated_seconds": progress.AVERAGE_TOTAL_SECONDS,
                "quota": quota.quota_summary(request.user, active),
                "accounts": accounts,
            }
        )


class ReportListCreateView(ListAPIView):
    """`GET` 내 리포트 목록 · `POST` 생성 시작."""

    permission_classes = [IsAuthenticated]
    serializer_class = ReportListItemSerializer

    def get_queryset(self):
        # 스키마 생성 시엔 AnonymousUser 로 호출된다(drf-spectacular) → 빈 쿼리셋.
        if getattr(self, "swagger_fake_view", False) or not self.request.user.is_authenticated:
            return InstagramReport.objects.none()
        qs = _my_reports(self.request.user).order_by("-created_at")
        conn = self.request.query_params.get("connection_id")
        if conn:
            qs = qs.filter(ig_connection_id=conn)
        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        return qs

    @extend_schema(
        tags=[TAG],
        summary="내 리포트 목록",
        description="""
        ## 목적
        지난 리포트를 다시 내려받을 수 있게 히스토리를 준다. **리포트 파일·집계는 계속 보관**한다
        (자동 삭제 없음).

        ## 인증
        JWT 필수 — 내가 속한 워크스페이스의 리포트만 반환.

        ## 쿼리 파라미터
        | 이름 | 예시 | 설명 |
        |---|---|---|
        | `connection_id` | `3f1c…` | 특정 IG 계정의 리포트만 |
        | `status` | `succeeded` | `queued`/`running`/`succeeded`/`failed`/`cancelled` |
        | `page` | `2` | 페이지네이션(페이지당 20건) |

        ## 프론트 가이드
        `download_ready=true` 인 행만 다운로드 버튼을 활성화하고 `download_url` 을 쓴다
        (Authorization 헤더가 필요하므로 fetch → blob 저장 방식 권장).
        """,
        responses={200: ReportListItemSerializer(many=True), 401: ERR_401, 500: ERR_500},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=[TAG],
        summary="리포트 생성 시작 (비동기)",
        description="""
        ## 목적
        선택한 IG 계정의 최근 게시물(최대 100개)을 수집·분석해 **성장 리포트(HTML 파일)**를 만든다.

        ## ⏱️ 평균 15~18분 (최대 30분)
        영상 30여 개를 AI로 한 편씩 보고, 그 결과로 문장을 쓰는 구조라 오래 걸린다.
        **202 를 받으면 즉시 폴링 화면으로 전환**하고, `GET /insta-reports/{id}/` 를
        **3초 간격**으로 호출해 `progress` / `steps` 를 갱신하라. 완료되면 서버가 이메일도 보낸다
        (사용자가 창을 닫아도 결과를 받을 수 있음 → 폴링 실패가 곧 생성 실패는 아니다).

        ## 인증 · 권한
        JWT 필수. **프로 플랜 전용**(`insta_report`). 이용 횟수는 **IG 계정당 캘린더월 1회**이며
        추가 IG 계정마다 1회씩 늘어난다. 관리자는 무제한.

        ## 요청
        `connection_id` — `GET /insta-reports/targets/` 가 준 값 그대로.

        ## 실패 코드 (`error.details.code`)
        | HTTP | code | 의미 | 프론트 처리 |
        |---|---|---|---|
        | 403 | `PLAN_REQUIRED` | 프로 아님 | 업그레이드 유도 |
        | 409 | `ALREADY_RUNNING` | 생성 중인 리포트 있음 | `running_report_id` 로 폴링 화면 이동 |
        | 429 | `PLAN_LIMIT_EXCEEDED` | 이번 달 이 계정 횟수 소진 | "다음 달 1일" 안내 |
        | 400 | `CONNECTION_INACTIVE` / `TOKEN_EXPIRED` | 계정 비활성 / 연결 만료 | 계정 설정으로 이동 |
        | 404 | — | 내 소유가 아닌 `connection_id` | — |

        ## 주의
        - 생성이 **실패하면 이용 횟수를 차감하지 않는다** — 사용자에게 "다시 시도" 를 열어 줘도 된다.
        - 조회수를 확인할 수 있는 릴스가 5개 미만이면 생성이 시작된 뒤 `NOT_ENOUGH_REELS` 로
          실패한다(수집 후에야 알 수 있음). 이 역시 횟수 미차감.
        """,
        request=ReportCreateSerializer,
        responses={
            202: ReportSerializer,
            400: OpenApiResponse(
                description="계정 비활성/연결 만료 또는 요청 본문 오류 "
                "(`error.details.code` = `CONNECTION_INACTIVE`|`TOKEN_EXPIRED`)"
            ),
            401: ERR_401,
            403: ERR_403,
            404: ERR_404,
            409: OpenApiResponse(
                description=(
                    "이미 생성 중인 리포트가 있습니다.\n\n"
                    "```json\n"
                    '{"success": false, "error": {"code": 409, '
                    '"message": "리포트를 만들고 있어요. 완료된 뒤에 다시 시도해 주세요.", '
                    '"details": {"code": "ALREADY_RUNNING", "running_report_id": "…"}}}\n'
                    "```"
                )
            ),
            429: OpenApiResponse(
                description=(
                    "이번 달 이 계정의 이용 횟수를 모두 썼습니다.\n\n"
                    "```json\n"
                    '{"success": false, "error": {"code": "PLAN_LIMIT_EXCEEDED", '
                    '"message": "플랜 사용량 한도를 초과했습니다", '
                    '"details": {"metric": "insta_report_monthly_per_account", '
                    '"current": 1, "limit": 1, "plan": "pro"}}}\n'
                    "```"
                )
            ),
            500: ERR_500,
        },
        examples=[
            OpenApiExample(
                "요청",
                value={"connection_id": "3f1c9b2e-0a44-4a3c-9d0e-1b2c3d4e5f60"},
                request_only=True,
            ),
            OpenApiExample(
                "202 — 생성 시작",
                value={
                    "id": "8f14e45f-ea11-4c1e-9b9a-1e2d3c4b5a60",
                    "status": "queued",
                    "stage": "queued",
                    "stage_label": "대기 중",
                    "progress": 0,
                    "message": "대기 중이에요",
                    "eta_seconds": 1090,
                    "steps": [
                        {
                            "key": "collecting",
                            "label": "게시물 모으는 중",
                            "status": "pending",
                            "detail": "",
                            "progress_start": 3,
                            "progress_end": 15,
                            "expected_seconds": 120,
                        },
                    ],
                    "account": {
                        "connection_id": "3f1c…",
                        "username": "reels_drgn",
                        "name": "이지용 | 릴스 드래곤",
                        "followers_count": 98293,
                        "media_count": 672,
                    },
                    "download_ready": False,
                    "download_url": None,
                    "error_code": "",
                    "error_message": "",
                },
                response_only=True,
            ),
        ],
    )
    def post(self, request):
        serializer = ReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection_id = serializer.validated_data["connection_id"]

        connection = _my_connections(request.user).filter(id=connection_id).first()
        if connection is None:
            raise Http404("연동된 인스타그램 계정을 찾을 수 없습니다.")

        quota.ensure_can_generate(connection, request.user)

        report = InstagramReport.objects.create(
            workspace=connection.workspace,
            ig_connection=connection,
            requested_by=request.user,
            ig_username=connection.username or "",
            ig_name=connection.name or "",
            followers_snapshot=connection.followers_count,
            media_count_snapshot=connection.media_count,
            message="대기 중이에요",
            stage_expected_seconds=progress.stage_expected("queued"),
        )
        async_result = generate_insta_report.delay(str(report.id))
        InstagramReport.objects.filter(pk=report.pk).update(celery_task_id=async_result.id or "")
        logger.info(
            "insta_report: queued report=%s conn=%s user=%s",
            report.id,
            connection.id,
            request.user.id,
        )
        return Response(ReportSerializer(report).data, status=status.HTTP_202_ACCEPTED)


class ReportDetailView(APIView):
    """진행 상태 폴링 / 완료 결과."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[TAG],
        summary="리포트 진행 상태 조회 (폴링)",
        description="""
        ## 목적
        생성 진행률과 완료 결과를 준다. **3초 간격 폴링** 권장.

        ## 진행률 읽는 법
        - `progress` (0~100) 은 **되돌아가지 않는다**.
        - `steps[]` 를 그대로 체크리스트로 렌더하면 된다
          (`status`: `done`/`active`/`pending`/`failed`, `label` 은 그대로 노출 가능).
        - "인사이트 쓰는 중" 처럼 3~5분간 서버 이벤트가 없는 단계가 있다.
          멈춰 보이지 않게, `stage_started_at` + `stage_expected_seconds` 로 진행률을
          클라이언트에서 부드럽게 보간하라(단계 끝값 `progress_end` 를 넘기지 말 것).
        - `eta_seconds` 는 남은 예상 시간(완료·실패 시 null).

        ## 단계 (10)
        `queued` → `collecting` → `metrics` → `preparing` → `extracting` → `comments`
        → `synthesizing` → `verifying` → `rendering` → `exporting` → (`done`)

        ## 종료 판정
        | status | 처리 |
        |---|---|
        | `succeeded` | 팝업 띄우고 `download_url` 로 다운로드 (이메일도 발송됨) |
        | `failed` | `error_message`(한국어) 노출 + 재시도 버튼. 실패는 이용 횟수 미차감 |
        | `cancelled` | 관리자/스위퍼가 중단 |

        ## 실패 코드 (`error_code`)
        `VIEWS_UNAVAILABLE`(조회수 수집 실패) · `NOT_ENOUGH_REELS`(릴스 5개 미만) ·
        `TOKEN_INVALID`(연결 만료) · `EXTRACT_FAILED` · `SYNTH_FAILED` · `RENDER_FAILED` · `TIMEOUT` · `INTERNAL`
        → 문구는 `error_message` 에 사람말로 이미 들어 있으니 그대로 써도 된다.

        ## 인증
        JWT 필수. 내 워크스페이스 소유가 아니면 404.
        """,
        responses={200: ReportSerializer, 401: ERR_401, 404: ERR_404, 500: ERR_500},
        examples=[
            OpenApiExample(
                "진행 중 (영상 분석 12/30)",
                value={
                    "id": "8f14e45f…",
                    "status": "running",
                    "stage": "extracting",
                    "stage_label": "영상 분석 중",
                    "progress": 44,
                    "message": "영상 분석 12/30",
                    "eta_seconds": 640,
                    "stage_started_at": "2026-07-29T20:41:02+09:00",
                    "stage_expected_seconds": 360,
                    "download_ready": False,
                    "download_url": None,
                    "error_code": "",
                    "error_message": "",
                },
                response_only=True,
            ),
            OpenApiExample(
                "완료",
                value={
                    "id": "8f14e45f…",
                    "status": "succeeded",
                    "stage": "done",
                    "stage_label": "",
                    "progress": 100,
                    "message": "리포트가 완성됐어요",
                    "eta_seconds": None,
                    "posts_analyzed": 100,
                    "reels_with_views": 75,
                    "videos_analyzed": 28,
                    "comments_analyzed": 214,
                    "period_from": "2026-02-03",
                    "period_to": "2026-07-28",
                    "download_ready": True,
                    "download_url": "/api/v1/insta-reports/8f14e45f…/download/",
                    "html_bytes": 486213,
                    "elapsed_seconds": 903,
                    "error_code": "",
                    "error_message": "",
                },
                response_only=True,
            ),
        ],
    )
    def get(self, request, pk):
        report = _my_reports(request.user).filter(pk=pk).first()
        if report is None:
            raise Http404("리포트를 찾을 수 없습니다.")
        return Response(ReportSerializer(report).data)


class ReportDownloadView(APIView):
    """리포트 HTML 다운로드 (인증 필수 — 공개 URL 없음)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[TAG],
        summary="리포트 다운로드 (HTML 파일)",
        description="""
        ## 목적
        완료된 리포트를 **자기완결 HTML 파일 1개**로 내려준다. 썸네일은 data-URI 로,
        차트 라이브러리는 파일 안에 인라인돼 있어 **인터넷 없이 열어도** 그대로 보인다.
        (탭 4개 · 차트 3종이 살아 있는 인터랙티브 문서다. 사용자가 브라우저에서
        Ctrl+P 로 인쇄/PDF 저장하면 인쇄용 레이아웃으로 나오게 CSS 도 넣어 뒀다.)

        ## 보안
        리포트에는 **팔로워 댓글 원문**이 들어가므로 공개 URL 을 제공하지 않는다.
        이 엔드포인트만이 접근 경로이며, 내 워크스페이스 소유가 아니면 404 다.
        응답은 항상 `Content-Disposition: attachment` (브라우저 내 실행 없이 저장)
        + `X-Content-Type-Options: nosniff` 로 내려간다.

        ## 프론트 구현
        `<a href>` 로는 Authorization 헤더를 실을 수 없다. fetch → blob 저장을 쓴다:
        ```js
        const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'turnflow_report.html';   // 서버도 Content-Disposition 을 준다
        a.click();
        ```
        > 앱 안에서 바로 보여 주고 싶다면 blob URL 을 `<iframe sandbox>` 로 띄우면 된다.
        > 같은 출처에 그대로 렌더하지는 말 것(리포트 안에 스크립트가 들어 있다).

        ## 오류
        | HTTP | 의미 |
        |---|---|
        | 404 | 리포트 없음 / 내 소유 아님 |
        | 409 | 아직 생성 중이거나 실패해 파일이 없음 (`error.details.code = FILE_NOT_READY`) |
        """,
        responses={
            # 파일 응답임을 스키마에 명시 — 프론트(MCP)가 JSON 으로 오해하지 않게.
            (200, "text/html"): OpenApiResponse(
                response=OpenApiTypes.STR,
                description="리포트 HTML (자기완결 단일 파일, 첨부 다운로드)",
            ),
            401: ERR_401,
            404: ERR_404,
            409: OpenApiResponse(description="파일 미준비 (`FILE_NOT_READY`)"),
            500: ERR_500,
        },
    )
    def get(self, request, pk):
        report = _my_reports(request.user).filter(pk=pk).first()
        if report is None:
            raise Http404("리포트를 찾을 수 없습니다.")
        if not report.html_file:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": status.HTTP_409_CONFLICT,
                        "message": "아직 리포트 파일이 준비되지 않았어요.",
                        "details": {"code": "FILE_NOT_READY", "status": report.status},
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )
        filename = (
            f"turnflow_report_{report.ig_username or 'instagram'}_{report.created_at:%Y%m%d}.html"
        )
        response = FileResponse(
            report.html_file.open("rb"), content_type="text/html; charset=utf-8"
        )
        # 브라우저에서 바로 실행되지 않도록 항상 첨부로 내려보낸다(리포트에 인라인 스크립트가 있다).
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response
