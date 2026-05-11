"""
TikTok Business API service layer.

Reference: https://business-api.tiktok.com/portal/docs

OAuth (Business Center / Advertiser)::

    Authorize: https://business-api.tiktok.com/portal/auth
               ?app_id=...&state=...&redirect_uri=...
    Token:     POST https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/
               body: {app_id, secret, auth_code}
    Advertiser
    discovery: GET  https://business-api.tiktok.com/open_api/v1.3/oauth2/advertiser/get/
               headers: {Access-Token: <token>}

Ad Comments (scope: ``Ad Comments``)::

    POST /open_api/v1.3/comment/list/                 (filter + paginate)
    POST /open_api/v1.3/comment/reference/            (ad/creative metadata for a batch)
    POST /open_api/v1.3/comment/status/update/        (HIDE / SHOW)
    POST /open_api/v1.3/comment/delete/               (own/brand comments only)
    POST /open_api/v1.3/comment/post/                 (reply)
    POST /open_api/v1.3/comment/task/create/          (bulk async job)
    GET  /open_api/v1.3/comment/task/check/           (job status)
    GET  /open_api/v1.3/comment/task/download/        (job result URL)

Blocked words (scope: ``Ad Comments``)::

    GET  /open_api/v1.3/blockedword/list/
    POST /open_api/v1.3/blockedword/check/
    POST /open_api/v1.3/blockedword/create/
    POST /open_api/v1.3/blockedword/update/
    POST /open_api/v1.3/blockedword/delete/
    POST /open_api/v1.3/blockedword/task/create/
    GET  /open_api/v1.3/blockedword/task/check/
    GET  /open_api/v1.3/blockedword/task/download/

All authenticated calls send the access token via the ``Access-Token`` HTTP
header (not ``Authorization: Bearer ...``).
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import timedelta
from typing import Iterable, Optional
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .models import TikTokAccountConnection

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────

class TikTokAPIError(Exception):
    """Raised on any unsuccessful TikTok Business API response."""

    def __init__(self, message: str, code: str = "", response: dict = None):
        self.code = code
        self.response = response or {}
        super().__init__(message)


# ─────────────────────────────────────────────────────────────────────────────
# OAuth (Business Center)
# ─────────────────────────────────────────────────────────────────────────────

BUSINESS_BASE = "https://business-api.tiktok.com"
BUSINESS_API_BASE = f"{BUSINESS_BASE}/open_api/v1.3"


class TikTokBusinessOAuthService:
    """OAuth + advertiser discovery against ``business-api.tiktok.com``."""

    AUTHORIZE_URL = f"{BUSINESS_BASE}/portal/auth"
    TOKEN_URL = f"{BUSINESS_API_BASE}/oauth2/access_token/"
    ADVERTISER_GET_URL = f"{BUSINESS_API_BASE}/oauth2/advertiser/get/"

    @classmethod
    def _app_id(cls) -> str:
        return settings.TIKTOK_BUSINESS_APP_ID

    @classmethod
    def _app_secret(cls) -> str:
        return settings.TIKTOK_BUSINESS_APP_SECRET

    @classmethod
    def get_authorization_url(cls, redirect_uri: str, state: str) -> str:
        params = {
            "app_id": cls._app_id(),
            "state": state,
            "redirect_uri": redirect_uri,
        }
        return f"{cls.AUTHORIZE_URL}?{urlencode(params)}"

    @classmethod
    def exchange_auth_code(cls, auth_code: str) -> dict:
        """
        Exchange ``auth_code`` for an access token.

        Response (data block)::

            {
              "access_token": "...",
              "advertiser_ids": ["1234567890"],
              "scope": [3, 17]
            }
        """
        body = {
            "app_id": cls._app_id(),
            "secret": cls._app_secret(),
            "auth_code": auth_code,
        }
        resp = requests.post(
            cls.TOKEN_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        return _unwrap(resp, "TIKTOK_TOKEN_EXCHANGE_FAILED")

    @classmethod
    def get_advertisers(cls, access_token: str) -> list:
        """List advertisers this token has been granted access to."""
        params = {
            "app_id": cls._app_id(),
            "secret": cls._app_secret(),
        }
        headers = {"Access-Token": access_token}
        resp = requests.get(
            cls.ADVERTISER_GET_URL, params=params, headers=headers, timeout=15,
        )
        data = _unwrap(resp, "TIKTOK_ADVERTISER_GET_FAILED")
        return data.get("list") or []


# ─────────────────────────────────────────────────────────────────────────────
# Generic request helper
# ─────────────────────────────────────────────────────────────────────────────

def _unwrap(resp: requests.Response, error_code: str) -> dict:
    """
    TikTok Business API success envelope::

        {"code": 0, "message": "OK", "data": {...}, "request_id": "..."}

    Anything else (``code != 0`` or HTTP ≥ 400) is raised as TikTokAPIError.
    """
    try:
        payload = resp.json()
    except ValueError:
        raise TikTokAPIError(
            f"Non-JSON TikTok response (HTTP {resp.status_code}): {resp.text[:500]}",
            error_code,
        )
    code = payload.get("code")
    if resp.status_code >= 400 or (code is not None and code != 0):
        raise TikTokAPIError(
            payload.get("message") or f"HTTP {resp.status_code}",
            str(code) if code is not None else error_code,
            response=payload,
        )
    return payload.get("data") or {}


def _auth_headers(connection: TikTokAccountConnection) -> dict:
    return {
        "Access-Token": connection.access_token,
        "Content-Type": "application/json",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ad Comments
# ─────────────────────────────────────────────────────────────────────────────

class TikTokAdCommentService:
    """Wraps the ``/comment/*`` endpoint family."""

    LIST_URL = f"{BUSINESS_API_BASE}/comment/list/"
    REFERENCE_URL = f"{BUSINESS_API_BASE}/comment/reference/"
    STATUS_UPDATE_URL = f"{BUSINESS_API_BASE}/comment/status/update/"
    DELETE_URL = f"{BUSINESS_API_BASE}/comment/delete/"
    POST_URL = f"{BUSINESS_API_BASE}/comment/post/"
    TASK_CREATE_URL = f"{BUSINESS_API_BASE}/comment/task/create/"
    TASK_CHECK_URL = f"{BUSINESS_API_BASE}/comment/task/check/"
    TASK_DOWNLOAD_URL = f"{BUSINESS_API_BASE}/comment/task/download/"

    # ── reads ───────────────────────────────────────────────────────────────

    @classmethod
    def list(
        cls,
        connection: TikTokAccountConnection,
        *,
        filtering: Optional[dict] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "page": page,
            "page_size": page_size,
        }
        if filtering:
            body["filtering"] = filtering
        resp = requests.post(
            cls.LIST_URL, json=body, headers=_auth_headers(connection), timeout=20,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_LIST_FAILED")

    @classmethod
    def reference(
        cls, connection: TikTokAccountConnection, *, comment_ids: Iterable[str]
    ) -> dict:
        """Resolve ad/creative metadata for a batch of comment ids."""
        body = {
            "advertiser_id": connection.external_account_id,
            "comment_ids": list(comment_ids),
        }
        resp = requests.post(
            cls.REFERENCE_URL, json=body, headers=_auth_headers(connection), timeout=20,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_REFERENCE_FAILED")

    # ── writes ──────────────────────────────────────────────────────────────

    @classmethod
    def set_status(
        cls,
        connection: TikTokAccountConnection,
        *,
        comment_ids: Iterable[str],
        action: str,  # "HIDE" or "SHOW"
    ) -> dict:
        if action not in ("HIDE", "SHOW"):
            raise TikTokAPIError(f"invalid action {action!r}", "INVALID_ACTION")
        body = {
            "advertiser_id": connection.external_account_id,
            "comment_ids": list(comment_ids),
            "action": action,
        }
        resp = requests.post(
            cls.STATUS_UPDATE_URL,
            json=body,
            headers=_auth_headers(connection),
            timeout=20,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_STATUS_UPDATE_FAILED")

    @classmethod
    def delete(
        cls, connection: TikTokAccountConnection, *, comment_ids: Iterable[str]
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "comment_ids": list(comment_ids),
        }
        resp = requests.post(
            cls.DELETE_URL, json=body, headers=_auth_headers(connection), timeout=20,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_DELETE_FAILED")

    @classmethod
    def post_reply(
        cls,
        connection: TikTokAccountConnection,
        *,
        parent_comment_id: str,
        text: str,
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "parent_comment_id": parent_comment_id,
            "text": text,
        }
        resp = requests.post(
            cls.POST_URL, json=body, headers=_auth_headers(connection), timeout=20,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_POST_FAILED")

    # ── bulk async task (large jobs) ────────────────────────────────────────

    @classmethod
    def task_create(
        cls,
        connection: TikTokAccountConnection,
        *,
        task_type: str,
        params: dict,
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "task_type": task_type,
            "params": params,
        }
        resp = requests.post(
            cls.TASK_CREATE_URL,
            json=body,
            headers=_auth_headers(connection),
            timeout=20,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_TASK_CREATE_FAILED")

    @classmethod
    def task_check(cls, connection: TikTokAccountConnection, *, task_id: str) -> dict:
        params = {
            "advertiser_id": connection.external_account_id,
            "task_id": task_id,
        }
        resp = requests.get(
            cls.TASK_CHECK_URL,
            params=params,
            headers=_auth_headers(connection),
            timeout=15,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_TASK_CHECK_FAILED")

    @classmethod
    def task_download(cls, connection: TikTokAccountConnection, *, task_id: str) -> dict:
        params = {
            "advertiser_id": connection.external_account_id,
            "task_id": task_id,
        }
        resp = requests.get(
            cls.TASK_DOWNLOAD_URL,
            params=params,
            headers=_auth_headers(connection),
            timeout=15,
        )
        return _unwrap(resp, "TIKTOK_COMMENT_TASK_DOWNLOAD_FAILED")


# ─────────────────────────────────────────────────────────────────────────────
# Blocked words
# ─────────────────────────────────────────────────────────────────────────────

class TikTokBlockedWordService:
    """Wraps the ``/blockedword/*`` endpoint family."""

    LIST_URL = f"{BUSINESS_API_BASE}/blockedword/list/"
    CHECK_URL = f"{BUSINESS_API_BASE}/blockedword/check/"
    CREATE_URL = f"{BUSINESS_API_BASE}/blockedword/create/"
    UPDATE_URL = f"{BUSINESS_API_BASE}/blockedword/update/"
    DELETE_URL = f"{BUSINESS_API_BASE}/blockedword/delete/"
    TASK_CREATE_URL = f"{BUSINESS_API_BASE}/blockedword/task/create/"
    TASK_CHECK_URL = f"{BUSINESS_API_BASE}/blockedword/task/check/"
    TASK_DOWNLOAD_URL = f"{BUSINESS_API_BASE}/blockedword/task/download/"

    @classmethod
    def list(
        cls,
        connection: TikTokAccountConnection,
        *,
        page: int = 1,
        page_size: int = 100,
    ) -> dict:
        params = {
            "advertiser_id": connection.external_account_id,
            "page": page,
            "page_size": page_size,
        }
        resp = requests.get(
            cls.LIST_URL, params=params, headers=_auth_headers(connection), timeout=15,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_LIST_FAILED")

    @classmethod
    def check(
        cls, connection: TikTokAccountConnection, *, words: Iterable[str]
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "words": list(words),
        }
        resp = requests.post(
            cls.CHECK_URL, json=body, headers=_auth_headers(connection), timeout=15,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_CHECK_FAILED")

    @classmethod
    def create(
        cls, connection: TikTokAccountConnection, *, words: Iterable[str]
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "blocked_words": list(words),
        }
        resp = requests.post(
            cls.CREATE_URL, json=body, headers=_auth_headers(connection), timeout=20,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_CREATE_FAILED")

    @classmethod
    def update(
        cls,
        connection: TikTokAccountConnection,
        *,
        items: list,  # [{id: ..., word: ...}, ...]
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "blocked_words": list(items),
        }
        resp = requests.post(
            cls.UPDATE_URL, json=body, headers=_auth_headers(connection), timeout=20,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_UPDATE_FAILED")

    @classmethod
    def delete(
        cls, connection: TikTokAccountConnection, *, ids: Iterable[str]
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "ids": list(ids),
        }
        resp = requests.post(
            cls.DELETE_URL, json=body, headers=_auth_headers(connection), timeout=20,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_DELETE_FAILED")

    # ── bulk tasks ──────────────────────────────────────────────────────────

    @classmethod
    def task_create(
        cls, connection: TikTokAccountConnection, *, task_type: str, params: dict
    ) -> dict:
        body = {
            "advertiser_id": connection.external_account_id,
            "task_type": task_type,
            "params": params,
        }
        resp = requests.post(
            cls.TASK_CREATE_URL,
            json=body,
            headers=_auth_headers(connection),
            timeout=20,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_TASK_CREATE_FAILED")

    @classmethod
    def task_check(cls, connection: TikTokAccountConnection, *, task_id: str) -> dict:
        params = {
            "advertiser_id": connection.external_account_id,
            "task_id": task_id,
        }
        resp = requests.get(
            cls.TASK_CHECK_URL,
            params=params,
            headers=_auth_headers(connection),
            timeout=15,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_TASK_CHECK_FAILED")

    @classmethod
    def task_download(cls, connection: TikTokAccountConnection, *, task_id: str) -> dict:
        params = {
            "advertiser_id": connection.external_account_id,
            "task_id": task_id,
        }
        resp = requests.get(
            cls.TASK_DOWNLOAD_URL,
            params=params,
            headers=_auth_headers(connection),
            timeout=15,
        )
        return _unwrap(resp, "TIKTOK_BLOCKEDWORD_TASK_DOWNLOAD_FAILED")


# ─────────────────────────────────────────────────────────────────────────────
# Mock provider — used when TIKTOK_MOCK_MODE=True
# ─────────────────────────────────────────────────────────────────────────────

class MockTikTokProvider:
    """In-process fake responses for development."""

    @staticmethod
    def is_mock_mode() -> bool:
        return getattr(settings, "TIKTOK_MOCK_MODE", True)

    # ── OAuth ───────────────────────────────────────────────────────────────

    @staticmethod
    def generate_authorization_url(redirect_uri: str, state: str) -> str:
        params = {"auth_code": f"mock_tt_code_{secrets.token_hex(8)}", "state": state}
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}{urlencode(params)}"

    @staticmethod
    def exchange_auth_code(auth_code: str) -> dict:
        adv_id = f"mock_adv_{uuid.uuid4().hex[:14]}"
        return {
            "access_token": f"mock_at_{secrets.token_hex(16)}",
            "advertiser_ids": [adv_id],
            "scope": ["AD_COMMENTS", "TIKTOK_ACCOUNTS"],
        }

    @staticmethod
    def get_advertisers(access_token: str) -> list:
        return [
            {
                "advertiser_id": f"mock_adv_{access_token[-10:]}",
                "advertiser_name": "Mock Advertiser",
                "bc_id": f"mock_bc_{access_token[-6:]}",
            }
        ]

    # ── Ad Comments ─────────────────────────────────────────────────────────

    @staticmethod
    def list_comments(advertiser_id: str) -> dict:
        seed = uuid.uuid4().hex[:8]
        return {
            "list": [
                {
                    "comment_id": f"tc_{seed}_1",
                    "ad_id": f"ad_{seed}",
                    "creative_id": f"cr_{seed}",
                    "text": "잘 봤어요! 너무 좋은 광고네요.",
                    "user_id": "mock_clean_fan",
                    "username": "CleanFan",
                    "status": "PUBLIC",
                },
                {
                    "comment_id": f"tc_{seed}_2",
                    "ad_id": f"ad_{seed}",
                    "creative_id": f"cr_{seed}",
                    "text": "follow me on bit.ly/spam now!",
                    "user_id": "mock_spammer",
                    "username": "Spammer",
                    "status": "PUBLIC",
                },
                {
                    "comment_id": f"tc_{seed}_3",
                    "ad_id": f"ad_{seed}",
                    "creative_id": f"cr_{seed}",
                    "text": "🔥🔥🔥",
                    "user_id": "mock_emoji",
                    "username": "EmojiBot",
                    "status": "PUBLIC",
                },
            ],
            "page_info": {"page": 1, "page_size": 20, "total_number": 3},
        }

    @staticmethod
    def set_comment_status(comment_ids: Iterable[str], action: str) -> dict:
        return {"mock": True, "comment_ids": list(comment_ids), "action": action}

    @staticmethod
    def delete_comments(comment_ids: Iterable[str]) -> dict:
        return {"mock": True, "deleted": list(comment_ids)}

    @staticmethod
    def post_reply(parent_comment_id: str, text: str) -> dict:
        return {
            "mock": True,
            "comment_id": f"reply_{uuid.uuid4().hex[:12]}",
            "parent_comment_id": parent_comment_id,
            "text": text,
        }

    # ── Blocked words ───────────────────────────────────────────────────────

    @staticmethod
    def list_blocked_words() -> dict:
        return {
            "list": [
                {"id": "bw_mock_1", "word": "스팸"},
                {"id": "bw_mock_2", "word": "광고"},
            ],
            "page_info": {"page": 1, "page_size": 100, "total_number": 2},
        }

    @staticmethod
    def check_blocked_words(words: Iterable[str]) -> dict:
        return {"results": [{"word": w, "blocked": False} for w in words]}

    @staticmethod
    def create_blocked_words(words: Iterable[str]) -> dict:
        return {
            "created": [
                {"id": f"bw_mock_{uuid.uuid4().hex[:8]}", "word": w}
                for w in words
            ]
        }

    @staticmethod
    def update_blocked_words(items: list) -> dict:
        return {"updated": list(items)}

    @staticmethod
    def delete_blocked_words(ids: Iterable[str]) -> dict:
        return {"deleted": list(ids)}


# ─────────────────────────────────────────────────────────────────────────────
# Token freshness
# ─────────────────────────────────────────────────────────────────────────────

def ensure_fresh_token(connection: TikTokAccountConnection) -> bool:
    """
    TikTok Business access tokens are typically long-lived (no expiry, unless
    revoked). If TikTok ever flips on token expiry (``token_expires_at`` set),
    we currently surface the issue by flipping the connection to ``EXPIRED``
    rather than auto-refreshing — Business API doesn't expose a standard refresh
    flow today.
    """
    if not connection.token_expires_at:
        return False
    if timezone.now() < connection.token_expires_at - timedelta(seconds=60):
        return False
    connection.status = TikTokAccountConnection.Status.EXPIRED
    connection.save(update_fields=["status", "updated_at"])
    return False
