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
from django.db.models import Count, Max, Min, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.workspace.models import Workspace

from .dm_migration import visibility
from .migration_serializers import (
    BulkApplyRequestSerializer,
    CandidateApplyRequestSerializer,
    CandidateConfirmRequestSerializer,
    CandidateSummarySerializer,
    DMCampaignCandidateSerializer,
    DMMigrationJobSerializer,
    DMMigrationJobStartResponseSerializer,
    DMMigrationJobStartSerializer,
    PaginatedCandidateSerializer,
)
from .models import AutoDMCampaign, DMCampaignCandidate, DMMigrationJob, IGAccountConnection
from .serializers import AutoDMCampaignCreateSerializer, AutoDMCampaignSerializer

logger = logging.getLogger(__name__)

_NON_TERMINAL = list(DMMigrationJob.NON_TERMINAL_STATUSES)
_REUSABLE = list(DMMigrationJob.REUSABLE_STATUSES)
# 완료 결과 재사용(캐시) 창 — 이 기간엔 새로 돌리지 않고 저장된 결과를 그대로 준다.
# 캠페인은 자주 바뀌지 않는데 1회 분석이 계정당 수십 분·수천 호출이라 길게 잡는다.
# 연동 직후 자동 선작업의 결과도 이 창 안에서 재사용되어, 사용자는 즉시 결과를 본다.
_REUSE_WINDOW = timedelta(days=7)
# 사용자가 "다시 찾기"(force)를 눌렀을 때의 최소 간격 — 연타 방어.
_FORCE_COOLDOWN = timedelta(hours=6)

_WORKSPACE_PARAM = OpenApiParameter(
    name="workspace_id",
    location=OpenApiParameter.QUERY,
    required=True,
    type=str,
    description="대상 워크스페이스 UUID (요청자가 멤버여야 함).",
)
_TAGS = ["DM Migration"]


class MigrationCooldownError(APIException):
    """force 재분석이 종료 후 쿨다운(_FORCE_COOLDOWN=6h) 이내 — HTTP 429."""

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


_ORDERING_WHITELIST = {
    "media_timestamp",
    "-media_timestamp",
    "confidence",
    "-confidence",
    "support_score",
    "-support_score",
    "draft_name",
    "-draft_name",
    "created_at",
    "-created_at",
}


def _int_param(request, name, default):
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


def _filter_candidates(qs, params):
    """후보 목록 필터/검색/정렬 — auto-dm-campaigns 목록과 같은 계약."""
    st = params.get("status")
    band = params.get("band")
    if st:
        qs = qs.filter(status=st)
    if band:
        qs = qs.filter(band=band)
    if params.get("needs_confirm") in ("1", "true", "True"):
        qs = qs.filter(confirm_required=True, confirmed_at__isnull=True)
    search = (params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(draft_name__icontains=search)
            | Q(draft_opening_message__icontains=search)
            | Q(media_caption_excerpt__icontains=search)
            | Q(offer_button_label__icontains=search)
        )
    after, before = params.get("media_after"), params.get("media_before")
    if after:
        qs = qs.filter(media_timestamp__date__gte=after)
    if before:
        qs = qs.filter(media_timestamp__date__lte=before)
    ordering = params.get("ordering") or "-media_timestamp"
    if ordering not in _ORDERING_WHITELIST:
        raise DRFValidationError(
            {"ordering": [f"허용되지 않는 정렬입니다. 가능: {sorted(_ORDERING_WHITELIST)}"]}
        )
    # media_timestamp 는 template_only 후보에서 NULL 이라 뒤로 몰아 안정 정렬.
    return qs.order_by(ordering, "-created_at")


