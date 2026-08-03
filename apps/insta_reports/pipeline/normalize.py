"""S1 정규화 — 공식 API + Apify 를 shortcode 로 병합해 canonical posts 생성.

감사 확정 규칙 (reference/design_audit.json):
- 조인 키 = shortcode (permalink 정규식 ↔ shortCode). id 필드는 소스 간 불일치.
- 조회수 단위 혼합 금지: videoPlayCount ≠ videoViewCount (1.5~11배 차이)
  → 계정별 다수(majority) 필드 하나만 대표 채택, 소수 단위 게시물은 views=None.
- likes/comments = 공식 우선 (Apify -1 숨김 복구, 단일 소스 원칙).
- 전수 목록 = 공식 (Apify 는 그리드 미노출 릴스 못 봄).
- timestamp 는 양소스 UTC → KST 파생 필수.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from . import config

KST = timezone(timedelta(hours=9))
SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([^/?]+)")
CTA_RE = re.compile(
    r"(댓글|코멘트|DM|디엠|남겨|남기|링크|프로필|팔로우|저장|공유|신청|무료|받아가|보내드릴|클릭|확인)"
)
CTA_KEYWORD_RE = re.compile(
    r"댓글[에로]?\s*[\"'‘’“”「]?([가-힣A-Za-z0-9!?]{1,12})[\"'‘’“”」]?\s*(?:남기|남겨|달|입력|적)"
)
HASHTAG_RE = re.compile(r"#([^\s#]+)")


def _parse_ts(v: str) -> datetime:
    v = v.replace("Z", "+00:00").replace("+0000", "+00:00")
    return datetime.fromisoformat(v)


def _shortcode(post: dict) -> str | None:
    m = SHORTCODE_RE.search(post.get("permalink") or post.get("url") or "")
    return m.group(1) if m else None


def _media_type(official: dict | None, apify: dict | None) -> str:
    v = (official or {}).get("media_type") or ""
    if v == "VIDEO":
        return "reel"
    if v == "IMAGE":
        return "image"
    if v == "CAROUSEL_ALBUM":
        return "carousel"
    t = (apify or {}).get("type") or ""
    return {"Video": "reel", "Image": "image", "Sidecar": "carousel"}.get(t, "unknown")


def _caption_features(caption: str) -> dict:
    caption = caption or ""
    first_line = caption.split("\n", 1)[0].strip()
    kw = CTA_KEYWORD_RE.search(caption)
    return {
        "length": len(caption),
        "first_line": first_line[:120],
        "hashtag_count": len(HASHTAG_RE.findall(caption)),
        "hashtags": [h.lower() for h in HASHTAG_RE.findall(caption)][:30],
        "has_cta": bool(CTA_RE.search(caption)),
        "cta_keyword": kw.group(1) if kw else "",
    }


def build_canonical(username: str) -> dict:
    off_doc = json.loads((config.RAW_DIR / f"{username}.json").read_text(encoding="utf-8"))
    api_path = config.APIFY_DIR / f"{username}.json"
    api_doc = (
        json.loads(api_path.read_text(encoding="utf-8")) if api_path.exists() else {"posts": []}
    )
    apify_posts = api_doc.get("posts") or api_doc.get("records") or []

    off_by_sc = {}
    for p in off_doc["posts"]:
        sc = _shortcode(p)
        if sc:
            off_by_sc[sc] = p
    api_by_sc = {}
    for p in apify_posts:
        sc = p.get("shortCode") or _shortcode(p)
        if sc:
            api_by_sc[sc] = p

    # ── 계정 대표 조회수 필드 결정 (단위 혼합 방지) ──
    n_play = sum(1 for p in api_by_sc.values() if (p.get("videoPlayCount") or 0) > 0)
    n_view = sum(1 for p in api_by_sc.values() if (p.get("videoViewCount") or 0) > 0)
    views_field = "videoPlayCount" if n_play >= n_view else "videoViewCount"

    posts = []
    for sc in {**off_by_sc, **{k: None for k in api_by_sc}}:
        off, api = off_by_sc.get(sc), api_by_sc.get(sc)
        ts = _parse_ts(off["timestamp"]) if off else _parse_ts(api["timestamp"])
        ts_kst = ts.astimezone(KST)
        caption = (off or {}).get("caption") or (api or {}).get("caption") or ""

        likes = None
        if off is not None:
            likes = off.get("like_count")
        if likes is None and api is not None and (api.get("likesCount") or -1) >= 0:
            likes = api.get("likesCount")

        comments = (off or {}).get("comments_count")
        if comments is None and api is not None:
            comments = api.get("commentsCount")

        views = None
        if api is not None:
            v = api.get(views_field)
            views = int(v) if v and v > 0 else None

        mt = _media_type(off, api)
        # 댓글 원천: **Graph(무료·전량) 우선**, 없으면 Apify latestComments 폴백.
        # Apify 는 게시물당 2~10개만 준다(실측) → 그것만 쓰면 팔로워 인사이트가 전체의 2% 표본이
        # 된다. Graph 는 우리 계정의 자기 게시물이라 무료로 다 받을 수 있다(collect_official).
        comments_sample = []
        graph_comments = (off or {}).get("comments") or []
        if graph_comments:
            for c in graph_comments:
                txt = (c.get("text") or "").strip()
                if not txt:
                    continue
                owner = (c.get("username") or "").lower()
                comments_sample.append(
                    {
                        "id": str(c.get("id") or f"{sc}:{len(comments_sample)}"),
                        "text": txt[:300],
                        "owner": owner,
                        # username 필드가 거부된 계정은 owner 가 빈 문자열이 된다 → 그때는
                        # 본인 댓글을 못 걸러내지만, 분류 단계에서 'other' 로 흡수된다.
                        "is_owner": bool(owner) and owner == username.lower(),
                        "likes": int(c.get("like_count") or 0),
                    }
                )
        elif api is not None:
            for c in (api.get("latestComments") or [])[:15]:
                txt = (c.get("text") or "").strip()
                if not txt:
                    continue
                owner = (c.get("ownerUsername") or "").lower()
                comments_sample.append(
                    {
                        "id": str(c.get("id") or f"{sc}:{len(comments_sample)}"),
                        "text": txt[:300],
                        "owner": owner,
                        "is_owner": owner == username.lower(),
                        "likes": int(c.get("likesCount") or 0),
                    }
                )
        posts.append(
            {
                "shortcode": sc,
                "permalink": (off or {}).get("permalink") or (api or {}).get("url") or "",
                "media_type": mt,
                "taken_at_utc": ts.isoformat(),
                "taken_at_kst": ts_kst.isoformat(),
                "kst_hour": ts_kst.hour,
                "kst_dow": ts_kst.weekday(),  # 0=월
                "caption": caption,
                "caption_features": _caption_features(caption),
                "likes": likes,
                "comments": comments,
                "views": views if mt == "reel" else None,
                "comments_sample": comments_sample,
                "video_local": (
                    str(config.MEDIA_DIR / f"{sc}.mp4")
                    if (config.MEDIA_DIR / f"{sc}.mp4").exists()
                    else None
                ),
                "thumb_local": (
                    str(config.MEDIA_DIR / f"{sc}_thumb.jpg")
                    if (config.MEDIA_DIR / f"{sc}_thumb.jpg").exists()
                    else None
                ),
                "in_official": off is not None,
                "in_apify": api is not None,
            }
        )

    posts.sort(key=lambda p: p["taken_at_utc"], reverse=True)
    meta = off_doc.get("account_meta", {})
    out = {
        "schema_version": 1,
        "username": username,
        "account": {
            "username": username,
            "name": meta.get("name", ""),
            "biography": meta.get("biography", ""),
            "followers": meta.get("followers_count"),
            "posts_total": meta.get("media_count"),
            "profile_picture_url": meta.get("profile_picture_url", ""),
            # IG 서명 URL 만료 대비 — 우리 스토리지 캐시본(render 가 1차 실패 시 사용)
            "profile_picture_fallback_url": meta.get("profile_picture_fallback_url", ""),
        },
        "views_field": views_field,
        "fetched_at_official": off_doc.get("fetched_at"),
        "fetched_at_apify": api_doc.get("fetched_at"),
        "posts": posts,
    }
    outp = config.RUNS_DIR / username / "posts.json"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
