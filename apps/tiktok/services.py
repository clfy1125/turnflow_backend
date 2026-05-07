"""
TikTok Content Posting API service layer.

Refs:
- https://developers.tiktok.com/doc/login-kit-manage-user-access-tokens
- https://developers.tiktok.com/doc/content-posting-api-get-started
- https://developers.tiktok.com/doc/content-posting-api-reference-direct-post

OAuth (v2):
    Authorize:  https://www.tiktok.com/v2/auth/authorize/
    Token:      POST https://open.tiktokapis.com/v2/oauth/token/

Content Posting (Direct Post):
    POST https://open.tiktokapis.com/v2/post/publish/creator_info/query/
    POST https://open.tiktokapis.com/v2/post/publish/video/init/
    POST https://open.tiktokapis.com/v2/post/publish/status/fetch/
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .models import TikTokAccountConnection, TikTokVideoPost

logger = logging.getLogger(__name__)


class TikTokAPIError(Exception):
    """Wraps an unsuccessful TikTok API response."""

    def __init__(self, message: str, code: str = "", response: dict = None):
        self.code = code
        self.response = response or {}
        super().__init__(message)


# ─────────────────────────────────────────────────────────────────────────────
# OAuth (Login Kit)
# ─────────────────────────────────────────────────────────────────────────────

class TikTokOAuthService:
    """OAuth helpers for ``developers.tiktok.com`` Login Kit (v2)."""

    AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
    TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
    REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
    USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"

    REQUIRED_SCOPES = [
        "user.info.basic",
        "video.publish",
        "video.upload",
    ]

    @classmethod
    def _client_key(cls) -> str:
        return settings.TIKTOK_CLIENT_KEY

    @classmethod
    def _client_secret(cls) -> str:
        return settings.TIKTOK_CLIENT_SECRET

    @classmethod
    def get_authorization_url(cls, redirect_uri: str, state: str) -> str:
        params = {
            "client_key": cls._client_key(),
            "scope": ",".join(cls.REQUIRED_SCOPES),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{cls.AUTHORIZE_URL}?{urlencode(params)}"

    @classmethod
    def exchange_code_for_token(cls, code: str, redirect_uri: str) -> dict:
        """
        Exchange authorization code for an access + refresh token bundle.

        Response (on success)::

            {
              "access_token": "...",
              "expires_in": 86400,
              "open_id": "...",
              "refresh_expires_in": 31536000,
              "refresh_token": "...",
              "scope": "user.info.basic,video.publish",
              "token_type": "Bearer"
            }
        """
        body = {
            "client_key": cls._client_key(),
            "client_secret": cls._client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"}
        resp = requests.post(cls.TOKEN_URL, data=body, headers=headers, timeout=20)
        return _raise_or_return(resp, "TIKTOK_TOKEN_EXCHANGE_FAILED")

    @classmethod
    def refresh_access_token(cls, refresh_token: str) -> dict:
        body = {
            "client_key": cls._client_key(),
            "client_secret": cls._client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"}
        resp = requests.post(cls.TOKEN_URL, data=body, headers=headers, timeout=20)
        return _raise_or_return(resp, "TIKTOK_TOKEN_REFRESH_FAILED")

    @classmethod
    def get_user_info(cls, access_token: str) -> dict:
        params = {"fields": "open_id,union_id,avatar_url,display_name"}
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(cls.USER_INFO_URL, params=params, headers=headers, timeout=15)
        data = _raise_or_return(resp, "TIKTOK_USER_INFO_FAILED")
        # Real response shape: {"data": {"user": {...}}, "error": {...}}
        return (data.get("data") or {}).get("user", {})


# ─────────────────────────────────────────────────────────────────────────────
# Content Posting API
# ─────────────────────────────────────────────────────────────────────────────

class TikTokContentPostingService:
    """Direct Post flow for an authorized creator."""

    BASE = "https://open.tiktokapis.com/v2"
    CREATOR_INFO_URL = f"{BASE}/post/publish/creator_info/query/"
    VIDEO_INIT_URL = f"{BASE}/post/publish/video/init/"
    STATUS_FETCH_URL = f"{BASE}/post/publish/status/fetch/"

    @classmethod
    def query_creator_info(cls, connection: TikTokAccountConnection) -> dict:
        """
        Returns allowed privacy levels, max video duration, comment/duet/stitch policy
        for the authorized creator. **Required** by TikTok before invoking
        the publish endpoint — the client must show these options to the user.
        """
        headers = {
            "Authorization": f"Bearer {connection.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        resp = requests.post(cls.CREATOR_INFO_URL, headers=headers, timeout=15)
        return _raise_or_return(resp, "TIKTOK_CREATOR_INFO_FAILED").get("data", {})

    @classmethod
    def init_pull_from_url(
        cls,
        connection: TikTokAccountConnection,
        post: TikTokVideoPost,
    ) -> dict:
        """
        Initiate a Direct Post using ``PULL_FROM_URL``. TikTok will fetch the
        video from the URL itself; we don't have to upload chunks.

        The video URL must be hosted on a domain prefix the developer has
        verified in the TikTok app dashboard.
        """
        body = {
            "post_info": _post_info_payload(post),
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": post.video_url,
            },
        }
        headers = {
            "Authorization": f"Bearer {connection.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        resp = requests.post(cls.VIDEO_INIT_URL, json=body, headers=headers, timeout=30)
        return _raise_or_return(resp, "TIKTOK_VIDEO_INIT_FAILED")

    @classmethod
    def init_file_upload(
        cls,
        connection: TikTokAccountConnection,
        post: TikTokVideoPost,
        *,
        chunk_size: int = 10 * 1024 * 1024,
    ) -> dict:
        """
        Initiate a Direct Post using ``FILE_UPLOAD``. Returns ``upload_url`` to PUT
        chunks to. Caller is responsible for the actual byte transfer.
        """
        total_size = post.video_size_bytes
        if total_size <= 0:
            raise TikTokAPIError("video_size_bytes must be set for FILE_UPLOAD", "INVALID_SIZE")
        # TikTok requires chunk_size between 5MB and 64MB except for the last chunk.
        chunk_size = max(5 * 1024 * 1024, min(chunk_size, 64 * 1024 * 1024))
        total_chunk_count = max(1, (total_size + chunk_size - 1) // chunk_size)
        body = {
            "post_info": _post_info_payload(post),
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": total_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunk_count,
            },
        }
        headers = {
            "Authorization": f"Bearer {connection.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        resp = requests.post(cls.VIDEO_INIT_URL, json=body, headers=headers, timeout=30)
        return _raise_or_return(resp, "TIKTOK_VIDEO_INIT_FAILED")

    @classmethod
    def fetch_status(cls, connection: TikTokAccountConnection, publish_id: str) -> dict:
        body = {"publish_id": publish_id}
        headers = {
            "Authorization": f"Bearer {connection.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        resp = requests.post(cls.STATUS_FETCH_URL, json=body, headers=headers, timeout=15)
        return _raise_or_return(resp, "TIKTOK_STATUS_FETCH_FAILED").get("data", {})


def _post_info_payload(post: TikTokVideoPost) -> dict:
    """Build the ``post_info`` block. Caller has already enforced privacy clamping."""
    return {
        "title": post.caption[:2200],
        "privacy_level": post.effective_privacy,
        "disable_duet": post.disable_duet,
        "disable_comment": post.disable_comment,
        "disable_stitch": post.disable_stitch,
        "video_cover_timestamp_ms": 1000,
    }


def _raise_or_return(resp: requests.Response, error_code: str) -> dict:
    """Parse JSON, raise TikTokAPIError on TikTok-side ``error.code`` != 'ok'."""
    try:
        data = resp.json()
    except ValueError:
        raise TikTokAPIError(
            f"Non-JSON TikTok response (HTTP {resp.status_code}): {resp.text[:500]}",
            error_code,
        )

    err = data.get("error") or {}
    code = (err.get("code") or "").lower()
    if resp.status_code >= 400 or (code and code != "ok"):
        msg = err.get("message") or f"HTTP {resp.status_code}"
        raise TikTokAPIError(msg, code or error_code, response=data)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Privacy clamping (MVP gate)
# ─────────────────────────────────────────────────────────────────────────────

def effective_privacy_for(connection: TikTokAccountConnection, requested: str) -> str:
    """
    Return the privacy level we will actually send to TikTok.

    Until ``connection.is_audited`` is True, TikTok forces every publish to
    SELF_ONLY. We mirror that on the server side so the user is told upfront
    instead of being silently overridden later.
    """
    if not connection.is_audited:
        return TikTokVideoPost.Privacy.SELF_ONLY.value
    return requested


# ─────────────────────────────────────────────────────────────────────────────
# Mock provider
# ─────────────────────────────────────────────────────────────────────────────

class MockTikTokProvider:
    """In-process mock for ``TIKTOK_MOCK_MODE=True`` development."""

    @staticmethod
    def is_mock_mode() -> bool:
        return getattr(settings, "TIKTOK_MOCK_MODE", True)

    @staticmethod
    def generate_authorization_url(redirect_uri: str, state: str) -> str:
        # Loop directly back to our callback with a fake code.
        params = {"code": f"mock_tiktok_code_{secrets.token_hex(8)}", "state": state}
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}{urlencode(params)}"

    @staticmethod
    def exchange_code_for_token(code: str) -> dict:
        return {
            "access_token": f"mock_at_{secrets.token_hex(16)}",
            "refresh_token": f"mock_rt_{secrets.token_hex(16)}",
            "expires_in": 86400,
            "refresh_expires_in": 365 * 24 * 3600,
            "open_id": f"mock_openid_{uuid.uuid4().hex[:12]}",
            "scope": ",".join(TikTokOAuthService.REQUIRED_SCOPES),
            "token_type": "Bearer",
        }

    @staticmethod
    def get_user_info(access_token: str) -> dict:
        return {
            "open_id": f"mock_openid_{access_token[-8:]}",
            "union_id": f"mock_unionid_{access_token[-6:]}",
            "display_name": "Mock TikTok Creator",
            "avatar_url": "https://placehold.co/200x200",
        }

    @staticmethod
    def init_publish(post: TikTokVideoPost) -> dict:
        return {
            "data": {
                "publish_id": f"mock_publish_{uuid.uuid4().hex[:16]}",
                "upload_url": (
                    "https://mock-upload.tiktokapis.local/upload?id=" + uuid.uuid4().hex
                    if post.source_type == TikTokVideoPost.SourceType.FILE_UPLOAD
                    else ""
                ),
            }
        }

    @staticmethod
    def fetch_status(publish_id: str, *, force_published: bool = True) -> dict:
        # MVP: assume mock posts publish almost instantly.
        return {
            "publish_id": publish_id,
            "status": "PUBLISH_COMPLETE" if force_published else "PROCESSING_DOWNLOAD",
            "publicaly_available_post_id": [f"mock_video_{uuid.uuid4().hex[:14]}"]
            if force_published
            else [],
        }

    @staticmethod
    def list_comments(video_id: str) -> dict:
        seed = uuid.uuid4().hex[:8]
        return {
            "comments": [
                {
                    "id": f"tc_{seed}_1",
                    "text": "잘 봤어요!",
                    "user": {"open_id": "mock_clean_fan", "display_name": "CleanFan"},
                    "is_hidden": False,
                },
                {
                    "id": f"tc_{seed}_2",
                    "text": "follow me on bit.ly/spam now",
                    "user": {"open_id": "mock_spammer", "display_name": "Spammer"},
                    "is_hidden": False,
                },
                {
                    "id": f"tc_{seed}_3",
                    "text": "🔥🔥🔥",
                    "user": {"open_id": "mock_emoji", "display_name": "EmojiBot"},
                    "is_hidden": False,
                },
            ],
        }

    @staticmethod
    def hide_comment(comment_id: str) -> dict:
        return {"mock": True, "comment_id": comment_id, "is_hidden": True}


# ─────────────────────────────────────────────────────────────────────────────
# Organic comment moderation (Business API — Phase 2 placeholder)
# ─────────────────────────────────────────────────────────────────────────────
#
# Real organic-comment hide/unhide for TikTok requires a separate OAuth flow
# against ``business-api.tiktok.com``. We expose a thin interface here so the
# rest of the app (ViewSets, tasks) can be written today; Phase 2 swaps the
# real implementation in once the Business app credentials clear approval.

class TikTokCommentService:
    """Stubbed wrapper for organic comment moderation."""

    @classmethod
    def list_comments(cls, connection: TikTokAccountConnection, video_id: str) -> dict:
        if MockTikTokProvider.is_mock_mode():
            return MockTikTokProvider.list_comments(video_id)
        raise TikTokAPIError(
            "Real TikTok organic comment listing requires Business API OAuth (Phase 2).",
            "NOT_IMPLEMENTED",
        )

    @classmethod
    def hide_comment(cls, connection: TikTokAccountConnection, comment_id: str) -> dict:
        if MockTikTokProvider.is_mock_mode():
            return MockTikTokProvider.hide_comment(comment_id)
        raise TikTokAPIError(
            "Real TikTok organic comment hide requires Business API OAuth (Phase 2).",
            "NOT_IMPLEMENTED",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Token refresh helper
# ─────────────────────────────────────────────────────────────────────────────

def ensure_fresh_token(connection: TikTokAccountConnection, *, leeway_seconds: int = 300) -> bool:
    """
    Refresh the connection's access token if it expires within ``leeway_seconds``.
    Returns True if a refresh happened.
    """
    if not connection.token_expires_at:
        return False
    if timezone.now() + timedelta(seconds=leeway_seconds) < connection.token_expires_at:
        return False
    if not connection.refresh_token:
        connection.mark_as_error("Refresh token missing; user must re-authenticate")
        return False

    if MockTikTokProvider.is_mock_mode():
        bundle = MockTikTokProvider.exchange_code_for_token("mock_refresh_loop")
    else:
        bundle = TikTokOAuthService.refresh_access_token(connection.refresh_token)

    connection.access_token = bundle["access_token"]
    if bundle.get("refresh_token"):
        connection.refresh_token = bundle["refresh_token"]
    if bundle.get("expires_in"):
        connection.token_expires_at = timezone.now() + timedelta(seconds=bundle["expires_in"])
    if bundle.get("refresh_expires_in"):
        connection.refresh_token_expires_at = timezone.now() + timedelta(
            seconds=bundle["refresh_expires_in"]
        )
    connection.status = TikTokAccountConnection.Status.ACTIVE
    connection.save()
    return True
