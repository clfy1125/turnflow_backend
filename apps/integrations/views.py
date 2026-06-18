"""
Instagram integration views
"""

import logging
import secrets
from datetime import timedelta

import requests
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.filters import SearchFilter
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

# AI 폼-작성 도움(게시물→캠페인 초안) 요청/작업 시리얼라이저. @extend_schema 가 클래스 정의 시점에
# 시리얼라이저 객체를 요구하므로 최상단 import (ai_jobs→integrations 의존이 없어 순환 없음).
from apps.ai_jobs.serializers import (
    AutoDMCampaignAiSuggestJobSerializer,
    AutoDMCampaignAiSuggestRequestSerializer,
)
from apps.workspace.models import Workspace

from .campaign_stats import (
    annotate_campaign_stats,
    build_counts,
    build_delivery_summary,
    compute_monthly_usage,
)
from .models import (
    AutoDMCampaign,
    EventInbox,
    IGAccountConnection,
    IGOAuthState,
    SentDMLog,
    SpamCommentLog,
    SpamFilterConfig,
)
from .serializers import (
    AutoDMCampaignCopySerializer,
    AutoDMCampaignCreateSerializer,
    AutoDMCampaignListSerializer,
    AutoDMCampaignScheduleSerializer,
    AutoDMCampaignSerializer,
    AutoDMCampaignSummarySerializer,
    AutoDMCampaignUpdateSerializer,
    CampaignBulkActionRequestSerializer,
    CampaignBulkActionResponseSerializer,
    ConnectionCallbackResponseSerializer,
    ConnectionStartResponseSerializer,
    DisconnectResponseSerializer,
    IGAccountConnectionSerializer,
    SentDMLogSerializer,
    SpamCommentLogSerializer,
    SpamFilterConfigSerializer,
    SpamFilterConfigUpdateSerializer,
)
from .services import InstagramOAuthService, MockInstagramProvider

