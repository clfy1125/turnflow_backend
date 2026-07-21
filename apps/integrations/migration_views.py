"""DM 캠페인 이전 API — 분석 잡 시작/폴링/취소 + 후보 목록/적용/무시.

전 엔드포인트: JWT(IsAuthenticated) + ``?workspace_id=`` 멤버십 검사(views.py 관례). 초안
적용(apply)은 기존 AutoDMCampaignCreateSerializer 로 검증해 DM 본문 한도/키워드 검증을
재사용하고, status=INACTIVE 로 생성한다(활성 중복 409 는 활성화 시점에 발동).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.workspace.models import Workspace

from .migration_serializers import (
    CandidateApplyRequestSerializer,
    DMCampaignCandidateSerializer,
    DMMigrationJobSerializer,
    DMMigrationJobStartSerializer,
)
from .models import AutoDMCampaign, DMCampaignCandidate, DMMigrationJob, IGAccountConnection
from .serializers import AutoDMCampaignCreateSerializer, AutoDMCampaignSerializer

logger = logging.getLogger(__name__)

_NON_TERMINAL = list(DMMigrationJob.NON_TERMINAL_STATUSES)
_REUSABLE = list(DMMigrationJob.REUSABLE_STATUSES)
_REUSE_WINDOW = timedelta(hours=24)
_FORCE_COOLDOWN = timedelta(hours=1)

_WORKSPACE_PARAM = OpenApiParameter(
    name="workspace_id",
    location=OpenApiParameter.QUERY,
    required=True,
    type=str,
    description="대상 워크스페이스 UUID (요청자가 멤버여야 함).",
)
_TAGS = ["DM Migration"]


class MigrationCooldownError(APIException):
    """force 재분석이 종료 후 쿨다운(1h) 이내 — HTTP 429."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "최근 분석 직후에는 다시 분석할 수 없습니다."
    default_code = "migration_cooldown"

    @classmethod
    def make(cls, cooldown_until, wait_seconds):
        return cls(
            {
                "message": "최근 분석이 방금 끝났어요. 잠시 후 다시 시도해주세요.",
                "code": cls.default_code,
                "cooldown_until": cooldown_until.isoformat(),
                "retry_after": max(wait_seconds, 1),
            }
        )


