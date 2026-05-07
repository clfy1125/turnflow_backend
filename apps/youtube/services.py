"""
YouTube Data API v3 service layer.

Refs:
- https://developers.google.com/youtube/v3/docs
- https://developers.google.com/youtube/v3/guides/uploading_a_video
- https://developers.google.com/youtube/v3/docs/comments/setModerationStatus
- https://developers.google.com/youtube/v3/determine_quota_cost

OAuth (Google):
    Authorize:  https://accounts.google.com/o/oauth2/v2/auth
    Token:      POST https://oauth2.googleapis.com/token

Scopes used:
    https://www.googleapis.com/auth/youtube.upload      (videos.insert)
    https://www.googleapis.com/auth/youtube.force-ssl   (comments moderation)
    https://www.googleapis.com/auth/youtube.readonly    (channel info)
    https://www.googleapis.com/auth/userinfo.email      (display only)

Quota costs (per call, defaults to 10,000 units/day per project):
    videos.insert                     1,600
    commentThreads.list                   1
    comments.list                         1
    comments.setModerationStatus         50
    channels.list                         1
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import timedelta
from typing import Optional
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .models import YouTubeAccountConnection, YouTubeQuotaUsage, YouTubeVideoPost

logger = logging.getLogger(__name__)


class YouTubeAPIError(Exception):
    def __init__(self, message: str, code: str = "", response: dict = None):
        self.code = code
        self.response = response or {}
        super().__init__(message)


class YouTubeQuotaExceeded(YouTubeAPIError):
    """Raised before issuing a call when the daily quota guard would be exceeded."""


# ─────────────────────────────────────────────────────────────────────────────
# OAuth (Google)
# ─────────────────────────────────────────────────────────────────────────────

class YouTubeOAuthService:
    AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — public Google endpoint
    USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

    REQUIRED_SCOPES = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
    ]

    @classmethod
    def _client_id(cls) -> str:
        return settings.GOOGLE_OAUTH_CLIENT_ID

    @classmethod
    def _client_secret(cls) -> str:
        return settings.GOOGLE_OAUTH_CLIENT_SECRET

    @classmethod
    def get_authorization_url(cls, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": cls._client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(cls.REQUIRED_SCOPES),
            "state": state,
            # ``offline`` + ``consent`` ensures we always receive a refresh_token.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{cls.AUTHORIZE_URL}?{urlencode(params)}"

    @classmethod
    def exchange_code_for_token(cls, code: str, redirect_uri: str) -> dict:
        body = {
            "client_id": cls._client_id(),
            "client_secret": cls._client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        resp = requests.post(cls.TOKEN_URL, data=body, timeout=20)
        return _raise_or_return(resp, "GOOGLE_TOKEN_EXCHANGE_FAILED")

    @classmethod
    def refresh_access_token(cls, refresh_token: str) -> dict:
        body = {
            "client_id": cls._client_id(),
            "client_secret": cls._client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        resp = requests.post(cls.TOKEN_URL, data=body, timeout=20)
        return _raise_or_return(resp, "GOOGLE_TOKEN_REFRESH_FAILED")

    @classmethod
    def get_userinfo(cls, access_token: str) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(cls.USERINFO_URL, headers=headers, timeout=15)
        return _raise_or_return(resp, "GOOGLE_USERINFO_FAILED")

    @classmethod
    def get_my_channel(cls, access_token: str) -> dict:
        """Fetch the authenticated user's primary YouTube channel."""
        url = "https://www.googleapis.com/youtube/v3/channels"
        params = {"part": "snippet", "mine": "true"}
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        data = _raise_or_return(resp, "YOUTUBE_CHANNELS_LIST_FAILED")
        items = data.get("items", [])
        if not items:
            raise YouTubeAPIError(
                "No YouTube channel found for this Google account",
                "NO_CHANNEL",
                response=data,
            )
        return items[0]


def _raise_or_return(resp: requests.Response, error_code: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise YouTubeAPIError(
            f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:500]}", error_code,
        )
    if resp.status_code >= 400:
        msg = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        raise YouTubeAPIError(msg, error_code, response=data)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Token freshness
# ─────────────────────────────────────────────────────────────────────────────