def apply_candidate(candidate: DMCampaignCandidate, ov: dict) -> AutoDMCampaign:
    """후보 → 비활성 초안 캠페인. apply / apply-all 공용 단일 소스.

    ⚠️ ``backfill_existing_comments`` 는 **항상 False** 로 고정한다.
    이전 대상 게시물의 과거 댓글 작성자는 **이미 예전 서비스로 DM 을 받은 사람들**이라,
    소급 발송을 켜면 최대 500명에게 같은 DM 이 두 번째로 간다. 사용자가 원하면 캠페인
    수정에서 직접 켤 수 있다 — 기본값의 실수 비용이 너무 크다.
    """
    media_id = ov.get("media_id") or candidate.media_id
    if not media_id:
        raise DRFValidationError(
            {"media_id": ["게시물 미상(template_only) 후보는 media_id 를 지정해야 합니다."]}
        )
    name = (
        ov.get("name") or candidate.draft_name or f"[이전] {candidate.media_caption_excerpt[:30]}"
    ).strip()
    base_desc = ov["description"] if "description" in ov else candidate.draft_description
    desc = (
        (base_desc or "") + f"\n\n[다른 서비스에서 불러옴 — 신뢰도 {candidate.confidence:.0%}]"
    ).strip()

    # 사용자가 확인/수정한 링크가 있으면 그것이 우선한다.
    url = ov.get("link_button_url") or candidate.confirmed_url or candidate.offer_url
    label = ov.get("link_button_label") or candidate.offer_button_label or "자료 받기"
    link_buttons = ov.get("link_buttons")
    if link_buttons is None:
        link_buttons = [{"url": url, "label": label[:20]}] if url else []

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
        "link_buttons": link_buttons,
        "backfill_existing_comments": False,  # ← 서버 강제. 위 docstring 참조.
    }
    # 팔로우 확인 단계가 원본에 있었으면 그대로 살린다(복원된 문구 사용).
    gate = ov.get("follow_gate_enabled")
    if gate is None:
        gate = candidate.gate_detected
    if gate:
        payload["follow_gate_enabled"] = True
        if candidate.gate_button_label:
            payload["follow_gate_button_label"] = candidate.gate_button_label[:20]
        if candidate.gate_message:
            payload["follow_gate_prompt_templates"] = [candidate.gate_message[:640]]
        if candidate.draft_opening_message:
            payload["reward_message_template"] = candidate.draft_opening_message[:640]

    cser = AutoDMCampaignCreateSerializer(data=payload)
    cser.is_valid(raise_exception=True)
    vdata = dict(cser.validated_data)
    vdata.pop("ig_connection_id", None)

    with transaction.atomic():
        cand = DMCampaignCandidate.objects.select_for_update().get(id=candidate.id)
        # 적용됐더라도 그 캠페인이 삭제됐으면(applied_campaign=NULL) 다시 적용할 수 있어야 한다.
        if cand.status == DMCampaignCandidate.Status.APPLIED and cand.applied_campaign_id:
            raise MigrationConflictError.make(
                "이미 적용된 후보입니다.", "candidate_already_applied"
            )
        campaign = AutoDMCampaign.objects.create(
            ig_connection=cand.ig_connection,
            status=AutoDMCampaign.Status.INACTIVE,
            source="dm_migration",
            **vdata,
        )
        cand.status = DMCampaignCandidate.Status.APPLIED
        cand.applied_campaign = campaign
        cand.applied_at = timezone.now()
        cand.dismissed_at = None
        cand.save(
            update_fields=["status", "applied_campaign", "applied_at", "dismissed_at", "updated_at"]
        )
    return campaign


