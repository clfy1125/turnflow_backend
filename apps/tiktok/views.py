"""
TikTok integration views.

MVP scope (audit/approval submission):
    - Workspace-scoped Login Kit OAuth (start + callback)
    - List active connections
    - Trigger a Direct Post publish (PULL_FROM_URL only end-to-end in MVP)
    - Poll a publish's status

Comment moderation lives in Step 3.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.billing.utils import UsageTracker
from apps.workspace.models import Workspace

from .models import (
    TikTokAccountConnection,
    TikTokCommentLog,
    TikTokOAuthState,
    TikTokSpamFilterConfig,
    TikTokVideoPost,
)
from .serializers import (
    ConnectionStartResponseSerializer,
    TikTokAccountConnectionSerializer,
    TikTokCommentLogSerializer,
    TikTokSpamFilterConfigSerializer,
    TikTokVideoPostSerializer,
    TikTokVideoPublishRequestSerializer,
)
from .services import (
    MockTikTokProvider,
    TikTokAPIError,
    TikTokOAuthService,
    effective_privacy_for,
)
from .tasks import fetch_and_screen_comments, moderate_comment, publish_video_task

logger = logging.getLogger(__name__)


def _get_workspace_for_member(user, workspace_id):
    workspace = Workspace.objects.filter(id=workspace_id).first()
    if not workspace:
        raise NotFound("Workspace not found")
    if not workspace.memberships.filter(user=user).exists():
        raise PermissionDenied("You are not a member of this workspace")
    return workspace


class TikTokIntegrationViewSet(viewsets.ViewSet):
    """OAuth + connection-management endpoints."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="TikTok 연동 시작",
        description=(
            "## 목적\n"
            "TikTok Content Posting API(Login Kit) OAuth 흐름을 시작합니다.\n\n"
            "## 인증\n"
            "- Bearer 토큰 필수\n"
            "- 호출자는 ``workspace_id`` 의 멤버여야 함\n\n"
            "## 동작\n"
            "1. CSRF 보호용 ``state`` 토큰 생성 → ``TikTokOAuthState`` 에 저장 (10분)\n"
            "2. ``https://www.tiktok.com/v2/auth/authorize/`` 에 client_key, scope, "
            "redirect_uri, state 를 붙여 권한 동의 URL 생성\n"
            "3. 프론트는 응답 ``authorization_url`` 을 새 창으로 열어 사용자 동의 유도\n\n"
            "## 요청 스코프\n"
            "- ``user.info.basic`` — display name 조회\n"
            "- ``video.publish`` — Direct Post 호출\n"
            "- ``video.upload`` — FILE_UPLOAD 방식 청크 업로드용\n\n"
            "## 미감사 클라이언트 안내\n"
            "TikTok 검수 완료 전에는 모든 발행이 ``SELF_ONLY`` 로 강제됩니다. "
            "호출자는 이를 UI에 명시해야 합니다.\n"
        ),
        responses={
            200: ConnectionStartResponseSerializer,
            401: OpenApiResponse(description="유효하지 않은 토큰"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="워크스페이스를 찾을 수 없음"),
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/connect/start",
    )
    def connect_start(self, request, workspace_id=None):
        workspace = _get_workspace_for_member(request.user, workspace_id)

        state = secrets.token_urlsafe(32)
        TikTokOAuthState.objects.create(
            state=state,
            workspace=workspace,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        redirect_uri = settings.TIKTOK_REDIRECT_URI or request.build_absolute_uri(
            "/api/v1/tiktok/connect/callback/"
        )

        if MockTikTokProvider.is_mock_mode():
            authorization_url = MockTikTokProvider.generate_authorization_url(redirect_uri, state)
            mode = "mock"
        else:
            authorization_url = TikTokOAuthService.get_authorization_url(redirect_uri, state)
            mode = "production"

        return Response(
            {"authorization_url": authorization_url, "state": state, "mode": mode}
        )

    @extend_schema(
        summary="TikTok 연동 콜백",
        description=(
            "## 목적\n"
            "TikTok에서 동의 후 리디렉션된 ``code`` + ``state`` 를 받아 토큰으로 교환하고 "
            "``TikTokAccountConnection`` 레코드를 만듭니다.\n\n"
            "## 동작\n"
            "1. ``state`` 검증 → 워크스페이스 매핑 복구\n"
            "2. ``POST https://open.tiktokapis.com/v2/oauth/token/`` (grant_type=authorization_code)\n"
            "3. ``GET /v2/user/info/`` 로 open_id, display_name, avatar 조회\n"
            "4. 토큰을 Fernet으로 암호화 저장 (``apps.integrations.encryption``)\n\n"
            "## Mock 모드\n"
            "``TIKTOK_MOCK_MODE=True`` 이면 가짜 토큰/사용자정보로 곧장 통과.\n\n"
            "## 응답\n"
            "팝업 흐름 호환을 위해 HTML(``window.opener.postMessage``) 을 반환합니다."
        ),
        responses={200: OpenApiResponse(description="HTML(success/error postMessage 포함)")},
    )
    @action(detail=False, methods=["get"], url_path="connect/callback", permission_classes=[])
    def connect_callback(self, request):
        code = request.GET.get("code", "")
        state = request.GET.get("state", "")
        error = request.GET.get("error", "")

        if error:
            return _callback_html(success=False, error_code="OAUTH_ERROR", message=error)
        if not code or not state:
            return _callback_html(
                success=False, error_code="MISSING_PARAMETERS", message="code or state missing",
            )

        state_obj = TikTokOAuthState.objects.filter(state=state).first()
        if not state_obj or state_obj.is_expired():
            return _callback_html(
                success=False, error_code="INVALID_STATE", message="state expired or invalid",
            )

        workspace = state_obj.workspace

        try:
            if MockTikTokProvider.is_mock_mode() or code.startswith("mock_tiktok_code_"):
                token_bundle = MockTikTokProvider.exchange_code_for_token(code)
                user_info = MockTikTokProvider.get_user_info(token_bundle["access_token"])
            else:
                redirect_uri = settings.TIKTOK_REDIRECT_URI or request.build_absolute_uri(
                    "/api/v1/tiktok/connect/callback/"
                )
                token_bundle = TikTokOAuthService.exchange_code_for_token(code, redirect_uri)
                user_info = TikTokOAuthService.get_user_info(token_bundle["access_token"])
        except TikTokAPIError as e:
            logger.error("tiktok.callback: api error %s — %s", e.code, str(e))
            return _callback_html(success=False, error_code="TIKTOK_API_ERROR", message=str(e))
        except Exception as e:  # noqa: BLE001 — last-resort UX guard
            logger.exception("tiktok.callback: unexpected error")
            return _callback_html(success=False, error_code="INTERNAL_ERROR", message=str(e))

        open_id = token_bundle.get("open_id") or user_info.get("open_id")
        if not open_id:
            return _callback_html(
                success=False,
                error_code="NO_OPEN_ID",
                message="TikTok did not return open_id",
            )

        expires_in = int(token_bundle.get("expires_in") or 86400)
        refresh_expires_in = int(token_bundle.get("refresh_expires_in") or (365 * 24 * 3600))

        connection, _created = TikTokAccountConnection.objects.get_or_create(
            workspace=workspace,
            external_account_id=open_id,
            defaults={
                "username": user_info.get("display_name", ""),
                "avatar_url": user_info.get("avatar_url", ""),
                "scopes": (token_bundle.get("scope") or "").split(","),
                "status": TikTokAccountConnection.Status.ACTIVE,
            },
        )

        connection.username = user_info.get("display_name", connection.username)
        connection.avatar_url = user_info.get("avatar_url", connection.avatar_url)
        connection.union_id = user_info.get("union_id", connection.union_id)
        connection.access_token = token_bundle["access_token"]
        connection.refresh_token = token_bundle.get("refresh_token", "")
        connection.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
        connection.refresh_token_expires_at = timezone.now() + timedelta(seconds=refresh_expires_in)
        connection.scopes = [s for s in (token_bundle.get("scope") or "").split(",") if s]
        connection.status = TikTokAccountConnection.Status.ACTIVE
        connection.last_verified_at = timezone.now()
        connection.error_message = ""
        connection.save()

        # Burn the state — single use.
        state_obj.delete()

        connection_payload = TikTokAccountConnectionSerializer(connection).data
        return _callback_html(
            success=True,
            error_code="",
            message="connected",
            connection=connection_payload,
        )

    @extend_schema(
        summary="연결된 TikTok 계정 목록",
        description=(
            "워크스페이스에 연결된 모든 TikTokAccountConnection 을 반환합니다.\n\n"
            "필터: 워크스페이스 멤버십 필요. 다른 워크스페이스의 연결은 보이지 않음."
        ),
        responses={
            200: TikTokAccountConnectionSerializer(many=True),
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
        },
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/connections",
    )
    def list_connections(self, request, workspace_id=None):
        workspace = _get_workspace_for_member(request.user, workspace_id)
        qs = TikTokAccountConnection.objects.filter(workspace=workspace).order_by("-created_at")
        return Response(TikTokAccountConnectionSerializer(qs, many=True).data)


class TikTokVideoViewSet(viewsets.ViewSet):
    """Publish + status endpoints."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="TikTok 영상 발행 요청",
        description=(
            "## 목적\n"
            "Content Posting API의 Direct Post 흐름을 시작합니다.\n\n"
            "## 인증\n"
            "Bearer 토큰. 호출자는 ``connection_id`` 가 속한 워크스페이스의 멤버여야 함.\n\n"
            "## 미감사 안내\n"
            "TikTok 검수 통과 전에는 ``requested_privacy`` 와 무관하게 ``SELF_ONLY`` 로 강제됩니다. "
            "응답의 ``effective_privacy`` 를 그대로 노출하세요.\n\n"
            "## source_type\n"
            "- ``PULL_FROM_URL`` (권장): ``video_url`` 필수. TikTok이 직접 fetch.\n"
            "- ``FILE_UPLOAD``: ``video_size_bytes`` 필수. ``upload_url`` 응답을 받은 뒤 "
            "별도 청크 업로드 호출 필요 (MVP에서는 init까지만 자동화)."
        ),
        request=TikTokVideoPublishRequestSerializer,
        responses={
            201: TikTokVideoPostSerializer,
            400: OpenApiResponse(description="유효성 오류"),
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="connection 또는 워크스페이스 없음"),
        },
    )
    def create(self, request):
        serializer = TikTokVideoPublishRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        connection = TikTokAccountConnection.objects.filter(id=data["connection_id"]).first()
        if not connection:
            raise NotFound("Connection not found")
        workspace = _get_workspace_for_member(request.user, connection.workspace_id)

        # 요금제 한도 검사 — 초과 시 PlanLimitExceededError → 429 + PLAN_LIMIT_EXCEEDED
        UsageTracker.check_and_increment(workspace, "videos_published", 1)

        post = TikTokVideoPost.objects.create(
            connection=connection,
            caption=data["caption"],
            source_type=data["source_type"],
            video_url=data.get("video_url", ""),
            video_size_bytes=data.get("video_size_bytes", 0),
            video_file_path=data.get("video_file_path", ""),
            requested_privacy=data["requested_privacy"],
            effective_privacy=effective_privacy_for(connection, data["requested_privacy"]),
            disable_duet=data["disable_duet"],
            disable_comment=data["disable_comment"],
            disable_stitch=data["disable_stitch"],
        )

        publish_video_task.delay(str(post.id))

        return Response(
            TikTokVideoPostSerializer(post).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="TikTok 발행 상태 조회",
        description=(
            "내부 ``TikTokVideoPost.id`` 로 진행 상태를 폴링합니다. "
            "최종 상태(``published`` / ``failed``)에서는 더 이상 변하지 않습니다."
        ),
        responses={
            200: TikTokVideoPostSerializer,
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="post 없음"),
        },
    )
    def retrieve(self, request, pk=None):
        post = (
            TikTokVideoPost.objects.select_related("connection", "connection__workspace")
            .filter(id=pk)
            .first()
        )
        if not post:
            raise NotFound("Post not found")
        _get_workspace_for_member(request.user, post.connection.workspace_id)
        return Response(TikTokVideoPostSerializer(post).data)

    @extend_schema(
        summary="TikTok 발행 목록",
        description="connection 또는 workspace 단위로 발행 이력을 반환.",
        responses={200: TikTokVideoPostSerializer(many=True)},
    )
    def list(self, request):
        workspace_id = request.query_params.get("workspace_id")
        connection_id = request.query_params.get("connection_id")
        if not workspace_id and not connection_id:
            return Response(
                {"detail": "workspace_id or connection_id query param is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if workspace_id:
            workspace = _get_workspace_for_member(request.user, workspace_id)
            qs = TikTokVideoPost.objects.filter(connection__workspace=workspace)
        else:
            connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
            if not connection:
                raise NotFound("Connection not found")
            _get_workspace_for_member(request.user, connection.workspace_id)
            qs = TikTokVideoPost.objects.filter(connection=connection)

        qs = qs.order_by("-created_at")[:100]
        return Response(TikTokVideoPostSerializer(qs, many=True).data)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class TikTokCommentViewSet(viewsets.ViewSet):
    """List cached comment logs + manually trigger fetch/screen and hide."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="TikTok 댓글 캐시 목록",
        description=(
            "내부에 캐시된 ``TikTokCommentLog`` 를 반환합니다. ``fetch`` 액션으로 갱신.\n\n"
            "필터: ``connection_id`` (필수), ``video_id`` (선택), ``status`` (선택)."
        ),
        responses={200: TikTokCommentLogSerializer(many=True)},
    )
    def list(self, request):
        connection_id = request.query_params.get("connection_id")
        if not connection_id:
            return Response(
                {"detail": "connection_id query param is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)

        qs = TikTokCommentLog.objects.filter(connection=connection)
        if video_id := request.query_params.get("video_id"):
            qs = qs.filter(external_video_id=video_id)
        if status_filter := request.query_params.get("status"):
            qs = qs.filter(status=status_filter)
        qs = qs.order_by("-created_at")[:200]
        return Response(TikTokCommentLogSerializer(qs, many=True).data)

    @extend_schema(
        summary="TikTok 댓글 가져오기 + 자동 분류",
        description=(
            "## 목적\n"
            "특정 영상에 달린 댓글을 가져와 ``TikTokCommentLog`` 로 캐시하고, 활성 ``TikTokSpamFilterConfig`` "
            "규칙에 따라 휴리스틱 분류를 수행합니다.\n\n"
            "## 비동기\n"
            "Celery 태스크가 백그라운드로 처리하며, 완료 후 list 호출로 결과를 확인하세요.\n\n"
            "## TikTok 한계 (Phase 2 대기)\n"
            "실제 organic 댓글 list/hide 는 ``business-api.tiktok.com`` 별도 OAuth 가 필요합니다. "
            "Phase 1 (현재) 에서는 ``TIKTOK_MOCK_MODE=True`` 환경에서만 실제로 동작합니다."
        ),
        request={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string", "format": "uuid"},
                "video_id": {"type": "string"},
            },
            "required": ["connection_id", "video_id"],
        },
        responses={202: OpenApiResponse(description="태스크 enqueued")},
    )
    @action(detail=False, methods=["post"], url_path="fetch")
    def fetch(self, request):
        connection_id = request.data.get("connection_id")
        video_id = request.data.get("video_id")
        if not connection_id or not video_id:
            return Response(
                {"detail": "connection_id and video_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)
        fetch_and_screen_comments.delay(str(connection.id), video_id)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="TikTok 댓글 숨기기",
        description=(
            "내부 ``TikTokCommentLog.id`` 단위로 hide 액션을 큐잉합니다. TikTok organic API의 "
            "``delete`` 는 fan 댓글에 적용 불가하므로 hide로 폴백됩니다.\n\n"
            "요금제 ``comments_moderated_per_month`` 한도를 초과하면 429."
        ),
        responses={202: OpenApiResponse(description="태스크 enqueued")},
    )
    @action(detail=True, methods=["post"], url_path="hide")
    def hide(self, request, pk=None):
        log = TikTokCommentLog.objects.select_related("connection").filter(id=pk).first()
        if not log:
            raise NotFound("Comment log not found")
        workspace = _get_workspace_for_member(request.user, log.connection.workspace_id)
        UsageTracker.check_and_increment(workspace, "comments_moderated", 1)
        moderate_comment.delay(str(log.id), "hide")
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)


class TikTokSpamFilterViewSet(viewsets.ViewSet):
    """CRUD + activation for the per-connection spam rule config."""

    permission_classes = [IsAuthenticated]

    def _get_for_connection(self, request, connection_id: str):
        connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)
        return connection

    @extend_schema(
        summary="TikTok 스팸 필터 조회/생성",
        description=(
            "특정 연결의 ``TikTokSpamFilterConfig`` 를 반환합니다. 없으면 비활성 상태로 자동 생성."
        ),
        responses={200: TikTokSpamFilterConfigSerializer},
    )
    @action(detail=False, methods=["get"], url_path="connections/(?P<connection_id>[^/.]+)")
    def get_or_create(self, request, connection_id=None):
        connection = self._get_for_connection(request, connection_id)
        cfg, _ = TikTokSpamFilterConfig.objects.get_or_create(connection=connection)
        return Response(TikTokSpamFilterConfigSerializer(cfg).data)

    @extend_schema(
        summary="TikTok 스팸 필터 수정",
        description="키워드/임계값/액션을 갱신하고 (선택적으로) 활성화합니다.",
        request=TikTokSpamFilterConfigSerializer,
        responses={200: TikTokSpamFilterConfigSerializer},
    )
    @action(
        detail=False,
        methods=["patch"],
        url_path="connections/(?P<connection_id>[^/.]+)/update",
    )
    def patch_config(self, request, connection_id=None):
        connection = self._get_for_connection(request, connection_id)
        cfg, _ = TikTokSpamFilterConfig.objects.get_or_create(connection=connection)
        serializer = TikTokSpamFilterConfigSerializer(cfg, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


def _callback_html(*, success: bool, error_code: str, message: str, connection: dict = None):
    """Render a tiny HTML page that ``postMessage``-s back to the opener."""
    import json

    payload = {
        "type": "TIKTOK_CONNECTED" if success else "TIKTOK_ERROR",
        "success": success,
        "errorCode": error_code,
        "message": message,
        "connection": connection,
    }
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    body = "✅ TikTok 연동 성공!" if success else f"❌ TikTok 연동 실패: {message}"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TikTok 연동</title>
<style>body{{font-family:-apple-system,sans-serif;text-align:center;padding:40px}}</style>
</head><body>
<h2>{body}</h2>
<p>창이 자동으로 닫힙니다…</p>
<script>
  if (window.opener) {{
    window.opener.postMessage({payload_json}, '*');
    setTimeout(function() {{ window.close(); }}, 1500);
  }}
</script>
</body></html>"""
    return HttpResponse(html)