def ensure_fresh_token(connection: YouTubeAccountConnection) -> bool:
    """Refresh the access token if it's expired or about to expire."""
    if not connection.is_token_expired():
        return False
    if not connection.refresh_token:
        connection.mark_as_error("Refresh token missing; user must re-authenticate")
        return False

    if MockYouTubeProvider.is_mock_mode():
        bundle = MockYouTubeProvider.refresh_access_token(connection.refresh_token)
    else:
        bundle = YouTubeOAuthService.refresh_access_token(connection.refresh_token)

    connection.access_token = bundle["access_token"]
    if bundle.get("refresh_token"):
        connection.refresh_token = bundle["refresh_token"]
    if bundle.get("expires_in"):
        connection.token_expires_at = timezone.now() + timedelta(seconds=bundle["expires_in"])
    connection.status = YouTubeAccountConnection.Status.ACTIVE
    connection.save()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Video upload (videos.insert)
# ─────────────────────────────────────────────────────────────────────────────

VIDEOS_INSERT_QUOTA_COST = 1600


class YouTubeUploadService:
    """Wraps ``videos.insert`` with the resumable upload protocol."""

    @classmethod
    def upload(
        cls, connection: YouTubeAccountConnection, post: YouTubeVideoPost
    ) -> dict:
        """
        Synchronous upload + insert. Caller (Celery task) is responsible for
        running this off the request thread.

        Uses ``google-api-python-client``. The dependency is imported lazily so
        the rest of the app (and Mock mode) can run even when the package is
        not installed in the current environment.
        """
        if not post.video_file_path:
            raise YouTubeAPIError("video_file_path is required for FILE_UPLOAD", "INVALID_INPUT")

        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError as e:
            raise YouTubeAPIError(
                f"google-api-python-client is required for production uploads: {e}",
                "MISSING_DEPENDENCY",
            )

        creds = Credentials(
            token=connection.access_token,
            refresh_token=connection.refresh_token,
            token_uri=YouTubeOAuthService.TOKEN_URL,
            client_id=YouTubeOAuthService._client_id(),
            client_secret=YouTubeOAuthService._client_secret(),
            scopes=YouTubeOAuthService.REQUIRED_SCOPES,
        )

        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

        body = {
            "snippet": {
                "title": post.title[:100],
                "description": post.description,
                "tags": list(post.tags or []),
                "categoryId": post.category_id,
            },
            "status": {
                "privacyStatus": post.privacy_status,
                "selfDeclaredMadeForKids": post.made_for_kids,
            },
        }

        media = MediaFileUpload(
            post.video_file_path, chunksize=-1, resumable=True, mimetype="video/*",
        )
        request = youtube.videos().insert(
            part=",".join(body.keys()), body=body, media_body=media, notifySubscribers=False,
        )

        try:
            response = request.execute()
        except HttpError as e:
            raise YouTubeAPIError(
                f"YouTube videos.insert failed: {e}", "VIDEOS_INSERT_FAILED",
                response={"raw": str(e.content)[:500] if hasattr(e, "content") else str(e)},
            )
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Quota guard
# ─────────────────────────────────────────────────────────────────────────────