class MigrationConflictError(APIException):
    """상태 충돌(취소 불가 종결 잡 / 이미 적용된 후보 등) — HTTP 409."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "요청을 처리할 수 없는 상태입니다."
    default_code = "conflict"

    @classmethod
    def make(cls, message, code):
        return cls({"message": message, "code": code})


class _WorkspaceScopedViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _get_workspace(self, request) -> Workspace:
        wid = request.query_params.get("workspace_id")
        if not wid:
            raise DRFValidationError({"workspace_id": ["필수 쿼리 파라미터입니다."]})
        try:
            workspace = Workspace.objects.get(id=wid)
        except (Workspace.DoesNotExist, DjangoValidationError, ValueError, TypeError) as exc:
            raise NotFound("워크스페이스를 찾을 수 없습니다.") from exc
        if not workspace.memberships.filter(user=request.user).exists():
            raise PermissionDenied("이 워크스페이스의 멤버가 아닙니다.")
        return workspace


class DMMigrationJobViewSet(_WorkspaceScopedViewSet):
    """DM 캠페인 이전 분석 잡 — 시작/폴링/목록/취소/후보 목록."""

    def _resolve_connection(self, workspace, ig_connection_id):
        if not ig_connection_id:
            conn = IGAccountConnection.get_active_connection(workspace)
            if not conn:
                raise DRFValidationError(
                    {"ig_connection_id": ["이 워크스페이스에 활성 IG 연동이 없습니다."]}
                )
            return conn
        try:
            conn = IGAccountConnection.objects.get(id=ig_connection_id)
        except (
            IGAccountConnection.DoesNotExist,
            DjangoValidationError,
            ValueError,
            TypeError,
        ) as exc:
            raise NotFound("IG 연동을 찾을 수 없습니다.") from exc
        if conn.workspace_id != workspace.id:
            raise PermissionDenied("이 IG 계정은 해당 워크스페이스에 속하지 않습니다.")
        if conn.status != IGAccountConnection.Status.ACTIVE or not conn.is_active:
            raise DRFValidationError(
                {"ig_connection_id": ["비활성 IG 연동입니다. 먼저 활성화하세요."]}
            )
        return conn

    def _get_job(self, pk, workspace) -> DMMigrationJob:
        job = (
            DMMigrationJob.objects.filter(id=pk, ig_connection__workspace=workspace)
            .select_related("ig_connection")
            .first()
        )
        if not job:
            raise NotFound("분석 잡을 찾을 수 없습니다.")
        return job

    @extend_schema(
        summary="DM 캠페인 이전 분석 시작",
        description=(
            "연동된 IG 계정의 최근 게시물·댓글·발신 DM 이력을 백그라운드에서 분석해, 기존 DM "
            "캠페인으로 보이는 게시물을 찾고 비활성(INACTIVE) 초안 캠페인 후보를 만든다.\n\n"
            "**동작 순서**\n"
            "1. 이 연결에 진행 중(비종결) 잡이 있으면 그 잡을 그대로 반환(**200**, `reused=true`).\n"
            "2. 24시간 내 완료된 결과가 있고 `force`=false 면 재사용(**200**, `reused=true`).\n"
            "3. `force`=true 인데 직전 분석 종료 후 1시간이 안 지났으면 **429**(쿨다운, "
            "`error.details.retry_after`/`cooldown_until`).\n"
            "4. 그 외엔 새 잡 생성 + 비동기 실행(**201**, `reused=false`).\n\n"
            "완료까지 보통 10~20분. `GET /dm-migration/jobs/{id}/` 로 3초 간격 폴링해 `status`/"
            "`stage`/`progress` 를 표시하라. 전 플랜 사용 가능(획득 기능)."
        ),
        parameters=[_WORKSPACE_PARAM],
        request=DMMigrationJobStartSerializer,
        responses={
            201: OpenApiResponse(
                response=DMMigrationJobSerializer, description="새 잡 생성됨(reused=false)."
            ),
            200: OpenApiResponse(
                response=DMMigrationJobSerializer, description="기존/최근 잡 재사용(reused=true)."
            ),
            400: OpenApiResponse(
                description="workspace_id 누락 / 활성 IG 연동 없음 / 비활성 연동."
            ),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님 / 다른 워크스페이스 연동."),
            404: OpenApiResponse(description="워크스페이스/IG 연동 없음."),
            429: OpenApiResponse(
                description="force 재분석 쿨다운(1h) — details.retry_after/cooldown_until."
            ),
            500: OpenApiResponse(description="서버 오류."),
        },
        examples=[
            OpenApiExample(
                "시작 요청",
                value={
                    "ig_connection_id": "b1a2...",
                    "media_limit": 50,
                    "force": False,
                    "llm_model": "deepseek",
                },
                request_only=True,
            ),
            OpenApiExample(
                "생성 응답",
                value={
                    "reused": False,
                    "job": {"id": "…", "status": "queued", "stage": "queued", "progress": 0},
                },
                response_only=True,
            ),
        ],
        tags=_TAGS,
    )
    def create(self, request):
        serializer = DMMigrationJobStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vd = serializer.validated_data
        workspace = self._get_workspace(request)
        conn = self._resolve_connection(workspace, vd.get("ig_connection_id"))
        now = timezone.now()

        # 1) 비종결 잡 존재 → 그대로 반환
        existing = (
            DMMigrationJob.objects.filter(ig_connection=conn, status__in=_NON_TERMINAL)
            .order_by("-created_at")
            .first()
        )
        if existing:
            return Response(
                {"reused": True, "job": DMMigrationJobSerializer(existing).data},
                status=status.HTTP_200_OK,
            )

        force = vd.get("force", False)
        recent = (
            DMMigrationJob.objects.filter(
                ig_connection=conn, status__in=_REUSABLE, finished_at__gte=now - _REUSE_WINDOW
            )
            .order_by("-finished_at")
            .first()
        )
        # 2) 24h 내 완료 결과 재사용
        if recent and not force:
            return Response(
                {"reused": True, "job": DMMigrationJobSerializer(recent).data},
                status=status.HTTP_200_OK,
            )
        # 3) force 쿨다운
        if force and recent and recent.finished_at and recent.finished_at > now - _FORCE_COOLDOWN:
            cooldown_until = recent.finished_at + _FORCE_COOLDOWN
            raise MigrationCooldownError.make(
                cooldown_until, int((cooldown_until - now).total_seconds())
            )

        # 4) 새 잡 생성 + 디스패치 (부분 UNIQUE 경합은 IntegrityError → 재조회)
        try:
            with transaction.atomic():
                job = DMMigrationJob.objects.create(
                    ig_connection=conn,
                    requested_by=request.user,
                    media_limit=vd.get("media_limit", 50),
                    llm_model=vd.get("llm_model", "deepseek"),
                )
        except IntegrityError:
            existing = (
                DMMigrationJob.objects.filter(ig_connection=conn, status__in=_NON_TERMINAL)
                .order_by("-created_at")
                .first()
            )
            if existing:
                return Response(
                    {"reused": True, "job": DMMigrationJobSerializer(existing).data},
                    status=status.HTTP_200_OK,
                )
            raise

        from .tasks import run_dm_migration_job  # 지연 import (celery 태스크 로딩 회피)

        run_dm_migration_job.delay(str(job.id))
        return Response(
            {"reused": False, "job": DMMigrationJobSerializer(job).data},
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="DM 캠페인 이전 잡 상태 조회(폴링)",
        description=(
            "분석 잡의 진행 상태를 반환한다. 프론트는 **3초 간격**으로 폴링하고 `status` 가 "
            "`ready`/`partial`/`failed`/`canceled`(종결)가 되면 멈춘다. `counters` 로 단계별 "
            "수집량을, `error` 로 실패 사유를 표시한다. 다른 워크스페이스의 잡은 404."
        ),
        parameters=[_WORKSPACE_PARAM],
        responses={
            200: DMMigrationJobSerializer,
            400: OpenApiResponse(description="workspace_id 누락."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님."),
            404: OpenApiResponse(description="잡 없음 / 다른 워크스페이스."),
            500: OpenApiResponse(description="서버 오류."),
        },
        tags=_TAGS,
    )
    def retrieve(self, request, pk=None):
        workspace = self._get_workspace(request)
        job = self._get_job(pk, workspace)
        return Response(DMMigrationJobSerializer(job).data)

    @extend_schema(
        summary="DM 캠페인 이전 잡 목록",
        description=(
            "워크스페이스(옵션: 특정 IG 연동)의 최근 분석 잡을 최신순 최대 20건 반환한다. "
            "페이지 진입 시 '최신 잡 찾기'용."
        ),
        parameters=[
            _WORKSPACE_PARAM,
            OpenApiParameter(
                name="ig_connection_id", location=OpenApiParameter.QUERY, required=False, type=str
            ),
        ],
        responses={
            200: DMMigrationJobSerializer(many=True),
            400: OpenApiResponse(description="workspace_id 누락."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님."),
            404: OpenApiResponse(description="워크스페이스 없음."),
            500: OpenApiResponse(description="서버 오류."),
        },
        tags=_TAGS,
    )
    def list(self, request):
        workspace = self._get_workspace(request)
        qs = DMMigrationJob.objects.filter(ig_connection__workspace=workspace)
        ig_id = request.query_params.get("ig_connection_id")
        if ig_id:
            qs = qs.filter(ig_connection_id=ig_id)
        jobs = list(qs.order_by("-created_at")[:20])
        return Response(DMMigrationJobSerializer(jobs, many=True).data)

    @extend_schema(
        summary="DM 캠페인 이전 잡 취소",
        description=(
            "진행 중(비종결) 잡을 취소한다. `queued` 면 즉시 `canceled`, 실행 중이면 "
            "`cancel_requested=true` 로 표시하고 파이프라인이 다음 단계 경계에서 멈춘다. "
            "이미 종결된 잡은 **409**(`error.details.code=job_already_terminal`)."
        ),
        parameters=[_WORKSPACE_PARAM],
        request=None,
        responses={
            200: DMMigrationJobSerializer,
            400: OpenApiResponse(description="workspace_id 누락."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님."),
            404: OpenApiResponse(description="잡 없음 / 다른 워크스페이스."),
            409: OpenApiResponse(description="이미 종결된 잡(job_already_terminal)."),
            500: OpenApiResponse(description="서버 오류."),
        },
        tags=_TAGS,
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        workspace = self._get_workspace(request)
        job = self._get_job(pk, workspace)
        if job.status not in _NON_TERMINAL:
            raise MigrationConflictError.make(
                "이미 종결된 잡은 취소할 수 없습니다.", "job_already_terminal"
            )
        now = timezone.now()
        job.cancel_requested = True
        if job.status == DMMigrationJob.Status.QUEUED:
            job.status = DMMigrationJob.Status.CANCELED
            job.finished_at = now
            job.raw_expires_at = now + timedelta(days=7)
            job.message = "사용자가 분석을 취소했습니다."
            job.save(
                update_fields=[
                    "cancel_requested",
                    "status",
                    "finished_at",
                    "raw_expires_at",
                    "message",
                    "updated_at",
                ]
            )
        else:
            job.save(update_fields=["cancel_requested", "updated_at"])
        return Response(DMMigrationJobSerializer(job).data)

    @extend_schema(
        summary="DM 캠페인 이전 후보 목록",
        description=(
            "잡이 감지한 캠페인 후보 목록. `status`(detected/applied/dismissed)·`band`"
            "(auto_draft/needs_review/template_only/excluded)로 필터. `evidence_raw` 는 원본 "
            "파기(완료 7일 후) 뒤 null 로 내려간다."
        ),
        parameters=[
            _WORKSPACE_PARAM,
            OpenApiParameter(
                name="status", location=OpenApiParameter.QUERY, required=False, type=str
            ),
            OpenApiParameter(
                name="band", location=OpenApiParameter.QUERY, required=False, type=str
            ),
        ],
        responses={
            200: DMCampaignCandidateSerializer(many=True),
            400: OpenApiResponse(description="workspace_id 누락."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님."),
            404: OpenApiResponse(description="잡 없음 / 다른 워크스페이스."),
            500: OpenApiResponse(description="서버 오류."),
        },
        tags=_TAGS,
    )
    @action(detail=True, methods=["get"])
    def candidates(self, request, pk=None):
        workspace = self._get_workspace(request)
        job = self._get_job(pk, workspace)
        qs = job.candidates.all()
        st = request.query_params.get("status")
        band = request.query_params.get("band")
        if st:
            qs = qs.filter(status=st)
        if band:
            qs = qs.filter(band=band)
        return Response(DMCampaignCandidateSerializer(qs, many=True).data)


class DMCampaignCandidateViewSet(_WorkspaceScopedViewSet):
    """DM 캠페인 이전 후보 — 적용(초안 캠페인 생성)/무시."""

    def _get_candidate(self, pk, workspace) -> DMCampaignCandidate:
        cand = (
            DMCampaignCandidate.objects.filter(id=pk, ig_connection__workspace=workspace)
            .select_related("ig_connection")
            .first()
        )
        if not cand:
            raise NotFound("후보를 찾을 수 없습니다.")
        return cand

    @extend_schema(
        summary="후보 적용 — 비활성 초안 캠페인 생성",
        description=(
            "후보를 실제 Auto DM 캠페인(**status=INACTIVE**)으로 만든다. 본문의 오버라이드 "
            "필드로 초안값을 덮어쓸 수 있고, 미지정 필드는 후보 초안값을 쓴다. 페이로드는 기존 "
            "`AutoDMCampaignCreateSerializer` 로 검증되므로 DM 본문 한도(버튼 640자/일반 1000바이트)"
            "·키워드 검증이 그대로 적용된다.\n\n"
            "INACTIVE 로 생성되므로 활성 중복(409) 검사는 걸리지 않는다 — **활성화 시점**에 같은 "
            "게시물의 활성 캠페인 충돌이 검사된다. 이미 적용된 후보는 **409**"
            "(`candidate_already_applied`). 무시(dismissed)된 후보는 다시 적용할 수 있다.\n\n"
            "`template_only` 후보(게시물 미상)는 본문에 `media_id` 를 반드시 지정해야 한다(없으면 400).\n\n"
            "⚠️ 활성화 전 안내: 매니챗 등 기존 자동화가 같은 게시물에 켜져 있으면 DM 이 중복 "
            "발송될 수 있으니 먼저 끄도록 안내하라(evidence.has_existing_campaign 도 확인)."
        ),
        parameters=[_WORKSPACE_PARAM],
        request=CandidateApplyRequestSerializer,
        responses={
            201: OpenApiResponse(
                description="{candidate, campaign} — 생성된 INACTIVE 캠페인 포함."
            ),
            400: OpenApiResponse(
                description="workspace_id 누락 / 검증 실패 / template_only 인데 media_id 없음."
            ),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님 / 다른 워크스페이스."),
            404: OpenApiResponse(description="후보 없음."),
            409: OpenApiResponse(description="이미 적용된 후보(candidate_already_applied)."),
            500: OpenApiResponse(description="서버 오류."),
        },
        examples=[
            OpenApiExample(
                "오버라이드 없이 적용",
                value={},
                request_only=True,
            ),
            OpenApiExample(
                "이름·키워드 수정 후 적용",
                value={
                    "name": "자료 DM",
                    "keyword_filter": ["자료", "링크"],
                    "keyword_mode": "any",
                },
                request_only=True,
            ),
        ],
        tags=_TAGS,
    )
    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        workspace = self._get_workspace(request)
        candidate = self._get_candidate(pk, workspace)
        if candidate.status == DMCampaignCandidate.Status.APPLIED:
            raise MigrationConflictError.make(
                "이미 적용된 후보입니다.", "candidate_already_applied"
            )

        req = CandidateApplyRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        ov = req.validated_data

        media_id = ov.get("media_id") or candidate.media_id
        if not media_id:
            raise DRFValidationError(
                {"media_id": ["게시물 미상(template_only) 후보는 media_id 를 지정해야 합니다."]}
            )

        name = (
            ov.get("name")
            or candidate.draft_name
            or f"[이전] {candidate.media_caption_excerpt[:30]}"
        ).strip()
        base_desc = ov["description"] if "description" in ov else candidate.draft_description
        desc = (
            (base_desc or "") + f"\n\n[DM 캠페인 이전으로 생성 — 신뢰도 {candidate.confidence:.0%}]"
        ).strip()

        payload = {
            "ig_connection_id": str(candidate.ig_connection_id),
            "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            "media_id": media_id,
            "media_url": candidate.media_permalink or None,
            "keyword_filter": ov.get("keyword_filter", candidate.suggested_keywords),
            "keyword_mode": ov.get("keyword_mode", candidate.suggested_keyword_mode),
            "name": name[:255],
            "description": desc,
            "opening_message_template": ov.get(
                "opening_message_template", candidate.draft_opening_message
            ),
            "public_reply_enabled": ov.get(
                "public_reply_enabled", bool(candidate.draft_public_reply_templates)
            ),
            "public_reply_templates": ov.get(
                "public_reply_templates", candidate.draft_public_reply_templates
            ),
        }
        cser = AutoDMCampaignCreateSerializer(data=payload)
        cser.is_valid(raise_exception=True)
        vdata = dict(cser.validated_data)
        vdata.pop("ig_connection_id", None)

        with transaction.atomic():
            cand = DMCampaignCandidate.objects.select_for_update().get(id=candidate.id)
            if cand.status == DMCampaignCandidate.Status.APPLIED:
                raise MigrationConflictError.make(
                    "이미 적용된 후보입니다.", "candidate_already_applied"
                )
            campaign = AutoDMCampaign.objects.create(
                ig_connection=cand.ig_connection,
                status=AutoDMCampaign.Status.INACTIVE,
                **vdata,
            )
            cand.status = DMCampaignCandidate.Status.APPLIED
            cand.applied_campaign = campaign
            cand.applied_at = timezone.now()
            cand.dismissed_at = None
            cand.save(
                update_fields=[
                    "status",
                    "applied_campaign",
                    "applied_at",
                    "dismissed_at",
                    "updated_at",
                ]
            )

        return Response(
            {
                "candidate": DMCampaignCandidateSerializer(cand).data,
                "campaign": AutoDMCampaignSerializer(campaign).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="후보 무시",
        description=(
            "후보를 무시(dismissed) 처리한다(목록에서 숨김용 상태). 이미 적용된(applied) 후보는 "
            "**409**(`candidate_already_applied`). 무시된 후보는 이후 apply 로 되살릴 수 있다."
        ),
        parameters=[_WORKSPACE_PARAM],
        request=None,
        responses={
            200: DMCampaignCandidateSerializer,
            400: OpenApiResponse(description="workspace_id 누락."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님 / 다른 워크스페이스."),
            404: OpenApiResponse(description="후보 없음."),
            409: OpenApiResponse(description="이미 적용된 후보(candidate_already_applied)."),
            500: OpenApiResponse(description="서버 오류."),
        },
        tags=_TAGS,
    )
    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        workspace = self._get_workspace(request)
        candidate = self._get_candidate(pk, workspace)
        if candidate.status == DMCampaignCandidate.Status.APPLIED:
            raise MigrationConflictError.make(
                "이미 적용된 후보입니다.", "candidate_already_applied"
            )
        candidate.status = DMCampaignCandidate.Status.DISMISSED
        candidate.dismissed_at = timezone.now()
        candidate.save(update_fields=["status", "dismissed_at", "updated_at"])
        return Response(DMCampaignCandidateSerializer(candidate).data)