def heal_orphaned_applied(job) -> int:
    """적용했던 캠페인이 **삭제됐으면** 후보를 다시 '적용 가능'(detected) 으로 되돌린다.

    ``AutoDMCampaign`` 삭제 시 ``applied_campaign`` 은 SET_NULL 이라 후보만 applied 로 남는다.
    그대로 두면 캠페인은 0개인데 화면은 "이미 다 불러왔어요" 가 되어 **그 게시물을 영영 다시
    불러올 수 없다**(dev 실계정에서 실제로 이 상태가 나왔다).

    ⚠️ ``applied_at`` 은 **지우지 않는다**. 지우면 "한 번도 안 불러온 후보" 와 "불러왔다가
    사용자가 지운 후보" 를 응답에서 구분할 수 없어, 프론트의 「N개 찾음 · 불러오기」 배너가
    사용자의 삭제 결정을 잊고 같은 걸 다시 권한다(프론트 2026-08-16 보고).
    → 판별식: ``status == "detected" and applied_at != null`` = **불러왔다가 지운 것**.
    """
    return job.candidates.filter(
        status=DMCampaignCandidate.Status.APPLIED, applied_campaign__isnull=True
    ).update(status=DMCampaignCandidate.Status.DETECTED, updated_at=timezone.now())


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
            "2. **7일** 내 완료된 결과가 있고 `force`=false 면 재사용(**200**, `reused=true`).\n"
            "3. `force`=true 인데 직전 분석 종료 후 **6시간**이 안 지났으면 **429**(쿨다운, "
            "`error.details.retry_after`/`cooldown_until`).\n"
            "4. 그 외엔 새 잡 생성 + 비동기 실행(**201**, `reused=false`).\n\n"
            "> **거부(429)는 `force=true` 일 때만 납니다.** 그냥 요청하면 캐시가 있어도 거부가 "
            "아니라 **200 + `reused=true`** 로 저장된 결과를 그대로 줍니다.\n\n"
            "완료까지 보통 10~20분. `GET /dm-migration/jobs/{id}/` 로 3초 간격 폴링해 `status`/"
            "`stage`/`progress` 를 표시하라. 전 플랜 사용 가능(획득 기능)."
        ),
        parameters=[_WORKSPACE_PARAM],
        request=DMMigrationJobStartSerializer,
        responses={
            201: OpenApiResponse(
                response=DMMigrationJobStartResponseSerializer,
                description="새 잡 생성됨 — `{reused: false, job: {...}}`.",
            ),
            200: OpenApiResponse(
                response=DMMigrationJobStartResponseSerializer,
                description="기존/캐시 잡 재사용 — `{reused: true, job: {...}}`.",
            ),
            400: OpenApiResponse(
                description="workspace_id 누락 / 활성 IG 연동 없음 / 비활성 연동."
            ),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님 / 다른 워크스페이스 연동."),
            404: OpenApiResponse(description="워크스페이스/IG 연동 없음."),
            429: OpenApiResponse(
                description="force 재분석 쿨다운(6h) — details.retry_after/cooldown_until."
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
        # 2) 캐시 창(_REUSE_WINDOW=7일) 내 완료 결과 재사용 — 거부가 아니라 200 으로 그대로 준다
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
            "잡이 감지한 캠페인 후보 목록.\n\n"
            "**응답은 항상 봉투** `{count, next, previous, results}` 다 "
            "(캠페인 목록과 반대다 — 그쪽은 `page` 를 줘야 봉투가 된다).\n\n"
            "`evidence_raw` 는 원본 파기(완료 7일 후) 뒤 null 로 내려간다.\n\n"
            "### 실제로 나오는 `band` 는 3종\n"
            "| band | 뜻 |\n|---|---|\n"
            "| `auto_draft` | 지지 0.60 이상 — 자동 적용해도 되는 것 |\n"
            "| `needs_review` | 근거가 약함 — 링크 확인을 받는 것 |\n"
            "| `excluded` | **DM 을 못 찾았지만 캠페인 정황(캡션 트리거·반복)은 있는 게시물** |\n\n"
            "`template_only` 는 **구조적으로 0건**이다(게시물을 특정 못 하면 후보 자체가 안 생긴다). "
            "enum 에는 예약값으로 남아 있다.\n\n"
            "### 삭제된 캠페인의 후보\n"
            "불러온 캠페인을 사용자가 지우면 후보는 `applied` → **`detected`** 로 돌아간다(재적용 가능). "
            '이때 **`applied_at` 은 남는다** — `status="detected" && applied_at != null` 이면 '
            "**'불러왔다가 지운 것'** 이다. 「N개 찾음」 배너에서 이건 빼는 게 맞다."
        ),
        parameters=[
            _WORKSPACE_PARAM,
            OpenApiParameter(
                name="status",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                enum=["detected", "applied", "dismissed"],
            ),
            OpenApiParameter(
                name="band",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                enum=["auto_draft", "needs_review", "excluded", "template_only"],
            ),
            OpenApiParameter(
                name="page", location=OpenApiParameter.QUERY, required=False, type=int
            ),
            OpenApiParameter(
                name="page_size",
                location=OpenApiParameter.QUERY,
                required=False,
                type=int,
                description="기본 20, **최대 100**. 범위 밖은 400 이 아니라 clamp.",
            ),
            OpenApiParameter(
                name="search",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                description="draft_name · draft_opening_message · media_caption_excerpt · "
                "offer_button_label 부분일치.",
            ),
            OpenApiParameter(
                name="ordering",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                enum=sorted(_ORDERING_WHITELIST),
                description="기본 `-media_timestamp`. 허용 목록 밖이면 **400**.",
            ),
            OpenApiParameter(
                name="media_after",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                description="게시물 작성일 하한 `YYYY-MM-DD`(포함).",
            ),
            OpenApiParameter(
                name="media_before",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                description="게시물 작성일 상한 `YYYY-MM-DD`(포함).",
            ),
            OpenApiParameter(
                name="needs_confirm",
                location=OpenApiParameter.QUERY,
                required=False,
                type=bool,
                description="true 면 링크 확인이 아직 안 끝난 후보만.",
            ),
            OpenApiParameter(
                name="view",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                enum=["list"],
                description="`list` 면 무거운 필드 4종(evidence_raw·evidence_aggregates·"
                "follow_up_candidates·matched_template)을 뺀다.",
            ),
        ],
        responses={
            200: PaginatedCandidateSerializer,
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
        heal_orphaned_applied(job)
        qs = _filter_candidates(visibility.visible(job.candidates.all()), request.query_params)

        page_size = min(max(_int_param(request, "page_size", 20), 1), 100)
        page = max(_int_param(request, "page", 1), 1)
        total = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start : start + page_size])
        light = request.query_params.get("view") == "list"
        data = DMCampaignCandidateSerializer(rows, many=True, context={"light": light}).data

        def _url(p):
            if p < 1 or (p - 1) * page_size >= total:
                return None
            q = request.query_params.copy()
            q["page"] = p
            return f"{request.build_absolute_uri(request.path)}?{q.urlencode()}"

        return Response(
            {"count": total, "next": _url(page + 1), "previous": _url(page - 1), "results": data}
        )

    @extend_schema(
        summary="후보 집계 — 필터 칩·날짜 범위용",
        description=(
            "후보 목록의 **개수 집계**를 한 번에 준다. 밴드별/상태별 개수와 게시물 작성일 범위를 "
            "반환하므로, 프론트가 전체를 받아서 세지 않아도 필터 칩과 날짜 선택기를 그릴 수 있다.\n\n"
            "`needs_confirm` 은 **링크 확인이 필요한 후보 수**다(표본이 적어 다른 캠페인 링크일 "
            "가능성이 있는 건). 이 숫자만큼 사용자에게 '이 링크가 맞나요?' 를 물으면 된다."
        ),
        parameters=[_WORKSPACE_PARAM],
        responses={
            200: CandidateSummarySerializer,
            400: OpenApiResponse(description="workspace_id 누락."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님."),
            404: OpenApiResponse(description="잡 없음 / 다른 워크스페이스."),
            500: OpenApiResponse(description="서버 오류."),
        },
        examples=[
            OpenApiExample(
                "집계 응답",
                value={
                    "total": 62,
                    "by_band": {"auto_draft": 36, "needs_review": 20, "excluded": 6},
                    "by_status": {"detected": 52, "applied": 8, "dismissed": 2},
                    "needs_confirm": 20,
                    "with_offer_url": 41,
                    "media_date_range": {"first": "2026-03-02", "last": "2026-08-11"},
                },
                response_only=True,
            )
        ],
        tags=_TAGS,
    )
    @action(detail=True, methods=["get"], url_path="candidates/summary")
    def candidates_summary(self, request, pk=None):
        workspace = self._get_workspace(request)
        job = self._get_job(pk, workspace)
        heal_orphaned_applied(job)
        # 고객에게 안 넘기는 밴드는 여기서도 빠진다 — 타일 숫자와 목록이 어긋나면
        # "N개 찾음" 을 눌렀을 때 그만큼 안 나온다.
        qs = visibility.visible(job.candidates.all())
        agg = qs.aggregate(first=Min("media_timestamp"), last=Max("media_timestamp"))
        return Response(
            {
                "total": qs.count(),
                "by_band": {r["band"]: r["n"] for r in qs.values("band").annotate(n=Count("id"))},
                "by_status": {
                    r["status"]: r["n"] for r in qs.values("status").annotate(n=Count("id"))
                },
                "needs_confirm": qs.filter(
                    confirm_required=True, confirmed_at__isnull=True
                ).count(),
                "with_offer_url": qs.exclude(offer_url="").count(),
                "media_date_range": {
                    "first": agg["first"].date().isoformat() if agg["first"] else None,
                    "last": agg["last"].date().isoformat() if agg["last"] else None,
                },
            }
        )

    @extend_schema(
        summary="후보 일괄 적용 — 확실한 것 전부 초안 생성",
        description=(
            "지정한 밴드의 후보를 **한 번의 호출로 전부** 비활성 초안 캠페인으로 만든다.\n\n"
            "기본 대상은 `auto_draft` — 링크·문구의 지지 근거가 충분해 자동 채택해도 되는 후보다. "
            "`template_only` 는 게시물을 특정할 수 없어 제외된다(개별 apply 에서 `media_id` 지정 필요).\n\n"
            "**부분 성공을 그대로 반환한다** — 실패 건은 `failed[]` 에 사유 코드와 함께 담기므로 "
            "프론트가 '10개 중 8개 만들었어요' 를 표시할 수 있다. 이미 적용된 후보는 조용히 건너뛴다.\n\n"
            "생성되는 캠페인은 전부 **INACTIVE** 이고 `backfill_existing_comments=false` 로 고정된다."
        ),
        parameters=[_WORKSPACE_PARAM],
        request=BulkApplyRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="{applied:[{candidate_id,campaign_id}], failed:[{candidate_id,code,message}], skipped:int}"
            ),
            400: OpenApiResponse(description="workspace_id 누락 / 잘못된 band."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님."),
            404: OpenApiResponse(description="잡 없음 / 다른 워크스페이스."),
            500: OpenApiResponse(description="서버 오류."),
        },
        examples=[
            OpenApiExample("기본(확실한 것만)", value={}, request_only=True),
            OpenApiExample(
                "검수 대상까지 포함",
                value={"bands": ["auto_draft", "needs_review"]},
                request_only=True,
            ),
        ],
        tags=_TAGS,
    )
    @action(detail=True, methods=["post"], url_path="apply-all")
    def apply_all(self, request, pk=None):
        workspace = self._get_workspace(request)
        job = self._get_job(pk, workspace)
        req = BulkApplyRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        bands = req.validated_data.get("bands") or [DMCampaignCandidate.Band.AUTO_DRAFT]

        heal_orphaned_applied(job)
        # 클라이언트가 숨은 밴드를 지정해도 적용되지 않게 교집합을 취한다.
        bands = [b for b in bands if b in visibility.visible_bands()]
        qs = job.candidates.filter(band__in=bands).exclude(media_id="")
        applied, failed, skipped = [], [], 0
        for cand in qs:
            if cand.status == DMCampaignCandidate.Status.APPLIED:
                skipped += 1
                continue
            try:
                campaign = apply_candidate(cand, {})
            except DRFValidationError as exc:
                failed.append(
                    {
                        "candidate_id": str(cand.id),
                        "code": "validation_error",
                        "message": str(exc.detail)[:200],
                    }
                )
            except Exception as exc:  # noqa: BLE001 — 1건 실패가 전체를 막지 않게
                logger.exception("apply-all 실패 (candidate=%s)", cand.id)
                failed.append(
                    {"candidate_id": str(cand.id), "code": "error", "message": str(exc)[:200]}
                )
            else:
                applied.append({"candidate_id": str(cand.id), "campaign_id": str(campaign.id)})
        return Response({"applied": applied, "failed": failed, "skipped": skipped})

    @extend_schema(
        summary="계정 단위 1회성 상태 조회/저장",
        description=(
            "프론트가 **IG 계정당 한 번만** 물어야 하는 값들을 서버에 보관한다. "
            "localStorage 로 두면 다른 기기·브라우저에서 같은 질문이 다시 떠서 "
            "'한 번만 묻는다' 전제가 깨진다.\n\n"
            "| 필드 | 뜻 |\n|---|---|\n"
            "| `prompt_answer` | 연동 직후 설문 답. `used`(다른 서비스 써봤음) / "
            "`first_time`(처음). `first_time` 은 잡을 만들지 않아 서버에 다른 흔적이 없다 |\n"
            "| `conflict_ack_at` | '타 서비스 연동 해제하셨나요?' 확인 시각. 불러온 캠페인을 "
            "**처음 켤 때** 1회 띄우고, 확인받으면 이 값을 채운다 |\n\n"
            "`GET` 은 현재 값을, `POST` 는 보낸 필드만 갱신한다(부분 갱신). "
            "`prefetched_job` 은 연동 직후 자동 선분석 결과가 이미 준비돼 있는지 알려준다 — "
            "있으면 '불러오기' 를 눌렀을 때 기다림 없이 바로 결과를 보여줄 수 있다.\n\n"
            "**충돌 확인 저장** — `conflict_ack` 과 `conflict_ack_at` **둘 다 받는다**(같은 뜻). "
            "보낸 값은 참/거짓 판단에만 쓰고 저장은 **서버 시각**으로 한다(클라이언트 시계를 "
            "신뢰하지 않는다). 그래서 ISO 시각을 보내도 응답의 `conflict_ack_at` 은 그 값이 "
            "아니라 서버가 찍은 시각이다. `false` 를 보내면 **해제**된다(재테스트용)."
        ),
        parameters=[_WORKSPACE_PARAM],
        responses={
            200: OpenApiResponse(
                description="{prompt_answer, prompt_answered_at, conflict_ack_at, prefetched_job}"
            ),
            400: OpenApiResponse(
                description=(
                    "workspace_id 누락 / 잘못된 값 / **활성 IG 연동 없음** / **비활성 연동**. "
                    "죽은 연결에서는 이 400 이 정상 동작이다 — 프론트는 "
                    "`is_active && status=='active'` 일 때만 호출하면 된다(서버 판정과 동일)."
                )
            ),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님 / 다른 워크스페이스의 연동."),
            404: OpenApiResponse(description="**없는 ig_connection_id** 를 지정한 경우에만."),
            500: OpenApiResponse(description="서버 오류."),
        },
        examples=[
            OpenApiExample("설문 답 저장", value={"prompt_answer": "used"}, request_only=True),
            OpenApiExample("충돌 확인 완료", value={"conflict_ack": True}, request_only=True),
            OpenApiExample(
                "충돌 확인 완료 (시각으로 보내도 동일)",
                value={"conflict_ack_at": "2026-08-14T11:36:00.000Z"},
                request_only=True,
            ),
            OpenApiExample("충돌 확인 해제", value={"conflict_ack": False}, request_only=True),
        ],
        tags=_TAGS,
    )
    @action(detail=False, methods=["get", "post"], url_path="prompt-answer")
    def prompt_answer(self, request):
        workspace = self._get_workspace(request)
        conn = self._resolve_connection(workspace, request.query_params.get("ig_connection_id"))
        if request.method == "POST":
            ans = request.data.get("prompt_answer")
            if ans is not None:
                if ans not in ("used", "first_time", ""):
                    raise DRFValidationError(
                        {"prompt_answer": ["used / first_time / 빈 문자열만 허용됩니다."]}
                    )
                conn.dm_migration_prompt_answer = ans
                conn.dm_migration_prompt_answered_at = timezone.now() if ans else None
            # 충돌 확인 — 프론트가 `conflict_ack`(bool) 또는 `conflict_ack_at`(ISO 시각) 중
            # 무엇을 보내도 받는다. 값은 시각 판단에만 쓰고 **저장은 서버 시각**으로 한다
            # (클라이언트 시계를 신뢰하지 않는다). false 를 보내면 해제 — 재테스트용.
            for key in ("conflict_ack", "conflict_ack_at"):
                if key in request.data:
                    conn.dm_migration_conflict_ack_at = (
                        timezone.now() if request.data.get(key) else None
                    )
                    break
            conn.save(
                update_fields=[
                    "dm_migration_prompt_answer",
                    "dm_migration_prompt_answered_at",
                    "dm_migration_conflict_ack_at",
                    "updated_at",
                ]
            )
        prefetched = (
            DMMigrationJob.objects.filter(
                ig_connection=conn,
                status__in=_REUSABLE,
                finished_at__gte=timezone.now() - _REUSE_WINDOW,
            )
            .order_by("-finished_at")
            .first()
        )
        return Response(
            {
                "prompt_answer": conn.dm_migration_prompt_answer or None,
                "prompt_answered_at": (
                    conn.dm_migration_prompt_answered_at.isoformat()
                    if conn.dm_migration_prompt_answered_at
                    else None
                ),
                "conflict_ack_at": (
                    conn.dm_migration_conflict_ack_at.isoformat()
                    if conn.dm_migration_conflict_ack_at
                    else None
                ),
                "prefetched_job": (
                    DMMigrationJobSerializer(prefetched).data if prefetched else None
                ),
            }
        )