def check_quota_or_raise(units: int) -> None:
    """
    Pre-flight quota check. Raises ``YouTubeQuotaExceeded`` if today's used+units
    would exceed the configured daily quota. Also accounts for the requested call.
    """
    daily_limit = getattr(settings, "YOUTUBE_DAILY_QUOTA", 10000)
    used = YouTubeQuotaUsage.units_used_today()
    if used + units > daily_limit:
        raise YouTubeQuotaExceeded(
            f"YouTube daily quota would be exceeded: used={used}, requested={units}, "
            f"limit={daily_limit}",
            "QUOTA_EXCEEDED",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Mock provider
# ─────────────────────────────────────────────────────────────────────────────

class MockYouTubeProvider:
    @staticmethod
    def is_mock_mode() -> bool:
        return getattr(settings, "YOUTUBE_MOCK_MODE", True)

    @staticmethod
    def generate_authorization_url(redirect_uri: str, state: str) -> str:
        params = {"code": f"mock_yt_code_{secrets.token_hex(8)}", "state": state}
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}{urlencode(params)}"

    @staticmethod
    def exchange_code_for_token(code: str) -> dict:
        return {
            "access_token": f"ya29.mock_{secrets.token_hex(16)}",
            "refresh_token": f"1//mock_{secrets.token_hex(16)}",
            "expires_in": 3600,
            "scope": " ".join(YouTubeOAuthService.REQUIRED_SCOPES),
            "token_type": "Bearer",
        }

    @staticmethod
    def refresh_access_token(refresh_token: str) -> dict:
        return {
            "access_token": f"ya29.mock_refreshed_{secrets.token_hex(16)}",
            "expires_in": 3600,
            "scope": " ".join(YouTubeOAuthService.REQUIRED_SCOPES),
            "token_type": "Bearer",
        }

    @staticmethod
    def get_userinfo(access_token: str) -> dict:
        return {
            "sub": f"mock_google_uid_{access_token[-8:]}",
            "email": "mock-creator@example.com",
            "email_verified": True,
        }

    @staticmethod
    def get_my_channel(access_token: str) -> dict:
        return {
            "id": f"UC{uuid.uuid4().hex[:22]}",
            "snippet": {
                "title": "Mock YouTube Channel",
                "description": "Mock channel for development",
                "thumbnails": {
                    "default": {"url": "https://placehold.co/88x88"},
                },
            },
        }

    @staticmethod
    def videos_insert(post: YouTubeVideoPost) -> dict:
        return {
            "id": f"mock_yt_vid_{uuid.uuid4().hex[:11]}",
            "kind": "youtube#video",
            "snippet": {
                "title": post.title,
                "description": post.description,
                "categoryId": post.category_id,
            },
            "status": {"privacyStatus": post.privacy_status},
        }

    @staticmethod
    def comment_threads_list(video_id: str) -> dict:
        """A small canned set of comments for development workflows."""
        seed = uuid.uuid4().hex[:8]
        return {
            "items": [
                {
                    "id": f"thread_{seed}_1",
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "id": f"cmt_{seed}_1",
                            "snippet": {
                                "authorDisplayName": "CleanFan",
                                "authorChannelId": {"value": "UCmock_clean_fan"},
                                "textDisplay": "정말 잘 만든 영상이네요. 감사합니다!",
                                "moderationStatus": "published",
                            },
                        },
                    },
                },
                {
                    "id": f"thread_{seed}_2",
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "id": f"cmt_{seed}_2",
                            "snippet": {
                                "authorDisplayName": "Spammer",
                                "authorChannelId": {"value": "UCmock_spammer"},
                                "textDisplay": "Click http://spam.example/win for free crypto!",
                                "moderationStatus": "published",
                            },
                        },
                    },
                },
                {
                    "id": f"thread_{seed}_3",
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "id": f"cmt_{seed}_3",
                            "snippet": {
                                "authorDisplayName": "ShortenerSpam",
                                "authorChannelId": {"value": "UCmock_shortener"},
                                "textDisplay": "follow me bit.ly/abc123",
                                "moderationStatus": "published",
                            },
                        },
                    },
                },
            ]
        }

    @staticmethod
    def set_moderation_status(comment_ids: list, moderation_status: str, ban_author: bool) -> dict:
        return {
            "mock": True,
            "moderationStatus": moderation_status,
            "banAuthor": ban_author,
            "ids": list(comment_ids),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Comment moderation (real API)
# ─────────────────────────────────────────────────────────────────────────────

COMMENT_THREADS_LIST_QUOTA_COST = 1
COMMENT_SET_MODERATION_QUOTA_COST = 50


class YouTubeCommentService:
    """Wraps ``commentThreads.list`` and ``comments.setModerationStatus``."""

    @staticmethod
    def list_threads_by_video(connection: YouTubeAccountConnection, video_id: str) -> dict:
        """Fetch top-level threads for a video. 1 quota unit per call."""
        url = "https://www.googleapis.com/youtube/v3/commentThreads"
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "textFormat": "plainText",
        }
        headers = {"Authorization": f"Bearer {connection.access_token}"}
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        return _raise_or_return(resp, "YOUTUBE_COMMENT_THREADS_LIST_FAILED")

    @staticmethod
    def set_moderation_status(
        connection: YouTubeAccountConnection,
        comment_ids: list,
        moderation_status: str,
        *,
        ban_author: bool = False,
    ) -> dict:
        """50 quota units. ``moderation_status`` ∈ {heldForReview, published, rejected}."""
        if moderation_status not in ("heldForReview", "published", "rejected"):
            raise YouTubeAPIError(
                f"Invalid moderation status: {moderation_status}",
                "INVALID_MODERATION_STATUS",
            )
        # POST with no body; params in query string per Google docs.
        url = "https://www.googleapis.com/youtube/v3/comments/setModerationStatus"
        params = {
            "id": ",".join(comment_ids),
            "moderationStatus": moderation_status,
        }
        if ban_author and moderation_status == "rejected":
            params["banAuthor"] = "true"
        headers = {"Authorization": f"Bearer {connection.access_token}"}
        resp = requests.post(url, params=params, headers=headers, timeout=20)
        # Success response is HTTP 204 — return a synthetic dict.
        if resp.status_code == 204:
            return {"ok": True, "ids": list(comment_ids), "moderationStatus": moderation_status}
        return _raise_or_return(resp, "YOUTUBE_SET_MODERATION_FAILED")
