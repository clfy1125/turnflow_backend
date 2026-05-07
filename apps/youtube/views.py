"""
YouTube integration views.

MVP scope:
    - Google OAuth (start + callback)
    - List active connections
    - Trigger a video upload (`videos.insert`, 1,600 quota units)
    - Poll a post's status
"""

from __future__ import annotations

import json
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
    YouTubeAccountConnection,
    YouTubeCommentLog,
    YouTubeOAuthState,
    YouTubeSpamFilterConfig,
    YouTubeVideoPost,
)
from .serializers import (
    ConnectionStartResponseSerializer,
    YouTubeAccountConnectionSerializer,
    YouTubeCommentLogSerializer,
    YouTubeSpamFilterConfigSerializer,
    YouTubeVideoPostSerializer,
    YouTubeVideoUploadRequestSerializer,
)
from .services import (
    MockYouTubeProvider,
    YouTubeAPIError,
    YouTubeOAuthService,
)
from .tasks import fetch_and_screen_comments, moderate_comment, upload_video_task

logger = logging.getLogger(__name__)


def _get_workspace_for_member(user, workspace_id):
    workspace = Workspace.objects.filter(id=workspace_id).first()
    if not workspace:
        raise NotFound("Workspace not found")
    if not workspace.memberships.filter(user=user).exists():
        raise PermissionDenied("You are not a member of this workspace")
    return workspace


class YouTubeIntegrationViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="YouTube 연동 시작",
        description=(
            "## 목적\n"
            "Google OAuth 2.0(offline access)로 YouTube 채널 권한을 요청합니다.\n\n"
            "## 인증\n"
            "Bearer 토큰. 호출자는 ``workspace_id`` 의 멤버여야 함.\n\n"
            "## 요청 스코프\n"
            "- ``youtube.upload`` — videos.insert (1,600 units/call)\n"
            "- ``youtube.force-ssl`` — comments.setModerationStatus\n"
            "- ``youtube.readonly`` — channels/commentThreads 조회\n"
            "- ``userinfo.email``, ``openid`` — 표시용 이메일\n\n"
            "## 동작\n"
            "1. CSRF 보호용 ``state`` 토큰 발급 → ``YouTubeOAuthState`` 에 10분 저장\n"
            "2. ``access_type=offline`` + ``prompt=consent`` 로 refresh_token 보장\n"
            "3. 응답의 ``authorization_url`` 을 프론트가 새 창으로 띄움\n\n"
            "## 할당량 안내\n"
            "기본 일일 quota 10,000 units. videos.insert 한 번에 1,600 units. "
            "검수 후 quota 증액 신청 가능."
        ),
        responses={
            200: ConnectionStartResponseSerializer,
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="워크스페이스 없음"),
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
        YouTubeOAuthState.objects.create(
            state=state,
            workspace=workspace,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI or request.build_absolute_uri(
            "/api/v1/youtube/connect/callback/"
        )

        if MockYouTubeProvider.is_mock_mode():
            authorization_url = MockYouTubeProvider.generate_authorization_url(redirect_uri, state)
            mode = "mock"
        else:
            authorization_url = YouTubeOAuthService.get_authorization_url(redirect_uri, state)
            mode = "production"

        return Response(
            {"authorization_url": authorization_url, "state": state, "mode": mode}
        )

    @extend_schema(
        summary="YouTube 연동 콜백",
        description=(
            "## 목적\n"
            "Google에서 동의 후 redirect된 ``code`` + ``state`` 로 토큰 교환 및 채널 정보 저장.\n\n"
            "## 동작\n"
            "1. ``state`` 검증\n"
            "2. ``POST https://oauth2.googleapis.com/token`` 으로 access/refresh 토큰 교환\n"
            "3. ``GET /v1/userinfo`` 로 sub/email 조회\n"
            "4. ``GET /youtube/v3/channels?mine=true`` 로 채널 ID/제목/썸네일 조회\n"
            "5. 토큰을 Fernet으로 암호화 저장\n\n"
            "## 응답\n"
            "팝업 흐름용 HTML(``window.opener.postMessage``)."
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

        state_obj = YouTubeOAuthState.objects.filter(state=state).first()
        if not state_obj or state_obj.is_expired():
            return _callback_html(
                success=False, error_code="INVALID_STATE", message="state expired or invalid",
            )

        workspace = state_obj.workspace
        try:
            if MockYouTubeProvider.is_mock_mode() or code.startswith("mock_yt_code_"):
                token_bundle = MockYouTubeProvider.exchange_code_for_token(code)
                userinfo = MockYouTubeProvider.get_userinfo(token_bundle["access_token"])
                channel = MockYouTubeProvider.get_my_channel(token_bundle["access_token"])
            else:
                redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI or request.build_absolute_uri(
                    "/api/v1/youtube/connect/callback/"
                )
                token_bundle = YouTubeOAuthService.exchange_code_for_token(code, redirect_uri)
                userinfo = YouTubeOAuthService.get_userinfo(token_bundle["access_token"])
                channel = YouTubeOAuthService.get_my_channel(token_bundle["access_token"])
        except YouTubeAPIError as e:
            logger.error("youtube.callback: api error %s — %s", e.code, str(e))
            return _callback_html(success=False, error_code=e.code or "YOUTUBE_API_ERROR", message=str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception("youtube.callback: unexpected error")
            return _callback_html(success=False, error_code="INTERNAL_ERROR", message=str(e))

        channel_id = channel.get("id")
        snippet = channel.get("snippet", {})
        thumbnails = (snippet.get("thumbnails") or {})
        thumb_url = (
            (thumbnails.get("default") or thumbnails.get("medium") or {}).get("url", "")
        )

        connection, _created = YouTubeAccountConnection.objects.get_or_create(
            workspace=workspace,
            external_account_id=channel_id,
            defaults={
                "channel_title": snippet.get("title", ""),
                "channel_thumbnail_url": thumb_url,
                "google_user_id": userinfo.get("sub", ""),
                "google_email": userinfo.get("email", ""),
                "scopes": (token_bundle.get("scope") or "").split(),
                "status": YouTubeAccountConnection.Status.ACTIVE,
            },
        )

        connection.channel_title = snippet.get("title", connection.channel_title)
        connection.channel_thumbnail_url = thumb_url or connection.channel_thumbnail_url
        connection.google_user_id = userinfo.get("sub", connection.google_user_id)
        connection.google_email = userinfo.get("email", connection.google_email)
        connection.access_token = token_bundle["access_token"]
        # Refresh token only present on first consent — never overwrite with empty.
        if token_bundle.get("refresh_token"):
            connection.refresh_token = token_bundle["refresh_token"]
        if token_bundle.get("expires_in"):
            connection.token_expires_at = timezone.now() + timedelta(
                seconds=int(token_bundle["expires_in"])
            )
        connection.scopes = (token_bundle.get("scope") or "").split()
        connection.status = YouTubeAccountConnection.Status.ACTIVE
        connection.last_verified_at = timezone.now()
        connection.error_message = ""
        connection.save()

        state_obj.delete()

        connection_payload = YouTubeAccountConnectionSerializer(connection).data
        return _callback_html(
            success=True, error_code="", message="connected", connection=connection_payload,
        )

    @extend_schema(
        summary="연결된 YouTube 채널 목록",
        description="워크스페이스에 연결된 YouTubeAccountConnection 을 반환.",
        responses={200: YouTubeAccountConnectionSerializer(many=True)},
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/connections",
    )
    def list_connections(self, request, workspace_id=None):
        workspace = _get_workspace_for_member(request.user, workspace_id)
        qs = YouTubeAccountConnection.objects.filter(workspace=workspace).order_by("-created_at")
        return Response(YouTubeAccountConnectionSerializer(qs, many=True).data)


class YouTubeVideoViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="YouTube 영상 업로드 요청",
        description=(
            "## 목적\n"
            "``youtube.videos.insert`` 를 통한 영상 업로드를 큐잉합니다 (resumable upload, 1,600 quota units).\n\n"
            "## 인증\n"
            "Bearer 토큰. 호출자는 ``connection_id`` 가 속한 워크스페이스의 멤버여야 함.\n\n"
            "## 검수 단계 안내\n"
            "검수 통과 전에는 ``privacy_status=private`` 권장. public 으로 발행해도 동작은 하지만 "
            "스코프 정당성 심사를 받지 않은 상태에서는 노출이 제한될 수 있음.\n\n"
            "## 할당량\n"
            "이 호출은 1,600 units 를 소비하며, ``YouTubeQuotaUsage`` 가 즉시 카운트.\n"
            "(기본 일일 quota = 10,000 → 사실상 6회/일 한도. 검수 후 증액 신청.)\n"
        ),
        request=YouTubeVideoUploadRequestSerializer,
        responses={
            201: YouTubeVideoPostSerializer,
            400: OpenApiResponse(description="유효성 오류"),
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="connection 없음"),
        },
    )
    def create(self, request):
        serializer = YouTubeVideoUploadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        connection = YouTubeAccountConnection.objects.filter(id=data["connection_id"]).first()
        if not connection:
            raise NotFound("Connection not found")
        workspace = _get_workspace_for_member(request.user, connection.workspace_id)

        # 요금제 한도 검사 — 초과 시 PlanLimitExceededError → 429 + PLAN_LIMIT_EXCEEDED
        UsageTracker.check_and_increment(workspace, "videos_published", 1)

        post = YouTubeVideoPost.objects.create(
            connection=connection,
            title=data["title"],
            description=data["description"],
            tags=list(data.get("tags") or []),
            category_id=data["category_id"],
            privacy_status=data["privacy_status"],
            made_for_kids=data["made_for_kids"],
            video_file_path=data["video_file_path"],
            video_size_bytes=data.get("video_size_bytes", 0),
        )

        upload_video_task.delay(str(post.id))

        return Response(
            YouTubeVideoPostSerializer(post).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="YouTube 업로드 상태 조회",
        responses={200: YouTubeVideoPostSerializer},
    )
    def retrieve(self, request, pk=None):
        post = (
            YouTubeVideoPost.objects.select_related("connection", "connection__workspace")
            .filter(id=pk)
            .first()
        )
        if not post:
            raise NotFound("Post not found")
        _get_workspace_for_member(request.user, post.connection.workspace_id)
        return Response(YouTubeVideoPostSerializer(post).data)

    @extend_schema(
        summary="YouTube 업로드 목록",
        description="connection 또는 workspace 단위로 업로드 이력을 반환.",
        responses={200: YouTubeVideoPostSerializer(many=True)},
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
            qs = YouTubeVideoPost.objects.filter(connection__workspace=workspace)
        else:
            connection = YouTubeAccountConnection.objects.filter(id=connection_id).first()
            if not connection:
                raise NotFound("Connection not found")
            _get_workspace_for_member(request.user, connection.workspace_id)
            qs = YouTubeVideoPost.objects.filter(connection=connection)

        qs = qs.order_by("-created_at")[:100]
        return Response(YouTubeVideoPostSerializer(qs, many=True).data)


class YouTubeCommentViewSet(viewsets.ViewSet):
    """List cached comment logs + manually trigger fetch/screen and moderation."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="YouTube 댓글 캐시 목록",
        description=(
            "내부에 캐시된 ``YouTubeCommentLog`` 를 반환합니다. ``fetch`` 액션으로 갱신.\n\n"
            "필터: ``connection_id`` (필수), ``video_id`` (선택), ``status`` (선택)."
        ),
        responses={200: YouTubeCommentLogSerializer(many=True)},
    )
    def list(self, request):
        connection_id = request.query_params.get("connection_id")
        if not connection_id:
            return Response(
                {"detail": "connection_id query param is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        connection = YouTubeAccountConnection.objects.filter(id=connection_id).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)

        qs = YouTubeCommentLog.objects.filter(connection=connection)
        if video_id := request.query_params.get("video_id"):
            qs = qs.filter(external_video_id=video_id)
        if status_filter := request.query_params.get("status"):
            qs = qs.filter(status=status_filter)
        qs = qs.order_by("-created_at")[:200]
        return Response(YouTubeCommentLogSerializer(qs, many=True).data)

    @extend_schema(
        summary="YouTube 댓글 가져오기 + 자동 분류",
        description=(
            "## 목적\n"
            "특정 영상의 top-level 댓글 스레드를 ``commentThreads.list`` 로 가져와 캐시하고, "
            "활성 ``YouTubeSpamFilterConfig`` 규칙에 따라 휴리스틱 분류를 수행합니다.\n\n"
            "## 할당량\n"
            "1 quota unit / call. 100건 제한(maxResults=100) — 페이지네이션은 Phase 2.\n\n"
            "## 비동기\n"
            "Celery 태스크로 백그라운드 처리. 완료 후 list 호출로 결과 확인."
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
        connection = YouTubeAccountConnection.objects.filter(id=connection_id).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)
        fetch_and_screen_comments.delay(str(connection.id), video_id)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="YouTube 댓글 모더레이션",
        description=(
            "내부 ``YouTubeCommentLog.id`` 단위로 ``setModerationStatus`` 를 큐잉합니다.\n\n"
            "- ``action=review`` → ``heldForReview``\n"
            "- ``action=reject`` → ``rejected`` (banAuthor 옵션은 ``YouTubeSpamFilterConfig.ban_authors_on_reject`` 설정에 따름)\n\n"
            "각 호출 50 quota units."
        ),
        request={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["review", "reject"]},
            },
            "required": ["action"],
        },
        responses={202: OpenApiResponse(description="태스크 enqueued")},
    )
    @action(detail=True, methods=["post"], url_path="moderate")
    def moderate(self, request, pk=None):
        action_value = request.data.get("action")
        if action_value not in ("review", "reject"):
            return Response(
                {"detail": "action must be 'review' or 'reject'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        log = YouTubeCommentLog.objects.select_related("connection").filter(id=pk).first()
        if not log:
            raise NotFound("Comment log not found")
        workspace = _get_workspace_for_member(request.user, log.connection.workspace_id)
        UsageTracker.check_and_increment(workspace, "comments_moderated", 1)
        moderate_comment.delay(str(log.id), action_value)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)


class YouTubeSpamFilterViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _get_for_connection(self, request, connection_id: str):
        connection = YouTubeAccountConnection.objects.filter(id=connection_id).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)
        return connection

    @extend_schema(
        summary="YouTube 스팸 필터 조회/생성",
        responses={200: YouTubeSpamFilterConfigSerializer},
    )
    @action(detail=False, methods=["get"], url_path="connections/(?P<connection_id>[^/.]+)")
    def get_or_create(self, request, connection_id=None):
        connection = self._get_for_connection(request, connection_id)
        cfg, _ = YouTubeSpamFilterConfig.objects.get_or_create(connection=connection)
        return Response(YouTubeSpamFilterConfigSerializer(cfg).data)

    @extend_schema(
        summary="YouTube 스팸 필터 수정",
        request=YouTubeSpamFilterConfigSerializer,
        responses={200: YouTubeSpamFilterConfigSerializer},
    )
    @action(
        detail=False,
        methods=["patch"],
        url_path="connections/(?P<connection_id>[^/.]+)/update",
    )
    def patch_config(self, request, connection_id=None):
        connection = self._get_for_connection(request, connection_id)
        cfg, _ = YouTubeSpamFilterConfig.objects.get_or_create(connection=connection)
        serializer = YouTubeSpamFilterConfigSerializer(cfg, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


def _callback_html(*, success: bool, error_code: str, message: str, connection: dict = None):
    payload = {
        "type": "YOUTUBE_CONNECTED" if success else "YOUTUBE_ERROR",
        "success": success,
        "errorCode": error_code,
        "message": message,
        "connection": connection,
    }
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    body = "✅ YouTube 연동 성공!" if success else f"❌ YouTube 연동 실패: {message}"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>YouTube 연동</title>
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
