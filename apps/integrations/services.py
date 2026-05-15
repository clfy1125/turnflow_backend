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
    #
    # NOTE: insights 권한 추가 (apps.insights 앱).
    # 기존 계정은 재연동(reconnect) 없이는 insights API 호출이 403 으로 떨어진다.
    # 프론트에서 신규 권한 누락 계정에 reconnect 배너를 띄우는 동선 필요.
    REQUIRED_SCOPES = [
        "instagram_business_basic",
        "instagram_business_manage_comments",
        "instagram_business_manage_messages",
        "instagram_business_manage_insights",
        # Optional:
        # "instagram_business_content_publish",
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

    @classmethod
    def unsubscribe_webhooks(cls, ig_user_id: str, access_token: str) -> Dict:
        """
        Disable webhook subscriptions for an Instagram professional account.

        Called when user disconnects their account from our app.
        DELETE https://graph.instagram.com/v25.0/{ig_user_id}/subscribed_apps
          ?access_token=...

        Returns:
            API response, typically {"success": true}

        Raises:
            requests.HTTPError on non-2xx (호출자가 best-effort 처리)
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/subscribed_apps"
        params = {"access_token": access_token}

        response = requests.delete(url, params=params, timeout=10)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return {"success": True}

    @classmethod
    def parse_signed_request(cls, signed_request: str) -> Optional[Dict]:
        """
        Meta Deauthorize Callback 의 signed_request 파싱.

        포맷: <signature_base64url>.<payload_base64url>
        - HMAC-SHA256(app_secret, payload) == signature 검증
        - payload 는 base64url 디코딩 후 JSON

        Meta가 사용자가 Instagram 설정에서 우리 앱을 제거할 때 POST 호출:
            Content-Type: application/x-www-form-urlencoded
            Body: signed_request=<value>

        Returns:
            검증 통과 시 페이로드 dict (user_id 포함). 실패 시 None.

        Ref: https://developers.facebook.com/docs/games/gamesonfacebook/login#parsingsr
        """
        import base64
        import hashlib
        import hmac
        import json as _json

        if not signed_request or "." not in signed_request:
            return None

        try:
            encoded_sig, payload = signed_request.split(".", 1)
        except ValueError:
            return None

        app_secret = cls.get_instagram_app_secret()
        if not app_secret:
            return None

        # base64url decode (Meta 는 padding 생략)
        def _b64url_decode(data: str) -> bytes:
            data = data.replace("-", "+").replace("_", "/")
            data += "=" * ((4 - len(data) % 4) % 4)
            return base64.b64decode(data)

        try:
            sig = _b64url_decode(encoded_sig)
            payload_bytes = _b64url_decode(payload)
        except Exception:
            return None

        # HMAC 검증 — payload 의 base64url 문자열 자체에 HMAC
        expected_sig = hmac.new(
            app_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        if not hmac.compare_digest(sig, expected_sig):
            return None

        try:
            data = _json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return None

        # algorithm 필드 검증
        if data.get("algorithm") != "HMAC-SHA256":
            return None

        return data


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
    Instagram API with Instagram Login 기반 DM 발송 + 검증 기능 제공.

    99.9% 발송 보증을 위한 능동 검증(GET /{message_id}) 포함.
    """

    GRAPH_API_BASE = "https://graph.instagram.com/v25.0"
    DEFAULT_TIMEOUT = 10  # seconds

    @classmethod
    def _get_headers(cls, access_token: str) -> Dict:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ===== 멱등성 키 =====

    @staticmethod
    def build_idempotency_key(
        *, workspace_id, ig_user_id: str, comment_id: str, campaign_id
    ) -> str:
        """
        중복 발송 차단용 키 생성.

        sha256(workspace:ig:comment:campaign) → 동일 댓글에 동일 캠페인이 여러 번
        트리거돼도 같은 키가 나오므로 DB UNIQUE 제약으로 중복 차단.
        """
        import hashlib

        raw = f"{workspace_id}:{ig_user_id}:{comment_id}:{campaign_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ===== 발송 =====

    @classmethod
    def send_dm_via_comment(
        cls,
        ig_user_id: str,
        comment_id: str,
        message_text: str,
        access_token: str,
    ) -> Dict:
        """
        Private Reply DM 발송 (POST /{ig_user_id}/messages, recipient.comment_id 사용)

        Returns:
            {"message_id": "...", "recipient_id": "...", "_raw": {...}}

        Raises:
            DMTransientError: 네트워크 타임아웃 / 5xx (재시도 가능)
            DMTokenError / DMWindowExpiredError / DMInvalidParamError / DMApiError
            DMAnomalyError: 200인데 message_id 누락
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/messages"
        payload = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": message_text},
        }
        return cls._post_message(url, payload, access_token)

    @classmethod
    def send_dm_via_user_id(
        cls,
        ig_user_id: str,
        recipient_id: str,
        message_text: str,
        access_token: str,
    ) -> Dict:
        """
        사용자 ID 기반 DM 발송 (24h 윈도우 내에서만 허용)
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/messages"
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": message_text},
        }
        return cls._post_message(url, payload, access_token)

    @classmethod
    def _post_message(cls, url: str, payload: dict, access_token: str) -> Dict:
        """공통 POST + 응답 강검증 + v3.2 에러 분류

        v3.2 매핑 (Meta v25 검증됨):
            code 10 + subcode in (2534022, 2018278)  → DMWindowExpiredError
            code in (102, 190, 200)                  → DMTokenError
                (190의 모든 subcode 458/460/463/467 포함)
            code == 100                              → DMInvalidParamError
                (Private Reply 7일 초과 = subcode 2018292 포함)
            code == 551                              → DMRecipientUnreachableError
            code in (1, 2, 4, 17, 32, 368, 613) / 5xx → DMTransientError
            그 외 4xx                                → DMApiError (→ FAILED_NO_TRACE)
        """
        from .dm_exceptions import (
            DMAnomalyError,
            DMApiError,
            DMInvalidParamError,
            DMRecipientUnreachableError,
            DMTokenError,
            DMTransientError,
            DMWindowExpiredError,
            RETRIABLE_CODES,
            TOKEN_CODES,
        )

        try:
            resp = requests.post(
                url,
                json=payload,
                headers=cls._get_headers(access_token),
                timeout=cls.DEFAULT_TIMEOUT,
            )
        except requests.Timeout as e:
            raise DMTransientError(f"API timeout: {e}") from e
        except requests.ConnectionError as e:
            raise DMTransientError(f"Connection error: {e}") from e

        # 4xx/5xx: 에러 분류
        if not resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"message": resp.text}}

            err = body.get("error", {}) or {}
            code = err.get("code")
            subcode = err.get("error_subcode")
            msg = err.get("message", f"HTTP {resp.status_code}")

            kwargs = dict(
                status=resp.status_code,
                code=code,
                subcode=subcode,
                api_response=body,
            )

            # 24h 메시징 윈도우 만료 (subcode 2534022 또는 2018278)
            if code == 10 and subcode in (2534022, 2018278):
                raise DMWindowExpiredError(msg, **kwargs)
            # 토큰 / 세션 / 권한 (190은 모든 subcode 포함)
            if code in TOKEN_CODES:
                raise DMTokenError(msg, **kwargs)
            # 잘못된 파라미터 (Private Reply 7일 초과 포함)
            if code == 100:
                raise DMInvalidParamError(msg, **kwargs)
            # 수신자 도달 불가
            if code == 551:
                raise DMRecipientUnreachableError(msg, **kwargs)
            # rate limit / transient
            if code in RETRIABLE_CODES:
                raise DMTransientError(msg, **kwargs)
            # 5xx
            if 500 <= resp.status_code < 600:
                raise DMTransientError(msg, **kwargs)
            # 그 외 4xx — 분류 불가
            raise DMApiError(msg, **kwargs)

        # 2xx: 본문 강검증
        try:
            body = resp.json()
        except ValueError as e:
            raise DMAnomalyError(f"Non-JSON 200 body: {resp.text[:200]}") from e

        message_id = body.get("message_id")
        recipient_id = body.get("recipient_id")

        if not message_id or not recipient_id:
            raise DMAnomalyError(
                f"200 OK but missing fields: {body}",
                status=resp.status_code,
                api_response=body,
            )

        return {
            "message_id": message_id,
            "recipient_id": recipient_id,
            "_raw": body,
        }

    # ===== 능동 검증: GET /{message_id} =====

    @classmethod
    def fetch_message(cls, message_id: str, access_token: str) -> Optional[Dict]:
        """
        GET /v25.0/{message_id} 로 메시지 단건 조회 (Conversations API).

        99.9% 발송 보증의 2차 안전망. echo 웹훅이 누락된 경우 이 호출이
        Meta DB에 메시지가 실존하는지를 직접 확인한다.

        Args:
            message_id: POST /messages 응답의 message_id

        Returns:
            메시지 객체 (도착 확정), None (Meta가 404 반환 — 미발견)

        Raises:
            DMTokenError: code 190
            DMTransientError: 5xx / 네트워크 오류 (재시도 권장)
            DMApiError: 그 외 4xx
        """
        from .dm_exceptions import (
            DMApiError,
            DMTokenError,
            DMTransientError,
            TOKEN_CODES,
        )

        if not message_id:
            return None

        url = f"{cls.GRAPH_API_BASE}/{message_id}"
        params = {"fields": "id,created_time,from,to,message"}

        try:
            resp = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=cls.DEFAULT_TIMEOUT,
            )
        except requests.Timeout as e:
            raise DMTransientError(f"fetch_message timeout: {e}") from e
        except requests.ConnectionError as e:
            raise DMTransientError(f"fetch_message connection error: {e}") from e

        if resp.status_code == 404:
            return None

        if not resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"message": resp.text}}
            err = body.get("error", {}) or {}
            code = err.get("code")
            msg = err.get("message", f"HTTP {resp.status_code}")
            kwargs = dict(
                status=resp.status_code,
                code=code,
                subcode=err.get("error_subcode"),
                api_response=body,
            )
            if code in TOKEN_CODES:
                raise DMTokenError(msg, **kwargs)
            if 500 <= resp.status_code < 600:
                raise DMTransientError(msg, **kwargs)
            raise DMApiError(msg, **kwargs)

        try:
            return resp.json()
        except ValueError:
            return None

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


