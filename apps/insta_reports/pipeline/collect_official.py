"""S1-a 공식 수집 — IG Graph API 로 계정 메타 + 최근 게시물 목록 + **댓글**.

호출 수(계정당): 계정 메타 1 + 미디어 페이지 2~4 (limit=50) + 댓글 페이지 최대
``MAX_COMMENT_REQUESTS``.

⚠️ **댓글은 Graph 로 받는다(2026-08-04 전환).** 예전에는 "Apify 가 게시물별로 함께 주므로
Graph 댓글 호출은 하지 않는다"였는데, 실측하니 Apify `latestComments` 는 게시물당 **2~10개**
뿐이었다(진용진 13게시물: Apify 86개 vs 실제 `commentsCount` 합계 **3,797개** — 약 2%).
그 2% 로 "팔로워 인사이트"를 만들고 있었다. 연동된 계정의 **자기 게시물 댓글은 Graph 로
무료·전량 조회**가 되므로(토큰은 이미 있다) Apify 는 조회수 전용으로 되돌린다.

산출물: ``RAW_DIR/{username}.json`` — 랩 `scripts/fetch_ig_posts.py` 와 동일 스키마 +
게시물마다 ``comments: [...]`` 추가(파이프라인 normalize.py 가 이 형태를 기대한다).
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

# ── 댓글 (Graph, 무료) ───────────────────────────────────────────────
COMMENT_FIELDS = "id,text,timestamp,like_count,username"
# username/like_count 가 거부되는 계정 유형이 있어 축소 세트로 1회 재시도한다.
COMMENT_FIELDS_MIN = "id,text,timestamp"
COMMENT_PAGE = 50  # Graph 최대
# 게시물 1개에서 가져올 상한. 한 바이럴 게시물이 예산을 다 먹지 않게 한다.
COMMENTS_PER_POST_MAX = 120
# 리포트 1건 전체 상한. 분류는 50개/콜이라 1,200개 = 24콜(≈1분) 수준.
COMMENTS_TOTAL_MAX = 1200
# 게시물이 아주 많은 계정에서도 breadth 를 보장하기 위한 게시물당 최소 할당.
COMMENTS_PER_POST_MIN = 20
# 레이트리밋 보호 — 이 횟수를 넘으면 남은 게시물은 건너뛴다.
MAX_COMMENT_REQUESTS = 80


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


def fetch_comments_for_post(media_id: str, token: str, want: int) -> list[dict]:
    """게시물 1개의 댓글 (최신순 페이지네이션). **fail-soft — 실패 시 []**.

    반환 원소: ``{id, text, timestamp, like_count, username}``.
    댓글이 막힌 게시물·삭제된 게시물·권한 거부는 조용히 빈 리스트다(리포트를 죽이지 않는다).
    """
    out: list[dict] = []
    fields, after, retried_min = COMMENT_FIELDS, None, False
    url = f"{GRAPH}/{media_id}/comments"
    while len(out) < want:
        params = {"fields": fields, "limit": min(COMMENT_PAGE, want - len(out))}
        params["access_token"] = token
        if after:
            params["after"] = after
        try:
            r = requests.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            logger.info(
                "insta_report: comments fetch failed media=%s %s", media_id, type(e).__name__
            )
            break
        if not r.ok:
            if not retried_min:  # 필드 거부 → 축소 세트로 1회 재시도
                retried_min, fields = True, COMMENT_FIELDS_MIN
                continue
            logger.info("insta_report: comments rejected %s media=%s", r.status_code, media_id)
            break
        body = r.json() or {}
        data = body.get("data") or []
        out.extend(data)
        paging = body.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after") if paging.get("next") else None
        if not after or not data:
            break
    return out[:want]


def fetch_comments(posts: list[dict], token: str) -> dict:
    """게시물 목록 전체의 댓글을 예산 안에서 수집 → ``{media_id: [comment, ...]}``.

    배분 규칙:
      · 게시물당 할당 = `총예산 // 게시물수` 를 [MIN, MAX] 로 클램프 → **breadth 보장**.
        (최신순으로 무제한 담으면 게시물 100개 계정에서 앞 10개가 예산을 다 먹는다.)
      · 최신 게시물부터 채운다 — '지금 팔로워의 목소리'가 인사이트의 목적이다.
      · 댓글 0개 게시물은 호출을 아예 하지 않는다.
      · 총 요청 수 상한(MAX_COMMENT_REQUESTS)으로 레이트리밋을 보호한다.
    """
    with_comments = [p for p in posts if (p.get("comments_count") or 0) > 0 and p.get("id")]
    if not with_comments:
        return {}

    per_post = max(
        COMMENTS_PER_POST_MIN,
        min(COMMENTS_PER_POST_MAX, COMMENTS_TOTAL_MAX // len(with_comments)),
    )
    # 최신순 (Graph media 목록이 이미 최신순이지만 명시해 둔다)
    with_comments.sort(key=lambda p: p.get("timestamp") or "", reverse=True)

    result: dict[str, list[dict]] = {}
    total, requests_used = 0, 0
    for p in with_comments:
        if total >= COMMENTS_TOTAL_MAX or requests_used >= MAX_COMMENT_REQUESTS:
            break
        want = min(per_post, p.get("comments_count") or per_post, COMMENTS_TOTAL_MAX - total)
        if want <= 0:
            continue
        got = fetch_comments_for_post(p["id"], token, want)
        requests_used += max(1, -(-want // COMMENT_PAGE))  # 페이지 수 추정
        if got:
            result[p["id"]] = got
            total += len(got)
        time.sleep(0.15)  # 레이트리밋 여유
    logger.info(
        "insta_report: comments collected %s개 (게시물 %s/%s, 게시물당 상한 %s, 요청 ~%s)",
        total,
        len(result),
        len(with_comments),
        per_post,
        requests_used,
    )
    return result


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

    # 댓글 — 무료(Graph). 실패해도 리포트는 만든다(팔로워 인사이트만 표본 부족으로 표시).
    comments_by_media = fetch_comments(posts, token)
    comments_collected = 0
    for p in posts:
        cs = comments_by_media.get(p.get("id")) or []
        if cs:
            p["comments"] = cs
            comments_collected += len(cs)

    doc = {
        "username": username,
        "external_account_id": ig_user_id,
        "fetched_at": timezone.now().isoformat(),
        "fields_fallback_used": fallback,
        "account_meta": meta,
        "post_count": len(posts),
        "comments_collected": comments_collected,
        "posts": posts,
    }
    out = config.RAW_DIR / f"{username}.json"
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return {
        "post_count": len(posts),
        "comments_collected": comments_collected,
        "followers_count": meta.get("followers_count"),
        "media_count": meta.get("media_count"),
        "name": meta.get("name") or "",
        "biography": meta.get("biography") or "",
        "fields_fallback_used": fallback,
    }
