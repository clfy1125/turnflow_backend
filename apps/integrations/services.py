"""
Instagram OAuth and Mock Provider services
"""

import requests
from django.conf import settings
from datetime import datetime, timedelta
from typing import Dict, Optional
import secrets
import uuid


class InstagramOAuthService:
    """
    Service for Instagram Graph API OAuth flow (Facebook Login for Instagram Business)

    Updated to Graph API v24.0 (Released: October 8, 2025)
    """

    # Facebook OAuth endpoints for Instagram Business API
    FACEBOOK_VERSION = "v24.0"  # Updated to latest version
    AUTHORIZE_URL = f"https://www.facebook.com/{FACEBOOK_VERSION}/dialog/oauth"
    TOKEN_URL = f"https://graph.facebook.com/{FACEBOOK_VERSION}/oauth/access_token"
    GRAPH_API_BASE = f"https://graph.facebook.com/{FACEBOOK_VERSION}"

    # Required scopes for Instagram Business API (v24.0 compatible)
    # Ref: https://developers.facebook.com/docs/instagram-platform/
    REQUIRED_SCOPES = [
        "pages_show_list",  # Required to list Facebook Pages
        "pages_read_engagement",  # Required to read page info and engagement
        "instagram_basic",  # Basic Instagram profile and media access
        "instagram_manage_comments",  # Manage Instagram comments
        "instagram_manage_messages",  # Manage Instagram Direct Messages
        # Optional but recommended for advanced features:
        "business_management",  # For multi-account management
        # "instagram_content_publish",  # For publishing content
        # "instagram_manage_insights",  # For insights and analytics
    ]

    @classmethod
    def get_authorization_url(cls, redirect_uri: str, state: str) -> str:
        """
        Generate Facebook OAuth authorization URL for Instagram Business

        Args:
            redirect_uri: Callback URL after authorization
            state: CSRF protection state parameter

        Returns:
            Authorization URL for user redirection (Facebook Login)
        """
        params = {
            "client_id": settings.META_APP_ID,
            "redirect_uri": redirect_uri,
            "scope": ",".join(cls.REQUIRED_SCOPES),
            "response_type": "code",
            "state": state,
        }

        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{cls.AUTHORIZE_URL}?{query_string}"

    @classmethod
    def exchange_code_for_token(cls, code: str, redirect_uri: str) -> Dict:
        """
        Exchange authorization code for Facebook access token

        Args:
            code: Authorization code from callback
            redirect_uri: Same redirect URI used in authorization

        Returns:
            Dict with access_token and token_type
        """
        params = {
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        }

        response = requests.get(cls.TOKEN_URL, params=params)
        response.raise_for_status()
        return response.json()

    @classmethod
    def get_long_lived_token(cls, short_lived_token: str) -> Dict:
        """
        Exchange short-lived token for long-lived token (60 days)

        Args:
            short_lived_token: Short-lived Facebook access token (1 hour validity)

        Returns:
            Dict with access_token and expires_in (typically 5184000 seconds = 60 days)

        Note: Long-lived tokens expire after 60 days and must be refreshed
        """
        url = f"{cls.GRAPH_API_BASE}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "fb_exchange_token": short_lived_token,
        }

        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()

    @classmethod
    def get_instagram_business_account(cls, facebook_page_id: str, access_token: str) -> Dict:
        """
        Get Instagram Business Account ID from Facebook Page

        Args:
            facebook_page_id: Facebook Page ID
            access_token: Facebook access token with pages_show_list permission

        Returns:
            Dict with Instagram Business Account info (id, username, etc.)

        Raises:
            ValueError: If no Instagram Business Account is linked to the Page
        """
        url = f"{cls.GRAPH_API_BASE}/{facebook_page_id}"
        params = {
            "fields": "instagram_business_account",
            "access_token": access_token,
        }

        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            if "instagram_business_account" not in data:
                raise ValueError("No Instagram Business Account linked to this Facebook Page")

            return data["instagram_business_account"]

        except requests.exceptions.HTTPError as e:
            raise ValueError(f"Failed to get Instagram account: {str(e)}")
        except Exception as e:
            raise

    @classmethod
    def get_facebook_pages(cls, access_token: str) -> list:
        """
        Get user's Facebook Pages with admin access

        Args:
            access_token: Facebook access token with pages_show_list permission

        Returns:
            List of Facebook Pages (each page includes id, name, access_token, etc.)

        Note: Only returns pages where the user has admin, editor, or moderator role
        """
        url = f"{cls.GRAPH_API_BASE}/me/accounts"
        params = {
            "access_token": access_token,
        }

        response = requests.get(url, params=params)
        response.raise_for_status()
        result = response.json()

        return result.get("data", [])

    @classmethod
    def get_account_info(cls, instagram_account_id: str, access_token: str) -> Dict:
        """
        Get Instagram Business Account information

        Args:
            instagram_account_id: Instagram Business Account ID
            access_token: Page access token with instagram_basic permission

        Returns:
            Dict with account information:
            - id: Instagram account ID
            - username: Instagram username
            - name: Display name
            - profile_picture_url: Profile picture URL

        Note: Requires instagram_basic permission
        """
        url = f"{cls.GRAPH_API_BASE}/{instagram_account_id}"
        params = {
            "fields": "id,username,name,profile_picture_url",
            "access_token": access_token,
        }

        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()


