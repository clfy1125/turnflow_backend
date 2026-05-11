"""
TikTok integration views — Business API only.

Scope:
- OAuth (business-api.tiktok.com): start + callback + connection list
- Ad Comments: list / fetch+screen / hide / show / delete / reply
- Blocked words: list / check / create / delete / sync

All actions are authorized via ``Access-Token`` against an advertiser_id.
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
    TikTokAccountConnection,
    TikTokBlockedWord,
    TikTokCommentLog,
    TikTokOAuthState,
    TikTokSpamFilterConfig,
)
from .serializers import (
    BlockedWordsBulkRequestSerializer,
    BlockedWordsCheckRequestSerializer,
    CommentFetchRequestSerializer,
    CommentReplyRequestSerializer,
    ConnectionStartResponseSerializer,
    TikTokAccountConnectionSerializer,
    TikTokBlockedWordSerializer,
    TikTokCommentLogSerializer,
    TikTokSpamFilterConfigSerializer,
)
from .services import (
    MockTikTokProvider,
    TikTokAdCommentService,
    TikTokAPIError,
    TikTokBlockedWordService,
    TikTokBusinessOAuthService,
    ensure_fresh_token,
)
from .tasks import (
    fetch_and_screen_comments,
    moderate_comment,
    push_blocked_words_to_tiktok,
    sync_blocked_words_from_tiktok,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_workspace_for_member(user, workspace_id):
    workspace = Workspace.objects.filter(id=workspace_id).first()
    if not workspace:
        raise NotFound("Workspace not found")
    if not workspace.memberships.filter(user=user).exists():
        raise PermissionDenied("You are not a member of this workspace")
    return workspace


def _callback_html(*, success: bool, error_code: str, message: str, connection: dict = None):
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


# ─────────────────────────────────────────────────────────────────────────────
# OAuth + Connections
# ─────────────────────────────────────────────────────────────────────────────

class TikTokIntegrationViewSet(viewsets.ViewSet):
    """OAuth + connection-management endpoints (Business API)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="TikTok Business 연동 시작",
        description=(
            "## 목적\n"
            "TikTok Business API(`business-api.tiktok.com`) OAuth 흐름을 시작합니다.\n\n"
            "## 인증\n"
            "- Bearer 토큰 필수\n"
            "- 호출자는 ``workspace_id`` 의 멤버여야 함\n\n"
            "## 요청 스코프 (TikTok 앱 등록 시 설정)\n"
            "- ``Ad Comments`` — 광고 댓글 list/hide/delete/reply\n"
            "- ``TikTok Accounts`` — 광고 계정 정보 조회\n\n"
            "## 동작\n"
            "1. CSRF 보호용 ``state`` 토큰 생성 → ``TikTokOAuthState`` 에 10분 저장\n"
            "2. ``https://business-api.tiktok.com/portal/auth`` 에 app_id, state, "
            "redirect_uri 를 붙여 권한 동의 URL 생성\n"
            "3. 프론트는 응답 ``authorization_url`` 을 새 창으로 열어 사용자 동의 유도"
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

        redirect_uri = settings.TIKTOK_BUSINESS_REDIRECT_URI or request.build_absolute_uri(
            "/api/v1/tiktok/integration/connect/callback/"
        )

        if MockTikTokProvider.is_mock_mode():
            authorization_url = MockTikTokProvider.generate_authorization_url(redirect_uri, state)
            mode = "mock"
        else:
            authorization_url = TikTokBusinessOAuthService.get_authorization_url(
                redirect_uri, state,
            )
            mode = "production"

        return Response(
            {"authorization_url": authorization_url, "state": state, "mode": mode}
        )

    @extend_schema(
        summary="TikTok Business 연동 콜백",
        description=(
            "## 목적\n"
            "TikTok에서 동의 후 리디렉션된 ``auth_code`` + ``state`` 를 받아 토큰으로 "
            "교환하고 advertiser별 ``TikTokAccountConnection`` 레코드를 생성합니다.\n\n"
            "## 동작\n"
            "1. ``state`` 검증 → 워크스페이스 매핑 복구\n"
            "2. ``POST /open_api/v1.3/oauth2/access_token/`` (auth_code → access_token)\n"
            "3. ``GET /open_api/v1.3/oauth2/advertiser/get/`` 로 권한 부여된 광고주 목록\n"
            "4. 광고주별로 connection 행을 upsert (한 OAuth 가 여러 advertiser 권한을 줄 수 있음)\n"
            "5. 토큰은 Fernet으로 암호화 저장 (``apps.integrations.encryption``)\n\n"
            "## 응답\n"
            "팝업 흐름 호환을 위해 HTML(``window.opener.postMessage``) 을 반환합니다."
        ),
        responses={200: OpenApiResponse(description="HTML(success/error postMessage 포함)")},
    )
    @action(detail=False, methods=["get"], url_path="connect/callback", permission_classes=[])
    def connect_callback(self, request):
        auth_code = request.GET.get("auth_code") or request.GET.get("code", "")
        state = request.GET.get("state", "")
        error = request.GET.get("error", "")

        if error:
            return _callback_html(success=False, error_code="OAUTH_ERROR", message=error)
        if not auth_code or not state:
            return _callback_html(
                success=False,
                error_code="MISSING_PARAMETERS",
                message="auth_code or state missing",
            )

        state_obj = TikTokOAuthState.objects.filter(state=state).first()
        if not state_obj or state_obj.is_expired():
            return _callback_html(
                success=False, error_code="INVALID_STATE",
                message="state expired or invalid",
            )

        workspace = state_obj.workspace

        try:
            if MockTikTokProvider.is_mock_mode() or auth_code.startswith("mock_tt_code_"):
                token_bundle = MockTikTokProvider.exchange_auth_code(auth_code)
                advertisers = MockTikTokProvider.get_advertisers(
                    token_bundle["access_token"],
                )
            else:
                token_bundle = TikTokBusinessOAuthService.exchange_auth_code(auth_code)
                advertisers = TikTokBusinessOAuthService.get_advertisers(
                    token_bundle["access_token"],
                )
        except TikTokAPIError as e:
            logger.error("tiktok.callback: api error %s — %s", e.code, str(e))
            return _callback_html(
                success=False, error_code=e.code or "TIKTOK_API_ERROR", message=str(e),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("tiktok.callback: unexpected error")
            return _callback_html(
                success=False, error_code="INTERNAL_ERROR", message=str(e),
            )

        granted_advertiser_ids = token_bundle.get("advertiser_ids") or []
        if not granted_advertiser_ids and not advertisers:
            return _callback_html(
                success=False,
                error_code="NO_ADVERTISERS",
                message="TikTok returned no advertisers for this token",
            )

        # Index advertiser metadata by id (from advertiser/get) and fall back
        # to bare ids returned in the token exchange.
        adv_meta = {a.get("advertiser_id"): a for a in advertisers if a.get("advertiser_id")}
        for adv_id in granted_advertiser_ids:
            adv_meta.setdefault(adv_id, {"advertiser_id": adv_id})

        first_connection = None
        scopes = token_bundle.get("scope") or []
        if isinstance(scopes, list):
            scopes_list = [str(s) for s in scopes]
        else:
            scopes_list = []

        for adv_id, meta in adv_meta.items():
            connection, _ = TikTokAccountConnection.objects.get_or_create(
                workspace=workspace,
                external_account_id=adv_id,
                defaults={
                    "advertiser_name": meta.get("advertiser_name", ""),
                    "bc_id": meta.get("bc_id", ""),
                    "scopes": scopes_list,
                    "status": TikTokAccountConnection.Status.ACTIVE,
                },
            )
            connection.advertiser_name = meta.get("advertiser_name", connection.advertiser_name)
            connection.bc_id = meta.get("bc_id", connection.bc_id)
            connection.access_token = token_bundle["access_token"]
            connection.scopes = scopes_list
            connection.status = TikTokAccountConnection.Status.ACTIVE
            connection.last_verified_at = timezone.now()
            connection.error_message = ""
            connection.save()
            if first_connection is None:
                first_connection = connection

        state_obj.delete()

        payload = (
            TikTokAccountConnectionSerializer(first_connection).data
            if first_connection
            else None
        )
        return _callback_html(
            success=True, error_code="", message="connected", connection=payload,
        )

    @extend_schema(
        summary="연결된 TikTok advertiser 목록",
        description=(
            "워크스페이스에 연결된 모든 advertiser별 connection을 반환합니다."
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


# ─────────────────────────────────────────────────────────────────────────────
# Ad Comments
# ─────────────────────────────────────────────────────────────────────────────

class TikTokAdCommentViewSet(viewsets.ViewSet):
    """List cached comment logs + sync/moderation actions."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="TikTok 광고 댓글 캐시 목록",
        description=(
            "내부에 캐시된 ``TikTokCommentLog`` 를 반환합니다. ``fetch`` 액션으로 갱신.\n\n"
            "필터: ``connection_id`` (필수), ``ad_id`` (선택), ``status`` (선택)."
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
        if ad_id := request.query_params.get("ad_id"):
            qs = qs.filter(ad_id=ad_id)
        if status_filter := request.query_params.get("status"):
            qs = qs.filter(status=status_filter)
        qs = qs.order_by("-created_at")[:200]
        return Response(TikTokCommentLogSerializer(qs, many=True).data)

    @extend_schema(
        summary="TikTok 광고 댓글 가져오기 + 자동 분류",
        description=(
            "## 목적\n"
            "TikTok Business API `/comment/list/` 를 호출해 광고 댓글을 가져와 "
            "``TikTokCommentLog`` 로 캐시하고, 활성 ``TikTokSpamFilterConfig`` 규칙에 따라 "
            "휴리스틱 분류를 수행합니다.\n\n"
            "## 비동기\n"
            "Celery 태스크가 백그라운드로 처리하며, 완료 후 list 호출로 결과를 확인하세요."
        ),
        request=CommentFetchRequestSerializer,
        responses={202: OpenApiResponse(description="태스크 enqueued")},
    )
    @action(detail=False, methods=["post"], url_path="fetch")
    def fetch(self, request):
        serializer = CommentFetchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        connection = TikTokAccountConnection.objects.filter(id=data["connection_id"]).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)
        fetch_and_screen_comments.delay(
            str(connection.id),
            ad_id=data.get("ad_id", ""),
            page=data.get("page", 1),
            page_size=data.get("page_size", 20),
        )
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="TikTok 댓글 숨기기 (HIDE)",
        description=(
            "내부 ``TikTokCommentLog.id`` 단위로 hide 액션을 큐잉합니다. "
            "TikTok Business API `/comment/status/update/` (action=HIDE) 호출.\n\n"
            "요금제 ``comments_moderated_per_month`` 한도 초과 시 429."
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

    @extend_schema(
        summary="TikTok 댓글 다시 보이기 (SHOW)",
        description=(
            "이전에 숨긴 댓글을 다시 공개합니다. "
            "`/comment/status/update/` (action=SHOW) 호출."
        ),
        responses={202: OpenApiResponse(description="태스크 enqueued")},
    )
    @action(detail=True, methods=["post"], url_path="show")
    def show(self, request, pk=None):
        log = TikTokCommentLog.objects.select_related("connection").filter(id=pk).first()
        if not log:
            raise NotFound("Comment log not found")
        _get_workspace_for_member(request.user, log.connection.workspace_id)
        moderate_comment.delay(str(log.id), "show")
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="TikTok 댓글 삭제",
        description=(
            "본인(브랜드) 댓글만 삭제 가능. fan 댓글은 TikTok 정책상 삭제 불가 — "
            "그 경우 API에서 거절되며 우리는 hide로 폴백."
        ),
        responses={202: OpenApiResponse(description="태스크 enqueued")},
    )
    @action(detail=True, methods=["post"], url_path="delete")
    def delete(self, request, pk=None):
        log = TikTokCommentLog.objects.select_related("connection").filter(id=pk).first()
        if not log:
            raise NotFound("Comment log not found")
        workspace = _get_workspace_for_member(request.user, log.connection.workspace_id)
        UsageTracker.check_and_increment(workspace, "comments_moderated", 1)
        moderate_comment.delay(str(log.id), "delete")
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="TikTok 댓글에 답글 달기",
        description=(
            "``/comment/post/`` 를 동기 호출해 답글을 게시합니다. mock 모드에서는 즉시 응답."
        ),
        request=CommentReplyRequestSerializer,
        responses={
            201: OpenApiResponse(description="답글 게시됨"),
            400: OpenApiResponse(description="텍스트 누락"),
            404: OpenApiResponse(description="comment log 없음"),
        },
    )
    @action(detail=True, methods=["post"], url_path="reply")
    def reply(self, request, pk=None):
        serializer = CommentReplyRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        text = serializer.validated_data["text"]

        log = TikTokCommentLog.objects.select_related("connection").filter(id=pk).first()
        if not log:
            raise NotFound("Comment log not found")
        _get_workspace_for_member(request.user, log.connection.workspace_id)

        try:
            ensure_fresh_token(log.connection)
            if MockTikTokProvider.is_mock_mode():
                resp = MockTikTokProvider.post_reply(log.external_comment_id, text)
            else:
                resp = TikTokAdCommentService.post_reply(
                    log.connection,
                    parent_comment_id=log.external_comment_id,
                    text=text,
                )
        except TikTokAPIError as e:
            return Response(
                {"detail": str(e), "code": e.code, "response": e.response},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(resp, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────────────────
# Spam filter rule config
# ─────────────────────────────────────────────────────────────────────────────

class TikTokSpamFilterViewSet(viewsets.ViewSet):
    """CRUD + activation for the per-connection heuristic spam rule config."""

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
            "특정 connection의 ``TikTokSpamFilterConfig`` 를 반환합니다. 없으면 자동 생성."
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


# ─────────────────────────────────────────────────────────────────────────────
# Blocked words
# ─────────────────────────────────────────────────────────────────────────────

class TikTokBlockedWordViewSet(viewsets.ViewSet):
    """CRUD + sync against TikTok's blockedword list."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="차단 단어 목록 (로컬 캐시)",
        description="``connection_id`` 의 차단 단어 캐시를 반환합니다.",
        responses={200: TikTokBlockedWordSerializer(many=True)},
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
        qs = TikTokBlockedWord.objects.filter(connection=connection)
        return Response(TikTokBlockedWordSerializer(qs, many=True).data)

    @extend_schema(
        summary="TikTok 측 차단 단어 재동기화",
        description=(
            "``/blockedword/list/`` 를 호출해 TikTok 측 차단 단어 목록을 로컬 캐시에 반영합니다."
        ),
        request={"type": "object", "properties": {"connection_id": {"type": "string"}}},
        responses={202: OpenApiResponse(description="동기화 enqueued")},
    )
    @action(detail=False, methods=["post"], url_path="sync")
    def sync(self, request):
        connection_id = request.data.get("connection_id")
        if not connection_id:
            return Response(
                {"detail": "connection_id required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)
        sync_blocked_words_from_tiktok.delay(str(connection.id))
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="차단 단어 일괄 등록",
        description=(
            "``/blockedword/create/`` 를 호출해 TikTok 측에 차단 단어를 일괄 추가하고, "
            "로컬 캐시도 함께 업데이트합니다."
        ),
        request=BlockedWordsBulkRequestSerializer,
        responses={202: OpenApiResponse(description="등록 enqueued")},
    )
    @action(detail=False, methods=["post"], url_path="create")
    def create_bulk(self, request):
        serializer = BlockedWordsBulkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = TikTokAccountConnection.objects.filter(
            id=serializer.validated_data["connection_id"],
        ).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)
        push_blocked_words_to_tiktok.delay(
            str(connection.id), list(serializer.validated_data["words"]),
        )
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="차단 단어 일괄 삭제 (TikTok + 로컬)",
        description=(
            "``/blockedword/delete/`` 를 동기 호출. 로컬 캐시에서도 즉시 제거."
        ),
        request=BlockedWordsBulkRequestSerializer,
        responses={200: OpenApiResponse(description="삭제됨")},
    )
    @action(detail=False, methods=["post"], url_path="delete")
    def delete_bulk(self, request):
        serializer = BlockedWordsBulkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = TikTokAccountConnection.objects.filter(
            id=serializer.validated_data["connection_id"],
        ).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)

        words = list(serializer.validated_data["words"])
        # Resolve external_ids from local cache (need ids for TikTok delete API)
        rows = TikTokBlockedWord.objects.filter(connection=connection, word__in=words)
        ids = [r.external_id for r in rows if r.external_id]

        try:
            ensure_fresh_token(connection)
            if MockTikTokProvider.is_mock_mode():
                resp = MockTikTokProvider.delete_blocked_words(ids)
            else:
                resp = TikTokBlockedWordService.delete(connection, ids=ids)
        except TikTokAPIError as e:
            return Response(
                {"detail": str(e), "code": e.code},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rows.delete()
        return Response({"deleted": words, "tiktok_response": resp})

    @extend_schema(
        summary="차단 단어 등록 여부 확인",
        description=(
            "``/blockedword/check/`` 를 동기 호출해 각 단어의 TikTok 측 등록 여부를 반환."
        ),
        request=BlockedWordsCheckRequestSerializer,
        responses={200: OpenApiResponse(description="결과 배열")},
    )
    @action(detail=False, methods=["post"], url_path="check")
    def check_bulk(self, request):
        serializer = BlockedWordsCheckRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = TikTokAccountConnection.objects.filter(
            id=serializer.validated_data["connection_id"],
        ).first()
        if not connection:
            raise NotFound("Connection not found")
        _get_workspace_for_member(request.user, connection.workspace_id)

        words = list(serializer.validated_data["words"])
        try:
            ensure_fresh_token(connection)
            if MockTikTokProvider.is_mock_mode():
                resp = MockTikTokProvider.check_blocked_words(words)
            else:
                resp = TikTokBlockedWordService.check(connection, words=words)
        except TikTokAPIError as e:
            return Response(
                {"detail": str(e), "code": e.code},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(resp)
