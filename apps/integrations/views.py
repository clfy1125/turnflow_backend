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
                        "username": connection.username,
                        "account_id": connection.external_account_id,
                    },
                    "query": {
                        "limit": limit,
                        "after": after,
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
        """사용자의 workspace에 속한 캠페인만 조회"""
        # 사용자가 속한 workspace들의 Instagram 연결 조회
        user_workspaces = Workspace.objects.filter(memberships__user=self.request.user)

        return (
            AutoDMCampaign.objects.filter(ig_connection__workspace__in=user_workspaces)
            .select_related("ig_connection")
            .order_by("-created_at")
        )

    @extend_schema(
        summary="캠페인 목록 조회",
        description="사용자의 workspace에 속한 모든 Auto DM 캠페인을 조회합니다.",
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

        # 활성화된 Instagram 연결 확인
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
        description="특정 캠페인의 DM 발송 로그를 조회합니다.",
        responses={200: SentDMLogSerializer(many=True)},
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["get"])
    def logs(self, request, pk=None):
        """캠페인의 발송 로그 조회"""
        campaign = self.get_object()
        logs = campaign.dm_logs.all().order_by("-created_at")

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
        last_24h = timezone.now() - timedelta(hours=24)
        recent_logs = campaign.dm_logs.filter(created_at__gte=last_24h)

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

                    # 다른 이벤트 타입도 필요시 처리
                    elif field in ["mentions", "messages", "messaging_postbacks"]:
                        logger.debug(f"Received {field} event, but not processing yet")

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