class MockInstagramProvider:
    """
    Mock Instagram OAuth provider for development/testing
    Simulates Instagram OAuth flow without actual API calls

    Useful for:
    - Local development without Meta App credentials
    - Testing OAuth flow without rate limits
    - CI/CD pipelines
    """

    # Mock token prefix to identify mock tokens
    MOCK_TOKEN_PREFIX = "mock_token_"

    @classmethod
    def is_mock_mode(cls) -> bool:
        """
        Check if running in mock mode (development)

        Returns:
            True if DEBUG=True and INSTAGRAM_MOCK_MODE=True (default)
        """
        return settings.DEBUG and getattr(settings, "INSTAGRAM_MOCK_MODE", True)

    @classmethod
    def generate_mock_authorization_url(cls, redirect_uri: str, state: str) -> str:
        """
        Generate mock authorization URL (redirects to callback with mock code)

        Args:
            redirect_uri: Callback URL
            state: State parameter for CSRF protection

        Returns:
            Mock authorization URL that auto-redirects to callback
        """
        # In mock mode, we directly redirect to callback with a mock code
        mock_code = f"mock_code_{secrets.token_urlsafe(16)}"
        return f"{redirect_uri}?code={mock_code}&state={state}"

    @classmethod
    def exchange_mock_code_for_token(cls, code: str) -> Dict:
        """
        Simulate token exchange with mock data

        Args:
            code: Mock authorization code (must start with 'mock_code_')

        Returns:
            Mock token response with access_token, user_id, and token_type

        Raises:
            ValueError: If code is not a valid mock code
        """
        if not code.startswith("mock_code_"):
            raise ValueError("Invalid mock code")

        # Generate mock token
        mock_token = f"{cls.MOCK_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        mock_user_id = f"mock_ig_{uuid.uuid4().hex[:12]}"

        return {
            "access_token": mock_token,
            "user_id": mock_user_id,
            "token_type": "bearer",
        }

    @classmethod
    def get_mock_long_lived_token(cls, short_lived_token: str) -> Dict:
        """
        Simulate long-lived token exchange (60 days)

        Args:
            short_lived_token: Mock short-lived token

        Returns:
            Mock long-lived token response with access_token, token_type, and expires_in
        """
        # Generate new mock long-lived token
        mock_token = f"{cls.MOCK_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"

        return {
            "access_token": mock_token,
            "token_type": "bearer",
            "expires_in": 5184000,  # 60 days in seconds
        }

    @classmethod
    def get_mock_account_info(cls, access_token: str) -> Dict:
        """
        Get mock account information

        Args:
            access_token: Mock access token

        Returns:
            Mock account info with id, username, and account_type
        """
        # Extract user ID from token or generate random
        user_id = f"mock_ig_{uuid.uuid4().hex[:12]}"
        username = f"mock_user_{secrets.token_hex(4)}"

        return {
            "id": user_id,
            "username": username,
            "account_type": "BUSINESS",
        }

    @classmethod
    def is_mock_token(cls, token: str) -> bool:
        """
        Check if token is a mock token

        Args:
            token: Access token to check

        Returns:
            True if token starts with MOCK_TOKEN_PREFIX
        """
        return token.startswith(cls.MOCK_TOKEN_PREFIX)