logger = logging.getLogger(__name__)


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
            return IGAccountConnection.objects.filter(workspace=workspace, status="active").first()

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

                # 진단용: 발급된 권한/사용자 식별자만 남긴다 (토큰·시크릿은 절대 로깅 금지).
                # graph.instagram.com 거부 원인(권한 미부여 vs 계정타입/역할) 구분에 사용.
                logger.info(
                    "IG short-lived token OK: user_id=%s permissions=%r",
                    ig_user_id,
                    token_response.get("permissions"),
                )

                # 2. Get long-lived token (60 days)
                long_lived_response = InstagramOAuthService.get_long_lived_token(short_lived_token)
                access_token = long_lived_response["access_token"]

                # 3. Get Instagram account info directly (no Facebook Pages needed)
                try:
                    account_info = InstagramOAuthService.get_account_info(access_token)
                except Exception as e:
                    logger.error(f"Exception during get_account_info: {str(e)}")

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
                        <h2 class="error">❌ Instagram API 오류</h2>
                        <p>Instagram API 호출 중 오류가 발생했습니다.</p>
                        <p>창이 자동으로 닫힙니다...</p>
                        <script>
                            if (window.opener) {
                                window.opener.postMessage({
                                    type: 'INSTAGRAM_ERROR',
                                    success: false,
                                    errorCode: 'INSTAGRAM_API_ERROR',
                                    message: 'Instagram API 호출 중 오류가 발생했습니다.'
                                }, '*');
                                setTimeout(() => window.close(), 2000);
                            }
                        </script>
                    </body>
                    </html>
                    """
                    return HttpResponse(html)

                # Use user_id from token response or account_info
                instagram_account_id = (
                    account_info.get("user_id") or ig_user_id or account_info.get("id", "")
                )

                account_info["id"] = instagram_account_id

                # Calculate expiration time
                expires_in = long_lived_response.get("expires_in", 5184000)  # Default 60 days
                expires_at = timezone.now() + timedelta(seconds=expires_in)

                # 재연동 = 완전 교체 (멱등):
                # 같은 (workspace, 계정) 의 기존 연동이 있으면 — revoked/error/expired 포함 —
                # 새 행을 만들지 않고 그 행을 그대로 재활성화하고 토큰/메타데이터를 덮어쓴다.
                # (workspace, external_account_id) 에 유니크 제약이 없어 get_or_create 는
                # 과거 중복 행에서 MultipleObjectsReturned 로 터질 수 있으므로 filter().first() 로
                # 안전하게 가장 최근 행을 잡고, 없으면 새로 만든다.
                connection = (
                    IGAccountConnection.objects.filter(
                        workspace=workspace,
                        external_account_id=account_info["id"],
                    )
                    .order_by("-created_at")
                    .first()
                )
                if connection is None:
                    connection = IGAccountConnection(
                        workspace=workspace,
                        external_account_id=account_info["id"],
                    )

                # 기존/신규 공통: 모든 필드를 최신 값으로 덮어써 재연동이 곧 교체가 되게 한다.
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
                    logger.debug(
                        f"Webhook subscription result for {instagram_account_id}: {subscribe_result}"
                    )
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
            # Meta/Instagram HTTPError 는 응답 본문에 실패 사유(JSON)가 들어있다.
            # raise_for_status() 가 본문을 버리므로 여기서 명시적으로 남겨 진단 가능하게 한다.
            meta_body = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    meta_body = f" | status={resp.status_code} body={resp.text[:1000]}"
                except Exception:
                    pass
            logger.error(
                f"Fatal error in connect_callback: {type(e).__name__} - {str(e)}{meta_body}"
            )

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

        signed_request = request.data.get("signed_request") or request.POST.get("signed_request")
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

    # 검색(?search=): 캠페인 이름/설명 + 연동 IG username (DRF SearchFilter, list 에서만 적용).
    filter_backends = [SearchFilter]
    search_fields = ["name", "description", "ig_connection__username"]

    # 목록(list) 정렬 허용 필드 화이트리스트 (임의 컬럼 정렬·인젝션 방지).
    # ordering 쿼리 파라미터는 '-' 접두사(내림차순)와 콤마 다중 지정을 지원한다.
    LIST_ORDERING_FIELDS = (
        "created_at",
        "updated_at",
        "name",
        "status",
        "total_sent",
        "total_failed",
        "started_at",
        "scheduled_start_at",
        "scheduled_end_at",
        "last_sent_at",  # annotate 기반 — 가장 최근 발송 시각
    )
    # 노출 정렬명 → 실제 정렬 컬럼(annotate 별칭)
    LIST_ORDERING_ALIASES = {"last_sent_at": "_last_sent_at"}
    DEFAULT_LIST_ORDERING = "-created_at"

    def get_serializer_class(self):
        # 목록/토글 응답은 per-item 통계 enrichment 포함 serializer 사용.
        if self.action in ("list", "pause", "resume"):
            return AutoDMCampaignListSerializer
        return AutoDMCampaignSerializer

    def get_queryset(self):
        """사용자의 workspace에 속한 캠페인만 조회.

        멀티 IG 계정: ?ig_connection_id=<uuid> 로 특정 IG 계정의 캠페인만 필터.
        다른 사용자의 connection 을 지정해도 user_workspaces 필터 때문에 결과 비어있음.

        목록(list) 조회에 한해 통계 annotate + status/생성일/facet 필터 + ordering 정렬을
        추가로 적용한다. 상세 조회·커스텀 액션(get_object)에는 영향을 주지 않는다(-created_at).
        """
        user_workspaces = Workspace.objects.filter(memberships__user=self.request.user)

        qs = AutoDMCampaign.objects.filter(
            ig_connection__workspace__in=user_workspaces
        ).select_related("ig_connection")

        ig_connection_id = self.request.query_params.get("ig_connection_id")
        if ig_connection_id:
            qs = qs.filter(ig_connection_id=ig_connection_id)

        if self.action == "list":
            qs = annotate_campaign_stats(qs)
            return self._filter_and_order_list(qs)
        return qs.order_by(self.DEFAULT_LIST_ORDERING)

    def _filter_and_order_list(self, qs):
        """목록 전용 status/생성일/facet 필터 + ordering 정렬 적용.

        잘못된 입력(허용되지 않은 status·trigger_type·정렬 필드, 날짜/불리언 형식 오류)은
        400 으로 거부한다.
        """
        params = self.request.query_params

        # status 필터 (콤마로 다중 지정 가능: ?status=active,paused)
        status_param = params.get("status")
        if status_param:
            valid = set(AutoDMCampaign.Status.values)
            requested = [s.strip() for s in status_param.split(",") if s.strip()]
            invalid = [s for s in requested if s not in valid]
            if invalid:
                raise DRFValidationError(
                    {"status": f"허용되지 않은 status 값: {invalid}. 가능: {sorted(valid)}"}
                )
            if requested:
                qs = qs.filter(status__in=requested)

        # facet: trigger_type (콤마 다중)
        trigger_param = params.get("trigger_type")
        if trigger_param:
            valid_tt = set(AutoDMCampaign.TriggerType.values)
            requested_tt = [t.strip() for t in trigger_param.split(",") if t.strip()]
            invalid_tt = [t for t in requested_tt if t not in valid_tt]
            if invalid_tt:
                raise DRFValidationError(
                    {
                        "trigger_type": f"허용되지 않은 trigger_type: {invalid_tt}. 가능: {sorted(valid_tt)}"
                    }
                )
            if requested_tt:
                qs = qs.filter(trigger_type__in=requested_tt)

        # facet: 불리언 토글 (follow_gate_enabled / public_reply_enabled)
        for bool_field in ("follow_gate_enabled", "public_reply_enabled"):
            raw = params.get(bool_field)
            if raw is not None and raw != "":
                qs = qs.filter(**{bool_field: self._parse_bool(raw, bool_field)})

        # 생성일 범위 필터 (created_after / created_before, 둘 다 경계 포함)
        created_after = params.get("created_after")
        if created_after:
            kind, value = self._parse_date_param(created_after, "created_after")
            lookup = "created_at__date__gte" if kind == "date" else "created_at__gte"
            qs = qs.filter(**{lookup: value})

        created_before = params.get("created_before")
        if created_before:
            kind, value = self._parse_date_param(created_before, "created_before")
            lookup = "created_at__date__lte" if kind == "date" else "created_at__lte"
            qs = qs.filter(**{lookup: value})

        # ordering 정렬 (콤마 다중, '-' 접두사 내림차순)
        ordering_param = params.get("ordering")
        if ordering_param:
            cleaned = []
            for raw in ordering_param.split(","):
                field = raw.strip()
                if not field:
                    continue
                desc = field.startswith("-")
                bare = field[1:] if desc else field
                if bare not in self.LIST_ORDERING_FIELDS:
                    raise DRFValidationError(
                        {
                            "ordering": (
                                f"허용되지 않은 정렬 필드: {bare!r}. "
                                f"가능: {list(self.LIST_ORDERING_FIELDS)}"
                            )
                        }
                    )
                col = self.LIST_ORDERING_ALIASES.get(bare, bare)
                if col == "_last_sent_at":
                    # 미발송(null) 은 항상 뒤로 — '최근 발송순'에서 빈 캠페인이 위로 뜨지 않게.
                    cleaned.append(
                        F(col).desc(nulls_last=True) if desc else F(col).asc(nulls_last=True)
                    )
                else:
                    cleaned.append(f"-{col}" if desc else col)
            if cleaned:
                return qs.order_by(*cleaned)

        return qs.order_by(self.DEFAULT_LIST_ORDERING)

    @staticmethod
    def _parse_bool(raw, field_name):
        """불리언 쿼리 파라미터 파싱 (true/false/1/0/yes/no). 실패 시 400."""
        val = str(raw).strip().lower()
        if val in ("1", "true", "yes", "y"):
            return True
        if val in ("0", "false", "no", "n"):
            return False
        raise DRFValidationError({field_name: f"불리언 값이어야 합니다 (true/false): {raw!r}"})

    @staticmethod
    def _parse_date_param(raw, field_name):
        """쿼리 파라미터를 (종류, 값) 으로 파싱. 종류는 "date" 또는 "dt".

        날짜만(YYYY-MM-DD) 이면 date 로 취급해 __date 룩업(그날 전체 포함)을 쓰게 한다.
        주의: Django 의 parse_datetime 은 날짜만 줘도 자정 datetime 을 돌려주므로,
        date-only 를 먼저 가려내려면 parse_date($-anchored)를 먼저 시도해야 한다.
        시각이 포함된 ISO8601 이면 datetime(naive 는 현재 타임존으로 aware 변환).
        둘 다 실패하면 400.
        """
        d = parse_date(raw)
        if d is not None:
            return "date", d
        dt = parse_datetime(raw)
        if dt is not None:
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return "dt", dt
        raise DRFValidationError(
            {field_name: f"날짜 형식이 올바르지 않습니다 (YYYY-MM-DD 또는 ISO8601): {raw!r}"}
        )

    @extend_schema(
        summary="캠페인 옵션 가이드 (정적, 인증 불필요)",
        description="""
        ## 목적
        프론트엔드 캠페인 생성/수정 폼의 라디오·토글 옆에 노출할 사용자 안내 문구를
        한 번에 받아간다.

        ## 응답 구조
        ```json
        {
          "version": "v4.0",
          "trigger_types": [
            {"value": "specific_media", "label": "...", "description": "...", "tier": "free"},
            {"value": "any_media", ..., "tier": "pro"},
            {"value": "next_media", ..., "notes": ["...", ...]}
          ],
          "keyword_modes": [...],
          "follow_gate": {
            "headline": "...",
            "modes": {"follow": "팔로우 확인 후 발송", "button": "버튼 클릭 즉시 발송", "off": "게이트 미사용"},
            "items": [...],
            "fields": {"follow_gate_enabled": "...", "gate_verify_follow": "...", "...": "..."}
          },
          "public_reply": {"headline": "...", "description": "...", "items": [...]},
          "scheduling": {"headline": "...", "items": [...]}
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
        summary="AI 캠페인 폼 자동 채우기 (게시물 기반)",
        description="""
        ## 목적
        사용자가 게시물만 고르면, 그 게시물의 **이미지 + 캡션**을 자체 LLM(gemma-4, 멀티모달)으로
        분석해 AutoDM 캠페인 폼 초안을 생성한다. 프론트는 결과값을 폼에 채워 넣고, 사용자가
        수정 후 일반 생성 엔드포인트(`POST /auto-dm-campaigns/`)로 제출한다.

        **DB(캠페인)에는 저장하지 않는 도움(draft) 기능.** 결과는 AiJob 으로만 보관된다.

        ## 비동기 흐름 (폴링)
        gemma-4 가 답글 변형 등 긴 출력을 만드느라 수십 초가 걸려 동기 응답은 프로덕션 타임아웃에
        걸린다. 그래서 **작업을 큐에 등록하고 `202` 로 `job_id` 를 즉시 반환**한다.
        1. `POST .../ai-suggest/?workspace_id=...` → `202 { job_id, poll_url, status }`
        2. `GET /api/v1/ai/jobs/{job_id}/` 를 1~2초 간격으로 폴링
        3. `status === "succeeded"` 가 되면 `result_json.suggestion` 을 폼에 채운다
           (`status === "failed"` 면 `error_message` 표시)

        ## result_json 형태 (폴링 완료 시) — 출력 키는 생성 시리얼라이저 필드명과 1:1
        ```json
        {
          "model": "gemma-4", "elapsed_seconds": 32.1, "vision_used": true,
          "suggestion": {
            "name": "신상 원피스 사이즈 문의 자동응대",
            "keyword_filter": ["사이즈", "재입고", "문의"], "keyword_mode": "any",
            "public_reply_enabled": true,
            "public_reply_templates": ["DM 보내드렸어요! 확인 부탁드려요 💌", "...(기본 50개, 모두 고유)..."],
            "simple": { "opening_message_template": "안녕하세요! 문의 감사해요 🥰 자료 보내드릴게요" },
            "follow_gate": {
              "follow_gate_prompt": "댓글 감사합니다! 버튼을 눌러 받아가세요 🎁",
              "follow_gate_button_label": "자료 받기",
              "follow_gate_button_label_alt": "팔로우했어요",
              "reward_message_template": "감사합니다! 약속드린 자료 보내드려요 🙌",
              "follow_gate_retry_message": "아직 팔로우 확인이 안 됐어요 🥲 ..."
            },
            "link_button": { "link_button_label": "받으러 가기", "link_button_url": "https://shop.example.com/dress" }
          },
          "echo": { "media_id": "...", "media_type": "IMAGE" }, "usage": { "...": 0 }
        }
        ```
        - `public_reply_templates`: 기본 50개(`reply_variant_count` 로 1~50 조절). **LLM 미사용** —
          코드 풀의 정형 인사 문구를 끝맺음(!/~/이모지) 변주해 즉시 추출하므로 빠르다. 서로 다르게(스팸 회피).
        - `follow_gate.*`: 검증 모드 / 버튼 전용 모드 둘 다 커버. `follow_gate_button_label_alt` 는
          검증 모드 토글용 대안 라벨(DB 컬럼 아님). `include_follow_gate=false` 면 `follow_gate` 는 null.
        - `link_button`: **항상** `{link_button_label, link_button_url}` 로 채워진다. `link_url` 을 주면 그 URL,
          안 주면 예시 URL(`https://example.com`)이 들어가니 **사용자가 실제 링크로 교체**해야 한다.
          **링크 URL 은 본문 텍스트엔 안 들어간다** — 그대로 캠페인 `link_button_url`/`link_button_label` 에 넣으면
          발송 DM 에 라벨 달린 링크 버튼으로 첨부된다(단순 DM·reward 모두).
        - AI 는 `trigger_type`/`media_id` 를 정하지 않는다(사용자가 이미 게시물/범위 선택). `echo` 로만 되돌려줌.

        ## 게시물 컨텍스트 입력
        - **권장**: 프론트가 게시물 목록에서 이미 받은 `caption` / `image_url` 을 그대로 전달
          (Graph 재조회 불필요, mock 모드 dev 에서도 동작).
        - `caption` / `image_url` 둘 다 비어 있고 `media_id` 가 있으면 백엔드가 Graph API 로 조회.
          단, **mock 모드이거나 활성 IG 연결이 없으면 400** — 이때는 caption/image_url 을 직접 전달.
        - `business_type` / `campaign_goal` / `tone` / `link_url` 은 선택(없으면 게시물에서 추론).
          `link_url` 은 본문이 아니라 **`link_button`** 으로 제안된다 — 주면 그 URL, 안 주면 예시 URL(교체용).

        ## 인증
        Bearer 토큰 필수. `?workspace_id=<uuid>` 쿼리 파라미터 필수(멤버십 검증).

        ## 사용 예시
        ```javascript
        const { job_id, poll_url } = await (await fetch(
          `/api/v1/integrations/auto-dm-campaigns/ai-suggest/?workspace_id=${wsId}`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify({
              media_id: "18418812427189917",
              caption: "신상 원피스 입고! 사이즈 문의는 댓글로 🥰",
              image_url: "https://scontent.cdninstagram.com/....jpg",
              business_type: "여성 의류 쇼핑몰",
              campaign_goal: "사이즈/재입고 문의 DM 유도",
              link_url: "https://shop.example.com/dress",
              include_follow_gate: true,
              reply_variant_count: 50
            })
          }
        )).json();

        // 폴링
        const timer = setInterval(async () => {
          const job = await (await fetch(poll_url, { headers: { Authorization: `Bearer ${token}` } })).json();
          if (job.status === "succeeded") {
            clearInterval(timer);
            const { suggestion } = job.result_json;  // 폼에 매핑
          } else if (job.status === "failed") {
            clearInterval(timer);
            // job.error_message 표시
          }
        }, 1500);
        ```
        """,
        request=AutoDMCampaignAiSuggestRequestSerializer,
        responses={
            202: OpenApiResponse(
                response=AutoDMCampaignAiSuggestJobSerializer,
                description="생성 작업이 큐에 등록됨. poll_url 을 폴링해 result_json.suggestion 사용.",
            ),
            400: OpenApiResponse(
                description="workspace_id 누락 / 게시물 컨텍스트 없음 / mock·연결없음 상태에서 Graph 조회 필요"
            ),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="해당 워크스페이스의 멤버가 아님"),
            404: OpenApiResponse(description="워크스페이스/게시물을 찾을 수 없음"),
            500: OpenApiResponse(
                description="예상치 못한 서버 오류 (LLM 호출 실패는 작업 status=failed 로 표면화)"
            ),
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
        examples=[
            OpenApiExample(
                "게시물 caption+image 직접 전달 (권장)",
                value={
                    "media_id": "18418812427189917",
                    "caption": "신상 원피스 입고! 사이즈 문의는 댓글로 남겨주세요 🥰 #원피스",
                    "image_url": "https://scontent.cdninstagram.com/....jpg",
                    "media_type": "IMAGE",
                    "business_type": "여성 의류 쇼핑몰",
                    "campaign_goal": "사이즈/재입고 문의를 DM으로 유도하고 링크 전달",
                    "tone": "친근하고 발랄한",
                    "link_url": "https://shop.example.com/dress",
                    "include_follow_gate": True,
                    "reply_variant_count": 50,
                },
                request_only=True,
            ),
        ],
        tags=["Auto DM"],
    )
    @action(detail=False, methods=["post"], url_path="ai-suggest")
    def ai_suggest(self, request):
        """게시물 기반 캠페인 폼 초안 생성 작업을 큐에 등록 (비동기, gemma-4 비전).

        gemma-4 가 답글 변형 등 긴 출력을 만드느라 수십 초가 걸려 동기 응답은 prod gunicorn
        타임아웃에 걸린다 → AiJob 으로 큐잉하고 job_id 를 즉시 반환(202). 프론트는
        GET /api/v1/ai/jobs/{id}/ 를 폴링해 succeeded 가 되면 result_json.suggestion 을 폼에 채운다.

        무거운 작업(이미지 다운로드 + LLM 호출)은 Celery 태스크로 미루고, 빠른 동기 검증
        (워크스페이스 멤버십 / 게시물 컨텍스트 / Graph 메타 조회)만 여기서 처리한다.
        """
        from rest_framework.exceptions import NotFound, PermissionDenied

        from apps.ai_jobs.models import AiJob
        from apps.ai_jobs.tasks import run_dm_campaign_assist_job

        ser = AutoDMCampaignAiSuggestRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        # 워크스페이스 확인 (create 와 동일 패턴 — DRF 예외로 통일 에러 포맷)
        workspace_id = request.query_params.get("workspace_id")
        if not workspace_id:
            raise DRFValidationError({"workspace_id": "workspace_id 쿼리 파라미터가 필요합니다."})
        try:
            workspace = Workspace.objects.get(id=workspace_id)
        except (Workspace.DoesNotExist, DjangoValidationError, ValueError, TypeError):
            raise NotFound("Workspace 를 찾을 수 없습니다.") from None
        if not workspace.memberships.filter(user=request.user).exists():
            raise PermissionDenied("이 워크스페이스의 멤버가 아닙니다.")

        caption = (vd.get("caption") or "").strip()
        image_url = (vd.get("image_url") or "").strip()
        media_type = (vd.get("media_type") or "").strip()
        media_id = (vd.get("media_id") or "").strip()
        ig_connection_id = vd.get("ig_connection_id")

        # caption/image 둘 다 없으면 media_id 로 Graph 메타 조회 (동기 검증: non-mock 한정).
        if not caption and not image_url:
            if not media_id:
                raise DRFValidationError("caption, image_url, media_id 중 최소 하나는 필요합니다.")
            if MockInstagramProvider.is_mock_mode():
                raise DRFValidationError(
                    "Mock 모드에서는 게시물 정보를 조회할 수 없습니다. caption/image_url 을 직접 전달해주세요."
                )
            connection = self._resolve_connection_for_suggest(workspace, ig_connection_id)
            if not connection:
                raise DRFValidationError(
                    "활성 Instagram 연결이 없습니다. caption/image_url 을 직접 전달해주세요."
                )
            caption, image_url, media_type = self._fetch_media_context(connection, media_id)

        job = AiJob.objects.create(
            user=request.user,
            job_type=AiJob.JobType.DM_CAMPAIGN_ASSIST,
            llm_model=AiJob.LlmModel.GEMMA,
            input_payload={
                "caption": caption,
                "image_url": image_url,
                "media_type": media_type,
                "media_id": media_id,
                "business_type": vd.get("business_type", ""),
                "campaign_goal": vd.get("campaign_goal", ""),
                "tone": vd.get("tone", ""),
                "link_url": vd.get("link_url", ""),
                "include_follow_gate": vd.get("include_follow_gate", True),
                "reply_variant_count": vd.get("reply_variant_count", 50),
                "workspace_id": str(workspace.id),
            },
        )
        run_dm_campaign_assist_job.delay(str(job.id))

        return Response(
            {
                "job_id": str(job.id),
                "status": job.status,
                "poll_url": f"/api/v1/ai/jobs/{job.id}/",
                "message": "캠페인 초안 생성을 시작했어요. 잠시 후 결과를 폴링해주세요.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def _resolve_connection_for_suggest(self, workspace, ig_connection_id):
        """ai_suggest 용 IG connection 해석. 미지정 시 첫 활성 connection."""
        if not ig_connection_id:
            return IGAccountConnection.objects.filter(workspace=workspace, status="active").first()
        from rest_framework.exceptions import PermissionDenied

        try:
            connection = IGAccountConnection.objects.get(id=ig_connection_id)
        except (IGAccountConnection.DoesNotExist, DjangoValidationError, ValueError, TypeError):
            return None
        if connection.workspace_id != workspace.id:
            raise PermissionDenied("이 IG 계정은 해당 워크스페이스에 속하지 않습니다.")
        if connection.status != IGAccountConnection.Status.ACTIVE:
            return None
        return connection

    def _fetch_media_context(self, connection, media_id):
        """Graph API 로 게시물 caption/image_url/media_type 조회 (get_media_detail 패턴)."""
        from rest_framework.exceptions import NotFound

        try:
            url = f"{InstagramOAuthService.GRAPH_API_BASE}/{media_id}"
            resp = requests.get(
                url,
                params={
                    "fields": "caption,media_type,media_url,thumbnail_url",
                    "access_token": connection.access_token,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as exc:
            raise NotFound(f"게시물 정보를 가져올 수 없습니다: {str(exc)[:200]}") from exc

        caption = data.get("caption") or ""
        image_url = data.get("media_url") or data.get("thumbnail_url") or ""
        media_type = data.get("media_type") or ""
        return caption, image_url, media_type

    @extend_schema(
        summary="캠페인 목록 조회 (필터·정렬)",
        description="""
        ## 목적
        로그인한 사용자가 멤버인 workspace 의 모든 Auto DM 캠페인을 조회합니다.
        상태(status)·생성일 범위로 필터링하고, 여러 기준으로 정렬할 수 있습니다.

        ## 응답 형태
        **페이지네이션 없이** 캠페인 객체 배열을 그대로 반환합니다(평면 리스트).
        각 항목의 `media_url` 이 비어 있고 `media_id` 가 있으면 Instagram Graph API 로
        best-effort 보강합니다(토큰 만료/실패 시 조용히 건너뜀).

        **항목별 통계 필드(read-only)** — 항목마다 통계를 따로 호출(N+1)할 필요 없이 함께 옵니다:
        | 필드 | 타입 | 의미 |
        |---|---|---|
        | `delivered_count` | int | 도착확인(delivered)+읽음(read) DM 수 |
        | `delivery_rate` | float(0~1) | ACCEPTED 진입 건 중 도착확인 비율 |
        | `needs_attention_count` | int | 사용자 조치 필요 로그 수 (토큰만료/윈도우만료/파라미터오류/도착미확인) |
        | `last_sent_at` | datetime\\|null | 가장 최근 발송 로그 시각 |
        | `thumbnail_url` | string\\|null | 게시물 썸네일(=media_url 미러) |

        ## 쿼리 파라미터
        | 파라미터 | 타입 | 설명 |
        |---|---|---|
        | `ig_connection_id` | uuid | 특정 IG 계정의 캠페인만. 권한 없는 ID 면 빈 배열. |
        | `search` | string | 캠페인 이름/설명 + 연동 IG username 부분일치 검색. |
        | `status` | string | 상태 필터. 콤마 다중. 값: `active`/`paused`/`completed`/`inactive`. 예: `status=active,paused` |
        | `trigger_type` | string | 트리거 필터. 콤마 다중. 값: `specific_media`/`any_media`/`next_media`/`story_reply`. |
        | `follow_gate_enabled` | bool | Follow-gate 사용 여부 필터 (true/false). |
        | `public_reply_enabled` | bool | 공개 답글 사용 여부 필터 (true/false). |
        | `created_after` | date \\| datetime | 이 시각/날짜 **이후**(포함) 생성분. `YYYY-MM-DD` 또는 ISO8601. 날짜만 주면 그날 **00:00:00**(Asia/Seoul)부터. |
        | `created_before` | date \\| datetime | 이 시각/날짜 **이전**(포함) 생성분. 날짜만 주면 그날 **23:59:59**(Asia/Seoul)까지(해당일 포함). |
        | `ordering` | string | 정렬 기준. 콤마 다중 지정 가능, `-` 접두사는 내림차순. 기본값 `-created_at`. |

        **정렬 가능 필드(ordering)**: `created_at`, `updated_at`, `name`, `status`,
        `total_sent`, `total_failed`, `started_at`, `scheduled_start_at`, `scheduled_end_at`,
        `last_sent_at`(최근 발송순 — 미발송은 항상 뒤로). 허용 목록 밖의 필드를 주면 **400** 입니다.

        > 빠른 대시보드 집계(상태별 개수·월 사용량·발송 품질)는 별도
        > `GET .../auto-dm-campaigns/summary/` 를 사용하세요.

        ## 예시
        ```bash
        # 활성/일시정지 캠페인을 발송수 많은 순으로
        curl -G 'https://dev-api.turnflow.link/api/v1/integrations/auto-dm-campaigns/' \\
          -H 'Authorization: Bearer <ACCESS_TOKEN>' \\
          --data-urlencode 'status=active,paused' \\
          --data-urlencode 'ordering=-total_sent'

        # 2026-06-01 ~ 2026-06-30 사이 생성된 캠페인을 이름 오름차순으로
        curl -G 'https://dev-api.turnflow.link/api/v1/integrations/auto-dm-campaigns/' \\
          -H 'Authorization: Bearer <ACCESS_TOKEN>' \\
          --data-urlencode 'created_after=2026-06-01' \\
          --data-urlencode 'created_before=2026-06-30' \\
          --data-urlencode 'ordering=name'
        ```
        ```javascript
        const qs = new URLSearchParams({
          status: "active",
          created_after: "2026-06-01",
          ordering: "-created_at",
        });
        const res = await fetch(
          `/api/v1/integrations/auto-dm-campaigns/?${qs}`,
          { headers: { Authorization: `Bearer ${token}` } }
        );
        const campaigns = await res.json(); // 배열(평면 리스트)
        ```

        ## 인증
        IsAuthenticated. 본인이 멤버인 workspace 의 캠페인만 노출됩니다.
        """,
        parameters=[
            OpenApiParameter(
                name="ig_connection_id",
                type=str,
                location=OpenApiParameter.QUERY,
                description="특정 IG 계정의 캠페인만 필터링 (UUID). 권한 없는 ID 면 빈 배열.",
                required=False,
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="캠페인 이름/설명 + 연동 IG username 부분일치 검색.",
            ),
            OpenApiParameter(
                name="status",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=["active", "paused", "completed", "inactive"],
                description="상태 필터. 콤마로 다중 지정 가능 (예: active,paused).",
            ),
            OpenApiParameter(
                name="trigger_type",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=["specific_media", "any_media", "next_media", "story_reply"],
                description="트리거 종류 필터. 콤마로 다중 지정 가능.",
            ),
            OpenApiParameter(
                name="follow_gate_enabled",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Follow-gate 사용 여부 필터 (true/false).",
            ),
            OpenApiParameter(
                name="public_reply_enabled",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description="공개 답글 사용 여부 필터 (true/false).",
            ),
            OpenApiParameter(
                name="created_after",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "생성일시 하한(포함). YYYY-MM-DD 또는 ISO8601. "
                    "날짜만 주면 그날 00:00:00(Asia/Seoul)부터. 예: 2026-06-01"
                ),
            ),
            OpenApiParameter(
                name="created_before",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "생성일시 상한(포함). YYYY-MM-DD 또는 ISO8601. "
                    "날짜만 주면 그날 23:59:59(Asia/Seoul)까지(해당일 포함). 예: 2026-06-30"
                ),
            ),
            OpenApiParameter(
                name="ordering",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=[
                    "created_at",
                    "-created_at",
                    "updated_at",
                    "-updated_at",
                    "name",
                    "-name",
                    "status",
                    "-status",
                    "total_sent",
                    "-total_sent",
                    "total_failed",
                    "-total_failed",
                    "started_at",
                    "-started_at",
                    "scheduled_start_at",
                    "-scheduled_start_at",
                    "scheduled_end_at",
                    "-scheduled_end_at",
                    "last_sent_at",
                    "-last_sent_at",
                ],
                description=(
                    "정렬 기준. 콤마로 다중 지정 가능, '-' 접두사는 내림차순. 기본 -created_at."
                ),
            ),
        ],
        responses={
            200: AutoDMCampaignListSerializer(many=True),
            400: OpenApiResponse(
                description=(
                    "잘못된 필터/정렬 값 (날짜·불리언 형식 오류, 허용되지 않은 "
                    "status·trigger_type·ordering 필드 등)"
                )
            ),
            401: OpenApiResponse(description="인증 필요"),
        },
        tags=["Auto DM"],
    )
    def list(self, request):
        """캠페인 목록 조회 (검색·필터·정렬 + per-item 통계 enrichment)"""
        # filter_queryset 으로 ?search= (이름/설명/IG username) 적용. get_queryset 에서
        # 이미 통계 annotate + status/facet/날짜 필터 + ordering 이 적용된 상태.
        queryset = self.filter_queryset(self.get_queryset())
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
                        # thumbnail_url 은 media_url 미러 — 보강된 값과 동기화.
                        item["thumbnail_url"] = media_url
                except Exception:
                    # Best-effort fallback: ignore and continue
                    continue

        return Response(data)

    @extend_schema(
        summary="캠페인 대시보드 요약",
        description="""
        ## 목적
        캠페인 목록 화면 상단 대시보드용 **집계 한 방** 엔드포인트. 상태별 개수,
        이번 달 DM 사용량/한도, 발송 품질, 마지막 활동 시각을 한 번에 반환합니다.
        (목록을 모두 받아 프론트에서 합산할 필요가 없습니다.)

        ## 스코프 결정
        - `ig_connection_id` 지정(권장): 해당 IG 계정의 캠페인으로 `counts`·`delivery` 를
          집계하고, `usage` 는 그 계정이 속한 **워크스페이스** 기준으로 계산합니다.
        - `workspace_id` 지정: 그 워크스페이스 전체.
        - 둘 다 생략: 사용자의 워크스페이스가 **하나면** 그것으로 자동 결정.
          여러 개면 **400** (둘 중 하나를 지정해야 함).

        ## 응답
        ```json
        {
          "counts": {"active": 3, "paused": 1, "completed": 2, "inactive": 0, "total": 6},
          "usage": {
            "sent_this_month": 42,
            "monthly_free_limit": 100,
            "remaining_this_month": 58,
            "is_over_limit": false,
            "period_start": "2026-06-01T00:00:00+09:00",
            "period_end": "2026-07-01T00:00:00+09:00"
          },
          "delivery": {
            "total_sent": 40,
            "delivery_rate": 0.95,
            "success_rate": 0.93,
            "needs_attention_total": 2
          },
          "last_activity_at": "2026-06-17T18:20:00+09:00"
        }
        ```

        - **usage**: 한도는 플랜(starter 100 / pro 1000 / enterprise -1=무제한). 단 **관리자
          (is_staff/superuser) 계정은 플랜과 무관하게 무제한(-1)**. `monthly_free_limit=-1`
          이면 `remaining_this_month=null`, `is_over_limit=false`. 사용량은 SentDMLog 에서
          이번 캘린더월(Asia/Seoul)을 직접 집계해 정확합니다.
        - **delivery_rate**: ACCEPTED 진입 건 중 도착확인(delivered+read) 비율 (0~1).
        - **needs_attention_total**: 토큰만료/윈도우만료/파라미터오류/도착미확인 로그 합.

        ## 인증
        IsAuthenticated. 본인이 멤버인 워크스페이스만.
        """,
        parameters=[
            OpenApiParameter(
                name="ig_connection_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="특정 IG 계정으로 스코프 (UUID). usage 는 그 워크스페이스 기준.",
            ),
            OpenApiParameter(
                name="workspace_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="특정 워크스페이스로 스코프 (UUID). ig_connection_id 미지정 시 사용.",
            ),
        ],
        responses={
            200: AutoDMCampaignSummarySerializer,
            400: OpenApiResponse(
                description="워크스페이스를 결정할 수 없음(여러 개) 또는 잘못된 id"
            ),
            401: OpenApiResponse(description="인증 필요"),
        },
        tags=["Auto DM"],
    )
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """캠페인 목록 상단 대시보드용 집계 (counts / usage / delivery / last_activity_at)."""
        user_workspaces = Workspace.objects.filter(memberships__user=request.user)
        campaigns = AutoDMCampaign.objects.filter(ig_connection__workspace__in=user_workspaces)

        ig_connection_id = request.query_params.get("ig_connection_id")
        workspace_id = request.query_params.get("workspace_id")

        if ig_connection_id:
            conn = (
                IGAccountConnection.objects.filter(
                    id=ig_connection_id, workspace__in=user_workspaces
                )
                .select_related("workspace")
                .first()
            )
            if conn is None:
                raise DRFValidationError(
                    {"ig_connection_id": "해당 IG 연동을 찾을 수 없거나 권한이 없습니다."}
                )
            workspace = conn.workspace
            campaigns = campaigns.filter(ig_connection_id=ig_connection_id)
        elif workspace_id:
            workspace = user_workspaces.filter(id=workspace_id).first()
            if workspace is None:
                raise DRFValidationError(
                    {"workspace_id": "워크스페이스를 찾을 수 없거나 권한이 없습니다."}
                )
            campaigns = campaigns.filter(ig_connection__workspace=workspace)
        else:
            ws_list = list(user_workspaces[:2])
            if len(ws_list) == 1:
                workspace = ws_list[0]
            elif not ws_list:
                raise DRFValidationError({"detail": "소속된 워크스페이스가 없습니다."})
            else:
                raise DRFValidationError(
                    {
                        "detail": (
                            "워크스페이스가 여러 개입니다. ig_connection_id 또는 "
                            "workspace_id 를 지정하세요."
                        )
                    }
                )

        delivery = build_delivery_summary(campaigns)
        last_activity = delivery.pop("_last_activity_at")
        data = {
            "counts": build_counts(campaigns),
            "usage": compute_monthly_usage(workspace, user=request.user),
            "delivery": delivery,
            "last_activity_at": last_activity,
        }
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
        - `scheduled_start_at`: 예약 시작일시 (ISO8601). 이 시각부터 발송 시작. 비우면 즉시.
        - `scheduled_end_at`: 예약 종료일시 (ISO8601). 이 시각 이후 자동 종료. 비우면 무기한.
          (생성 후 변경은 `POST .../{id}/schedule/` 사용 권장)

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

        ### 버튼 게이트 (선택 — 버튼 클릭 시 reward DM)
        opening DM 에 버튼을 붙이고, 사용자가 그 버튼을 누르면 본 DM(`reward_message_template`)을 발송합니다.
        - `follow_gate_enabled`: true 면 버튼 게이트 사용 (이때 `reward_message_template` 필수)
        - `gate_verify_follow`: true(기본)=버튼 클릭 시 팔로우 여부를 확인한 뒤 발송 (follow 모드) /
          false=팔로우 확인 없이 버튼 클릭 즉시 발송 (**button-only** 모드)
        - `follow_gate_button_label`: 버튼 라벨 (최대 20자). button-only 면 직접 지정 권장 (비우면 "팔로우했어요")
        - `follow_gate_prompt`: opening DM 본문(버튼 안내 문구). 비우면 기본 문구
        - `follow_gate_retry_message`: 미팔로우 시 재안내 (follow 모드 전용 — button-only 면 미사용)
        - `reward_message_template`: 버튼 클릭 후 보낼 본 DM (게이트 사용 시 필수)

        button-only 예시 (팔로우 확인 없이 버튼만 누르면 발송):
        ```json
        {
          "media_id": "18418812427189917",
          "name": "쿠폰 신청",
          "message_template": "신청 안내드립니다",
          "follow_gate_enabled": true,
          "gate_verify_follow": false,
          "follow_gate_prompt": "버튼을 누르면 바로 보내드려요!",
          "follow_gate_button_label": "받기",
          "reward_message_template": "감사합니다! 쿠폰 링크: https://example.com/coupon"
        }
        ```

        ### 링크 버튼 (선택 — DM 카드에 라벨 달린 web_url 버튼)
        URL 을 본문 텍스트에 박는 대신, 발송 DM 카드에 **"라벨 달린 링크 버튼"** 으로 첨부합니다
        (Meta generic-template `web_url` 버튼). 인스타 앱에서 버튼 형태로 보이고, 첫 DM 텍스트에
        URL 직박 시 스팸 판정되는 문제를 피합니다.
        - `link_button_url`: 버튼이 여는 URL (http/https). 비우면 버튼 미첨부.
        - `link_button_label`: 버튼 글자 (최대 20자). 비우면 "자세히 보기".
        - **적용 위치**: 단순 DM(게이트 off)은 그 DM 에, follow-gate(검증/버튼즉시)는 **reward DM** 에 붙습니다
          (opening/재안내 DM 에는 게이트 버튼이 붙으므로 링크 버튼은 reward 에만).
        ```json
        {
          "media_id": "18418812427189917",
          "name": "무료 가이드 배포",
          "opening_message_template": "안녕하세요! 무료 가이드 보내드려요 😊",
          "link_button_url": "https://example.com/guide",
          "link_button_label": "가이드 받기"
        }
        ```

        ## 동작 방식
        1. 캠페인 생성 후 자동으로 `ACTIVE` 상태로 설정
        2. 해당 게시물에 댓글이 달리면 Webhook으로 수신
        3. Celery 태스크가 자동으로 DM 발송 처리
        4. 중복 발송 방지 (같은 댓글에 대해 1회만 발송)
        5. 시간당 발송 제한 적용
        6. 예약 발송: `scheduled_start_at` 이전엔 발송하지 않고, `scheduled_end_at` 이후엔
           자동 종료(`completed`)됩니다. 응답의 `schedule_state` 로 현재 상태 확인.

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
        description=(
            "기존 Auto DM 캠페인을 수정합니다(전체 교체, PUT).\n\n"
            "예약 발송 필드 `scheduled_start_at` / `scheduled_end_at` 도 함께 수정할 수 있습니다 "
            "(부분 변경은 PATCH 또는 `POST .../{id}/schedule/` 권장). "
            "`scheduled_end_at` 은 `scheduled_start_at` 보다 미래여야 합니다."
        ),
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
        description=(
            "Auto DM 캠페인의 일부 필드만 수정합니다(PATCH).\n\n"
            "예약 발송 창만 바꾸려면 `scheduled_start_at` / `scheduled_end_at` 만 보내면 됩니다 "
            "(보내지 않은 필드는 기존 값 유지). 활성화까지 한 번에 처리하려면 "
            "`POST .../{id}/schedule/` 를 사용하세요."
        ),
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
        description=(
            "활성 상태의 캠페인을 일시정지합니다.\n\n"
            "응답은 **목록 항목과 동일한 형태(통계 enrichment 포함)** 의 갱신된 캠페인 객체라,"
            " 인라인 토글 후 해당 1건만 교체하면 됩니다."
        ),
        responses={200: AutoDMCampaignListSerializer},
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
        description=(
            "일시정지/종료된 캠페인을 다시 활성화(status=active)합니다.\n\n"
            "**예약 발송 주의**: 종료 예약(scheduled_end_at)이 이미 과거라면, 재개 직후 "
            "자동 종료 배치가 다시 종료시킵니다. 이를 막기 위해 재개 시 **과거가 된 "
            "scheduled_end_at 은 자동으로 해제(null)** 합니다. 기간을 다시 지정하려면 "
            "`POST .../schedule/` 또는 PATCH 로 새 종료일을 설정하세요.\n\n"
            "응답은 **목록 항목과 동일한 형태(통계 enrichment 포함)** 의 갱신된 캠페인 객체입니다."
        ),
        responses={200: AutoDMCampaignListSerializer},
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        """캠페인 재개 (과거가 된 종료 예약은 해제)"""
        campaign = self.get_object()
        campaign.status = AutoDMCampaign.Status.ACTIVE
        # 종료 예약이 이미 지났으면 즉시 재종료되지 않도록 해제
        if campaign.scheduled_end_at and campaign.scheduled_end_at <= timezone.now():
            campaign.scheduled_end_at = None
        # 자동 종료로 기록된 ended_at 을 비워 ACTIVE 인데 과거 종료시각이 남는 모순 방지
        # (schedule 액션의 activate 분기와 동일하게 정리)
        campaign.ended_at = None
        campaign.save()
        serializer = self.get_serializer(campaign)
        return Response(serializer.data)

    @extend_schema(
        summary="캠페인 일괄 일시정지",
        description=(
            "여러 캠페인을 한 번에 일시정지합니다.\n\n"
            '요청 `{"ids": [<uuid>, ...]}` (최대 200개). 응답은 '
            '`{"succeeded": [<uuid>...], "failed": [{"id", "reason"}...]}` 형태로, '
            "권한 없거나 존재하지 않는 id 는 `failed`(reason=`not_found`)에 담깁니다 "
            "(전체 실패가 아니라 건별 부분 성공)."
        ),
        request=CampaignBulkActionRequestSerializer,
        responses={
            200: CampaignBulkActionResponseSerializer,
            400: OpenApiResponse(description="ids 누락/형식 오류"),
            401: OpenApiResponse(description="인증 필요"),
        },
        tags=["Auto DM"],
    )
    @action(detail=False, methods=["post"], url_path="bulk-pause")
    def bulk_pause(self, request):
        """캠페인 일괄 일시정지"""
        return self._bulk_action(request, "pause")

    @extend_schema(
        summary="캠페인 일괄 재개",
        description=(
            "여러 캠페인을 한 번에 재개(status=active)합니다. 과거가 된 종료 예약은 건별로 "
            "자동 해제합니다(단건 resume 과 동일 규칙).\n\n"
            "요청/응답 형식은 일괄 일시정지와 동일."
        ),
        request=CampaignBulkActionRequestSerializer,
        responses={
            200: CampaignBulkActionResponseSerializer,
            400: OpenApiResponse(description="ids 누락/형식 오류"),
            401: OpenApiResponse(description="인증 필요"),
        },
        tags=["Auto DM"],
    )
    @action(detail=False, methods=["post"], url_path="bulk-resume")
    def bulk_resume(self, request):
        """캠페인 일괄 재개"""
        return self._bulk_action(request, "resume")

    @extend_schema(
        summary="캠페인 일괄 삭제",
        description=(
            "여러 캠페인을 한 번에 삭제합니다(되돌릴 수 없음).\n\n"
            "요청/응답 형식은 일괄 일시정지와 동일. 권한 없거나 없는 id 는 "
            "`failed`(reason=`not_found`)."
        ),
        request=CampaignBulkActionRequestSerializer,
        responses={
            200: CampaignBulkActionResponseSerializer,
            400: OpenApiResponse(description="ids 누락/형식 오류"),
            401: OpenApiResponse(description="인증 필요"),
        },
        tags=["Auto DM"],
    )
    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        """캠페인 일괄 삭제"""
        return self._bulk_action(request, "delete")

    def _bulk_action(self, request, op):
        """벌크 액션 공통 처리 — 건별 부분 성공.

        본인 워크스페이스 소속 캠페인만 대상이며, 그 외 id 는 not_found 로 실패 처리한다.
        반환: {"succeeded": [id...], "failed": [{"id", "reason"}...]}.
        """
        in_ser = CampaignBulkActionRequestSerializer(data=request.data)
        in_ser.is_valid(raise_exception=True)
        # 입력 순서 유지하며 중복 제거
        ids = list(dict.fromkeys(str(i) for i in in_ser.validated_data["ids"]))

        user_workspaces = Workspace.objects.filter(memberships__user=request.user)
        owned = {
            str(c.id): c
            for c in AutoDMCampaign.objects.filter(
                id__in=ids, ig_connection__workspace__in=user_workspaces
            )
        }

        succeeded, failed = [], []
        now = timezone.now()
        for cid in ids:
            campaign = owned.get(cid)
            if campaign is None:
                failed.append({"id": cid, "reason": "not_found"})
                continue
            try:
                if op == "pause":
                    campaign.status = AutoDMCampaign.Status.PAUSED
                    campaign.save(update_fields=["status", "updated_at"])
                elif op == "resume":
                    campaign.status = AutoDMCampaign.Status.ACTIVE
                    if campaign.scheduled_end_at and campaign.scheduled_end_at <= now:
                        campaign.scheduled_end_at = None
                    campaign.ended_at = None
                    campaign.save(
                        update_fields=["status", "scheduled_end_at", "ended_at", "updated_at"]
                    )
                elif op == "delete":
                    campaign.delete()
                succeeded.append(cid)
            except Exception as exc:  # noqa: BLE001 — 건별 격리, 사유를 응답에 담는다
                failed.append({"id": cid, "reason": str(exc)[:200]})

        return Response({"succeeded": succeeded, "failed": failed})

    @extend_schema(
        summary="캠페인 복사 (비활성 복사본 생성)",
        description="""
        ## 기능
        기존 캠페인을 **비활성(INACTIVE) 복사본**으로 복제합니다. 잘 만든 캠페인을
        템플릿처럼 재사용할 때 사용합니다.

        - 캠페인 **이름**만 바뀌고(기본 `"{원본명} 복사"`), **나머지 설정은 전부 동일**하게
          복사됩니다 — 트리거/미디어/키워드/메시지/공개답글/Follow-gate/발송제한,
          그리고 **예약 발송 기간(scheduled_start_at/end_at)** 까지 그대로.
        - 복사본은 항상 **status=inactive** 로 생성됩니다(사용자가 검토 후 직접 활성화).
        - 발송 통계(total_sent/total_failed)와 실행 기록(started_at/ended_at)은 **초기화**됩니다.
        - 발송 로그(SentDMLog)는 복사되지 않습니다.

        > 복사본은 원본과 **동일한 IG 연동(ig_connection)** 에 속합니다.
        > 비활성 상태라 활성화 전까지는 어떤 DM도 발송되지 않습니다.

        ## 요청 본문 (선택)
        | 필드 | 타입 | 필수 | 설명 |
        |---|---|---|---|
        | `name` | string(≤255) | 선택 | 복사본 이름. 생략/공백이면 `"{원본명} 복사"` 자동 생성. |

        ## 예시
        ```bash
        curl -X POST \\
          'https://dev-api.turnflow.link/api/v1/integrations/auto-dm-campaigns/{id}/copy/' \\
          -H 'Authorization: Bearer <ACCESS_TOKEN>' \\
          -H 'Content-Type: application/json' \\
          -d '{}'
        ```
        이름 직접 지정:
        ```json
        { "name": "여름 이벤트 (B안)" }
        ```
        응답(201)은 새로 생성된 복사본 전체이며 `status="inactive"`, `total_sent=0` 입니다.

        ## 인증
        IsAuthenticated — 본인 워크스페이스의 캠페인만 복사 가능(타 워크스페이스는 404).
        """,
        request=AutoDMCampaignCopySerializer,
        responses={
            201: OpenApiResponse(
                response=AutoDMCampaignSerializer, description="복사본 생성 성공 (비활성)"
            ),
            400: OpenApiResponse(description="잘못된 요청 (name 형식 오류 등)"),
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="권한 없음 (해당 workspace 멤버 아님)"),
            404: OpenApiResponse(description="캠페인을 찾을 수 없음"),
        },
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["post"])
    def copy(self, request, pk=None):
        """캠페인을 비활성 복사본으로 복제"""
        source = self.get_object()  # 테넌시/권한 + 404 자동 처리
        in_ser = AutoDMCampaignCopySerializer(data=request.data)
        in_ser.is_valid(raise_exception=True)
        new_campaign = source.copy(new_name=in_ser.validated_data.get("name") or None)
        return Response(
            AutoDMCampaignSerializer(new_campaign).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="캠페인 예약 발송 설정",
        description="""
        ## 기능
        캠페인이 **실제로 DM을 발송하는 활성 기간(window)** 을 지정합니다.
        시작일 전에는 발송하지 않고, 종료일이 지나면 **자동으로 종료(completed)** 됩니다
        (별도 운영자 조작 불필요).

        ## 요청 본문
        | 필드 | 타입 | 필수 | 설명 |
        |---|---|---|---|
        | `scheduled_start_at` | datetime(ISO8601) \\| null | 선택 | 발송 시작일시. 생략/null 이면 즉시 시작. |
        | `scheduled_end_at`   | datetime(ISO8601) \\| null | 선택 | 자동 종료일시. 생략/null 이면 무기한. |
        | `activate`           | boolean | 선택(기본 true) | true 면 status 를 active 로 전환하며 과거 종료 기록 해제. false 면 status 유지. |

        > 이 API는 예약 창을 **통째로 교체**합니다. 한쪽만 보내면 다른 쪽은 해제(null)됩니다.
        > 부분만 바꾸려면 PATCH `/auto-dm-campaigns/{id}/` 로 해당 필드만 보내세요.

        ## 검증 규칙
        - `scheduled_end_at` 은 `scheduled_start_at` 보다 미래여야 합니다.
        - `scheduled_end_at` 은 현재 시각보다 미래여야 합니다(과거면 즉시 종료되므로 거부).
        - 시각은 타임존 포함을 권장합니다 (서버 기준 Asia/Seoul, UTC 저장).

        ## 동작 방식
        1. 댓글/스토리 웹훅 처리 시, 현재 시각이 활성 기간 안인 캠페인만 발송 후보가 됩니다.
        2. Celery Beat(1분 주기)가 `scheduled_end_at` 경과 캠페인을 `completed` 로 전환합니다.
        3. 응답의 `schedule_state` 로 현재 상태를 확인하세요:
           `always_on`(기간 미설정) / `scheduled`(시작 대기) / `running`(진행 중) / `ended`(종료됨).

        ## 예시
        ```bash
        curl -X POST \\
          'https://dev-api.turnflow.link/api/v1/integrations/auto-dm-campaigns/{id}/schedule/' \\
          -H 'Authorization: Bearer <ACCESS_TOKEN>' \\
          -H 'Content-Type: application/json' \\
          -d '{
            "scheduled_start_at": "2026-07-01T09:00:00+09:00",
            "scheduled_end_at": "2026-07-31T23:59:59+09:00",
            "activate": true
          }'
        ```
        예약 해제(상시 발송으로 되돌리기):
        ```json
        { "scheduled_start_at": null, "scheduled_end_at": null }
        ```
        """,
        request=AutoDMCampaignScheduleSerializer,
        responses={
            200: OpenApiResponse(response=AutoDMCampaignSerializer, description="예약 설정 적용됨"),
            400: OpenApiResponse(description="검증 실패 (종료일 ≤ 시작일, 종료일 과거 등)"),
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="권한 없음 (해당 workspace 멤버 아님)"),
            404: OpenApiResponse(description="캠페인을 찾을 수 없음"),
        },
        tags=["Auto DM"],
    )
    @action(detail=True, methods=["post"])
    def schedule(self, request, pk=None):
        """예약 발송 창(활성 기간) 설정 + 자동 종료 예약"""
        campaign = self.get_object()
        serializer = AutoDMCampaignScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        campaign.scheduled_start_at = data.get("scheduled_start_at")
        campaign.scheduled_end_at = data.get("scheduled_end_at")
        update_fields = ["scheduled_start_at", "scheduled_end_at", "updated_at"]

        if data.get("activate", True):
            campaign.status = AutoDMCampaign.Status.ACTIVE
            campaign.ended_at = None
            update_fields += ["status", "ended_at"]
            if campaign.started_at is None:
                campaign.started_at = timezone.now()
                update_fields.append("started_at")

        campaign.save(update_fields=update_fields)
        return Response(AutoDMCampaignSerializer(campaign).data)

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
                name="include_children",
                type=bool,
                location=OpenApiParameter.QUERY,
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
            "1",
            "true",
            "yes",
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
        recent_logs = campaign.dm_logs.filter(created_at__gte=last_24h, parent_log__isnull=True)

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
def _enqueue_messaging_event(event_type: str, mid: str, payload: dict, logger) -> None:
    """P2c — 웹훅 echo/read 를 EventInbox 에 멱등 INSERT 후 최초 1회만 Celery enqueue.

    Meta 재전송/동시 도달은 event_key UNIQUE 로 흡수되어 한 번만 후속 처리된다.
    이렇게 하면 webhook 응답 critical path 의 PG 쓰기가 INLINE UPDATE 2~4회 → 가벼운 INSERT 1회로 줄고,
    실제 SentDMLog UPDATE 는 process_messaging_event 가 select_for_update 로 직렬화 처리한다.

    WEBHOOK_ASYNC_MESSAGING=False 면 레거시 inline 경로로 폴백(즉시 롤백 가능).
    """
    if not getattr(settings, "WEBHOOK_ASYNC_MESSAGING", True):
        if event_type == EventInbox.EVENT_ECHO:
            _mark_log_delivered_by_echo(
                mid=mid,
                page_ig_user_id=payload.get("page_ig_user_id", ""),
                recipient_user_id=payload.get("recipient_user_id", ""),
                logger=logger,
            )
        else:
            _mark_log_read_by_mid(mid, logger)
        return

    from django.db import IntegrityError

    from .tasks import process_messaging_event

    key = f"{event_type}:{mid}"
    try:
        _, created = EventInbox.objects.get_or_create(
            event_key=key,
            defaults={"event_type": event_type, "payload": payload},
        )
    except IntegrityError:
        created = False  # 동시 INSERT 레이스의 패자 — 이미 등록됨
    if created:
        process_messaging_event.delay(key)


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
                _enqueue_messaging_event(EventInbox.EVENT_READ, mid, {"mid": mid}, logger)
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

        is_echo = bool(message.get("is_echo")) or (page_ig_user_id and sender_id == page_ig_user_id)
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
            _enqueue_messaging_event(
                EventInbox.EVENT_ECHO,
                mid,
                {
                    "mid": mid,
                    "page_ig_user_id": page_ig_user_id,
                    "recipient_user_id": recipient_id,
                },
                logger,
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
                        "sender_user_id": sender_id,
                        "sender_username": "",  # messages webhook 엔 username 없음
                        "story_id": story_id,
                        "message_mid": mid or "",
                        "message_text": message.get("text", "") or "",
                        "entry_time": ev.get("timestamp"),
                    }
                )
                logger.debug(
                    f"Story reply queued: sender={sender_id}, " f"story={story_id}, mid={mid}"
                )
            else:
                logger.debug(f"Inbound DM received (no handler): sender={sender_id}, mid={mid}")


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
        opening_log_id,
        sender_id,
    )
    return True


def _mark_log_delivered_by_echo(
    *, mid: str, page_ig_user_id: str, recipient_user_id: str, logger
) -> None:
    """is_echo:true 이벤트의 mid 와 SentDMLog.meta_message_id 매칭 → DELIVERED"""
    qs = SentDMLog.objects.filter(meta_message_id=mid).select_related("campaign__ig_connection")
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
        import json
        import logging

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
    @action(
        detail=False,
        methods=["get", "patch"],
        url_path="ig-connections/(?P<ig_connection_id>[^/.]+)",
    )
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
