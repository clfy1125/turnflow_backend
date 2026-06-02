"""
Instagram integration views
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiParameter, OpenApiExample
from django.shortcuts import redirect
from django.http import HttpResponse
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
import secrets

from apps.workspace.models import Workspace
from apps.workspace.permissions import IsWorkspaceMember
from .models import IGAccountConnection, AutoDMCampaign, SentDMLog, SpamFilterConfig, SpamCommentLog, IGOAuthState
from .serializers import (
    IGAccountConnectionSerializer,
    ConnectionStartResponseSerializer,
    DisconnectResponseSerializer,
    ConnectionCallbackResponseSerializer,
    AutoDMCampaignSerializer,
    AutoDMCampaignCreateSerializer,
    AutoDMCampaignUpdateSerializer,
    SentDMLogSerializer,
    SpamFilterConfigSerializer,
    SpamFilterConfigUpdateSerializer,
    SpamCommentLogSerializer,
)
from .services import InstagramOAuthService, MockInstagramProvider
import requests


class InstagramIntegrationViewSet(viewsets.ViewSet):
    """
    ViewSet for Instagram account integration
    """

    permission_classes = [IsAuthenticated]

    def get_workspace(self, workspace_id):
        """Get workspace and check membership"""
        workspace = Workspace.objects.get(id=workspace_id)
        if not workspace.memberships.filter(user=self.request.user).exists():
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You are not a member of this workspace")
        return workspace

    def resolve_ig_connection(self, workspace, ig_connection_id: str | None):
        """
        멀티 IG 계정: 쿼리 파라미터 ig_connection_id 로 활성 IG 계정 1개 선택.
        미지정 시 워크스페이스의 첫 활성 connection 반환 (backward compat).

        Returns: IGAccountConnection 또는 None.
        Raises: PermissionDenied — 다른 워크스페이스의 connection 을 지정한 경우.
        """
        from django.core.exceptions import ValidationError
        from rest_framework.exceptions import PermissionDenied

        if not ig_connection_id:
            return IGAccountConnection.objects.filter(
                workspace=workspace, status="active"
            ).first()

        try:
            connection = IGAccountConnection.objects.get(id=ig_connection_id)
        except (IGAccountConnection.DoesNotExist, ValidationError, ValueError, TypeError):
            return None
        if connection.workspace_id != workspace.id:
            raise PermissionDenied("이 IG 계정은 해당 워크스페이스에 속하지 않습니다.")
        return connection

    @extend_schema(
        summary="Instagram 연동 시작",
        description="""
        ## 목적
        Instagram Business 계정 연동을 시작합니다.
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 동작 방식
        1. Instagram OAuth 인증 URL 생성
        2. 사용자를 Facebook OAuth 페이지로 리디렉션
        3. 사용자가 권한 승인
        4. Callback URL로 리디렉션됨
        5. 백엔드에서 토큰 교환 및 Instagram 계정 정보 조회
        
        ## 필요한 Facebook 권한
        - `pages_show_list` - Facebook Page 목록 조회
        - `pages_read_engagement` - Page 정보 및 engagement 읽기
        - `instagram_basic` - Instagram 프로필 및 미디어 접근
        - `instagram_manage_comments` - Instagram 댓글 관리
        - `instagram_manage_messages` - Instagram DM 관리
        - `business_management` - 비즈니스 계정 관리
        
        ## 사용 예시
        ```javascript
        // 프론트엔드에서 새 창으로 OAuth 시작
        const response = await fetch(
            `/api/v1/integrations/instagram/workspaces/${workspaceId}/connect/start/`,
            {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        const data = await response.json();
        // 새 창으로 Facebook OAuth 페이지 열기
        window.open(data.authorization_url, '_blank', 'width=600,height=800');
        ```
        
        ## 응답 예시
        ```json
        {
            "authorization_url": "https://www.facebook.com/v24.0/dialog/oauth?client_id=...",
            "state": "abc123...",
            "mode": "production"
        }
        ```
        """,
        responses={
            200: ConnectionStartResponseSerializer,
            401: OpenApiResponse(description="인증 실패 - 유효하지 않은 토큰"),
            403: OpenApiResponse(description="권한 없음 - 워크스페이스 멤버가 아님"),
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/connect/start",
    )
    def connect_start(self, request, workspace_id=None):
        """Start Instagram OAuth flow"""
        workspace = self.get_workspace(workspace_id)

        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)

        # Persist state in DB instead of session so popup flows without cookie/session work
        expires_at = timezone.now() + timedelta(minutes=10)
        IGOAuthState.objects.create(state=state, workspace=workspace, expires_at=expires_at)

        # Build redirect URI - use INSTAGRAM_REDIRECT_URI from settings if available
        redirect_uri = settings.INSTAGRAM_REDIRECT_URI
        if not redirect_uri:
            # Fallback to build_absolute_uri
            redirect_uri = request.build_absolute_uri(
                "/api/v1/integrations/instagram/connect/callback/"
            )

        # Check if mock mode
        if MockInstagramProvider.is_mock_mode():
            auth_url = MockInstagramProvider.generate_mock_authorization_url(redirect_uri, state)
            mode = "mock"
        else:
            auth_url = InstagramOAuthService.get_authorization_url(redirect_uri, state)
            mode = "production"

        return Response(
            {
                "authorization_url": auth_url,
                "state": state,
                "mode": mode,
            }
        )

    @extend_schema(
        summary="Instagram 연동 콜백",
        description="""
        ## 목적
        Instagram OAuth 콜백을 처리하고 계정 연결을 완료합니다.
        
        ## 동작 방식
        1. Authorization code 수신
        2. Code를 Access Token으로 교환
        3. Long-lived Token 획득 (60일 유효)
        4. Facebook Pages 조회
        5. Instagram Business Account 확인
        6. 계정 정보 조회 및 IGAccountConnection 생성
        
        ## 주의사항
        - 이 엔드포인트는 **Facebook OAuth에서 자동으로 호출**됩니다
        - 사용자가 직접 호출할 필요 없음
        - Meta 개발자 센터에 이 URL을 **OAuth 리디렉션 URI**로 등록 필수
        
        ## Meta 앱 설정
        리디렉션 URI 등록:
        ```
        https://your-domain.com/api/v1/integrations/instagram/connect/callback/
        ```
        
        ## 성공 응답 예시
        ```json
        {
            "success": true,
            "message": "Instagram account connected successfully",
            "connection": {
                "id": "d3fa8212-81c0-4fea-9f3b-5dc46d6e6922",
                "workspace_id": "70286ddf-a5eb-4d09-b460-fbb937a22b15",
                "workspace_name": "test",
                "external_account_id": "17841462186894820",
                "username": "turnflow_official",
                "account_type": "BUSINESS",
                "token_expires_at": "2026-04-10T12:14:35.576386+09:00",
                "scopes": [
                    "pages_show_list",
                    "pages_read_engagement",
                    "instagram_basic",
                    "instagram_manage_comments",
                    "instagram_manage_messages",
                    "business_management"
                ],
                "status": "active",
                "last_verified_at": "2026-02-09T12:14:35.603024+09:00",
                "error_message": "",
                "is_expired": false,
                "created_at": "2026-02-09T12:14:35.578249+09:00",
                "updated_at": "2026-02-09T12:14:35.603142+09:00"
            }
        }
        ```
        
        ## 에러 응답
        - `OAUTH_AUTHORIZATION_FAILED` - OAuth 인증 실패
        - `MISSING_PARAMETERS` - code 또는 state 파라미터 누락
        - `INVALID_STATE` - 잘못된 state (CSRF 공격 또는 세션 만료)
        - `FACEBOOK_API_ERROR` - Facebook API 호출 실패
        - `NO_FACEBOOK_PAGE` - 연결된 Facebook Page 없음
        - `NO_INSTAGRAM_BUSINESS_ACCOUNT` - Page에 Instagram 비즈니스 계정 미연결
        - `INTERNAL_ERROR` - 서버 내부 오류
        """,
        responses={
            200: ConnectionCallbackResponseSerializer,
            400: OpenApiResponse(
                description="요청 오류",
                examples=[
                    {
                        "error_code": "NO_FACEBOOK_PAGE",
                        "message": "Facebook Page가 없습니다",
                    },
                    {
                        "error_code": "NO_INSTAGRAM_BUSINESS_ACCOUNT",
                        "message": "Instagram 비즈니스 계정이 연결되지 않음",
                    },
                ],
            ),
            500: OpenApiResponse(description="서버 내부 오류"),
        },
    )
    @action(detail=False, methods=["get"], url_path="connect/callback", permission_classes=[])
    def connect_callback(self, request):
        """Handle Instagram OAuth callback"""
        code = request.GET.get("code")
        state = request.GET.get("state")
        error = request.GET.get("error")

        # Check for errors
        if error:
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Instagram 연동 실패</title>
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 40px; text-align: center; }}
                    .error {{ color: #dc3545; }}
                </style>
            </head>
            <body>
                <h2 class="error">❌ OAuth 인증 실패</h2>
                <p>{error}</p>
                <p>창이 자동으로 닫힙니다...</p>
                <script>
                    if (window.opener) {{
                        window.opener.postMessage({{
                            type: 'INSTAGRAM_ERROR',
                            success: false,
                            errorCode: 'OAUTH_AUTHORIZATION_FAILED',
                            message: 'OAuth 인증에 실패했습니다: {error}'
                        }}, '*');
                        setTimeout(() => window.close(), 2000);
                    }}
                </script>
            </body>
            </html>
            """
            return HttpResponse(html)

        if not code or not state:
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Instagram 연동 실패</title>
                <style>
                    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 40px; text-align: center; }
                    .error { color: #dc3545; }
                </style>
            </head>
            <body>
                <h2 class="error">❌ 필수 파라미터 누락</h2>
                <p>창이 자동으로 닫힙니다...</p>
                <script>
                    if (window.opener) {
                        window.opener.postMessage({
                            type: 'INSTAGRAM_ERROR',
                            success: false,
                            errorCode: 'MISSING_PARAMETERS',
                            message: '필수 파라미터가 누락되었습니다.'
                        }, '*');
                        setTimeout(() => window.close(), 2000);
                    }
                </script>
            </body>
            </html>
            """
            return HttpResponse(html)

        # Verify state (CSRF protection) using persisted IGOAuthState
        state_obj = IGOAuthState.objects.filter(state=state).first()
        if not state_obj or state_obj.is_expired():
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Instagram 연동 실패</title>
                <style>
                    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 40px; text-align: center; }
                    .error { color: #dc3545; }
                </style>
            </head>
            <body>
                <h2 class="error">❌ 세션 만료</h2>
                <p>세션이 만료되었거나 잘못된 요청입니다.</p>
                <p>창이 자동으로 닫힙니다...</p>
                <script>
                    if (window.opener) {
                        window.opener.postMessage({
                            type: 'INSTAGRAM_ERROR',
                            success: false,
                            errorCode: 'INVALID_STATE',
                            message: '세션이 만료되었거나 잘못된 요청입니다. 다시 시도해주세요.'
                        }, '*');
                        setTimeout(() => window.close(), 2000);
                    }
                </script>
            </body>
            </html>
            """
            return HttpResponse(html)

        try:
            workspace = state_obj.workspace

            # Build redirect URI - use INSTAGRAM_REDIRECT_URI from settings if available
            redirect_uri = settings.INSTAGRAM_REDIRECT_URI
            if not redirect_uri:
                # Fallback to build_absolute_uri
                redirect_uri = request.build_absolute_uri(
                    "/api/v1/integrations/instagram/connect/callback/"
                )

            # Exchange code for token
            if code.startswith("mock_code_"):
                # Mock mode
                token_response = MockInstagramProvider.exchange_mock_code_for_token(code)
                long_lived_response = MockInstagramProvider.get_mock_long_lived_token(
                    token_response["access_token"]
                )
                account_info = MockInstagramProvider.get_mock_account_info(
                    long_lived_response["access_token"]
                )
            else:
                # Production mode - Instagram Business Login
                import logging

                logger = logging.getLogger(__name__)

                # 1. Exchange code for short-lived Instagram User access token
                # Returns: {"access_token": "...", "user_id": "...", "permissions": "..."}
                token_response = InstagramOAuthService.exchange_code_for_token(code, redirect_uri)
                short_lived_token = token_response["access_token"]
                ig_user_id = token_response.get("user_id", "")

                # 2. Get long-lived token (60 days)
                long_lived_response = InstagramOAuthService.get_long_lived_token(short_lived_token)
                access_token = long_lived_response["access_token"]

                # 3. Get Instagram account info directly (no Facebook Pages needed)
                try:
                    account_info = InstagramOAuthService.get_account_info(access_token)
                except Exception as e:
                    logger.error(f"Exception during get_account_info: {str(e)}")

                    html = f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <meta charset="UTF-8">
                        <title>Instagram 연동 실패</title>
                        <style>
                            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 40px; text-align: center; }}
                            .error {{ color: #dc3545; }}
                        </style>
                    </head>
                    <body>
                        <h2 class="error">❌ Instagram API 오류</h2>
                        <p>Instagram API 호출 중 오류가 발생했습니다.</p>
                        <p>창이 자동으로 닫힙니다...</p>
                        <script>
                            if (window.opener) {{
                                window.opener.postMessage({{
                                    type: 'INSTAGRAM_ERROR',
                                    success: false,
                                    errorCode: 'INSTAGRAM_API_ERROR',
                                    message: 'Instagram API 호출 중 오류가 발생했습니다.'
                                }}, '*');
                                setTimeout(() => window.close(), 2000);
                            }}
                        </script>
                    </body>
                    </html>
                    """
                    return HttpResponse(html)

                # Use user_id from token response or account_info
                instagram_account_id = account_info.get("user_id") or ig_user_id or account_info.get("id", "")

                account_info["id"] = instagram_account_id

                # Calculate expiration time
                expires_in = long_lived_response.get("expires_in", 5184000)  # Default 60 days
                expires_at = timezone.now() + timedelta(seconds=expires_in)

                # Get or create connection (without access_token in defaults)
                connection, created = IGAccountConnection.objects.get_or_create(
                    workspace=workspace,
                    external_account_id=account_info["id"],
                    defaults={
                        "username": account_info.get("username", account_info.get("name", "")),
                        "account_type": "BUSINESS",
                        "token_expires_at": expires_at,
                        "scopes": InstagramOAuthService.REQUIRED_SCOPES,
                        "status": IGAccountConnection.Status.ACTIVE,
                        "last_verified_at": timezone.now(),
                        "error_message": "",
                    },
                )

                # Set encrypted field through descriptor and update other fields
                connection.username = account_info.get("username", account_info.get("name", ""))
                connection.account_type = "BUSINESS"
                connection.access_token = (
                    access_token
                    if not code.startswith("mock_code_")
                    else long_lived_response["access_token"]
                )
                connection.token_expires_at = expires_at
                connection.scopes = InstagramOAuthService.REQUIRED_SCOPES
                connection.status = IGAccountConnection.Status.ACTIVE
                connection.last_verified_at = timezone.now()
                connection.error_message = ""
                connection.save()

                # Enable webhook subscriptions for this account (per-account requirement)
                try:
                    subscribe_result = InstagramOAuthService.subscribe_to_webhooks(
                        ig_user_id=instagram_account_id,
                        access_token=access_token,
                        fields="comments,messages",
                    )
                    logger.debug(f"Webhook subscription result for {instagram_account_id}: {subscribe_result}")
                except Exception as e:
                    logger.warning(f"Failed to subscribe webhooks for {instagram_account_id}: {e}")

                # 신규 연동/권한 추가 재연동 직후 — 메타데이터 + 모든 인사이트 자동 부트스트랩
                # (프론트가 별도로 sync 트리거할 필요 없이 즉시 데이터 확보)
                try:
                    from apps.insights.tasks import bootstrap_account
                    bootstrap_account.delay(str(connection.id))
                    logger.info(f"Enqueued insights bootstrap for {connection.id}")
                except Exception as e:
                    logger.warning(f"Failed to enqueue insights bootstrap (non-fatal): {e}")

                # 프로필 사진 캐싱 — IG CDN URL 은 서명된 일시 URL 이라 만료될 수 있으므로
                # 연동 즉시 우리 스토리지로 사본을 끌어와서 안정 URL 확보. best-effort.
                try:
                    from .tasks import sync_ig_profile_picture
                    sync_ig_profile_picture.delay(str(connection.id))
                    logger.info(f"Enqueued profile picture sync for {connection.id}")
                except Exception as e:
                    logger.warning(f"Failed to enqueue profile sync (non-fatal): {e}")

            # Clean up persisted state
            try:
                state_obj.delete()
            except Exception:
                pass

            # Return success response with HTML
            connection_data = IGAccountConnectionSerializer(connection).data
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Instagram 연동 성공</title>
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 40px; text-align: center; }}
                    .success {{ color: #28a745; }}
                    .account {{ margin-top: 20px; padding: 15px; background: #f8f9fa; border-radius: 8px; }}
                </style>
            </head>
            <body>
                <h2 class="success">✅ Instagram 연동 성공!</h2>
                <div class="account">
                    <p><strong>계정:</strong> @{connection_data.get('username', 'Unknown')}</p>
                    <p><strong>유형:</strong> {connection_data.get('account_type', 'BUSINESS')}</p>
                </div>
                <p>창이 자동으로 닫힙니다...</p>
                <script>
                    if (window.opener) {{
                        window.opener.postMessage({{
                            type: 'INSTAGRAM_CONNECTED',
                            success: true,
                            connection: {str(connection_data).replace("'", '"')}
                        }}, '*');
                        setTimeout(() => window.close(), 1500);
                    }}
                </script>
            </body>
            </html>
            """
            return HttpResponse(html)

        except Exception as e:
            logger.error(f"Fatal error in connect_callback: {type(e).__name__} - {str(e)}")

            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Instagram 연동 오류</title>
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 40px; text-align: center; }}
                    .error {{ color: #dc3545; }}
                </style>
            </head>
            <body>
                <h2 class="error">❌ 서버 오류</h2>
                <p>연동 중 오류가 발생했습니다.</p>
                <p>잠시 후 다시 시도해주세요.</p>
                <p>창이 자동으로 닫힙니다...</p>
                <script>
                    if (window.opener) {{
                        window.opener.postMessage({{
                            type: 'INSTAGRAM_ERROR',
                            success: false,
                            errorCode: 'INTERNAL_ERROR',
                            message: '서버 오류가 발생했습니다: {str(e)}'
                        }}, '*');
                        setTimeout(() => window.close(), 2000);
                    }}
                </script>
            </body>
            </html>
            """
            return HttpResponse(html)

    @extend_schema(
        summary="연결된 Instagram 계정 목록",
        description="""
        ## 목적
        워크스페이스에 연결된 Instagram 계정 목록을 조회합니다.
        
        ## 사용 시나리오
        - 연결된 계정 확인
        - 토큰 만료 상태 확인
        - 계정 정보 조회
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 응답 데이터
        - `id`: 연결 ID
        - `external_account_id`: Instagram 계정 ID
        - `username`: Instagram 사용자명
        - `account_type`: 계정 유형 (BUSINESS/CREATOR)
        - `token_expires_at`: 토큰 만료 시간
        - `status`: 연결 상태 (active/expired/revoked/error)
        - `is_expired`: 토큰 만료 여부
        
        ## 사용 예시
        ```javascript
        const response = await fetch(
            `http://localhost:8000/api/v1/integrations/instagram/workspaces/${workspaceId}/connections/`,
            {
                headers: {
                    'Authorization': `Bearer ${accessToken}`
                }
            }
        );
        
        const connections = await response.json();
        ```
        """,
        responses={
            200: IGAccountConnectionSerializer(many=True),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버가 아님"),
        },
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/connections",
    )
    def list_connections(self, request, workspace_id=None):
        """List Instagram connections for workspace"""
        workspace = self.get_workspace(workspace_id)
        connections = IGAccountConnection.objects.filter(workspace=workspace)
        serializer = IGAccountConnectionSerializer(connections, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="[개발용] Instagram API 테스트",
        description="""
        ## 목적 (개발 전용)
        연동된 Instagram 계정으로 실제 Instagram Graph API를 호출하여 테스트합니다.
        
        ## 테스트 항목
        - ✅ 프로필 정보 조회 (username, profile_picture, followers_count 등)
        - ✅ 최근 미디어 조회 (게시물 5개)
        - ✅ 미디어 상세 정보 (좋아요 수, 댓글 수 등)
        
        ## 응답
        연결된 Instagram 계정의 프로필 및 최근 게시물 정보를 반환합니다.
        
        ## 주의
        - 개발/테스트 용도로만 사용하세요.
        - 프로덕션에서는 제거될 수 있습니다.
        """,
        responses={
            200: OpenApiResponse(
                description="API 테스트 성공",
                response={
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "connection": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "username": {"type": "string"},
                            },
                        },
                        "profile": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "username": {"type": "string"},
                                "name": {"type": "string"},
                                "profile_picture_url": {"type": "string"},
                                "followers_count": {"type": "integer"},
                                "follows_count": {"type": "integer"},
                                "media_count": {"type": "integer"},
                            },
                        },
                        "recent_media": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "caption": {"type": "string"},
                                    "media_type": {"type": "string"},
                                    "media_url": {"type": "string"},
                                    "permalink": {"type": "string"},
                                    "timestamp": {"type": "string"},
                                    "like_count": {"type": "integer"},
                                    "comments_count": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
            ),
            404: OpenApiResponse(description="연결된 Instagram 계정 없음"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버가 아님"),
            500: OpenApiResponse(description="Instagram API 호출 실패"),
        },
        tags=["개발 전용"],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/test-api",
    )
    def test_instagram_api(self, request, workspace_id=None):
        """
        [DEV ONLY] Test Instagram Graph API with connected account
        """
        workspace = self.get_workspace(workspace_id)

        # 연결된 Instagram 계정 찾기
        connection = IGAccountConnection.objects.filter(
            workspace=workspace, status="active"
        ).first()

        if not connection:
            return Response(
                {
                    "success": False,
                    "error": "연결된 Instagram 계정이 없습니다. 먼저 계정을 연동해주세요.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            # Instagram Graph API 호출
            graph_api_base = InstagramOAuthService.GRAPH_API_BASE
            access_token = connection.access_token  # 자동 복호화됨

            # 1. 프로필 정보 조회
            profile_url = f"{graph_api_base}/{connection.external_account_id}"
            profile_params = {
                "fields": "id,username,name,profile_picture_url,followers_count,follows_count,media_count",
                "access_token": access_token,
            }

            profile_response = requests.get(profile_url, params=profile_params)
            profile_response.raise_for_status()
            profile_data = profile_response.json()

            # 2. 최근 미디어 조회 (5개)
            media_url = f"{graph_api_base}/{connection.external_account_id}/media"
            media_params = {
                "fields": "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count",
                "limit": 5,
                "access_token": access_token,
            }

            media_response = requests.get(media_url, params=media_params)
            media_response.raise_for_status()
            media_data = media_response.json()

            return Response(
                {
                    "success": True,
                    "message": "✅ Instagram API 테스트 성공!",
                    "connection": {
                        "id": str(connection.id),
                        "username": connection.username,
                        "account_type": connection.account_type,
                        "status": connection.status,
                        "connected_at": connection.created_at,
                    },
                    "profile": profile_data,
                    "recent_media": {
                        "count": len(media_data.get("data", [])),
                        "data": media_data.get("data", []),
                    },
                    "api_info": {
                        "graph_api_version": InstagramOAuthService.FACEBOOK_VERSION,
                        "scopes_used": connection.scopes,
                    },
                }
            )

        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json() if e.response else str(e)
            return Response(
                {
                    "success": False,
                    "error": "Instagram API 호출 실패",
                    "detail": error_detail,
                    "status_code": e.response.status_code if e.response else None,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception as e:
            return Response(
                {
                    "success": False,
                    "error": "예상치 못한 오류 발생",
                    "detail": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @extend_schema(
        summary="[개발용] Instagram 게시물 목록 조회",
        description="""
        ## 목적 (개발 전용)
        연동된 Instagram 계정의 게시물(미디어) 목록을 조회합니다.
        
        ## 기능
        - 📸 게시물 목록 조회 (IMAGE, VIDEO, CAROUSEL_ALBUM)
        - 📊 각 게시물의 인게이지먼트 데이터 (좋아요, 댓글 수)
        - 🔄 페이지네이션 지원 (limit, after)
        
        ## Query Parameters
        - `limit`: 가져올 게시물 수 (기본값: 10, 최대: 50)
        - `after`: 페이지네이션 커서 (다음 페이지 조회 시 사용)
        
        ## 응답
        게시물 목록과 페이지네이션 정보를 반환합니다.
        
        ## 주의
        - 개발/테스트 용도로만 사용하세요.
        """,
        responses={
            200: OpenApiResponse(
                description="게시물 조회 성공",
                response={
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "data": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "caption": {"type": "string"},
                                    "media_type": {"type": "string"},
                                    "media_url": {"type": "string"},
                                    "permalink": {"type": "string"},
                                    "timestamp": {"type": "string"},
                                    "like_count": {"type": "integer"},
                                    "comments_count": {"type": "integer"},
                                },
                            },
                        },
                        "paging": {
                            "type": "object",
                            "properties": {
                                "cursors": {
                                    "type": "object",
                                    "properties": {
                                        "before": {"type": "string"},
                                        "after": {"type": "string"},
                                    },
                                },
                                "next": {"type": "string"},
                            },
                        },
                        "count": {"type": "integer"},
                    },
                },
            ),
            404: OpenApiResponse(description="연결된 Instagram 계정 없음"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버가 아님"),
            500: OpenApiResponse(description="Instagram API 호출 실패"),
        },
        parameters=[
            OpenApiParameter(
                name="limit",
                type=int,
                location=OpenApiParameter.QUERY,
                description="가져올 게시물 수 (기본값: 10, 최대: 50)",
                required=False,
                default=10,
            ),
            OpenApiParameter(
                name="after",
                type=str,
                location=OpenApiParameter.QUERY,
                description="페이지네이션 커서 (다음 페이지 조회 시 사용)",
                required=False,
            ),
            OpenApiParameter(
                name="ig_connection_id",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "조회할 IG 계정 connection UUID. 워크스페이스에 여러 IG 계정이 "
                    "연동돼 있을 때 특정 계정의 게시물만 조회. 미지정 시 첫 번째 "
                    "활성 connection 사용 (backward compat)."
                ),
                required=False,
            ),
        ],
        tags=["개발 전용"],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/media",
    )
    def get_media(self, request, workspace_id=None):
        """
        [DEV ONLY] Get Instagram media (posts) for connected account
        """
        workspace = self.get_workspace(workspace_id)

        # Query parameters
        limit = min(int(request.query_params.get("limit", 10)), 50)  # 최대 50개
        after = request.query_params.get("after", None)
        ig_connection_id = request.query_params.get("ig_connection_id")

        # 연결된 Instagram 계정 찾기 (멀티 IG: 쿼리로 지정 가능, 미지정 시 첫 활성)
        connection = self.resolve_ig_connection(workspace, ig_connection_id)

        if not connection:
            return Response(
                {
                    "success": False,
                    "error": "연결된 Instagram 계정이 없습니다. 먼저 계정을 연동해주세요.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            graph_api_base = InstagramOAuthService.GRAPH_API_BASE
            access_token = connection.access_token

            # Instagram Media API 호출
            media_url = f"{graph_api_base}/{connection.external_account_id}/media"
            params = {
                "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count,media_product_type",
                "limit": limit,
                "access_token": access_token,
            }

            # 페이지네이션 커서 추가
            if after:
                params["after"] = after

            response = requests.get(media_url, params=params)
            response.raise_for_status()
            data = response.json()

            return Response(
                {
                    "success": True,
                    "data": data.get("data", []),
                    "paging": data.get("paging", {}),
                    "count": len(data.get("data", [])),
                    "connection": {
                        "id": str(connection.id),
                        "username": connection.username,
                        "account_id": connection.external_account_id,
                    },
                    "query": {
                        "limit": limit,
                        "after": after,
                        "ig_connection_id": ig_connection_id,
                    },
                }
            )

        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json() if e.response else str(e)
            return Response(
                {
                    "success": False,
                    "error": "Instagram API 호출 실패",
                    "detail": error_detail,
                    "status_code": e.response.status_code if e.response else None,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception as e:
            return Response(
                {
                    "success": False,
                    "error": "예상치 못한 오류 발생",
                    "detail": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @extend_schema(
        summary="Instagram 현재 활성 Story 목록",
        description="""
        ## 목적
        연동된 Instagram Business 계정의 **현재 활성 Story** 목록을 조회합니다.
        24시간 이내의 Story 만 반환됩니다 (Meta 정책).

        ## 사용 시나리오
        - Story 답장 기반 캠페인 (`trigger_type=story_reply`) 생성 시 대상 Story 선택
        - "현재 올라간 Story" 에 대한 자동화 룰 설정

        ## 인증
        Bearer JWT 필수 + 해당 workspace 멤버십 필요.

        ## 응답 항목
        - `id`: Story 고유 ID (캠페인의 `media_id` 로 사용)
        - `media_type`: IMAGE | VIDEO
        - `media_url`: 미디어 직접 URL (만료될 수 있음)
        - `media_product_type`: 항상 "STORY"
        - `permalink`: 사용자가 클릭해 볼 수 있는 영구 링크
        - `timestamp`: 게시 시각 (ISO8601)
        - `caption`: 캡션 텍스트 (있는 경우)
        - `thumbnail_url`: 썸네일 (VIDEO 인 경우)

        ## 제약
        - Story 는 24시간 후 자동 삭제 → API 응답에서 사라짐
        - 캠페인 생성 시 선택한 Story 가 만료되면 그 캠페인은 더 이상 트리거 안 됨
        """,
        parameters=[
            OpenApiParameter(
                name="workspace_id",
                location=OpenApiParameter.PATH,
                description="조회할 워크스페이스의 UUID",
                required=True,
                type=str,
            ),
            OpenApiParameter(
                name="ig_connection_id",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "조회할 IG 계정 connection UUID. 워크스페이스에 여러 IG 계정이 "
                    "연동돼 있을 때 특정 계정의 Story 만 조회. 미지정 시 첫 번째 "
                    "활성 connection 사용 (backward compat)."
                ),
                required=False,
            ),
        ],
        responses={
            200: OpenApiResponse(description="활성 Story 목록"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="활성 IG 계정 없음"),
            500: OpenApiResponse(description="Instagram API 호출 실패"),
        },
        tags=["Integrations"],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/stories",
    )
    def get_stories(self, request, workspace_id=None):
        """현재 활성 Story 목록 조회"""
        from .services import InstagramMediaService

        workspace = self.get_workspace(workspace_id)
        ig_connection_id = request.query_params.get("ig_connection_id")
        connection = self.resolve_ig_connection(workspace, ig_connection_id)

        if not connection:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 404,
                        "message": "연결된 Instagram 계정이 없습니다.",
                    },
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            stories = InstagramMediaService.list_stories(
                ig_user_id=connection.external_account_id,
                access_token=connection.access_token,
            )
        except requests.exceptions.HTTPError as e:
            err_body = e.response.json() if e.response is not None else {}
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": e.response.status_code if e.response is not None else 500,
                        "message": "Instagram API 호출 실패",
                        "details": err_body,
                    },
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as e:
            return Response(
                {
                    "success": False,
                    "error": {"code": 500, "message": str(e)},
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "count": len(stories),
                "data": stories,
                "connection": {
                    "id": str(connection.id),
                    "username": connection.username,
                    "account_id": connection.external_account_id,
                },
            }
        )

    # ===== v3.8 — Instagram 연동 해제 =====

    @extend_schema(
        summary="Instagram 연동 해제",
        description="""
        ## 목적
        사용자가 Instagram Business 계정 연동을 자발적으로 해제합니다.
        모든 후속 자동화(캠페인/DM 발송/웹훅)를 즉시 안전하게 정지하고,
        암호화 저장된 access_token 을 폐기합니다.

        ## 사용 시나리오
        - 설정 화면의 "Instagram 연동 끊기" 버튼 클릭
        - 다른 IG 계정으로 재연동하기 위해 기존 연동 정리
        - 보안 이슈로 즉시 토큰을 폐기해야 할 때

        ## 동작 (4단계, 멱등성 보장)
        1. **Webhook 구독 해제 시도** — `DELETE /v25.0/{ig_user_id}/subscribed_apps`
           (best-effort: 토큰이 이미 무효일 수 있어 실패해도 다음 단계 진행)
        2. **활성 캠페인 일시정지** — 이 IG 계정 소유의 `status=active` 캠페인 모두 `paused` 로 전환
        3. **진행 중 DM 정리** — 발송 큐에 남은 SentDMLog (QUEUED/SUBMITTING/ACCEPTED/RATE_LIMITED) 를
           SKIPPED 로 마킹. 이미 DELIVERED/READ 건은 그대로 보존
        4. **연동 폐기** — `status=revoked` + 토큰 컬럼 빈 문자열

        ## Meta 측에서 자동으로 일어나는 일
        - Meta 는 별도 토큰 무효화 API 를 제공하지 않습니다
        - 우리 DB 에서 토큰을 폐기하면 그 토큰은 더 이상 우리 서버에서 사용되지 않음
        - 사용자가 Instagram 설정 → "앱 및 웹사이트" 에서 제거할 수도 있음 (그 경우 별도로 deauth callback 수신)

        ## 인증
        - Bearer JWT 필수
        - 해당 IG 연동이 속한 workspace 의 멤버여야 함

        ## 응답 (DisconnectResponseSerializer)
        ```json
        {
          "success": true,
          "ig_connection_id": "uuid",
          "username": "myshop",
          "status": "revoked",
          "campaigns_paused": 3,
          "logs_cancelled": 12,
          "webhook_unsubscribed": true,
          "webhook_error": null,
          "reason": "user_requested"
        }
        ```

        ## 멱등성
        이미 `revoked` 상태인 연동에 다시 호출해도 안전합니다 (이미 정리된 항목 0건으로 응답).

        ## 재연동
        해제 후 다시 연결하려면 일반 OAuth 흐름을 처음부터 다시 수행:
        `POST /api/v1/integrations/instagram/workspaces/{workspace_id}/connect/`
        """,
        responses={
            200: DisconnectResponseSerializer,
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="해당 IG 연동 없음"),
        },
        tags=["Integrations"],
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="connections/(?P<ig_connection_id>[^/.]+)/disconnect",
    )
    def disconnect(self, request, ig_connection_id=None):
        """Instagram 연동 해제 (자발적)"""
        try:
            connection = IGAccountConnection.objects.select_related("workspace").get(
                id=ig_connection_id
            )
        except IGAccountConnection.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": {"code": 404, "message": "IG 연동을 찾을 수 없습니다."},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if not connection.workspace.memberships.filter(user=request.user).exists():
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 403,
                        "message": "이 IG 연동이 속한 워크스페이스의 멤버가 아닙니다.",
                    },
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        report = connection.disconnect(reason="user_requested")

        return Response(
            {
                "success": True,
                "ig_connection_id": str(connection.id),
                "username": connection.username,
                "status": connection.status,
                "campaigns_paused": report["campaigns_paused"],
                "logs_cancelled": report["logs_cancelled"],
                "webhook_unsubscribed": report["webhook_unsubscribed"],
                "webhook_error": report["webhook_error"],
                "reason": "user_requested",
            }
        )

    @extend_schema(
        summary="Instagram 프로필 사진 최신화",
        description="""
        ## 목적
        IG /me API 를 호출하여 프로필 사진을 최신 상태로 갱신합니다.
        IG CDN URL 은 서명된 일시 URL 이라 만료될 수 있으므로 우리 R2/로컬 스토리지에
        사본을 저장하고 안정 URL 을 반환합니다.

        ## 사용 시나리오
        - 사용자가 인스타에서 프로필 사진을 변경한 후 우리 서비스에 반영하고 싶을 때
        - 캐시된 이미지가 깨져 보일 때 (CDN 만료 의심)
        - 정기적으로 (예: 사용자 설정 화면 열 때) 최신화

        ## 인증
        - Bearer JWT 필수
        - 해당 IG 연동이 속한 workspace 의 멤버여야 함

        ## 동작 (기본: 비동기)
        Celery 태스크 `sync_ig_profile_picture` 큐잉 후 즉시 202 응답.
        백그라운드에서:
          1) IG /me 호출 → 최신 profile_picture_url 획득
          2) source URL 이 기존과 같으면 synced_at 만 갱신
          3) 다르면 다운로드 → 정제 → default_storage 저장 → URL 갱신

        ## 옵션 — 동기 실행 (?sync=1)
        `?sync=1` 쿼리 파라미터를 붙이면 위 흐름을 인라인으로 실행하고 갱신 결과를 200 응답에 포함.
        외부 API 호출이 포함되므로 응답이 수 초 걸릴 수 있음. 디버그/관리자용.

        ## 응답 예시 (비동기, 202)
        ```json
        {
          "success": true,
          "data": {
            "connection_id": "uuid",
            "task_queued": true,
            "message": "프로필 사진 동기화가 큐에 등록되었습니다."
          }
        }
        ```

        ## 응답 예시 (?sync=1, 200)
        ```json
        {
          "success": true,
          "data": {
            "connection_id": "uuid",
            "status": "updated",
            "profile_picture_url": "https://r2-domain.example/ig_profiles/.../abc.jpg"
          }
        }
        ```

        ## 에러
        - 401: 인증 실패
        - 403: 워크스페이스 멤버 아님
        - 404: 해당 IG 연동 없음
        - 502: (?sync=1) IG API 호출/이미지 다운로드 실패
        """,
        parameters=[
            OpenApiParameter(
                name="sync",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="'1' 이면 동기 실행 (디버그). 그 외엔 비동기 큐잉 (기본).",
            ),
        ],
        responses={
            200: OpenApiResponse(description="동기 실행 성공 (sync=1)"),
            202: OpenApiResponse(description="비동기 큐잉 성공"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버 아님"),
            404: OpenApiResponse(description="해당 IG 연동 없음"),
            502: OpenApiResponse(description="(sync=1) IG API/이미지 다운로드 실패"),
        },
        examples=[
            OpenApiExample(
                name="JavaScript fetch (비동기)",
                value=(
                    "fetch('/api/v1/integrations/instagram/connections/UUID/refresh-profile/', "
                    "{method: 'POST', headers: {'Authorization': 'Bearer ' + token}})"
                ),
                request_only=True,
            ),
        ],
        tags=["Integrations"],
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="connections/(?P<ig_connection_id>[^/.]+)/refresh-profile",
    )
    def refresh_profile(self, request, ig_connection_id=None):
        """프로필 사진 강제 동기화."""
        try:
            connection = IGAccountConnection.objects.select_related("workspace").get(
                id=ig_connection_id
            )
        except IGAccountConnection.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": {"code": 404, "message": "IG 연동을 찾을 수 없습니다."},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if not connection.workspace.memberships.filter(user=request.user).exists():
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 403,
                        "message": "이 IG 연동이 속한 워크스페이스의 멤버가 아닙니다.",
                    },
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        sync_flag = (request.query_params.get("sync") or "").strip() == "1"

        from .tasks import sync_ig_profile_picture

        if sync_flag:
            # 인라인 실행 — Celery 태스크 함수를 직접 호출
            try:
                result = sync_ig_profile_picture.apply(args=[str(connection.id)]).get(
                    disable_sync_subtasks=False
                )
            except Exception as e:  # noqa: BLE001
                return Response(
                    {
                        "success": False,
                        "error": {"code": 502, "message": f"동기화 실패: {e}"},
                    },
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            # 최신 DB 값 반영
            connection.refresh_from_db()
            return Response(
                {
                    "success": True,
                    "data": {
                        "connection_id": str(connection.id),
                        "status": result.get("status") if isinstance(result, dict) else "unknown",
                        "profile_picture_url": connection.profile_picture_url,
                        "profile_picture_synced_at": connection.profile_picture_synced_at,
                    },
                },
                status=status.HTTP_200_OK,
            )

        # 기본: 비동기 큐잉
        sync_ig_profile_picture.delay(str(connection.id))
        return Response(
            {
                "success": True,
                "data": {
                    "connection_id": str(connection.id),
                    "task_queued": True,
                    "message": "프로필 사진 동기화가 큐에 등록되었습니다.",
                },
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Meta Deauthorize Callback (사용자가 IG 설정에서 앱 제거 시)",
        description="""
        ## 목적
        Meta App Dashboard 의 **Deauthorize Callback URL** 로 등록되는 엔드포인트.

        사용자가 Instagram 앱 > 설정 > 계정 센터 > 비즈니스 도구 및 컨트롤 > 비즈니스 통합
        (또는 Facebook 설정 → 앱 및 웹사이트) 에서 우리 앱을 제거할 때
        Meta 가 자동으로 POST 호출합니다.

        ## 요청 포맷 (Meta 표준)
        ```
        POST /api/v1/integrations/instagram/deauthorize/
        Content-Type: application/x-www-form-urlencoded

        signed_request=<base64url_signature>.<base64url_payload>
        ```

        Payload (HMAC-SHA256 검증 후 디코딩):
        ```json
        {
          "algorithm": "HMAC-SHA256",
          "issued_at": 1715404800,
          "user_id":   "17841466999619187"
        }
        ```

        ## 동작
        1. `signed_request` 를 app_secret 으로 HMAC-SHA256 검증
        2. 실패 시 400 + 빈 응답 (보안: 어떤 페이로드가 잘못됐는지 안 알림)
        3. payload.user_id 와 일치하는 `IGAccountConnection.external_account_id` 조회
        4. 매칭되는 연동 모두에 대해 `disconnect(reason="meta_deauth")` 실행
        5. 200 OK 반환 (Meta 는 200 만 받으면 OK)

        ## 인증
        - **AllowAny** — Meta 가 호출하므로 JWT 없음
        - 진짜 Meta 인지는 `signed_request` HMAC 으로 검증

        ## 설정 방법 (Meta App Dashboard)
        1. App Dashboard → Settings → Basic
        2. "Deauthorize Callback URL" 에 다음 URL 등록:
           `https://<your-domain>/api/v1/integrations/instagram/deauthorize/`
        3. 저장 후 활성화

        ## 응답
        - 200 OK + `{"success": true, "disconnected": N}` (정상)
        - 400 + `{"error": "invalid signed_request"}` (서명 검증 실패)
        - Meta 는 200 만 받으면 OK 로 간주
        """,
        request={
            "application/x-www-form-urlencoded": {
                "type": "object",
                "properties": {
                    "signed_request": {
                        "type": "string",
                        "description": "<base64url_signature>.<base64url_payload>",
                    },
                },
                "required": ["signed_request"],
            }
        },
        responses={
            200: OpenApiResponse(description="해제 처리 완료"),
            400: OpenApiResponse(description="signed_request 검증 실패"),
        },
        tags=["Integrations"],
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="deauthorize",
        permission_classes=[AllowAny],
        authentication_classes=[],
    )
    def deauthorize(self, request):
        """Meta Deauthorize Callback — 사용자가 IG 설정에서 앱 제거 시 호출됨"""
        from .services import InstagramOAuthService

        signed_request = request.data.get("signed_request") or request.POST.get(
            "signed_request"
        )
        if not signed_request:
            return Response(
                {"error": "missing signed_request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = InstagramOAuthService.parse_signed_request(signed_request)
        if not payload:
            return Response(
                {"error": "invalid signed_request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_id = str(payload.get("user_id") or "")
        if not user_id:
            return Response(
                {"error": "user_id missing in payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 해당 IG user ID 로 연동된 모든 connection (보통 1개) 해제
        connections = IGAccountConnection.objects.filter(
            external_account_id=user_id,
            status=IGAccountConnection.Status.ACTIVE,
        )
        disconnected = 0
        for conn in connections:
            conn.disconnect(reason="meta_deauth")
            disconnected += 1

        return Response({"success": True, "disconnected": disconnected})

    @extend_schema(
        summary="[개발용] Instagram 게시물 상세 조회",
        description="""
        ## 목적 (개발 전용)
        특정 Instagram 게시물의 상세 정보를 조회합니다.
        
        ## 기능
        - 📸 게시물 상세 정보
        - 💬 댓글 목록 (최근 50개)
        - 📊 인게이지먼트 통계
        
        ## Path Parameters
        - `media_id`: Instagram 미디어 ID
        
        ## 주의
        - 개발/테스트 용도로만 사용하세요.
        """,
        responses={
            200: OpenApiResponse(
                description="게시물 상세 조회 성공",
                response={
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "media": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "caption": {"type": "string"},
                                "media_type": {"type": "string"},
                                "media_url": {"type": "string"},
                                "permalink": {"type": "string"},
                                "timestamp": {"type": "string"},
                                "like_count": {"type": "integer"},
                                "comments_count": {"type": "integer"},
                            },
                        },
                        "comments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "username": {"type": "string"},
                                    "timestamp": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            ),
            404: OpenApiResponse(description="연결된 Instagram 계정 또는 게시물 없음"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버가 아님"),
            500: OpenApiResponse(description="Instagram API 호출 실패"),
        },
        tags=["개발 전용"],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workspaces/(?P<workspace_id>[^/.]+)/media/(?P<media_id>[^/.]+)",
    )
    def get_media_detail(self, request, workspace_id=None, media_id=None):
        """
        [DEV ONLY] Get detailed information about a specific Instagram media
        """
        workspace = self.get_workspace(workspace_id)

        # 연결된 Instagram 계정 찾기
        connection = IGAccountConnection.objects.filter(
            workspace=workspace, status="active"
        ).first()

        if not connection:
            return Response(
                {"success": False, "error": "연결된 Instagram 계정이 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            graph_api_base = InstagramOAuthService.GRAPH_API_BASE
            access_token = connection.access_token

            # 1. 미디어 상세 정보 조회
            media_url = f"{graph_api_base}/{media_id}"
            media_params = {
                "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count,media_product_type,owner",
                "access_token": access_token,
            }

            media_response = requests.get(media_url, params=media_params)
            media_response.raise_for_status()
            media_data = media_response.json()

            # 2. 댓글 조회
            comments_url = f"{graph_api_base}/{media_id}/comments"
            comments_params = {
                "fields": "id,text,username,timestamp,like_count",
                "limit": 50,
                "access_token": access_token,
            }

            comments_response = requests.get(comments_url, params=comments_params)
            comments_response.raise_for_status()
            comments_data = comments_response.json()

            return Response(
                {
                    "success": True,
                    "media": media_data,
                    "comments": {
                        "data": comments_data.get("data", []),
                        "count": len(comments_data.get("data", [])),
                    },
                    "connection": {
                        "username": connection.username,
                        "account_id": connection.external_account_id,
                    },
                }
            )

        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json() if e.response else str(e)
            return Response(
                {
                    "success": False,
                    "error": "Instagram API 호출 실패",
                    "detail": error_detail,
                    "status_code": e.response.status_code if e.response else None,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception as e:
            return Response(
                {
                    "success": False,
                    "error": "예상치 못한 오류 발생",
                    "detail": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AutoDMCampaignViewSet(viewsets.ModelViewSet):
    """
    Auto DM Campaign 관리 ViewSet
    """

    permission_classes = [IsAuthenticated]
    serializer_class = AutoDMCampaignSerializer

    def get_queryset(self):
        """사용자의 workspace에 속한 캠페인만 조회.

        멀티 IG 계정: ?ig_connection_id=<uuid> 로 특정 IG 계정의 캠페인만 필터.
        다른 사용자의 connection 을 지정해도 user_workspaces 필터 때문에 결과 비어있음.
        """
        user_workspaces = Workspace.objects.filter(memberships__user=self.request.user)

        qs = (
            AutoDMCampaign.objects.filter(ig_connection__workspace__in=user_workspaces)
            .select_related("ig_connection")
            .order_by("-created_at")
        )

        ig_connection_id = self.request.query_params.get("ig_connection_id")
        if ig_connection_id:
            qs = qs.filter(ig_connection_id=ig_connection_id)

        return qs

    @extend_schema(
        summary="캠페인 옵션 가이드 (정적, 인증 불필요)",
        description="""
        ## 목적
        프론트엔드 캠페인 생성/수정 폼의 라디오·토글 옆에 노출할 사용자 안내 문구를
        한 번에 받아간다.

        ## 응답 구조
        ```json
        {
          "version": "v3.4",
          "trigger_types": [
            {"value": "specific_media", "label": "...", "description": "...", "tier": "free"},
            {"value": "any_media", ..., "tier": "pro"},
            {"value": "next_media", ..., "notes": ["...", ...]}
          ],
          "keyword_modes": [...],
          "follow_gate": {"headline": "...", "items": [...]},
          "public_reply": {"headline": "...", "description": "...", "items": [...]}
        }
        ```

        ## 인증
        AllowAny — 정적 가이드라 공개.
        """,
        responses={200: OpenApiResponse(description="캠페인 가이드")},
        tags=["Auto DM"],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="guide",
        permission_classes=[AllowAny],
    )
    def guide(self, request):
        from .campaign_guides import build_campaign_guide

        return Response(build_campaign_guide())

    @extend_schema(
        summary="캠페인 목록 조회",
        description=(
            "사용자의 workspace 에 속한 모든 Auto DM 캠페인을 조회합니다.\n\n"
            "**멀티 IG 계정**: 한 워크스페이스에 여러 IG 계정이 연동된 경우 "
            "`?ig_connection_id=<uuid>` 로 특정 계정의 캠페인만 필터링할 수 있습니다. "
            "권한이 없는 connection ID 를 지정하면 빈 리스트가 반환됩니다."
        ),
        parameters=[
            OpenApiParameter(
                name="ig_connection_id",
                type=str,
                location=OpenApiParameter.QUERY,
                description="특정 IG 계정의 캠페인만 필터링 (UUID)",
                required=False,
            ),
        ],
        responses={200: AutoDMCampaignSerializer(many=True)},
        tags=["Auto DM"],
    )
    def list(self, request):
        """캠페인 목록 조회"""
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        data = serializer.data

        # Enrich missing `media_url` by querying Instagram Graph API using the
        # campaign's `media_id` and its `ig_connection` access token.
        # We keep this best-effort: if the API call fails or the token is
        # unavailable/expired, we silently leave `media_url` as-is.
        campaigns = list(queryset)
        campaign_map = {str(c.id): c for c in campaigns}

        for item in data:
            if not item.get("media_url") and item.get("media_id"):
                campaign = campaign_map.get(item["id"])
                if not campaign:
                    continue
                connection = getattr(campaign, "ig_connection", None)
                if not connection or not connection.access_token:
                    continue
                try:
                    # Skip if token appears expired
                    if not connection.refresh_token_if_needed():
                        continue

                    media_id = item["media_id"]
                    url = f"{InstagramOAuthService.GRAPH_API_BASE}/{media_id}"
                    params = {
                        "fields": "id,media_type,media_url,permalink",
                        "access_token": connection.access_token,
                    }
                    resp = requests.get(url, params=params, timeout=5)
                    resp.raise_for_status()
                    media_data = resp.json()
                    media_url = media_data.get("media_url") or media_data.get("permalink")
                    if media_url:
                        item["media_url"] = media_url
                except Exception:
                    # Best-effort fallback: ignore and continue
                    continue

        return Response(data)

    @extend_schema(
        summary="캠페인 상세 조회",
        description="특정 Auto DM 캠페인의 상세 정보를 조회합니다.",
        responses={200: AutoDMCampaignSerializer},
        tags=["Auto DM"],
    )
    def retrieve(self, request, pk=None):
        """캠페인 상세 조회"""
        campaign = self.get_object()
        serializer = self.get_serializer(campaign)
        return Response(serializer.data)

    @extend_schema(
        summary="캠페인 생성",
        description="""
        ## 기능
        새로운 Auto DM 캠페인을 생성합니다.
        
        ## 사용 방법
        
        ### 1단계: 게시물ID 확인
        먼저 `/api/v1/integrations/instagram/{workspace_id}/media/list/` API로 게시물 목록을 조회하여 `media_id`를 얻습니다.
        
        ### 2단계: 캠페인 생성
        URL: `POST /api/v1/integrations/auto-dm-campaigns/?workspace_id={workspace_id}`
        
        **필수 파라미터:**
        - `workspace_id` (쿼리 파라미터): Workspace UUID
        
        **필수 필드:**
        - `media_id`: Instagram 게시물ID (예: "18418812427189917")
        - `name`: 캠페인 이름 (예: "신규 고객 DM 자동발송")
        - `message_template`: DM 메시지 내용 (예: "댓글 감사합니다! 링크: https://...")
        
        **선택 필드:**
        - `media_url`: 게시물 URL (비워두거나 null 가능)
        - `description`: 캠페인 설명
        - `max_sends_per_hour`: 시간당 최대 발송 수 (기본값: 200, 최대: 500)
        
        ### 예시
        ```json
        {
          "media_id": "18418812427189917",
          "name": "신제품 프로모션 DM",
          "description": "신제품 게시물 댓글 작성자에게 할인 쿠폰 발송",
          "message_template": "댓글 남겨주셔서 감사합니다! 🎁 특별 할인 링크: https://example.com/coupon",
          "max_sends_per_hour": 150
        }
        ```
        
        ## 동작 방식
        1. 캠페인 생성 후 자동으로 `ACTIVE` 상태로 설정
        2. 해당 게시물에 댓글이 달리면 Webhook으로 수신
        3. Celery 태스크가 자동으로 DM 발송 처리
        4. 중복 발송 방지 (같은 댓글에 대해 1회만 발송)
        5. 시간당 발송 제한 적용
        
        ## 주의사항
        - Workspace에 활성화된 Instagram 연결이 있어야 함
        - Meta에서 `instagram_manage_messages` 권한 승인 필요
        - Webhook 설정이 완료되어 있어야 함
        """,
        request=AutoDMCampaignCreateSerializer,
        responses={
            201: OpenApiResponse(response=AutoDMCampaignSerializer, description="캠페인 생성 성공"),
            400: OpenApiResponse(
                description="잘못된 요청 (필수 필드 누락, workspace_id 없음, 유효하지 않은 데이터 등)"
            ),
            403: OpenApiResponse(description="권한 없음 (해당 workspace의 멤버가 아님)"),
            404: OpenApiResponse(description="Workspace를 찾을 수 없음"),
        },
        parameters=[
            OpenApiParameter(
                name="workspace_id",
                description="Workspace UUID (필수)",
                required=True,
                type=str,
                location=OpenApiParameter.QUERY,
            ),
        ],
        tags=["Auto DM"],
    )
    def create(self, request):
        """캠페인 생성"""
        serializer = AutoDMCampaignCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # workspace_id는 쿼리 파라미터에서 받음
        workspace_id = request.query_params.get("workspace_id")
        if not workspace_id:
            return Response(
                {"error": "workspace_id is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        # workspace 확인 및 권한 체크
        try:
            workspace = Workspace.objects.get(id=workspace_id)
            if not workspace.memberships.filter(user=request.user).exists():
                return Response(
                    {"error": "You are not a member of this workspace"},
                    status=status.HTTP_403_FORBIDDEN,
                )
        except Workspace.DoesNotExist:
            return Response({"error": "Workspace not found"}, status=status.HTTP_404_NOT_FOUND)

        # 멀티 IG: body 의 ig_connection_id 가 우선, 미지정 시 첫 활성 connection 사용.
        # validated_data 에 같은 이름의 필드가 남아 있으면 ORM create() 가 충돌하니
        # 먼저 pop 해서 분리한다.
        ig_connection_id = serializer.validated_data.pop("ig_connection_id", None)
        if ig_connection_id:
            try:
                ig_connection = IGAccountConnection.objects.get(id=ig_connection_id)
            except IGAccountConnection.DoesNotExist:
                return Response(
                    {"error": "Instagram connection not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if ig_connection.workspace_id != workspace.id:
                return Response(
                    {"error": "이 IG 계정은 해당 워크스페이스에 속하지 않습니다."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if ig_connection.status != IGAccountConnection.Status.ACTIVE:
                return Response(
                    {"error": "지정한 IG 계정 연동이 활성 상태가 아닙니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            ig_connection = IGAccountConnection.get_active_connection(workspace)
            if not ig_connection:
                return Response(
                    {"error": "No active Instagram connection found for this workspace"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 캠페인 생성
        campaign = AutoDMCampaign.objects.create(
            ig_connection=ig_connection, **serializer.validated_data
        )

        # 시작 시간 기록
        campaign.started_at = timezone.now()
        campaign.save()

        # next_media 트리거: baseline 즉시 스냅샷 (과거 게시물 attach 방지)
        if (
            campaign.trigger_type == AutoDMCampaign.TriggerType.NEXT_MEDIA
            and not ig_connection.last_seen_media_id
        ):
            from .tasks import snapshot_baseline_for_account

            snapshot_baseline_for_account.delay(str(ig_connection.id))

        response_serializer = AutoDMCampaignSerializer(campaign)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="캠페인 수정",
        description="기존 Auto DM 캠페인을 수정합니다.",
        request=AutoDMCampaignUpdateSerializer,
        responses={200: AutoDMCampaignSerializer},
        tags=["Auto DM"],
    )
    def update(self, request, pk=None):
        """캠페인 수정"""
        campaign = self.get_object()
        serializer = self.get_serializer(campaign, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        summary="캠페인 부분 수정",
        description="Auto DM 캠페인의 일부 필드만 수정합니다.",
        request=AutoDMCampaignUpdateSerializer,
        responses={200: AutoDMCampaignSerializer},
        tags=["Auto DM"],
    )
    def partial_update(self, request, pk=None):
        """캠페인 부분 수정"""
        campaign = self.get_object()
        serializer = self.get_serializer(campaign, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        summary="캠페인 삭제",
        description="Auto DM 캠페인을 삭제합니다.",
        responses={204: None},
        tags=["Auto DM"],
    )
    def destroy(self, request, pk=None):
        """캠페인 삭제"""
        campaign = self.get_object()
        campaign.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="캠페인 일시정지",
        description="활성 상태의 캠페인을 일시정지합니다.",
        responses={200: AutoDMCampaignSerializer},
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        """캠페인 일시정지"""
        campaign = self.get_object()
        campaign.status = AutoDMCampaign.Status.PAUSED
        campaign.save()
        serializer = self.get_serializer(campaign)
        return Response(serializer.data)

    @extend_schema(
        summary="캠페인 재개",
        description="일시정지된 캠페인을 다시 활성화합니다.",
        responses={200: AutoDMCampaignSerializer},
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        """캠페인 재개"""
        campaign = self.get_object()
        campaign.status = AutoDMCampaign.Status.ACTIVE
        campaign.save()
        serializer = self.get_serializer(campaign)
        return Response(serializer.data)

    @extend_schema(
        summary="캠페인 발송 로그 조회",
        description=(
            "특정 캠페인의 DM 발송 로그를 조회합니다.\n\n"
            "**v3.8 기본 동작 (Follow-gate)**: 한 댓글 → opening DM → (선택) 재안내/보상 DM 흐름이 "
            "여러 SentDMLog 로 기록되는데, 기본적으로 list 응답에는 **opening / standalone 행만** "
            "노출하여 통계가 부풀려지지 않도록 한다 (= 댓글 1건 = 1 row).\n\n"
            "각 행의 `follow_passed` 필드로 그 흐름의 팔로우 전환 결과를 한눈에 확인 가능.\n\n"
            "**전체 흐름(자식 로그 포함)** 을 보려면 `?include_children=true` 쿼리 파라미터 사용."
        ),
        parameters=[
            OpenApiParameter(
                name="include_children", type=bool, location=OpenApiParameter.QUERY,
                description="true 면 follow-gate 자식 로그(재안내·보상 DM)까지 모두 반환 (디버깅용).",
                required=False,
            ),
        ],
        responses={200: SentDMLogSerializer(many=True)},
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["get"])
    def logs(self, request, pk=None):
        """캠페인의 발송 로그 조회 (기본: opening/standalone 만 = 1 흐름당 1 row)"""
        campaign = self.get_object()
        logs = campaign.dm_logs.all().order_by("-created_at")

        include_children = str(request.query_params.get("include_children", "")).lower() in (
            "1", "true", "yes",
        )
        if not include_children:
            logs = logs.filter(parent_log__isnull=True)

        # 페이지네이션 적용
        page = self.paginate_queryset(logs)
        if page is not None:
            serializer = SentDMLogSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = SentDMLogSerializer(logs, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="캠페인 통계 조회",
        description="캠페인의 발송 통계를 조회합니다.",
        responses={200: OpenApiResponse(description="통계 정보")},
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["get"])
    def stats(self, request, pk=None):
        """캠페인 통계"""
        campaign = self.get_object()

        # 최근 24시간 통계
        # v3.8: child log (재안내/보상 DM) 는 통계 부풀림 방지 차원에서 제외 →
        # opening / standalone 행만 카운트해 "댓글 1건 = 1 row" 와 일치시킨다.
        last_24h = timezone.now() - timedelta(hours=24)
        recent_logs = campaign.dm_logs.filter(
            created_at__gte=last_24h, parent_log__isnull=True
        )

        stats = {
            "total_sent": campaign.total_sent,
            "total_failed": campaign.total_failed,
            "success_rate": (
                campaign.total_sent / (campaign.total_sent + campaign.total_failed) * 100
                if (campaign.total_sent + campaign.total_failed) > 0
                else 0
            ),
            "last_24h": {
                "total": recent_logs.count(),
                "sent": recent_logs.filter(status=SentDMLog.Status.SENT).count(),
                "failed": recent_logs.filter(status=SentDMLog.Status.FAILED).count(),
                "pending": recent_logs.filter(status=SentDMLog.Status.PENDING).count(),
                "skipped": recent_logs.filter(status=SentDMLog.Status.SKIPPED).count(),
            },
            "can_send_more": campaign.can_send_more(),
            "status": campaign.status,
        }

        return Response(stats)


@extend_schema(
    methods=["GET"],
    summary="Instagram Webhook 인증 (GET)",
    description="Meta에서 webhook URL을 검증하기 위한 엔드포인트입니다.",
    parameters=[
        OpenApiParameter(
            name="hub.mode",
            description="Webhook 모드 (subscribe)",
            required=True,
            type=str,
            location=OpenApiParameter.QUERY,
        ),
        OpenApiParameter(
            name="hub.verify_token",
            description="Webhook 검증 토큰",
            required=True,
            type=str,
            location=OpenApiParameter.QUERY,
        ),
        OpenApiParameter(
            name="hub.challenge",
            description="Meta가 제공하는 challenge 값",
            required=True,
            type=str,
            location=OpenApiParameter.QUERY,
        ),
    ],
    responses={
        200: OpenApiResponse(description="Challenge 값 반환"),
        403: OpenApiResponse(description="인증 실패"),
    },
    tags=["Integrations"],
)
def _process_messaging_events(entry: dict, logger) -> None:
    """
    Instagram Webhook entry.messaging[] 처리.

    Instagram Login API에는 별도의 message_echoes 웹훅 필드가 없고,
    `messages` 필드 안에서 우리가 보낸 메시지는 message.is_echo=true 로 들어옴.

    처리 신호:
        - messages + is_echo:true   → SentDMLog ACCEPTED → DELIVERED 승격
        - messages + sender=우리계정 → 마찬가지 (echo의 또 다른 식별자)
        - messaging_seen.read.mid    → SentDMLog DELIVERED → READ 승격
        - messages (사용자→우리)     → 향후 inbound 핸들러용 (현재 로깅만)

    Args:
        entry: webhook payload의 entry 항목 (id=ig_user_id, time, messaging=[...])
        logger: 로거
    """
    page_ig_user_id = str(entry.get("id") or "")
    messaging_events = entry.get("messaging") or []
    if not messaging_events:
        return

    for ev in messaging_events:
        sender_id = str((ev.get("sender") or {}).get("id") or "")
        recipient_id = str((ev.get("recipient") or {}).get("id") or "")
        message = ev.get("message") or {}
        read = ev.get("read") or {}
        postback = ev.get("postback") or {}

        # ----- messaging_seen (read receipt) -----
        if read:
            mid = read.get("mid")
            if mid:
                _mark_log_read_by_mid(mid, logger)
            continue

        # ----- postback (button click) -----
        # Meta 가 일부 케이스에선 별도 postback 이벤트로 보낸다.
        if postback:
            _maybe_dispatch_follow_gate(
                payload=str(postback.get("payload") or ""),
                sender_id=sender_id,
                logger=logger,
            )
            continue

        # ----- messages -----
        if not message:
            continue

        is_echo = bool(message.get("is_echo")) or (
            page_ig_user_id and sender_id == page_ig_user_id
        )
        mid = message.get("mid")

        # Follow-gate quick_reply 응답 (사용자 → 우리). is_echo 제외.
        if not is_echo:
            qr = message.get("quick_reply") or {}
            if _maybe_dispatch_follow_gate(
                payload=str(qr.get("payload") or ""),
                sender_id=sender_id,
                logger=logger,
            ):
                continue

        if is_echo and mid:
            _mark_log_delivered_by_echo(
                mid=mid,
                page_ig_user_id=page_ig_user_id,
                recipient_user_id=recipient_id,
                logger=logger,
            )
        else:
            # 사용자 → 우리 (inbound) 메시지.
            # v3.7: Story 답장이면 process_story_reply_and_send_dm 로 라우팅.
            #       그 외 (일반 inbound DM) 은 로깅만.
            reply_to = message.get("reply_to") or {}
            story_ref = reply_to.get("story") or {}
            story_id = str(story_ref.get("id") or "")

            if story_id:
                from .tasks import process_story_reply_and_send_dm

                process_story_reply_and_send_dm.delay(
                    {
                        "page_ig_user_id": page_ig_user_id,
                        "sender_user_id":  sender_id,
                        "sender_username": "",  # messages webhook 엔 username 없음
                        "story_id":        story_id,
                        "message_mid":     mid or "",
                        "message_text":    message.get("text", "") or "",
                        "entry_time":      ev.get("timestamp"),
                    }
                )
                logger.debug(
                    f"Story reply queued: sender={sender_id}, "
                    f"story={story_id}, mid={mid}"
                )
            else:
                logger.debug(
                    f"Inbound DM received (no handler): sender={sender_id}, mid={mid}"
                )


def _maybe_dispatch_follow_gate(*, payload: str, sender_id: str, logger) -> bool:
    """quick_reply / postback payload 가 follow-gate 인지 검사 후 Celery 라우팅.

    payload 포맷: "fg:{opening_log_id}"

    Returns:
        True  — follow-gate 로 인식하고 큐 등록함
        False — 우리가 처리할 payload 가 아님 (호출부는 평소처럼 진행)
    """
    if not payload or not payload.startswith("fg:"):
        return False
    if not sender_id:
        logger.warning("follow-gate postback received but sender_id missing")
        return True  # 우리 payload 였지만 라우팅 불가 — 평소 흐름 막진 않음

    opening_log_id = payload.split(":", 1)[1].strip()
    if not opening_log_id:
        return True

    from .tasks import process_follow_gate_postback

    process_follow_gate_postback.delay(opening_log_id, sender_id)
    logger.info(
        "follow-gate postback queued: opening_log=%s igsid=%s",
        opening_log_id, sender_id,
    )
    return True


def _mark_log_delivered_by_echo(
    *, mid: str, page_ig_user_id: str, recipient_user_id: str, logger
) -> None:
    """is_echo:true 이벤트의 mid 와 SentDMLog.meta_message_id 매칭 → DELIVERED"""
    qs = SentDMLog.objects.filter(meta_message_id=mid).select_related(
        "campaign__ig_connection"
    )
    if not qs.exists() and recipient_user_id:
        # 일부 케이스에서 mid가 echo 단계에서 다르게 발급될 수 있어
        # recipient + 최근 ACCEPTED 건으로 fallback 매칭
        qs = SentDMLog.objects.filter(
            recipient_user_id=recipient_user_id,
            status=SentDMLog.Status.ACCEPTED,
            campaign__ig_connection__external_account_id=page_ig_user_id,
        ).order_by("-accepted_at")[:1]

    matched = 0
    for log in qs:
        if log.status == SentDMLog.Status.READ:
            continue
        log.append_verification_log({"path": "echo", "result": "matched", "mid": mid})
        log.mark_delivered(via=SentDMLog.VerifiedVia.ECHO, mid=mid)
        matched += 1

    if matched == 0:
        logger.debug(f"Echo received but no matching SentDMLog (mid={mid})")
    else:
        logger.info(f"Echo matched {matched} SentDMLog(s) (mid={mid}) → DELIVERED")


def _mark_log_read_by_mid(mid: str, logger) -> None:
    """messaging_seen.read.mid → SentDMLog READ 승격"""
    logs = SentDMLog.objects.filter(meta_message_id=mid)
    if not logs.exists():
        logs = SentDMLog.objects.filter(echo_mid=mid)

    matched = 0
    for log in logs:
        log.mark_read()
        matched += 1

    if matched:
        logger.info(f"messaging_seen matched {matched} SentDMLog(s) (mid={mid}) → READ")


@extend_schema(
    methods=["POST"],
    summary="Instagram Webhook 이벤트 수신 (POST)",
    description="Instagram에서 발생한 이벤트(댓글, 멘션, 메시지 등)를 수신합니다.",
    responses={
        200: OpenApiResponse(description="이벤트 수신 완료"),
        500: OpenApiResponse(description="처리 중 오류 발생"),
    },
    tags=["Integrations"],
)
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def instagram_webhook(request):
    """
    Instagram Webhook 엔드포인트

    GET: Webhook 인증
    POST: Webhook 이벤트 수신
    """
    if request.method == "GET":
        # Webhook 인증
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")

        # 환경변수에서 설정된 verify token과 비교
        verify_token = settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN

        if mode == "subscribe" and token == verify_token:
            # 인증 성공 - challenge 값을 그대로 반환
            return HttpResponse(challenge, content_type="text/plain")
        else:
            # 인증 실패
            return HttpResponse("Forbidden", status=403)

    elif request.method == "POST":
        # Webhook 이벤트 수신
        import logging
        import json

        logger = logging.getLogger(__name__)

        try:
            # 받은 데이터 파싱
            payload = json.loads(request.body)
            logger.debug(f"Instagram webhook received: {payload}")

            # Meta webhook 구조: {"object": "instagram", "entry": [...]}
            if payload.get("object") != "instagram":
                logger.warning(f"Unknown webhook object type: {payload.get('object')}")
                return HttpResponse("EVENT_RECEIVED", status=200)

            # entry 배열 처리
            entries = payload.get("entry", [])

            for entry in entries:
                # entry 안의 changes 배열 처리
                changes = entry.get("changes", [])

                for change in changes:
                    field = change.get("field")
                    value = change.get("value", {})

                    logger.debug(f"Processing webhook field: {field}")

                    # 댓글 이벤트 처리
                    if field == "comments":
                        # Celery 태스크 비동기 실행
                        from .tasks import process_comment_and_send_dm

                        webhook_data = {
                            "field": field,
                            "value": value,
                            "entry_id": entry.get("id"),
                            "time": entry.get("time"),
                        }

                        # 비동기 태스크 실행
                        process_comment_and_send_dm.delay(webhook_data)
                        logger.debug(f"Queued DM task for comment: {value.get('id')}")

                    elif field in ["mentions", "messaging_postbacks"]:
                        logger.debug(f"Received {field} event, but not processing yet")

                # ★ messages / messaging_seen 처리:
                # Instagram Login API에서는 별도 message_echoes 필드가 없고
                # `messages` 필드 안에 is_echo:true 로 우리가 보낸 메시지가 함께 옴.
                _process_messaging_events(entry, logger)

            return HttpResponse("EVENT_RECEIVED", status=200)

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in webhook: {str(e)}")
            return HttpResponse("Invalid JSON", status=400)
        except Exception as e:
            logger.exception(f"Error processing webhook: {str(e)}")
            return HttpResponse("Error", status=500)


class SpamFilterViewSet(viewsets.ViewSet):
    """
    스팸 필터 관리 ViewSet
    """

    permission_classes = [IsAuthenticated]

    def get_spam_filter(self, ig_connection_id):
        """스팸 필터 설정 가져오기 (없으면 생성)"""
        from rest_framework.exceptions import NotFound

        try:
            ig_connection = IGAccountConnection.objects.get(id=ig_connection_id)
        except IGAccountConnection.DoesNotExist:
            raise NotFound(
                detail="Instagram 계정을 찾을 수 없습니다. 올바른 ig_connection_id를 사용하세요."
            )

        # 워크스페이스 멤버십 확인
        if not ig_connection.workspace.memberships.filter(user=self.request.user).exists():
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You are not a member of this workspace")

        # 스팸 필터 설정 가져오기 또는 생성
        spam_filter, created = SpamFilterConfig.objects.get_or_create(
            ig_connection=ig_connection,
            defaults={
                "spam_keywords": ["아이돌", "주소창", "사건", "원본영상", "실시간검색"],
                "block_urls": True,
            },
        )

        return spam_filter

    @extend_schema(
        summary="스팸 필터 설정 조회",
        description="""
        ## 목적
        Instagram Business 계정에 연결된 스팸 필터 설정을 조회합니다.
        설정이 없는 경우 자동으로 기본 설정을 생성하여 반환합니다.
        
        ## 사용 시나리오
        - 스팸 필터 관리 페이지 진입 시 현재 설정 로드
        - 스팸 필터 활성화 상태 확인
        - 현재 설정된 스팸 키워드 목록 확인
        
        ## 인증
        - **Bearer 토큰 필수**
        - 해당 Instagram 계정이 속한 워크스페이스의 멤버여야 함
        
        ## 기본 설정 (자동 생성 시)
        - `status`: inactive (비활성)
        - `spam_keywords`: ["아이돌", "주소창", "사건", "원본영상", "실시간검색"]
        - `block_urls`: true
        
        ## 응답 필드
        - `is_active`: 현재 활성화 여부 (boolean)
        - `total_spam_detected`: 총 스팸 감지 수
        - `total_hidden`: 총 숨김 처리 수
        
        ## 사용 예시
        ```javascript
        // Instagram 계정의 스팸 필터 설정 조회
        const response = await fetch(
            `/api/v1/integrations/spam-filters/ig-connections/${igConnectionId}/`,
            {
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        const config = await response.json();
        console.log('활성화 상태:', config.is_active);
        console.log('스팸 키워드:', config.spam_keywords);
        ```
        
        ## 응답 예시
        ```json
        {
            "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "ig_connection_id": "f1e2d3c4-b5a6-7890-dcba-fe0987654321",
            "ig_username": "my_business_account",
            "status": "active",
            "spam_keywords": ["아이돌", "주소창", "사건", "원본영상"],
            "block_urls": true,
            "total_spam_detected": 127,
            "total_hidden": 122,
            "is_active": true,
            "created_at": "2026-02-15T10:30:00Z",
            "updated_at": "2026-02-18T02:00:00Z"
        }
        ```
        """,
        responses={
            200: SpamFilterConfigSerializer,
            401: OpenApiResponse(description="인증 실패 - Bearer 토큰이 없거나 유효하지 않음"),
            403: OpenApiResponse(description="권한 없음 - 해당 워크스페이스의 멤버가 아님"),
            404: OpenApiResponse(description="Instagram 계정을 찾을 수 없음"),
        },
    )
    @action(detail=False, methods=["get"], url_path="ig-connections/(?P<ig_connection_id>[^/.]+)")
    def get_config(self, request, ig_connection_id=None):
        """스팸 필터 설정 조회"""
        spam_filter = self.get_spam_filter(ig_connection_id)
        serializer = SpamFilterConfigSerializer(spam_filter)
        return Response(serializer.data)
    @extend_schema(
        summary="스팸 필터 설정 조회 및 업데이트",
        description="GET으로 조회하고 PATCH로 부분 업데이트합니다.",
        request=SpamFilterConfigUpdateSerializer,
        responses={
            200: SpamFilterConfigSerializer,
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="Instagram 계정을 찾을 수 없음"),
        },
    )
    @action(detail=False, methods=["get", "patch"], url_path="ig-connections/(?P<ig_connection_id>[^/.]+)")
    def config(self, request, ig_connection_id=None):
        """스팸 필터 설정 조회/업데이트"""
        spam_filter = self.get_spam_filter(ig_connection_id)

        if request.method == "GET":
            serializer = SpamFilterConfigSerializer(spam_filter)
            return Response(serializer.data)

        # PATCH
        serializer = SpamFilterConfigUpdateSerializer(spam_filter, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(SpamFilterConfigSerializer(spam_filter).data)

    @extend_schema(
        summary="스팸 필터 활성화",
        description="""
        ## 목적
        Instagram Business 계정의 스팸 필터를 즉시 활성화합니다.
        활성화 후 수신되는 모든 댓글에 대해 스팸 검사가 자동으로 수행됩니다.
        
        ## 사용 시나리오
        - 스팸 필터 토글 버튼을 ON으로 전환할 때
        - 스팸 댓글이 갑자기 많아져서 긴급하게 필터를 켜야 할 때
        - 설정 완료 후 필터 작동 시작
        
        ## 인증
        - **Bearer 토큰 필수**
        - 해당 Instagram 계정이 속한 워크스페이스의 멤버여야 함
        
        ## 동작 방식
        1. 스팸 필터 상태를 "active"로 변경
        2. 이후 수신되는 댓글부터 스팸 검사 시작
        3. 스팸으로 판정된 댓글은 자동으로 숨김 처리
        4. 정상 댓글만 DM 자동발송 대상이 됨
        
        ## 주의사항
        - 이미 수신된 댓글에는 소급 적용되지 않음
        - 스팸 키워드가 설정되어 있어야 정상 작동
        - 활성화 즉시 웹훅으로 들어오는 댓글부터 필터링됨
        
        ## 사용 예시
        ```javascript
        // 스팸 필터 활성화
        const response = await fetch(
            `/api/v1/integrations/spam-filters/ig-connections/${igConnectionId}/activate/`,
            {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        const result = await response.json();
        if (result.is_active) {
            console.log('스팸 필터가 활성화되었습니다.');
        }
        ```
        
        ## 응답 예시
        ```json
        {
            "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "ig_connection_id": "f1e2d3c4-b5a6-7890-dcba-fe0987654321",
            "ig_username": "my_business_account",
            "status": "active",
            "is_active": true,
            "spam_keywords": ["아이돌", "주소창"],
            "block_urls": true,
            "total_spam_detected": 127,
            "total_hidden": 122,
            "updated_at": "2026-02-18T02:30:00Z"
        }
        ```
        """,
        responses={
            200: SpamFilterConfigSerializer,
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="Instagram 계정을 찾을 수 없음"),
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="ig-connections/(?P<ig_connection_id>[^/.]+)/activate",
    )
    def activate(self, request, ig_connection_id=None):
        """스팸 필터 활성화"""
        spam_filter = self.get_spam_filter(ig_connection_id)
        spam_filter.status = SpamFilterConfig.Status.ACTIVE
        spam_filter.save()

        serializer = SpamFilterConfigSerializer(spam_filter)
        return Response(serializer.data)

    @extend_schema(
        summary="스팸 필터 비활성화",
        description="""
        ## 목적
        Instagram Business 계정의 스팸 필터를 즉시 비활성화합니다.
        비활성화 후에는 스팸 검사가 수행되지 않으며, 모든 댓글이 정상 처리됩니다.
        
        ## 사용 시나리오
        - 스팸 필터 토글 버튼을 OFF로 전환할 때
        - 스팸 필터가 정상 댓글을 너무 많이 차단할 때
        - 일시적으로 필터링을 중단하고 싶을 때
        - 테스트 또는 디버깅 목적
        
        ## 인증
        - **Bearer 토큰 필수**
        - 해당 Instagram 계정이 속한 워크스페이스의 멤버여야 함
        
        ## 동작 방식
        1. 스팸 필터 상태를 "inactive"로 변경
        2. 이후 수신되는 댓글에 대해 스팸 검사 미수행
        3. 모든 댓글이 정상 댓글로 처리됨
        4. DM 자동발송 캠페인이 설정되어 있으면 모든 댓글에 DM 발송
        
        ## 주의사항
        - 비활성화해도 기존 스팸 로그는 유지됨
        - 스팸 키워드 설정도 그대로 보존됨
        - 언제든지 다시 활성화 가능
        - 통계 데이터는 초기화되지 않음
        
        ## 사용 예시
        ```javascript
        // 스팸 필터 비활성화
        const response = await fetch(
            `/api/v1/integrations/spam-filters/ig-connections/${igConnectionId}/deactivate/`,
            {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        const result = await response.json();
        if (!result.is_active) {
            console.log('스팸 필터가 비활성화되었습니다.');
        }
        ```
        
        ## 응답 예시
        ```json
        {
            "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "ig_connection_id": "f1e2d3c4-b5a6-7890-dcba-fe0987654321",
            "ig_username": "my_business_account",
            "status": "inactive",
            "is_active": false,
            "spam_keywords": ["아이돌", "주소창"],
            "block_urls": true,
            "total_spam_detected": 127,
            "total_hidden": 122,
            "updated_at": "2026-02-18T02:35:00Z"
        }
        ```
        """,
        responses={
            200: SpamFilterConfigSerializer,
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="Instagram 계정을 찾을 수 없음"),
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="ig-connections/(?P<ig_connection_id>[^/.]+)/deactivate",
    )
    def deactivate(self, request, ig_connection_id=None):
        """스팸 필터 비활성화"""
        spam_filter = self.get_spam_filter(ig_connection_id)
        spam_filter.status = SpamFilterConfig.Status.INACTIVE
        spam_filter.save()

        serializer = SpamFilterConfigSerializer(spam_filter)
        return Response(serializer.data)

    @extend_schema(
        summary="스팸 댓글 로그 조회",
        description="""
        ## 목적
        스팸으로 감지된 댓글들의 상세 로그를 조회합니다.
        댓글 내용, 작성자, 스팸 판정 이유, 처리 상태 등을 확인할 수 있습니다.
        
        ## 사용 시나리오
        - 스팸 필터 성능 모니터링
        - 잘못 차단된 댓글(오탐) 확인
        - 특정 사용자의 스팸 댓글 이력 조회
        - 스팸 패턴 분석을 위한 데이터 수집
        
        ## 인증
        - **Bearer 토큰 필수**
        - 해당 Instagram 계정이 속한 워크스페이스의 멤버여야 함
        
        ## Query Parameters
        - `status`: 로그 상태 필터 (선택)
          - `detected`: 스팸으로 감지됨
          - `hidden`: 숨김 처리 완료
          - `failed`: 숨김 처리 실패
        - `limit`: 반환할 최대 개수 (선택, 기본: 50, 최대: 500)
        
        ## 응답 필드
        - `spam_reasons`: 스팸으로 판정된 이유 배열
          - `contains_url`: URL 포함
          - `keyword:xxx`: 특정 키워드 매칭
        - `status`: 처리 상태
        - `hidden_at`: 숨김 처리된 시각 (null일 수 있음)
        
        ## 정렬
        - 최신 순으로 정렬됨 (created_at DESC)
        
        ## 주의사항
        - 대량 조회 시 성능을 위해 limit 설정 권장
        - 웹훅 원본 데이터도 포함되므로 민감 정보 주의
        
        ## 사용 예시
        ```javascript
        // 최근 숨김 처리된 스팸 댓글 20개 조회
        const response = await fetch(
            `/api/v1/integrations/spam-filters/ig-connections/${igConnectionId}/logs/?status=hidden&limit=20`,
            {
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        const logs = await response.json();
        logs.forEach(log => {
            console.log(`${log.commenter_username}: ${log.comment_text}`);
            console.log(`판정 이유: ${log.spam_reasons.join(', ')}`);
        });
        ```
        
        ## 응답 예시
        ```json
        [
            {
                "id": "log-uuid-1",
                "spam_filter_id": "filter-uuid",
                "ig_username": "my_business_account",
                "comment_id": "comment-123",
                "comment_text": "주소창 yako.asia 아이돌A양 사건",
                "commenter_user_id": "user-456",
                "commenter_username": "spam_user_123",
                "media_id": "media-789",
                "spam_reasons": [
                    "contains_url",
                    "keyword:주소창",
                    "keyword:아이돌",
                    "keyword:사건"
                ],
                "status": "hidden",
                "error_message": "",
                "created_at": "2026-02-18T02:15:00Z",
                "hidden_at": "2026-02-18T02:15:02Z"
            }
        ]
        ```
        """,
        parameters=[
            OpenApiParameter(
                name="status",
                type=str,
                required=False,
                description="로그 상태 필터 (detected/hidden/failed)",
                enum=["detected", "hidden", "failed"],
            ),
            OpenApiParameter(
                name="limit",
                type=int,
                required=False,
                description="반환할 최대 개수 (기본: 50, 최대: 500)",
            ),
        ],
        responses={
            200: SpamCommentLogSerializer(many=True),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="Instagram 계정을 찾을 수 없음"),
        },
    )
    @action(
        detail=False, methods=["get"], url_path="ig-connections/(?P<ig_connection_id>[^/.]+)/logs"
    )
    def get_logs(self, request, ig_connection_id=None):
        """스팸 댓글 로그 조회"""
        spam_filter = self.get_spam_filter(ig_connection_id)

        logs = SpamCommentLog.objects.filter(spam_filter=spam_filter)

        # 상태 필터
        status_param = request.query_params.get("status")
        if status_param:
            logs = logs.filter(status=status_param)

        # 개수 제한
        limit = int(request.query_params.get("limit", 50))
        logs = logs[:limit]

        serializer = SpamCommentLogSerializer(logs, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="스팸 필터 통계 조회",
        description="""
        ## 목적
        스팸 필터의 성능 지표와 통계 데이터를 조회합니다.
        대시보드나 리포트 화면에서 스팸 필터 효과를 시각화할 때 사용합니다.
        
        ## 사용 시나리오
        - 스팸 필터 대시보드 화면 로드
        - 필터 성능 모니터링
        - 일별 스팸 추이 그래프 데이터 수집
        - 필터 효과성 평가 (성공률 확인)
        
        ## 인증
        - **Bearer 토큰 필수**
        - 해당 Instagram 계정이 속한 워크스페이스의 멤버여야 함
        
        ## 응답 필드
        - `total_spam_detected`: 총 스팸 감지 수 (누적)
        - `total_hidden`: 총 숨김 처리 수 (누적)
        - `success_rate`: 숨김 처리 성공률 (% 단위, 소수점 2자리)
        - `recent_spam`: 최근 7일간 일별 스팸 감지 수
          - `date`: 날짜 (YYYY-MM-DD)
          - `count`: 해당 날짜의 스팸 감지 수
        
        ## 성공률 계산식
        ```
        success_rate = (total_hidden / total_spam_detected) * 100
        ```
        
        ## 주의사항
        - 통계는 실시간으로 업데이트됨
        - `recent_spam`은 최근 7일간 데이터만 포함
        - 데이터가 없는 날짜는 배열에 포함되지 않음
        
        ## 사용 예시
        ```javascript
        // 스팸 필터 통계 조회 및 차트 렌더링
        const response = await fetch(
            `/api/v1/integrations/spam-filters/ig-connections/${igConnectionId}/stats/`,
            {
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        const stats = await response.json();
        
        // 성공률 표시
        console.log(`스팸 차단 성공률: ${stats.success_rate}%`);
        
        // 일별 스팸 추이 차트 데이터
        const chartData = stats.recent_spam.map(item => ({
            x: new Date(item.date),
            y: item.count
        }));
        
        renderChart(chartData);
        ```
        
        ## 응답 예시
        ```json
        {
            "total_spam_detected": 427,
            "total_hidden": 413,
            "success_rate": 96.72,
            "recent_spam": [
                {
                    "date": "2026-02-12",
                    "count": 12
                },
                {
                    "date": "2026-02-13",
                    "count": 28
                },
                {
                    "date": "2026-02-14",
                    "count": 45
                },
                {
                    "date": "2026-02-15",
                    "count": 67
                },
                {
                    "date": "2026-02-16",
                    "count": 89
                },
                {
                    "date": "2026-02-17",
                    "count": 103
                },
                {
                    "date": "2026-02-18",
                    "count": 83
                }
            ]
        }
        ```
        
        ## 데이터 해석
        - **성공률 90% 이상**: 필터가 잘 작동하고 있음
        - **성공률 80% 미만**: API 오류 또는 네트워크 문제 확인 필요
        - **일별 추이 급증**: 스팸 공격 가능성, 키워드 업데이트 고려
        """,
        responses={
            200: OpenApiResponse(
                description="스팸 필터 통계",
                examples=[
                    OpenApiExample(
                        "Success Example",
                        value={
                            "total_spam_detected": 427,
                            "total_hidden": 413,
                            "success_rate": 96.72,
                            "recent_spam": [{"date": "2026-02-18", "count": 83}],
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="Instagram 계정을 찾을 수 없음"),
        },
    )
    @action(
        detail=False, methods=["get"], url_path="ig-connections/(?P<ig_connection_id>[^/.]+)/stats"
    )
    def get_stats(self, request, ig_connection_id=None):
        """스팸 필터 통계 조회"""
        spam_filter = self.get_spam_filter(ig_connection_id)

        # 최근 7일간 스팸 통계
        from django.db.models import Count
        from django.db.models.functions import TruncDate

        seven_days_ago = timezone.now() - timedelta(days=7)
        recent_spam = (
            SpamCommentLog.objects.filter(spam_filter=spam_filter, created_at__gte=seven_days_ago)
            .annotate(date=TruncDate("created_at"))
            .values("date")
            .annotate(count=Count("id"))
            .order_by("date")
        )

        # 성공률 계산
        success_rate = 0
        if spam_filter.total_spam_detected > 0:
            success_rate = (spam_filter.total_hidden / spam_filter.total_spam_detected) * 100

        return Response(
            {
                "total_spam_detected": spam_filter.total_spam_detected,
                "total_hidden": spam_filter.total_hidden,
                "success_rate": round(success_rate, 2),
                "recent_spam": [
                    {"date": item["date"].strftime("%Y-%m-%d"), "count": item["count"]}
                    for item in recent_spam
                ],
            }
        )
