"""
Instagram OAuth and Mock Provider services
"""

import requests
from django.conf import settings
from datetime import datetime, timedelta
from typing import Dict, Optional
from urllib.parse import urlencode
import secrets
import uuid


class InstagramOAuthService:
    """
    Service for Instagram API with Instagram Business Login

    Ref: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
    """

    # Instagram Business Login endpoints (NOT Facebook Login)
    AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
    TOKEN_URL = "https://api.instagram.com/oauth/access_token"
    LONG_LIVED_TOKEN_URL = "https://graph.instagram.com/access_token"
    REFRESH_TOKEN_URL = "https://graph.instagram.com/refresh_access_token"
    GRAPH_API_BASE = "https://graph.instagram.com"

    # Required scopes for Instagram Business Login
    # Ref: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
    REQUIRED_SCOPES = [
        "instagram_business_basic",
        "instagram_business_manage_comments",
        "instagram_business_manage_messages",
        # Optional:
        # "instagram_business_content_publish",
        # "instagram_business_manage_insights",
    ]

    @classmethod
    def get_instagram_app_id(cls) -> str:
        """Get Instagram App ID (from App Dashboard > Instagram > Business Login settings)"""
        # Prefer INSTAGRAM_APP_ID; fall back to META_APP_ID for backwards compat
        app_id = getattr(settings, "INSTAGRAM_APP_ID", "") or settings.META_APP_ID
        return app_id

    @classmethod
    def get_instagram_app_secret(cls) -> str:
        """Get Instagram App Secret"""
        app_secret = getattr(settings, "INSTAGRAM_APP_SECRET", "") or settings.META_APP_SECRET
        return app_secret

    @classmethod
    def get_authorization_url(cls, redirect_uri: str, state: str) -> str:
        """
        Generate Instagram Business Login authorization URL

        Uses https://www.instagram.com/oauth/authorize (NOT facebook.com)
        """
        params = {
            "client_id": cls.get_instagram_app_id(),
            "redirect_uri": redirect_uri,
            "scope": ",".join(cls.REQUIRED_SCOPES),
            "response_type": "code",
            "state": state,
        }

        query_string = urlencode(params)
        return f"{cls.AUTHORIZE_URL}?{query_string}"

    @classmethod
    def exchange_code_for_token(cls, code: str, redirect_uri: str) -> Dict:
        """
        Exchange authorization code for short-lived Instagram User access token

        POST https://api.instagram.com/oauth/access_token
        Returns: {"data": [{"access_token": "...", "user_id": "...", "permissions": "..."}]}
        """
        data = {
            "client_id": cls.get_instagram_app_id(),
            "client_secret": cls.get_instagram_app_secret(),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        }

        response = requests.post(cls.TOKEN_URL, data=data)
        response.raise_for_status()
        result = response.json()

        # Instagram API returns {"data": [{"access_token": ..., "user_id": ..., "permissions": ...}]}
        if "data" in result and result["data"]:
            return result["data"][0]
        return result

    @classmethod
    def get_long_lived_token(cls, short_lived_token: str) -> Dict:
        """
        Exchange short-lived token for long-lived token (60 days)

        GET https://graph.instagram.com/access_token
          ?grant_type=ig_exchange_token
          &client_secret=...
          &access_token=...
        """
        params = {
            "grant_type": "ig_exchange_token",
            "client_secret": cls.get_instagram_app_secret(),
            "access_token": short_lived_token,
        }

        response = requests.get(cls.LONG_LIVED_TOKEN_URL, params=params)
        response.raise_for_status()
        return response.json()

    @classmethod
    def refresh_long_lived_token(cls, long_lived_token: str) -> Dict:
        """
        Refresh a long-lived token for another 60 days

        GET https://graph.instagram.com/refresh_access_token
          ?grant_type=ig_refresh_token
          &access_token=...
        """
        params = {
            "grant_type": "ig_refresh_token",
            "access_token": long_lived_token,
        }

        response = requests.get(cls.REFRESH_TOKEN_URL, params=params)
        response.raise_for_status()
        return response.json()

    @classmethod
    def get_account_info(cls, access_token: str) -> Dict:
        """
        Get Instagram professional account info for the logged-in user

        GET https://graph.instagram.com/me?fields=user_id,username,name,profile_picture_url,account_type
        """
        url = f"{cls.GRAPH_API_BASE}/me"
        params = {
            "fields": "user_id,username,name,profile_picture_url,account_type",
            "access_token": access_token,
        }

        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()

    @classmethod
    def subscribe_to_webhooks(cls, ig_user_id: str, access_token: str, fields: str = "comments,messages") -> Dict:
        """
        Enable webhook subscriptions for an Instagram professional account.

        Must be called per account after OAuth connection.
        POST https://graph.instagram.com/v25.0/{ig_user_id}/subscribed_apps
          ?subscribed_fields=comments,messages
          &access_token=...
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/subscribed_apps"
        params = {
            "subscribed_fields": fields,
            "access_token": access_token,
        }

        response = requests.post(url, params=params)
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
    Instagram Messaging API Service (Meta Graph API v25.0)
    Instagram API with Instagram Login 기반 DM 발송 기능 제공
    """

    GRAPH_API_BASE = "https://graph.instagram.com/v25.0"

    @classmethod
    def _get_headers(cls, access_token: str) -> Dict:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    @classmethod
    def send_dm_via_comment(
        cls, ig_user_id: str, comment_id: str, message_text: str, access_token: str
    ) -> Dict:
        """
        댓글 ID를 통해 Private Reply DM 발송

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
        }

        response = requests.post(url, json=payload, headers=cls._get_headers(access_token))
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
        }

        response = requests.post(url, json=payload, headers=cls._get_headers(access_token))
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

    GRAPH_API_BASE = "https://graph.instagram.com/v25.0"

    @classmethod
    def _get_headers(cls, access_token: str) -> Dict:
        return {
            "Authorization": f"Bearer {access_token}",
        }

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

        params = {"hide": "true"}

        response = requests.post(url, params=params, headers=cls._get_headers(access_token))
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

        params = {"hide": "false"}

        response = requests.post(url, params=params, headers=cls._get_headers(access_token))
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

        response = requests.delete(url, headers=cls._get_headers(access_token))
        response.raise_for_status()

        return response.json()
