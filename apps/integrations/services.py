"""
Instagram OAuth and Mock Provider services
"""

import hashlib
import logging
import random
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ── H-9/M-22: 토큰·시크릿 마스킹 ──
# Meta Graph API 는 access_token 을 URL 쿼리로 받으므로, requests 의 HTTPError 문자열
# (``... for url: https://.../refresh_access_token?...&access_token=<TOKEN>``)에 토큰이
# 평문으로 들어간다. 로그·DB(error_message) 저장 전 반드시 scrub_secrets 를 통과시킬 것.
_SECRET_QS_RE = re.compile(
    r"(access_token|client_secret|authKey|billingKey|refresh_token|customerKey)=[^&\s\"']+",
    re.IGNORECASE,
)


def scrub_secrets(text) -> str:
    """URL 쿼리스트링/예외 문자열에서 토큰·시크릿 값을 ``name=***`` 로 마스킹."""
    return _SECRET_QS_RE.sub(r"\1=***", str(text))


def raise_for_status_clean(response) -> None:
    """requests.raise_for_status 를 감싸 예외 메시지에서 토큰 URL 을 제거 (H-9).

    상태코드만 남긴 예외로 교체하고 원본(URL 포함)은 ``from None`` 으로 체인에서 끊는다.
    호출부가 ``e.response`` 로 상태코드/본문에 접근하는 것은 그대로 유지된다.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise requests.HTTPError(
            f"Instagram Graph API error: {response.status_code}", response=response
        ) from None


# ── P3b: Meta Graph API 호출용 커넥션 풀 재사용 세션 ──
# 매 DM 발송마다 새 TCP+TLS 핸드셰이크를 만들지 않게 모듈 단위 Session 을 공유한다.
# 고동시성(초당 수백 발송)에서 핸드셰이크 CPU + ephemeral 포트 고갈(TIME_WAIT)을 줄인다.
# Retry 는 connect 만(read=0) — 요청 전송 후 재시도하지 않으므로 POST 중복 발송 위험 없음.
# urllib3 PoolManager 는 thread-safe → Celery threads 풀에서 안전, prefork 는 프로세스별 1개.
_http_session: Optional[requests.Session] = None
_http_session_lock = threading.Lock()


def get_http_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        with _http_session_lock:
            if _http_session is None:
                s = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=64,
                    pool_maxsize=256,
                    max_retries=Retry(
                        total=2,
                        connect=2,
                        read=0,
                        redirect=0,
                        backoff_factor=0.2,
                        status_forcelist=(),
                    ),
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _http_session = s
    return _http_session


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
    # NOTE: insights 권한은 현재 임시 비활성 (apps.insights 기능 출시 보류).
    # 활성화 시점에 아래 줄 주석 해제 + 기존 계정은 재연동(reconnect) 필요.
    REQUIRED_SCOPES = [
        "instagram_business_basic",
        "instagram_business_manage_comments",
        "instagram_business_manage_messages",
        # "instagram_business_manage_insights",  # 임시 비활성 — insights 출시 시 복원
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
    def exchange_code_for_token(cls, code: str, redirect_uri: str) -> dict:
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
    def get_long_lived_token(cls, short_lived_token: str) -> dict:
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
        raise_for_status_clean(response)
        return response.json()

    @classmethod
    def refresh_long_lived_token(cls, long_lived_token: str) -> dict:
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
        raise_for_status_clean(response)
        return response.json()

    @classmethod
    def get_account_info(cls, access_token: str) -> dict:
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
        raise_for_status_clean(response)
        return response.json()

    # 토큰이 확실히 죽었다고 볼 OAuth 에러코드 (190 만료/회수, 102 세션, 104/2500 토큰필요).
    OAUTH_DEAD_ERROR_CODES = (190, 102, 104, 2500)

    @classmethod
    def verify_token(cls, access_token: str) -> dict:
        """라이브 GET /me 로 액세스 토큰 생사를 판정한다 (verify-before-brick 과 동일 기준).

        Returns:
            {"valid": True|False|None, "error_code": int|None}
            - True : /me 2xx (살아있음)
            - False: /me 4xx + OAuth 사망 에러코드 (진짜 죽음)
            - None : 네트워크/타임아웃/5xx/애매 (판정 불가 — 보수적으로 살아있다 취급 X, 미확인)
        """
        try:
            resp = get_http_session().get(
                f"{cls.GRAPH_API_BASE}/me",
                params={"fields": "id", "access_token": access_token},
                timeout=10,
            )
        except requests.RequestException:
            return {"valid": None, "error_code": None}

        if resp.ok:
            return {"valid": True, "error_code": None}

        code = None
        if resp.status_code in (400, 401, 403):
            try:
                code = ((resp.json() or {}).get("error", {}) or {}).get("code")
            except ValueError:
                code = None
            if code in cls.OAUTH_DEAD_ERROR_CODES:
                return {"valid": False, "error_code": code}
        # 그 외 4xx/5xx/애매 → 판정 불가
        return {"valid": None, "error_code": code}

    @classmethod
    def subscribe_to_webhooks(
        cls, ig_user_id: str, access_token: str, fields: str = "comments,messages"
    ) -> dict:
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
        raise_for_status_clean(response)
        return response.json()

    @classmethod
    def get_webhook_subscriptions(cls, ig_user_id: str, access_token: str) -> dict:
        """
        Read current webhook field subscriptions for an Instagram professional account.

        GET https://graph.instagram.com/v25.0/{ig_user_id}/subscribed_apps?access_token=...
        구독 필드는 data[].subscribed_fields (버전별로 name/version dict 또는 문자열 list).
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/subscribed_apps"
        response = requests.get(url, params={"access_token": access_token}, timeout=10)
        raise_for_status_clean(response)
        return response.json()

    @classmethod
    def unsubscribe_webhooks(cls, ig_user_id: str, access_token: str) -> dict:
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
        raise_for_status_clean(response)
        try:
            return response.json()
        except ValueError:
            return {"success": True}

    @classmethod
    def parse_signed_request(cls, signed_request: str) -> Optional[dict]:
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
    def exchange_mock_code_for_token(cls, code: str) -> dict:
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
    def get_mock_long_lived_token(cls, short_lived_token: str) -> dict:
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
    def get_mock_account_info(cls, access_token: str) -> dict:
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

        # Mock profile picture — placehold.co 는 외부 다운로드 가능한 더미 이미지를 제공.
        # 실제 운영 전환 시 IG 가 준 서명된 CDN URL 로 대체됨.
        profile_picture_url = f"https://placehold.co/320x320/png?text=@{username}"

        return {
            "id": user_id,
            "user_id": user_id,
            "username": username,
            "name": f"Mock {username}",
            "account_type": "BUSINESS",
            "profile_picture_url": profile_picture_url,
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

    # ===== DM 캠페인 이전(마이그레이션) 분석용 mock 픽스처 =====
    # 실 Graph 호출 없이 dev/CI 에서 전체 파이프라인을 돌리기 위한 결정적 합성 데이터.
    # 연결(ig_user_id)/미디어(media_id) 시드로 sha1→Random 을 만들어, 재실행해도 동일 결과.
    # media_id 규약: "mm-{ig}-{i}"(일반) / "mm-{ig}-{i}-camp{k}"(캠페인형, k=키워드 인덱스).
    #   ig 는 언더스코어만 포함(하이픈 없음)이라 '-' split 으로 안전 파싱된다.
    _MOCK_KEYWORDS = ["링크", "정보", "신청", "자료", "가격"]
    _MOCK_DM_TEMPLATES = [
        "요청하신 {kw} 보내드려요! {url} 에서 확인해주세요 😊",
        "안녕하세요! {kw} 안내드립니다 👉 {url}",
        "감사합니다 :) 아래에서 {kw} 확인 가능해요 {url}",
    ]
    _MOCK_GENERIC_COMMENTS = [
        "좋아요",
        "예뻐요",
        "대박이에요",
        "멋져요 ㅎㅎ",
        "잘 봤습니다",
        "👍",
        "❤️",
        "화이팅",
        "오 신기하네요",
    ]
    _MOCK_MEDIA_POOL = 100  # 고정 풀 — media_page 는 앞 limit 개, conversations 는 전체에 정렬.

    @staticmethod
    def _mock_rng(seed_str: str) -> random.Random:
        """seed 문자열로 결정적 Random 생성 (Date.now/Math.random 미사용 재현성)."""
        h = hashlib.sha1(seed_str.encode("utf-8")).hexdigest()
        return random.Random(int(h[:16], 16))

    @staticmethod
    def _mock_graph_ts(dt) -> str:
        """Graph API 형식 timestamp('2026-05-07T03:14:15+0000')."""
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")

    @classmethod
    def _mock_campaign_media(cls, ig_user_id: str) -> list:
        """고정 크기(_MOCK_MEDIA_POOL) 합성 미디어 풀 — media_page/conversations 공유 진실.

        Returns: [{"index","id","is_campaign","kw_idx","timestamp"(aware dt)}, ...] (newest first).
        """
        rng = cls._mock_rng(f"media:{ig_user_id}")
        now = timezone.now()
        items = []
        for i in range(cls._MOCK_MEDIA_POOL):
            is_camp = rng.random() < 0.4
            kw_idx = rng.randrange(len(cls._MOCK_KEYWORDS))
            days_ago = i * (60.0 / cls._MOCK_MEDIA_POOL) + rng.uniform(0, 0.5)
            ts = now - timedelta(days=days_ago, hours=rng.uniform(0, 12))
            mid = f"mm-{ig_user_id}-{i}-camp{kw_idx}" if is_camp else f"mm-{ig_user_id}-{i}"
            items.append(
                {"index": i, "id": mid, "is_campaign": is_camp, "kw_idx": kw_idx, "timestamp": ts}
            )
        return items

    @classmethod
    def mock_list_media_page(
        cls, ig_user_id: str, limit: int = 50, after: str | None = None
    ) -> dict:
        """합성 미디어 목록 1페이지 (InstagramMediaService.list_media_page 대응 mock)."""
        pool = cls._mock_campaign_media(ig_user_id)[: max(1, min(limit, cls._MOCK_MEDIA_POOL))]
        data = []
        for m in pool:
            is_camp = m["is_campaign"]
            kw = cls._MOCK_KEYWORDS[m["kw_idx"]]
            rng = cls._mock_rng(f"cnt:{m['id']}")
            caption = (
                f'오늘 콘텐츠 준비했어요! 궁금하면 댓글에 "{kw}" 남겨주세요 :)'
                if is_camp
                else "오늘의 일상 기록 📷 봐주셔서 감사해요"
            )
            comments_count = rng.randint(30, 120) if is_camp else rng.randint(0, 10)
            data.append(
                {
                    "id": m["id"],
                    "caption": caption,
                    "timestamp": cls._mock_graph_ts(m["timestamp"]),
                    "media_type": "VIDEO" if is_camp and rng.random() < 0.5 else "IMAGE",
                    "media_product_type": "REELS" if is_camp and rng.random() < 0.5 else "FEED",
                    "permalink": f"https://www.instagram.com/p/{m['id']}/",
                    "comments_count": comments_count,
                }
            )
        return {"data": data, "paging_after": None}

    @classmethod
    def mock_list_media_comments(cls, media_id: str, after: str | None = None) -> dict:
        """합성 댓글 1페이지 (InstagramMediaService.list_media_comments 대응 mock).

        캠페인형 media("-camp{k}")는 키워드 댓글 70% + 일반 + 계정 본인 공개답글 몇 개,
        일반 media 는 소량의 일반 댓글만.
        """
        parts = media_id.split("-")
        # "mm-{ig}-{i}[-camp{k}]" — ig 는 하이픈 없음이라 parts[1] 이 ig.
        ig = parts[1] if len(parts) > 1 else "mock_ig_x"
        is_camp = "camp" in media_id
        kw_idx = 0
        if is_camp:
            for p in parts:
                if p.startswith("camp") and p[4:].isdigit():
                    kw_idx = int(p[4:]) % len(cls._MOCK_KEYWORDS)
        kw = cls._MOCK_KEYWORDS[kw_idx]
        rng = cls._mock_rng(f"cmt:{media_id}")
        now = timezone.now()
        total = rng.randint(30, 120) if is_camp else rng.randint(0, 10)
        data = []
        for j in range(total):
            r = rng.random()
            if is_camp and r < 0.7:
                text = rng.choice(
                    [kw, f"{kw}이요", f"{kw} 주세요", f"{kw} 부탁드려요", f"저도 {kw}!"]
                )
            else:
                text = rng.choice(cls._MOCK_GENERIC_COMMENTS)
            data.append(
                {
                    "id": f"c-{media_id}-{j}",
                    "text": text,
                    "username": f"user_{rng.randrange(100000)}",
                    "timestamp": cls._mock_graph_ts(now - timedelta(hours=rng.uniform(0, 72))),
                    "parent_id": None,
                    "from": {"id": f"igsid_{rng.randrange(10**9)}"},
                }
            )
        # 캠페인형엔 계정 본인 공개답글(대댓글) 몇 개 — owner-reply 신호 exercise.
        if is_camp:
            for k in range(rng.randint(1, 3)):
                data.append(
                    {
                        "id": f"c-{media_id}-self-{k}",
                        "text": rng.choice(
                            ["DM 보내드렸어요! 확인해주세요 :)", "디엠 확인 부탁드려요~"]
                        ),
                        "username": f"acct_{ig[-6:]}",
                        "timestamp": cls._mock_graph_ts(now - timedelta(hours=rng.uniform(0, 48))),
                        "parent_id": f"c-{media_id}-0",
                        "from": {"id": ig},
                    }
                )
        return {"data": data, "paging_after": None}

    @classmethod
    def mock_list_conversations(cls, ig_user_id: str, after: str | None = None) -> dict:
        """합성 대화 목록 1페이지 (InstagramMessagingService.list_conversations 대응 mock).

        캠페인형 미디어마다 ~10~15개 대화의 발신 메시지가 2~3개 템플릿을 재사용하고
        (min-support≥3 클러스터 형성), 발송 시각이 해당 미디어 timestamp 직후로 버스트.
        + 수동 상담형 노이즈 대화 소량.
        """
        media = cls._mock_campaign_media(ig_user_id)
        rng = cls._mock_rng(f"conv:{ig_user_id}")
        data = []
        conv_i = 0
        for m in media:
            if not m["is_campaign"]:
                continue
            kw = cls._MOCK_KEYWORDS[m["kw_idx"]]
            tmpl = cls._MOCK_DM_TEMPLATES[m["index"] % len(cls._MOCK_DM_TEMPLATES)]
            for _ in range(rng.randint(10, 15)):
                sent_at = m["timestamp"] + timedelta(
                    days=rng.uniform(0, 3), hours=rng.uniform(0, 12)
                )
                url = f"https://ex.co/{rng.randrange(10**6)}"
                out_msg = tmpl.format(kw=kw, url=url)
                recipient = f"igsid_{rng.randrange(10**9)}"
                data.append(
                    {
                        "id": f"conv-{ig_user_id}-{conv_i}",
                        "updated_time": cls._mock_graph_ts(sent_at),
                        "messages": {
                            "data": [
                                {
                                    "id": f"m-{conv_i}-out",
                                    "created_time": cls._mock_graph_ts(sent_at),
                                    "from": {"id": ig_user_id},
                                    "to": {"data": [{"id": recipient}]},
                                    "message": out_msg,
                                },
                                {
                                    "id": f"m-{conv_i}-in",
                                    "created_time": cls._mock_graph_ts(
                                        sent_at - timedelta(minutes=rng.uniform(1, 30))
                                    ),
                                    "from": {"id": recipient},
                                    "to": {"data": [{"id": ig_user_id}]},
                                    "message": kw,
                                },
                            ]
                        },
                    }
                )
                conv_i += 1
        # 수동 상담형 노이즈(발신 1개, 템플릿 아님) — 자기발송/클러스터 최소지지 미달로 제외돼야 정상.
        for _ in range(rng.randint(3, 6)):
            recipient = f"igsid_{rng.randrange(10**9)}"
            sent_at = timezone.now() - timedelta(days=rng.uniform(0, 30))
            data.append(
                {
                    "id": f"conv-{ig_user_id}-noise-{conv_i}",
                    "updated_time": cls._mock_graph_ts(sent_at),
                    "messages": {
                        "data": [
                            {
                                "id": f"m-noise-{conv_i}",
                                "created_time": cls._mock_graph_ts(sent_at),
                                "from": {"id": ig_user_id},
                                "to": {"data": [{"id": recipient}]},
                                "message": f"네 {rng.randrange(100)}번 주문 건은 오늘 발송했습니다. 감사합니다!",
                            }
                        ]
                    },
                }
            )
            conv_i += 1
        return {"data": data, "paging_after": None}


class InstagramMessagingService:
    """
    Instagram Messaging API Service (Meta Graph API v25.0)
    Instagram API with Instagram Login 기반 DM 발송 + 검증 기능 제공.

    99.9% 발송 보증을 위한 능동 검증(GET /{message_id}) 포함.
    """

    GRAPH_API_BASE = "https://graph.instagram.com/v25.0"
    DEFAULT_TIMEOUT = 10  # seconds

    @classmethod
    def _is_mock(cls) -> bool:
        """Mock 모드 여부 (DEBUG=True + INSTAGRAM_MOCK_MODE=True).

        InstagramMessagingService 는 InstagramOAuthService 를 상속하지 않으므로
        is_mock_mode 를 물려받지 못한다 → _post_message 의 cls._is_mock() 가
        과거 AttributeError 를 냈다. 동일 로직을 자체 정의해 보완.
        prod 는 DEBUG=False 라 항상 False (실제 Meta 호출).
        """
        return settings.DEBUG and getattr(settings, "INSTAGRAM_MOCK_MODE", True)

    @classmethod
    def _get_headers(cls, access_token: str) -> dict:
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
        quick_replies: Optional[list] = None,
        buttons: Optional[list] = None,
    ) -> dict:
        """
        Private Reply DM 발송 (POST /{ig_user_id}/messages, recipient.comment_id 사용)

        Args:
            quick_replies: 메시지 하단 inline 옵션. {"title", "payload"} 리스트. 최대 13개.
            buttons: 메시지 카드(generic template) 내부 버튼. 최대 3개.
                - postback: {"type":"postback","title","payload"} — 클릭 시 webhook postback (follow-gate).
                - web_url:  {"type":"web_url","title","url"} — 클릭 시 URL 열기 (링크 버튼).
                buttons 가 있으면 generic template 포맷으로 전송되어
                인스타 앱에서 "버튼이 박힌 메시지" 형태로 보인다.
                quick_replies 와 동시에 지정 시 buttons 우선.

        Returns:
            {"message_id": "...", "recipient_id": "...", "_raw": {...}}
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/messages"
        message = cls._build_message_payload(
            text=message_text, quick_replies=quick_replies, buttons=buttons
        )
        payload = {
            "recipient": {"comment_id": comment_id},
            "message": message,
        }
        return cls._post_message(url, payload, access_token)

    @classmethod
    def send_dm_via_user_id(
        cls,
        ig_user_id: str,
        recipient_id: str,
        message_text: str,
        access_token: str,
        quick_replies: Optional[list] = None,
        buttons: Optional[list] = None,
    ) -> dict:
        """사용자 ID 기반 DM 발송 (24h 윈도우 내에서만 허용)."""
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/messages"
        message = cls._build_message_payload(
            text=message_text, quick_replies=quick_replies, buttons=buttons
        )
        payload = {
            "recipient": {"id": recipient_id},
            "message": message,
        }
        return cls._post_message(url, payload, access_token)

    @classmethod
    def _build_message_payload(
        cls,
        *,
        text: str,
        quick_replies: Optional[list] = None,
        buttons: Optional[list] = None,
    ) -> dict:
        """Meta IG message 페이로드 빌드.

        - buttons 있음 → button template (text 640자 + postback/web_url 버튼 1~3개)
        - quick_replies 있음 → plain text + quick_replies
        - 둘 다 없음 → plain text
        발송 직전 방어 클립: 버튼 텍스트 640자, 일반 텍스트 UTF-8 1000바이트(멀티바이트 미분할).
        """
        if buttons:
            norm = cls._normalize_buttons(buttons)
            if norm:
                # Meta IG **button template**: text(640자) + 버튼 1~3개(postback/web_url).
                # generic(title 80자) 대신 button 을 써서 본문을 640자까지 보낼 수 있다.
                # (postback·web_url 모두 button template 지원 — Meta 공식 문서 확인)
                from .models import BUTTON_TEMPLATE_TEXT_MAX

                text_val = (text or " ").strip() or " "
                return {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "button",
                            "text": text_val[:BUTTON_TEMPLATE_TEXT_MAX],
                            "buttons": norm,
                        },
                    }
                }
        # 일반 텍스트 DM: Meta UTF-8 1000바이트 한도 → 발송 직전 바이트 안전 클립.
        # (시리얼라이저가 생성 시 막지만, 큐 적재 후 버튼 제거 등으로 포맷이 바뀌어 평문이 될 수 있어
        #  발송 지점에서도 2차 방어. errors="ignore" 로 끝의 잘린 멀티바이트 문자 제거.)
        from .models import DM_TEXT_MAX_BYTES

        message_text = text or ""
        encoded = message_text.encode("utf-8")
        if len(encoded) > DM_TEXT_MAX_BYTES:
            message_text = encoded[:DM_TEXT_MAX_BYTES].decode("utf-8", "ignore")
        message: dict = {"text": message_text}
        if quick_replies:
            qr = cls._normalize_quick_replies(quick_replies)
            if qr:
                message["quick_replies"] = qr
        return message

    @staticmethod
    def _normalize_quick_replies(buttons: list) -> list:
        """Meta v25 quick_replies 스키마로 변환. 최대 13개, title 20자."""
        out = []
        for b in buttons[:13]:
            if not isinstance(b, dict):
                continue
            title = str(b.get("title", "")).strip()
            payload_val = str(b.get("payload", "")).strip()
            if not title or not payload_val:
                continue
            out.append(
                {
                    "content_type": b.get("content_type", "text"),
                    "title": title[:20],
                    "payload": payload_val[:1000],
                }
            )
        return out

    @staticmethod
    def _normalize_buttons(buttons: list) -> list:
        """Meta generic template buttons 스키마로 변환. 최대 3개, title 20자.

        두 종류 지원:
          - postback: ``{"type":"postback","title","payload"}`` — 버튼 클릭 시 webhook 으로
            payload 가 돌아온다 (follow-gate 버튼). payload 없으면 제외.
          - web_url:  ``{"type":"web_url","title","url"}`` — 버튼 클릭 시 URL 을 연다 (링크 버튼).
            url 이 http/https 가 아니면 제외.
        """
        out = []
        for b in buttons[:3]:
            if not isinstance(b, dict):
                continue
            btype = str(b.get("type", "postback")).strip()
            title = str(b.get("title", "")).strip()
            if not title:
                continue
            if btype == "web_url":
                url = str(b.get("url", "")).strip()
                if not (url.startswith("http://") or url.startswith("https://")):
                    continue
                out.append({"type": "web_url", "title": title[:20], "url": url})
            else:
                payload_val = str(b.get("payload", "")).strip()
                if not payload_val:
                    continue
                out.append({"type": "postback", "title": title[:20], "payload": payload_val[:1000]})
        return out

    # ===== Follow-gate: 사용자가 비즈니스 계정을 팔로우 중인지 조회 =====

    @classmethod
    def check_user_follow_business(
        cls,
        igsid: str,
        access_token: str,
    ) -> Optional[bool]:
        """
        GET /v25.0/{IGSID}?fields=is_user_follow_business

        Meta IG User Profile API. 24h 메시징 윈도우 내인 IGSID(상호작용 이력이 있는
        사용자)에 한해 호출 가능. 응답의 `is_user_follow_business` 가 true 면
        해당 사용자는 비즈니스 계정을 팔로우 중.

        Returns:
            True/False  — Meta 가 명시적으로 응답한 경우
            None        — 필드 누락(권한 부족) / 호출 자체 실패 (호출부에서 보수적 처리)

        Raises:
            DMTokenError: code 190 등 토큰 무효 (즉시 처리 중단해야 함)
            DMTransientError: 5xx / 네트워크 (재시도 가능)
        """
        from .dm_exceptions import TOKEN_CODES, DMTokenError, DMTransientError

        if not igsid:
            return None

        url = f"{cls.GRAPH_API_BASE}/{igsid}"
        params = {"fields": "is_user_follow_business"}

        try:
            resp = get_http_session().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=cls.DEFAULT_TIMEOUT,
            )
        except requests.Timeout as e:
            raise DMTransientError(f"check_follow timeout: {e}") from e
        except requests.ConnectionError as e:
            raise DMTransientError(f"check_follow connection error: {e}") from e

        if resp.status_code == 404:
            # IGSID 가 더 이상 유효하지 않음 (사용자 삭제 등) — 보수적으로 None
            return None

        if not resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"message": resp.text}}
            err = body.get("error", {}) or {}
            code = err.get("code")
            msg = err.get("message", f"HTTP {resp.status_code}")
            if code in TOKEN_CODES:
                raise DMTokenError(msg, status=resp.status_code, code=code, api_response=body)
            if 500 <= resp.status_code < 600:
                raise DMTransientError(msg, status=resp.status_code, code=code, api_response=body)
            # 그 외 4xx — 권한 부족 / 잘못된 IGSID 등은 None 으로 fallback
            return None

        try:
            body = resp.json()
        except ValueError:
            return None
        if "is_user_follow_business" not in body:
            return None
        return bool(body.get("is_user_follow_business"))

    # ===== User Profile: IGSID → username 해석 (수신자 목록 표시용) =====

    @classmethod
    def get_user_profile(cls, igsid: str, access_token: str, fields=("name", "username")) -> dict:
        """
        GET /v25.0/{IGSID}?fields=name,username — IG User Profile API.

        Story 답장 messaging 웹훅은 sender IGSID 만 주고 username 이 없으므로, 표시용
        핸들은 이 API 로 별도 해석해야 한다(댓글 웹훅은 from.username 을 이미 포함).
        `check_user_follow_business` 와 동일 엔드포인트/권한(instagram_business_manage_messages)이며,
        사용자가 메시지를 보낸(consent) ~24h 윈도우 내 IGSID 에 한해 조회 가능.

        Returns:
            dict — Meta 응답 JSON. 권한부족/consent 없음/잘못된 IGSID/파싱실패 → {} (best-effort).

        Raises:
            DMTokenError: code 190 등 토큰 무효.
            DMTransientError: 5xx / 네트워크 (재시도 가능).
        """
        from .dm_exceptions import TOKEN_CODES, DMTokenError, DMTransientError

        if not igsid:
            return {}

        url = f"{cls.GRAPH_API_BASE}/{igsid}"
        params = {"fields": ",".join(fields)}

        try:
            resp = get_http_session().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=cls.DEFAULT_TIMEOUT,
            )
        except requests.Timeout as e:
            raise DMTransientError(f"get_user_profile timeout: {e}") from e
        except requests.ConnectionError as e:
            raise DMTransientError(f"get_user_profile connection error: {e}") from e

        if resp.status_code == 404:
            return {}

        if not resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"message": resp.text}}
            err = body.get("error", {}) or {}
            code = err.get("code")
            msg = err.get("message", f"HTTP {resp.status_code}")
            if code in TOKEN_CODES:
                raise DMTokenError(msg, status=resp.status_code, code=code, api_response=body)
            if 500 <= resp.status_code < 600:
                raise DMTransientError(msg, status=resp.status_code, code=code, api_response=body)
            # 그 외 4xx (consent 없음 / 필드 누락 / 잘못된 IGSID) → best-effort {}
            return {}

        try:
            return resp.json() or {}
        except ValueError:
            return {}

    @classmethod
    def resolve_username(cls, igsid: str, access_token: str) -> str:
        """
        IGSID → Instagram username 해석 (best-effort, 캐시 적용).

        - Mock 모드: 실제 호출 없이 결정적 가짜 핸들 반환(로컬/테스트 UX).
        - 캐시 `dm:uname:{igsid}`: 양성 7일 / 음성(실패·빈값) 1시간(윈도우 내 재시도 허용).
        - **모든 실패(토큰·5xx·consent 없음·네트워크)를 흡수하고 "" 반환** — 수신자 목록 표시가
          이 조회 때문에 절대 막히면 안 된다(호출부는 "" 를 표시용 폴백으로 처리).
        """
        if not igsid:
            return ""

        if cls._is_mock():
            return f"mock_user_{str(igsid)[:8]}"

        cache_key = f"dm:uname:{igsid}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached  # "" (음성 캐시 히트) 포함

        username = ""
        try:
            profile = cls.get_user_profile(igsid, access_token, fields=("username", "name"))
            username = (profile.get("username") or "").strip()
        except Exception as e:  # noqa: BLE001 — best-effort: 표시/발송 절대 미차단
            logger.debug("resolve_username failed igsid=%s err=%s", igsid, scrub_secrets(e))
            username = ""

        # 양성 7일 / 음성 1시간
        cache.set(cache_key, username, 7 * 24 * 3600 if username else 3600)
        return username

    @classmethod
    def _post_message(cls, url: str, payload: dict, access_token: str) -> dict:
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
            RETRIABLE_CODES,
            TOKEN_CODES,
            DMAnomalyError,
            DMApiError,
            DMInvalidParamError,
            DMRecipientUnreachableError,
            DMTokenError,
            DMTransientError,
            DMWindowExpiredError,
        )

        # Mock 모드: DEBUG=True + INSTAGRAM_MOCK_MODE=True 일 때만 (prod 는 DEBUG=False 라 절대 미동작).
        # 실제 Meta 호출 없이 가짜 message_id 반환 — 로컬/스테이징 DM 파이프라인 테스트·부하측정용.
        # (프로젝트 지침: INSTAGRAM_MOCK_MODE 로 발송 경로도 분기해야 함 — 기존 누락 보완.)
        if cls._is_mock():
            rcpt = payload.get("recipient", {}) or {}
            return {
                "message_id": f"mock_mid_{secrets.token_hex(8)}",
                "recipient_id": rcpt.get("comment_id") or rcpt.get("id") or "mock_recipient",
                "_raw": {"mock": True},
            }

        try:
            resp = get_http_session().post(
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
            # 토큰 / 세션 (190은 모든 subcode 포함, 102 세션) → 연결 브릭 대상
            if code in TOKEN_CODES:
                raise DMTokenError(msg, **kwargs)
            # code 200: 권한/수신자 단위 오류(예: subcode 2534066 "대상 ID 유효 확인").
            # 연결 전체 토큰 문제가 아니므로 개별 발송 실패(FAILED_NO_TRACE)로 처리한다(v3.3).
            if code == 200:
                raise DMRecipientUnreachableError(msg, **kwargs)
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
    def fetch_message(cls, message_id: str, access_token: str) -> Optional[dict]:
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
        from .dm_exceptions import TOKEN_CODES, DMApiError, DMTokenError, DMTransientError

        if not message_id:
            return None

        url = f"{cls.GRAPH_API_BASE}/{message_id}"
        params = {"fields": "id,created_time,from,to,message"}

        try:
            resp = get_http_session().get(
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
    def has_recent_message_to_recipient(
        cls, ig_user_id: str, recipient_id: str, access_token: str, since_seconds: int = 900
    ):
        """우리(page) → recipient 로 최근 since_seconds 내 보낸 메시지가 있는지 확인 (P6).

        message_id 없는 anomaly(200-no-msgid)·SUBMITTING 크래시 재발송 직전에
        '이미 보냈는지'를 Conversations API 로 확인해 중복 발송을 막는다.

        Returns:
            True  — 최근 발송 흔적 있음(재발송 금지)
            False — 흔적 없음(재발송해도 됨)
            None  — 확인 불가(API 에러/미지원) → 호출부가 보수적으로 판단
        """
        from datetime import timedelta

        from django.utils import timezone

        if not (ig_user_id and recipient_id):
            return None

        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/conversations"
        params = {
            "platform": "instagram",
            "user_id": str(recipient_id),
            "fields": "messages.limit(5){from,created_time}",
        }
        try:
            resp = get_http_session().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=cls.DEFAULT_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError):
            return None
        if not resp.ok:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None

        cutoff = timezone.now() - timedelta(seconds=since_seconds)
        for conv in body.get("data") or []:
            for m in ((conv.get("messages") or {}).get("data")) or []:
                frm = (m.get("from") or {}).get("id")
                if str(frm) != str(ig_user_id):
                    continue  # 우리(page)가 보낸 메시지만
                created = m.get("created_time") or ""
                dt = cls._parse_graph_time(created)
                if dt and dt >= cutoff:
                    return True
        return False

    @staticmethod
    def _parse_graph_time(value: str):
        """Graph API created_time('2026-06-26T03:14:15+0000') 파싱. 실패 시 None."""
        from datetime import datetime as _dt

        if not value:
            return None
        v = value.replace("+0000", "+00:00").replace("Z", "+00:00")
        try:
            return _dt.fromisoformat(v)
        except ValueError:
            return None

    @classmethod
    def list_conversations(
        cls,
        ig_user_id: str,
        access_token: str,
        limit: int = 25,
        after: str | None = None,
        message_limit: int = 20,
    ) -> dict:
        """대화 목록 1페이지 + 각 대화의 최근 메시지(네스티드) 조회 — DM 캠페인 이전 분석용.

        GET /{ig}/conversations?platform=instagram
            &fields=id,updated_time,messages.limit(N){id,created_time,from,to,message}&limit=25

        네스티드 ``messages`` 필드 문법은 ``has_recent_message_to_recipient``
        (messages.limit(5)) 에서 이미 검증됨. Meta 는 대화당 최근 ~20개 메시지 본문만
        제공하므로(오래된 최초 DM 누락 가능) 대화는 '표본'으로 다룬다.

        Returns:
            {"data": [{"id","updated_time","messages": {"data": [{"id","created_time",
                       "from","to","message"}, ...]}}, ...],
             "paging_after": "<cursor>" | None}

        Raises:
            requests.HTTPError — non-2xx (호출부가 code/subcode 로 분류; 권한 없음이면 스코프
                누락으로 판단해 DM 분석을 건너뛰고 partial 종결).
            requests.RequestException — 네트워크 오류.
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/conversations"
        params = {
            "platform": "instagram",
            "fields": (
                f"id,updated_time,messages.limit({message_limit})"
                "{id,created_time,from,to,message}"
            ),
            "limit": limit,
            "access_token": access_token,
        }
        if after:
            params["after"] = after
        resp = get_http_session().get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        raise_for_status_clean(resp)
        body = resp.json() or {}
        paging = body.get("paging") or {}
        after_cursor = (paging.get("cursors") or {}).get("after")
        return {
            "data": body.get("data", []) or [],
            "paging_after": after_cursor if paging.get("next") else None,
        }

    @classmethod
    def list_user_conversation(
        cls,
        ig_user_id: str,
        access_token: str,
        user_id: str,
        message_limit: int = 25,
    ) -> list:
        """특정 상대(user_id=IGSID)와의 대화 1건의 메시지 목록 조회 — 타겟 DM 복원용.

        GET /{ig}/conversations?platform=instagram&user_id={IGSID}
            &fields=messages.limit(N){id,created_time,from,to,message}

        댓글 작성자 IGSID(comments 스코프 from.id)와 메시징 user_id 가 **같은 ID 공간**임이
        실측 확인됨(2026-07-21, 겹침 11/20) → 캠페인 게시물 댓글러 → 그가 받은 발신 DM 을
        직접 복원(3만 개 대화방 페이징 불필요). 대화 없으면 빈 리스트.

        Returns:
            [{"id","created_time","from","to","message"}, ...]  (없으면 [])

        Raises:
            requests.HTTPError — non-2xx (호출부 분류), requests.RequestException — 네트워크.
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/conversations"
        params = {
            "platform": "instagram",
            "user_id": str(user_id),
            "fields": (f"id,messages.limit({message_limit})" "{id,created_time,from,to,message}"),
            "access_token": access_token,
        }
        resp = get_http_session().get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        raise_for_status_clean(resp)
        body = resp.json() or {}
        data = body.get("data") or []
        if not data:
            return []
        return (data[0].get("messages") or {}).get("data") or []

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
        raise_for_status_clean(resp)
        body = resp.json() or {}
        return body.get("data", []) or []

    @classmethod
    def list_media_page(
        cls,
        ig_user_id: str,
        access_token: str,
        limit: int = 50,
        after: str | None = None,
        fields: str = (
            "id,timestamp,media_type,media_product_type,caption,permalink,comments_count"
        ),
    ) -> dict:
        """미디어 목록 1페이지 (커서 페이지네이션) — DM 캠페인 이전 분석용.

        ``list_recent_media`` 와 달리 ``comments_count``·``media_product_type`` 를 포함하고
        커서를 반환한다. (list_recent_media 는 next_media baseline 스냅샷 전용이라 시그니처를
        건드리지 않는다 — 회귀 방지.)

        Returns:
            {"data": [{"id","timestamp","media_type","media_product_type","caption",
                       "permalink","comments_count"}, ...],
             "paging_after": "<cursor>" | None}

        Raises:
            requests.HTTPError — non-2xx (호출부가 code/subcode 로 분류·재시도/pause).
            requests.RequestException — 네트워크 오류.
        """
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/media"
        params = {"fields": fields, "limit": limit, "access_token": access_token}
        if after:
            params["after"] = after
        resp = get_http_session().get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        raise_for_status_clean(resp)
        body = resp.json() or {}
        paging = body.get("paging") or {}
        after_cursor = (paging.get("cursors") or {}).get("after")
        return {
            "data": body.get("data", []) or [],
            "paging_after": after_cursor if paging.get("next") else None,
        }

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
        raise_for_status_clean(resp)
        body = resp.json() or {}
        return body.get("data", []) or []

    @classmethod
    def get_media_timestamp(cls, media_id: str, access_token: str) -> "datetime | None":
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

    @classmethod
    def list_media_comments(
        cls,
        media_id: str,
        access_token: str,
        limit: int = 50,
        after: str | None = None,
        raise_on_error: bool = False,
    ) -> dict:
        """미디어의 최근 댓글을 1페이지 조회 (웹훅 누락 보정 폴링용).

        GET /v25.0/{media_id}/comments?fields=id,text,username,timestamp,parent_id,from

        Meta 사양 (v25.0): reverse-chronological(newest-first), 페이지당 최대 50,
        cursor 페이지네이션. 문서상 top-level 만 반환한다고 하나 **실측(2026-07-14)으로는
        계정 본인이 단 답글(공개답글 등)이 응답에 섞여 들어와** 셀프 DM 루프를 유발했다 →
        ``parent_id`` 를 요청해 호출부(_poll_one_media)가 대댓글을 걸러낸다.
        ``from``(작성자 id) 도 요청한다 — self-comment 필터에 필요한 건 정확히 '본인 토큰
        = 본인 댓글' 케이스라 걸러야 할 댓글에서는 IGSID 가 확정적으로 내려온다(username
        단독 비교는 계정 핸들 변경 시 stale 위험 — 적대적 리뷰 지적). 혹시 이 필드가
        400 을 유발하는 계정/버전이 있으면 축소 필드로 1회 재시도해 폴링 자체는 지킨다.

        Args:
            raise_on_error: True 면 실패를 삼키지 않고 requests 예외를 올린다 (DM 캠페인 이전
                수집기가 레이트리밋/토큰 오류를 분류해 pause/fail 하도록). 이때는 mock no-op
                도 건너뛴다(수집기가 mock 여부를 스스로 판단해 mock 픽스처를 호출하므로,
                이 경로로 오는 건 항상 실 API 호출이다). 기본 False = 폴링용 best-effort.

        Returns:
            {"data": [{"id","text","username","timestamp","parent_id"?,"from"?}, ...],
             "paging_after": "<cursor>" | None}   # 다음 페이지 없으면 None

        실패(타임아웃/4xx/5xx) 시 ``{"data": [], "paging_after": None}`` 반환
        (get_media_timestamp 와 동일한 방어적 처리 — 폴링은 best-effort). raise_on_error=True
        면 대신 requests.HTTPError/RequestException 을 올린다.
        """
        if not media_id:
            return {"data": [], "paging_after": None}

        # Mock 모드(dev): 실제 API 호출 없이 no-op. 테스트는 이 메서드를 직접 patch.
        # raise_on_error(마이그레이션 수집기)는 실 API 만 태우므로 no-op 을 건너뛴다.
        if MockInstagramProvider.is_mock_mode() and not raise_on_error:
            return {"data": [], "paging_after": None}

        url = f"{cls.GRAPH_API_BASE}/{media_id}/comments"
        params = {
            "fields": "id,text,username,timestamp,parent_id,from",
            "limit": limit,
            "access_token": access_token,
        }
        if after:
            params["after"] = after

        try:
            resp = requests.get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError):
            if raise_on_error:
                raise
            return {"data": [], "paging_after": None}

        if not resp.ok and resp.status_code == 400:
            # `from` 필드 미지원 응답 방어 — 필드 축소 후 1회 재시도 (폴링 무음사 방지).
            params["fields"] = "id,text,username,timestamp,parent_id"
            try:
                resp = requests.get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
            except (requests.Timeout, requests.ConnectionError):
                if raise_on_error:
                    raise
                return {"data": [], "paging_after": None}

        if not resp.ok:
            if raise_on_error:
                raise_for_status_clean(resp)
            return {"data": [], "paging_after": None}

        try:
            body = resp.json() or {}
        except ValueError:
            return {"data": [], "paging_after": None}

        paging = body.get("paging") or {}
        after_cursor = (paging.get("cursors") or {}).get("after")
        # paging.next 가 있을 때만 다음 페이지 존재 → after 커서 반환
        return {
            "data": body.get("data", []) or [],
            "paging_after": after_cursor if paging.get("next") else None,
        }

    @classmethod
    def list_comment_replies(
        cls,
        comment_id: str,
        access_token: str,
        limit: int = 50,
    ) -> dict:
        """댓글(스레드 루트)의 답글 1페이지 조회 (실패 DM 복구 재댓글 폴링용).

        GET /v25.0/{comment_id}/replies?fields=id,text,username,timestamp,from

        IG 답글은 2단계 평탄화 — 사용자가 우리 복구 안내 대댓글에 단 답글도 루트 댓글의
        replies edge 로 내려온다. 웹훅 유실 시 poll_recovery_recomments 가 이 edge 로
        RECOVERY_PENDING 스레드의 재댓글을 보정한다 (미디어 comments edge 는 문서상
        top-level only 라 답글 관측이 보장되지 않음).

        ``from`` 필드가 400 을 유발하는 계정/버전이 있으면 축소 필드로 1회 재시도
        (list_media_comments 와 동일 방어). 루트 댓글 삭제(code=33)도 400 이라 재시도가
        한 번 헛돌 수 있으나 폴링은 best-effort 라 허용.

        Returns:
            {"data": [{"id","text","username","timestamp","from"?}, ...],
             "paging_after": "<cursor>" | None}

        실패(타임아웃/4xx/5xx) 시 ``{"data": [], "paging_after": None}`` 반환.
        """
        if not comment_id:
            return {"data": [], "paging_after": None}

        # Mock 모드(dev): 실제 API 호출 없이 no-op. 테스트는 이 메서드를 직접 patch.
        if MockInstagramProvider.is_mock_mode():
            return {"data": [], "paging_after": None}

        url = f"{cls.GRAPH_API_BASE}/{comment_id}/replies"
        params = {
            "fields": "id,text,username,timestamp,from",
            "limit": limit,
            "access_token": access_token,
        }

        try:
            resp = requests.get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError):
            return {"data": [], "paging_after": None}

        if not resp.ok and resp.status_code == 400:
            # `from` 필드 미지원 응답 방어 — 필드 축소 후 1회 재시도.
            params["fields"] = "id,text,username,timestamp"
            try:
                resp = requests.get(url, params=params, timeout=cls.DEFAULT_TIMEOUT)
            except (requests.Timeout, requests.ConnectionError):
                return {"data": [], "paging_after": None}

        if not resp.ok:
            return {"data": [], "paging_after": None}

        try:
            body = resp.json() or {}
        except ValueError:
            return {"data": [], "paging_after": None}

        paging = body.get("paging") or {}
        after_cursor = (paging.get("cursors") or {}).get("after")
        return {
            "data": body.get("data", []) or [],
            "paging_after": after_cursor if paging.get("next") else None,
        }


class CommentReplyPermanentError(Exception):
    """
    공개 답글 게시 영구 실패 — 재시도해도 동일 결과.

    Meta v25 의 `POST /{comment_id}/replies` 호출이 다음 사유로 거부된 경우:
        - code=100/subcode=33: 댓글 삭제/접근 불가
        - code=100 (기타):     7일 윈도우 초과 등 잘못된 파라미터
        - code=190:            토큰 만료
        - code=200, 10:        권한 / 정책 위반

    `post_public_reply` task 가 이 예외를 잡으면 retry 없이 즉시 종결.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        status: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.status = status

    def __str__(self) -> str:
        return f"{self.message} (code={self.code} subcode={self.subcode} http={self.status})"


