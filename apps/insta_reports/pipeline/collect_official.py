"""S1-a 공식 수집 — IG Graph API 로 계정 메타 + 최근 게시물 목록.

호출 수(계정당): 계정 메타 1 + 미디어 페이지 2~4 (limit=50) = **최대 5콜**.
댓글은 Apify 가 게시물별로 함께 주므로 Graph 댓글 호출은 하지 않는다(레이트리밋 절약).

산출물: ``RAW_DIR/{username}.json`` — 랩 `scripts/fetch_ig_posts.py` 와 동일 스키마
(파이프라인 normalize.py 가 이 형태를 기대한다. 스키마를 바꾸면 normalize 도 함께 고쳐야 함).
"""

from __future__ import annotations

import json
import logging
import time

import requests
from django.utils import timezone

from . import config

logger = logging.getLogger(__name__)

GRAPH = "https://graph.instagram.com/v25.0"
TARGET_POSTS = 100

MEDIA_FIELDS_RICH = (
    "id,timestamp,media_type,media_product_type,caption,permalink,"
    "comments_count,like_count,media_url,thumbnail_url,"
    "children{media_type,media_url,thumbnail_url,permalink}"
)
MEDIA_FIELDS_MIN = "id,timestamp,media_type,media_product_type,caption,permalink,comments_count"

ACCOUNT_FIELDS = (
    "user_id,username,name,account_type,media_count,"
    "followers_count,follows_count,profile_picture_url,biography"
)
# 위 필드 중 하나라도 거부되면 표시명만이라도 확보하기 위한 축소 세트.
ACCOUNT_FIELDS_MIN = "username,name,account_type,media_count"


class CollectError(RuntimeError):
    """수집 불가 — 토큰 만료/권한/네트워크. 잡은 실패하되 이용 횟수는 차감하지 않는다."""

    def __init__(self, message: str, *, token_invalid: bool = False):
        super().__init__(message)
        self.token_invalid = token_invalid


def fetch_account_meta(ig_user_id: str, token: str) -> dict:
    """계정 메타(팔로워·게시물 수·바이오).

    **절대 예외를 올리지 않는다(fail-soft).** 팔로워 수가 없어도 리포트는 만들 수 있고,
    필드 하나가 거부돼(계정 유형·권한 변경) 400 이 오는 경우까지 잡을 실패로 만들면
    리포트 전체가 죽는다. 토큰 유효성 판정은 `fetch_media`(실제로 필요한 데이터) 가 맡는다.
    거부되면 축소 필드로 1회 재시도해 표시명만이라도 확보한다.
    """
    for fields in (ACCOUNT_FIELDS, ACCOUNT_FIELDS_MIN):
        try:
            r = requests.get(
                f"{GRAPH}/{ig_user_id}",
                params={"fields": fields, "access_token": token},
                timeout=20,
            )
        except requests.RequestException as e:
            logger.warning("insta_report: account meta fetch failed: %s", type(e).__name__)
            return {}
        if r.ok:
            return r.json() or {}
        logger.info(
            "insta_report: account meta rejected %s (fields=%s…)", r.status_code, fields[:40]
        )
    return {}


def fetch_media(ig_user_id: str, token: str, target: int = TARGET_POSTS) -> tuple[list, bool]:
    """`GET /{ig_user_id}/media` 페이지네이션. (posts, 축소필드_사용여부)

    like_count/media_url 등이 거부되는 계정이 있어(권한·미디어 종류) 1회는 축소 필드로 재시도한다.
    """
    posts: list[dict] = []
    after, fields, used_fallback = None, MEDIA_FIELDS_RICH, False
    url = f"{GRAPH}/{ig_user_id}/media"
    while len(posts) < target:
        params = {"fields": fields, "limit": min(50, target - len(posts)), "access_token": token}
        if after:
            params["after"] = after
        try:
            r = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            raise CollectError(f"미디어 목록 조회 실패: {type(e).__name__}") from e
        if not r.ok:
            if not used_fallback:
                used_fallback, fields = True, MEDIA_FIELDS_MIN
                continue
            token_invalid = r.status_code in (400, 401, 403)
            raise CollectError(
                f"미디어 목록 조회 실패 {r.status_code}", token_invalid=token_invalid
            )
        body = r.json() or {}
        data = body.get("data") or []
        posts.extend(data)
        paging = body.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after") if paging.get("next") else None
        if not after or not data:
            break
        time.sleep(0.3)  # 레이트리밋 여유
    return posts[:target], used_fallback


def collect(
    username: str,
    ig_user_id: str,
    token: str,
    *,
    target: int = TARGET_POSTS,
    fallback_profile_url: str = "",
) -> dict:
    """공식 수집 실행 → RAW_DIR/{username}.json 기록 후 요약 반환.

    ``fallback_profile_url`` — IG 프로필 사진은 서명 URL 이라 만료·차단될 수 있다. 우리
    스토리지에 캐싱해 둔 안정 URL 을 함께 넘겨 두면 렌더가 1차 실패 시 이걸로 대체한다.
    """
    meta = fetch_account_meta(ig_user_id, token)
    if fallback_profile_url:
        meta = {**meta, "profile_picture_fallback_url": fallback_profile_url}
    posts, fallback = fetch_media(ig_user_id, token, target)
    if not posts:
        raise CollectError("게시물이 없습니다.")

    doc = {
        "username": username,
        "external_account_id": ig_user_id,
        "fetched_at": timezone.now().isoformat(),
        "fields_fallback_used": fallback,
        "account_meta": meta,
        "post_count": len(posts),
        "posts": posts,
    }
    out = config.RAW_DIR / f"{username}.json"
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return {
        "post_count": len(posts),
        "followers_count": meta.get("followers_count"),
        "media_count": meta.get("media_count"),
        "name": meta.get("name") or "",
        "biography": meta.get("biography") or "",
        "fields_fallback_used": fallback,
    }