class DMCampaignCandidateViewSet(_WorkspaceScopedViewSet):
    """DM 캠페인 이전 후보 — 적용(초안 캠페인 생성)/무시."""

    def _get_candidate(self, pk, workspace) -> DMCampaignCandidate:
        cand = (
            DMCampaignCandidate.objects.filter(id=pk, ig_connection__workspace=workspace)
            .select_related("ig_connection")
            .first()
        )
        if not cand or not visibility.is_visible(cand):
            # 숨은 밴드는 **존재 자체를 알리지 않는다.** 403 을 주면 "있긴 있다" 가 새고,
            # 프론트는 처리 못 하는 오류를 받는다.
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
        campaign = apply_candidate(candidate, req.validated_data)
        candidate.refresh_from_db()
        return Response(
            {
                "candidate": DMCampaignCandidateSerializer(candidate).data,
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

    @extend_schema(
        summary="자료 링크 확인/수정",
        description=(
            "복원한 **자료 링크가 맞는지** 인플루언서에게 확인받는다.\n\n"
            "`confirm_required=true` 인 후보만 물으면 된다. 근거 표본이 적어 복원한 링크가 "
            "**다른 게시물 캠페인의 링크**일 수 있는 건들이다(표본 1~2명이면 그럴 확률이 높다).\n\n"
            "화면에는 **링크 하나만** 보여주고 '이 게시물에서 보내시던 링크가 맞나요?' 만 물으면 된다.\n\n"
            "- 맞으면 → 바디 없이 호출(또는 `{}`). 복원된 링크가 확정된다.\n"
            '- 다르면 → `{"url": "https://..."}` 로 올바른 링크를 보내면 교체된다.\n'
            '- 캠페인이 아니었으면 → `{"correct": false}` 로 후보를 무시 처리한다.\n\n'
            "확정 후 `apply` 하면 이 링크가 **링크 버튼**으로 들어간다(본문에 URL 을 직접 넣으면 "
            "인스타가 스팸으로 잡을 수 있어 버튼으로 승격한다). 이미 적용된 후보는 409."
        ),
        parameters=[_WORKSPACE_PARAM],
        request=CandidateConfirmRequestSerializer,
        responses={
            200: DMCampaignCandidateSerializer,
            400: OpenApiResponse(description="workspace_id 누락 / 잘못된 URL 형식."),
            401: OpenApiResponse(description="인증 필요."),
            403: OpenApiResponse(description="워크스페이스 멤버 아님 / 다른 워크스페이스."),
            404: OpenApiResponse(description="후보 없음."),
            409: OpenApiResponse(description="이미 적용된 후보(candidate_already_applied)."),
            500: OpenApiResponse(description="서버 오류."),
        },
        examples=[
            OpenApiExample("맞아요", value={}, request_only=True),
            OpenApiExample(
                "링크가 달라요",
                value={"url": "https://myshop.co.kr/lookbook"},
                request_only=True,
            ),
            OpenApiExample("캠페인이 아니에요", value={"correct": False}, request_only=True),
        ],
        tags=_TAGS,
    )
    @action(detail=True, methods=["post"], url_path="confirm-link")
    def confirm_link(self, request, pk=None):
        workspace = self._get_workspace(request)
        candidate = self._get_candidate(pk, workspace)
        if candidate.status == DMCampaignCandidate.Status.APPLIED:
            raise MigrationConflictError.make(
                "이미 적용된 후보입니다.", "candidate_already_applied"
            )
        req = CandidateConfirmRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        vd = req.validated_data

        if not vd.get("correct", True):
            candidate.status = DMCampaignCandidate.Status.DISMISSED
            candidate.dismissed_at = timezone.now()
            candidate.save(update_fields=["status", "dismissed_at", "updated_at"])
            return Response(DMCampaignCandidateSerializer(candidate).data)

        url = vd.get("url")
        candidate.confirmed_url = (url if url is not None else candidate.offer_url) or ""
        candidate.confirmed_at = timezone.now()
        candidate.confirm_required = False
        candidate.save(
            update_fields=["confirmed_url", "confirmed_at", "confirm_required", "updated_at"]
        )
        return Response(DMCampaignCandidateSerializer(candidate).data)