class InstagramCommentService:
    """
    Instagram 댓글 관리 서비스
    """

    GRAPH_API_BASE = "https://graph.instagram.com/v25.0"

    @classmethod
    def _get_headers(cls, access_token: str) -> dict:
        return {
            "Authorization": f"Bearer {access_token}",
        }

    # Meta 가 응답하는 영구 에러 코드 (재시도해도 동일 결과)
    # code=1   subcode=4928012/4928011: 활동 차단 (Action Block) — 스팸/봇 의심
    #          message="We restrict certain activity to protect our community"
    #          → is_transient=false. 인스타가 IG 계정 단위로 답글 활동 차단.
    #          몇 시간 ~ 24h 후 자동 해제. 그 사이에 계속 시도하면 차단 연장됨.
    # code=100 subcode=33: 댓글 삭제됨/접근 불가
    # code=100 (기타):     잘못된 파라미터 / 7일 윈도우 초과 등
    # code=200, 10:        권한 / 정책 위반
    # code=190:            토큰 만료
    _PERMANENT_REPLY_ERROR_CODES = (1, 100, 190, 200, 10)

    @classmethod
    def post_reply(cls, comment_id: str, message: str, access_token: str) -> dict:
        """
        댓글에 공개 답글(reply) 게시.

        POST https://graph.instagram.com/v25.0/{ig-comment-id}/replies?message=...

        Returns:
            {"id": "<reply_id>"} 형태의 응답

        Raises:
            CommentReplyPermanentError: 재시도해도 동일 결과 — 영구 실패
                (댓글 삭제, 권한 없음, 토큰 만료, 7일 윈도우 초과 등)
            requests.HTTPError: 그 외 transient 에러 — 재시도 가능
        """
        url = f"{cls.GRAPH_API_BASE}/{comment_id}/replies"
        params = {"message": message}

        response = requests.post(
            url,
            params=params,
            headers=cls._get_headers(access_token),
            timeout=10,
        )
        if response.ok:
            return response.json()

        # 4xx — Meta error 본문 파싱해서 영구/일시 분기
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text}}
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        code = err.get("code")
        subcode = err.get("error_subcode")
        msg = err.get("message") or f"HTTP {response.status_code}"

        if code in cls._PERMANENT_REPLY_ERROR_CODES:
            raise CommentReplyPermanentError(
                msg, code=code, subcode=subcode, status=response.status_code
            )
        # 5xx / rate limit 등은 그대로 HTTPError 로 raise → task 가 retry
        response.raise_for_status()
        return response.json()  # unreachable

    @classmethod
    def hide_comment(cls, comment_id: str, access_token: str) -> dict:
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
    def unhide_comment(cls, comment_id: str, access_token: str) -> dict:
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
    def delete_comment(cls, comment_id: str, access_token: str) -> dict:
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