class InstagramMessagingService:
    """
    Instagram Messaging API Service (Meta Graph API v24.0)
    DM 발송 기능 제공
    """

    GRAPH_API_BASE = f"https://graph.facebook.com/v24.0"

    @classmethod
    def send_dm_via_comment(
        cls, ig_user_id: str, comment_id: str, message_text: str, access_token: str
    ) -> Dict:
        """
        댓글 ID를 통해 DM 발송

        Args:
            ig_user_id: Instagram 비즈니스 계정 ID
            comment_id: 댓글 ID (webhook에서 받은 id)
            message_text: 전송할 메시지 내용
            access_token: Instagram User Access Token

        Returns:
            API 응답 (성공 시 message_id 포함)

        Raises:
            requests.exceptions.HTTPError: API 호출 실패 시
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/messages"

        payload = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": message_text},
            "access_token": access_token,
        }

        response = requests.post(url, json=payload)
        response.raise_for_status()

        return response.json()

    @classmethod
    def send_dm_via_user_id(
        cls, ig_user_id: str, recipient_id: str, message_text: str, access_token: str
    ) -> Dict:
        """
        사용자 ID를 통해 DM 발송 (24시간 메시징 윈도우 내에서만 가능)

        Args:
            ig_user_id: Instagram 비즈니스 계정 ID
            recipient_id: 수신자 Instagram User ID
            message_text: 전송할 메시지 내용
            access_token: Instagram User Access Token

        Returns:
            API 응답

        Raises:
            requests.exceptions.HTTPError: API 호출 실패 시
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/messages"

        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": message_text},
            "access_token": access_token,
        }

        response = requests.post(url, json=payload)
        response.raise_for_status()

        return response.json()

    @classmethod
    def check_messaging_window(cls, comment_timestamp: datetime) -> bool:
        """
        24시간 메시징 윈도우 체크

        Args:
            comment_timestamp: 댓글 작성 시간

        Returns:
            True if within 24 hour window, False otherwise
        """
        from django.utils import timezone

        now = timezone.now()
        time_diff = now - comment_timestamp

        # 24시간 = 86400초
        return time_diff.total_seconds() < 86400


class SpamDetectionService:
    """
    스팸 댓글 감지 서비스
    """

    # 기본 스팸 키워드 (관리자 설정에서 추가 가능)
    DEFAULT_SPAM_KEYWORDS = [
        "아이돌",
        "주소창",
        "사건",
        "원본영상",
        "실시간검색",
    ]

    @classmethod
    def is_spam(
        cls, text: str, spam_keywords: list = None, check_urls: bool = True
    ) -> tuple[bool, list]:
        """
        댓글이 스팸인지 검사

        Args:
            text: 검사할 댓글 텍스트
            spam_keywords: 검사할 스팸 키워드 리스트
            check_urls: URL 포함 여부 검사

        Returns:
            (is_spam: bool, reasons: list) - 스팸 여부와 판정 이유 목록
        """
        if not text:
            return False, []

        text_lower = text.lower()
        reasons = []

        # 1. URL 검사
        if check_urls and cls._contains_url(text_lower):
            reasons.append("contains_url")

        # 2. 스팸 키워드 검사
        keywords_to_check = spam_keywords if spam_keywords else cls.DEFAULT_SPAM_KEYWORDS
        for keyword in keywords_to_check:
            if keyword.lower() in text_lower:
                reasons.append(f"keyword:{keyword}")

        return len(reasons) > 0, reasons

    @classmethod
    def _contains_url(cls, text: str) -> bool:
        """
        텍스트에 URL이 포함되어 있는지 검사
        """
        import re

        # HTTP/HTTPS URL 패턴
        url_pattern = r"https?://[^\s]+"
        if re.search(url_pattern, text):
            return True

        # 도메인 패턴 (예: example.com, site.co.kr)
        domain_pattern = r"\b[a-zA-Z0-9-]+\.(com|net|org|co\.kr|asia|io|app|xyz|info|biz)\b"
        if re.search(domain_pattern, text):
            return True

        return False


class InstagramCommentService:
    """
    Instagram 댓글 관리 서비스
    """

    GRAPH_API_BASE = f"https://graph.facebook.com/v24.0"

    @classmethod
    def hide_comment(cls, comment_id: str, access_token: str) -> Dict:
        """
        댓글 숨김 처리

        Args:
            comment_id: 숨길 댓글 ID
            access_token: Instagram Business Account 액세스 토큰

        Returns:
            API 응답 데이터

        Raises:
            requests.HTTPError: API 호출 실패 시
        """
        url = f"{cls.GRAPH_API_BASE}/{comment_id}"

        params = {
            "hide": "true",
            "access_token": access_token,
        }

        response = requests.post(url, params=params)
        response.raise_for_status()

        return response.json()

    @classmethod
    def unhide_comment(cls, comment_id: str, access_token: str) -> Dict:
        """
        댓글 숨김 해제

        Args:
            comment_id: 숨김 해제할 댓글 ID
            access_token: Instagram Business Account 액세스 토큰

        Returns:
            API 응답 데이터
        """
        url = f"{cls.GRAPH_API_BASE}/{comment_id}"

        params = {
            "hide": "false",
            "access_token": access_token,
        }

        response = requests.post(url, params=params)
        response.raise_for_status()

        return response.json()

    @classmethod
    def delete_comment(cls, comment_id: str, access_token: str) -> Dict:
        """
        댓글 삭제 (영구 삭제)

        Args:
            comment_id: 삭제할 댓글 ID
            access_token: Instagram Business Account 액세스 토큰

        Returns:
            API 응답 데이터
        """
        url = f"{cls.GRAPH_API_BASE}/{comment_id}"

        params = {"access_token": access_token}

        response = requests.delete(url, params=params)
        response.raise_for_status()

        return response.json()