class InstagramMediaService:
    """
    Instagram Media 조회 서비스 (v3.4 — next_media 트리거 폴링용).

    GET /v25.0/{ig_user_id}/media?fields=id,timestamp,media_type,caption,permalink
    """

    GRAPH_API_BASE = "https://graph.instagram.com/v25.0"
    DEFAULT_TIMEOUT = 10

    @classmethod
    def list_recent_media(
        cls,
        ig_user_id: str,
        access_token: str,
        limit: int = 5,
        fields: str = "id,timestamp,media_type,caption,permalink",
    ) -> list:
        """
        최근 게시물 N건 조회 (timestamp DESC).

        v3.6: next_media 트리거가 webhook 기반으로 전환되어 일반 발송 흐름에선
              사용 안 함. baseline 스냅샷 등 보조 용도로 유지.

        Returns:
            [{"id": "...", "timestamp": "ISO8601", "media_type": "...", ...}, ...]

        Raises:
            requests.HTTPError on non-2xx
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/media"
        params = {
            "fields": fields,
            "limit": limit,
            "access_token": access_token,
        }
        resp = requests.get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        resp.raise_for_status()
        body = resp.json() or {}
        return body.get("data", []) or []

    @classmethod
    def list_stories(
        cls,
        ig_user_id: str,
        access_token: str,
        fields: str = (
            "id,media_type,media_url,media_product_type,"
            "permalink,timestamp,caption,thumbnail_url"
        ),
    ) -> list:
        """
        현재 활성 Story 목록 조회 (v3.7).

        GET /v25.0/{ig_user_id}/stories

        Meta 정책: Story는 24시간 동안만 active 상태이며, 만료 후엔
        이 엔드포인트에서 사라짐.

        Returns:
            [{"id": "...", "media_type": "IMAGE|VIDEO", "permalink": "...", ...}, ...]

        Raises:
            requests.HTTPError on non-2xx
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/stories"
        params = {"fields": fields, "access_token": access_token}
        resp = requests.get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        resp.raise_for_status()
        body = resp.json() or {}
        return body.get("data", []) or []

    @classmethod
    def get_media_timestamp(
        cls, media_id: str, access_token: str
    ) -> "datetime | None":
        """
        단일 미디어의 timestamp 조회 (v3.6 next_media webhook 검증용).

        GET /v25.0/{media-id}?fields=timestamp

        Args:
            media_id: Instagram 미디어 ID (webhook 의 media.id)

        Returns:
            datetime (timezone-aware) 또는 None (API 실패/404 시)

        Raises:
            requests.HTTPError on non-2xx (호출자에서 처리)
        """
        if not media_id:
            return None

        url = f"{cls.GRAPH_API_BASE}/{media_id}"
        params = {"fields": "timestamp", "access_token": access_token}

        try:
            resp = requests.get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError):
            return None

        if resp.status_code == 404:
            return None
        if not resp.ok:
            return None

        try:
            body = resp.json() or {}
        except ValueError:
            return None

        ts_raw = body.get("timestamp")
        if not ts_raw:
            return None

        # Meta 는 "2026-05-07T03:14:15+0000" 형식 (콜론 없음). ISO8601 로 정규화.
        from datetime import datetime as _dt

        v = ts_raw.replace("+0000", "+00:00").replace("Z", "+00:00")
        try:
            return _dt.fromisoformat(v)
        except ValueError:
            return None


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
    def post_reply(cls, comment_id: str, message: str, access_token: str) -> Dict:
        """
        댓글에 공개 답글(reply) 게시.

        POST https://graph.instagram.com/v25.0/{ig-comment-id}/replies
            ?message=...

        Args:
            comment_id: 답글을 달 부모 댓글 ID
            message:    답글 내용
            access_token: Instagram User Access Token

        Returns:
            {"id": "<reply_id>"} 형태의 응답

        Raises:
            requests.HTTPError: API 호출 실패 시
        """
        url = f"{cls.GRAPH_API_BASE}/{comment_id}/replies"
        params = {"message": message}

        response = requests.post(
            url,
            params=params,
            headers=cls._get_headers(access_token),
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

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
