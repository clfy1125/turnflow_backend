"""
Instagram Insights API 클라이언트 + 동기화 로직.

호출량 절감 원칙:
    - /me/media 한 번으로 50건씩 페이징 (1 호출 = 50 미디어 메타데이터)
    - /{media-id}/insights 는 1 미디어 = 1 호출 (Meta 가 batch 미지원).
      stale TTL 로 재호출 빈도 차등 (IGMedia.insight_stale_ttl 참조)
    - field selection 으로 단일 호출에 최대한 많은 필드를 받아옴
    - 인사이트 동기화는 항상 IGMedia.is_insights_fresh() == False 인 미디어만 대상

Meta API Reference:
    https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/
    https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import requests
from django.db import transaction
from django.utils import timezone

from apps.integrations.models import IGAccountConnection

from .models import IGAccountInsight, IGMedia, IGMediaInsight, MediaProductType, MediaType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 미디어 타입별 metric 카탈로그 (v25 기준)
# ─────────────────────────────────────────────────────────────
#
# Meta 가 미디어 타입별로 다른 metric set 을 받기 때문에 한 곳에 정리.
# 잘못된 metric 을 보내면 code=100 으로 거부됨.
#
# 참고: 2025-04 deprecate
#   - plays, clips_replays_count (대체: views)
#   - video_views (FEED) — 사용 안 함

METRICS_FEED = (
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
    "follows",
    "profile_visits",
    "profile_activity",
    "views",
)

METRICS_REELS = (
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
    "views",
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
)

METRICS_STORY = (
    "reach",
    "views",
    "shares",
    "total_interactions",
    "follows",
    "profile_visits",
    "profile_activity",
    "replies",
)


def select_metrics(product_type: str) -> tuple[str, ...]:
    """미디어 타입에 맞는 metric 튜플 반환."""
    if product_type == MediaProductType.REELS:
        return METRICS_REELS
    if product_type == MediaProductType.STORY:
        return METRICS_STORY
    return METRICS_FEED


# ─────────────────────────────────────────────────────────────
# 에러 분류
# ─────────────────────────────────────────────────────────────


class InsightsAPIError(Exception):
    """일반 IG Insights API 에러."""

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class InsightsPermissionError(InsightsAPIError):
    """403 / code=190 — 권한 누락 또는 토큰 만료. 사용자 재연동 필요."""


class InsightsTransientError(InsightsAPIError):
    """5xx / rate limit — 재시도 가능."""


# ─────────────────────────────────────────────────────────────
# Raw HTTP 클라이언트
# ─────────────────────────────────────────────────────────────


@dataclass
class MediaPage:
    items: list[dict]
    next_cursor: str | None


class IGInsightsClient:
    """
    Instagram Graph API 의 미디어/인사이트 엔드포인트 호출 래퍼.

    상태가 없는 classmethod 모음. 각 메서드는 raw dict 를 반환하고,
    DB 매핑은 sync 함수가 담당한다.
    """

    GRAPH_API_BASE = "https://graph.instagram.com/v25.0"
    DEFAULT_TIMEOUT = 15

    # /me/media 한 호출에서 가져올 필드
    MEDIA_FIELDS = (
        "id,caption,media_type,media_product_type,media_url,thumbnail_url,"
        "permalink,timestamp,children{id,media_type,media_url,thumbnail_url}"
    )
    # 영상 길이는 별도 필드 — v25 에서 children 과 함께 받기 어려워 옵션으로 분리
    MEDIA_VIDEO_FIELDS = MEDIA_FIELDS + ",is_shared_to_feed"

    PAGE_LIMIT = 50

    @classmethod
    def _request(
        cls,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        access_token: str,
    ) -> dict:
        """공통 요청 + Meta 에러 분류."""
        params = {**(params or {}), "access_token": access_token}
        try:
            resp = requests.request(method, url, params=params, timeout=cls.DEFAULT_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise InsightsTransientError(f"network error: {e}") from e

        if resp.status_code == 200:
            return resp.json() or {}

        # Meta 에러 응답 파싱
        try:
            body = resp.json() or {}
        except ValueError:
            body = {"error": {"message": resp.text}}
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        code = err.get("code")
        subcode = err.get("error_subcode")
        msg = err.get("message") or f"HTTP {resp.status_code}"

        # 권한 / 토큰
        if resp.status_code == 403 or code in (190, 200, 102):
            raise InsightsPermissionError(msg, code=code, subcode=subcode)
        # rate limit / transient
        if resp.status_code in (429,) or code in (4, 17, 32, 613):
            raise InsightsTransientError(msg, code=code, subcode=subcode)
        if 500 <= resp.status_code < 600:
            raise InsightsTransientError(msg, code=code, subcode=subcode)
        # 기타 4xx
        raise InsightsAPIError(msg, code=code, subcode=subcode)

    # ===== 미디어 메타데이터 =====

    @classmethod
    def list_media(
        cls,
        *,
        ig_user_id: str,
        access_token: str,
        after: str | None = None,
        limit: int = PAGE_LIMIT,
    ) -> MediaPage:
        """
        GET /{ig_user_id}/media — 50건 단위 페이지네이션.

        Returns:
            MediaPage(items=[...], next_cursor=...) — next_cursor 가 None 이면 마지막 페이지.
        """
        params = {
            "fields": cls.MEDIA_FIELDS,
            "limit": limit,
        }
        if after:
            params["after"] = after
        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/media"
        body = cls._request("GET", url, params=params, access_token=access_token)
        return MediaPage(
            items=body.get("data") or [],
            next_cursor=(body.get("paging") or {}).get("cursors", {}).get("after"),
        )

    @classmethod
    def get_media_video_meta(cls, *, media_id: str, access_token: str) -> dict:
        """
        영상 길이(`video_duration_ms`) 조회 — REELS / VIDEO 만.

        v25 의 `/{media-id}?fields=video_duration_ms,...` — 일부 계정에선 권한 미허용.
        """
        url = f"{cls.GRAPH_API_BASE}/{media_id}"
        params = {"fields": "video_duration_ms"}
        return cls._request("GET", url, params=params, access_token=access_token)

    # ===== 인사이트 =====

    @classmethod
    def get_media_insights(
        cls,
        *,
        media_id: str,
        product_type: str,
        access_token: str,
    ) -> dict:
        """
        GET /{media-id}/insights — 1 미디어당 1회 호출.

        metric 리스트는 미디어 타입에 맞춰 자동 선택. 응답 구조:
            {
              "data": [
                {"name": "reach", "values": [{"value": 12345}], ...},
                ...
              ]
            }
        """
        metrics = select_metrics(product_type)
        url = f"{cls.GRAPH_API_BASE}/{media_id}/insights"
        params = {"metric": ",".join(metrics)}
        return cls._request("GET", url, params=params, access_token=access_token)

    # ===== 계정 단위 인사이트 (follow_type breakdown) =====

    @classmethod
    def get_account_reach_by_follow_type(
        cls,
        *,
        ig_user_id: str,
        access_token: str,
        period_days: int = 30,
    ) -> dict:
        """
        GET /{ig_user_id}/insights — `breakdown=follow_type` 으로 reach 분할.

        v25 응답 예 (요약):
            {
              "data": [{
                "name": "reach",
                "total_value": {
                  "value": 100000,
                  "breakdowns": [{
                    "dimension_keys": ["follow_type"],
                    "results": [
                      {"dimension_values": ["FOLLOWER"],     "value": 80000},
                      {"dimension_values": ["NON_FOLLOWER"], "value": 18000},
                      {"dimension_values": ["UNKNOWN"],      "value": 2000}
                    ]
                  }]
                }
              }]
            }

        Meta 가 since/until 을 epoch 초로 요구 — 최대 90일.
        """
        import time

        until = int(time.time())
        since = until - period_days * 86400

        url = f"{cls.GRAPH_API_BASE}/{ig_user_id}/insights"
        params = {
            "metric": "reach",
            "breakdown": "follow_type",
            "metric_type": "total_value",
            "period": "day",
            "since": since,
            "until": until,
        }
        return cls._request("GET", url, params=params, access_token=access_token)

    @classmethod
    def get_reels_skip_rate(
        cls,
        *,
        media_id: str,
        access_token: str,
    ) -> float | None:
        """
        Reels Skip Rate — 별도 호출 (v25 신규).

        Meta 가 `reels_skip_rate` 메트릭을 별도 노출했고, 일부 계정에선 아직
        미허용이라 실패 시 None 반환 (전체 동기화 흐름은 계속).
        """
        url = f"{cls.GRAPH_API_BASE}/{media_id}/insights"
        params = {"metric": "reels_skip_rate"}
        try:
            body = cls._request("GET", url, params=params, access_token=access_token)
        except InsightsAPIError as e:
            logger.info("reels_skip_rate unavailable for %s: %s", media_id, e)
            return None
        data = body.get("data") or []
        for entry in data:
            if entry.get("name") == "reels_skip_rate":
                values = entry.get("values") or []
                if values:
                    v = values[0].get("value")
                    try:
                        return float(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None
        return None


# ─────────────────────────────────────────────────────────────
# 응답 → 모델 매핑 헬퍼
# ─────────────────────────────────────────────────────────────


def _parse_ig_timestamp(value: str | None) -> datetime | None:
    """Meta 의 ISO8601 (`+0000` 표기) → timezone-aware datetime."""
    if not value:
        return None
    normalized = value.replace("+0000", "+00:00").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _extract_insight_values(payload: dict) -> dict[str, int | float | None]:
    """
    Meta `/insights` 응답을 `{metric_name: numeric_value}` 로 평탄화.

    응답 형식이 metric 마다 다르므로 안전하게 파싱.
    """
    out: dict[str, int | float | None] = {}
    for entry in (payload.get("data") or []):
        name = entry.get("name")
        if not name:
            continue
        # total_value (period=lifetime 일부 metric)
        if "total_value" in entry:
            val = (entry.get("total_value") or {}).get("value")
        else:
            values = entry.get("values") or []
            val = values[0].get("value") if values else None
        if isinstance(val, dict):
            # breakdown 형태 — 합산
            try:
                val = sum(v for v in val.values() if isinstance(v, (int, float)))
            except TypeError:
                val = None
        out[name] = val
    return out


def upsert_media_from_api(
    *,
    account: IGAccountConnection,
    item: dict,
) -> tuple[IGMedia, bool]:
    """
    /me/media 한 건을 IGMedia 로 upsert.

    Returns:
        (instance, created) — Django get_or_create 와 동일 시그니처.
    """
    external_id = str(item.get("id") or "")
    if not external_id:
        raise ValueError("missing media id")

    media_type_raw = (item.get("media_type") or "").upper()
    if media_type_raw not in MediaType.values:
        media_type_raw = MediaType.IMAGE
    product_type_raw = (item.get("media_product_type") or "").upper() or MediaProductType.FEED
    if product_type_raw not in MediaProductType.values:
        product_type_raw = MediaProductType.FEED

    published = _parse_ig_timestamp(item.get("timestamp")) or timezone.now()

    children_raw = (item.get("children") or {}).get("data") if isinstance(item.get("children"), dict) else None
    children_list: list[dict] = []
    if isinstance(children_raw, list):
        for c in children_raw:
            children_list.append(
                {
                    "id": c.get("id"),
                    "media_type": (c.get("media_type") or "").upper(),
                    "media_url": c.get("media_url") or "",
                    "thumbnail_url": c.get("thumbnail_url") or "",
                }
            )

    defaults = {
        "workspace": account.workspace,
        "media_type": media_type_raw,
        "media_product_type": product_type_raw,
        "caption": item.get("caption") or "",
        "permalink": item.get("permalink") or "",
        "media_url": item.get("media_url") or "",
        "thumbnail_url": item.get("thumbnail_url") or "",
        "children": children_list,
        "published_at": published,
        "metadata_last_synced_at": timezone.now(),
        "raw_metadata": item,
    }
    obj, created = IGMedia.objects.update_or_create(
        account=account,
        external_media_id=external_id,
        defaults=defaults,
    )
    return obj, created


def upsert_insight_from_api(
    *,
    media: IGMedia,
    insights_payload: dict,
    skip_rate: float | None,
    video_duration_seconds: float | None = None,
) -> IGMediaInsight:
    """
    `/insights` 응답을 IGMediaInsight 로 upsert.

    `skip_rate` 와 `video_duration_seconds` 는 별도 호출에서 채워서 전달.
    """
    values = _extract_insight_values(insights_payload)

    def _i(name: str) -> int:
        v = values.get(name)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def _opt_i(name: str) -> int | None:
        v = values.get(name)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    insight, _ = IGMediaInsight.objects.get_or_create(media=media)
    insight.reach = _i("reach")
    insight.likes = _i("likes")
    insight.comments = _i("comments")
    insight.shares = _i("shares")
    insight.saved = _i("saved")
    insight.total_interactions = _i("total_interactions")
    insight.views = _opt_i("views")
    insight.impressions = _opt_i("impressions")
    insight.follows = _opt_i("follows")
    insight.profile_visits = _opt_i("profile_visits")
    insight.profile_activity = _opt_i("profile_activity")
    insight.ig_reels_avg_watch_time_ms = _opt_i("ig_reels_avg_watch_time")
    insight.ig_reels_video_view_total_time_ms = _opt_i("ig_reels_video_view_total_time")
    insight.reels_skip_rate = skip_rate
    insight.raw_payload = insights_payload
    insight.save()

    # 영상 길이는 IGMedia 쪽에 저장
    if video_duration_seconds is not None and media.duration_seconds != video_duration_seconds:
        media.duration_seconds = video_duration_seconds
        media.save(update_fields=["duration_seconds", "updated_at"])

    media.insights_last_synced_at = timezone.now()
    media.insights_sync_error = ""
    media.save(update_fields=["insights_last_synced_at", "insights_sync_error", "updated_at"])
    return insight


# ─────────────────────────────────────────────────────────────
# 동기화 오케스트레이션
# ─────────────────────────────────────────────────────────────


def sync_account_media(
    account: IGAccountConnection,
    *,
    max_pages: int = 10,
) -> dict:
    """
    한 계정의 미디어 메타데이터를 페이지 단위로 가져와 DB 에 upsert.

    Args:
        account: 활성 IG 계정
        max_pages: 한 번 호출당 최대 페이지 수 (50 * max_pages 건 = 호출 1회 / 50건)

    Returns:
        {"fetched": int, "created": int, "pages": int}

    호출 비용:
        max_pages 호출 (한 페이지 = 1 IG API call).
        기본 10 페이지 = 500 미디어. 일반 비즈니스 계정 대부분 1~3 페이지에서 종료.
    """
    fetched = 0
    created_count = 0
    pages = 0
    cursor: str | None = None

    while pages < max_pages:
        page = IGInsightsClient.list_media(
            ig_user_id=account.external_account_id,
            access_token=account.access_token,
            after=cursor,
        )
        pages += 1
        for item in page.items:
            try:
                _, created = upsert_media_from_api(account=account, item=item)
                fetched += 1
                if created:
                    created_count += 1
            except Exception:
                logger.exception("upsert media failed: %s", item.get("id"))
        cursor = page.next_cursor
        if not cursor:
            break

    return {"fetched": fetched, "created": created_count, "pages": pages}


def sync_media_insights(media: IGMedia, *, force: bool = False) -> IGMediaInsight | None:
    """
    한 미디어의 인사이트를 동기화.

    `force=False` 이면 `media.is_insights_fresh()` 면 호출 자체를 건너뜀
    (= API 호출량 절감).

    Returns:
        IGMediaInsight (성공) | None (skip / 권한 에러로 보류)
    """
    if not force and media.is_insights_fresh():
        return getattr(media, "insight", None)

    account = media.account
    if account.status != IGAccountConnection.Status.ACTIVE:
        return None

    try:
        payload = IGInsightsClient.get_media_insights(
            media_id=media.external_media_id,
            product_type=media.media_product_type,
            access_token=account.access_token,
        )
    except InsightsPermissionError as e:
        # 권한/정책 에러도 한 사이클은 끝난 것으로 처리: last_synced_at 을 찍어
        # is_insights_fresh()=True 로 만들어 TTL 동안 같은 미디어를 재호출하지 않는다.
        # (Meta 28일 윈도우 만료처럼 영구적인 사유면 매번 호출해도 같은 결과)
        media.insights_sync_error = f"permission: {e}"
        media.insights_last_synced_at = timezone.now()
        media.save(
            update_fields=["insights_sync_error", "insights_last_synced_at", "updated_at"]
        )
        # account 자체에도 표시
        account.mark_as_error(f"insights permission error: {e}")
        return None
    except InsightsTransientError:
        # transient — 다음 주기에 재시도 (last_synced_at 도 의도적으로 갱신하지 않음)
        raise
    except InsightsAPIError as e:
        media.insights_sync_error = f"api: {e}"
        media.insights_last_synced_at = timezone.now()
        media.save(
            update_fields=["insights_sync_error", "insights_last_synced_at", "updated_at"]
        )
        return None

    # REELS 부가 호출
    skip_rate = None
    duration_seconds = media.duration_seconds
    if media.media_product_type == MediaProductType.REELS:
        try:
            skip_rate = IGInsightsClient.get_reels_skip_rate(
                media_id=media.external_media_id,
                access_token=account.access_token,
            )
        except InsightsAPIError:
            skip_rate = None
        if duration_seconds is None:
            try:
                meta = IGInsightsClient.get_media_video_meta(
                    media_id=media.external_media_id,
                    access_token=account.access_token,
                )
                duration_ms = meta.get("video_duration_ms")
                if duration_ms is not None:
                    duration_seconds = float(duration_ms) / 1000.0
            except InsightsAPIError:
                duration_seconds = None

    return upsert_insight_from_api(
        media=media,
        insights_payload=payload,
        skip_rate=skip_rate,
        video_duration_seconds=duration_seconds,
    )


def sync_account_audience_insight(
    account: IGAccountConnection,
    *,
    period_days: int = 30,
) -> IGAccountInsight | None:
    """
    한 계정의 follow_type breakdown reach 를 가져와 IGAccountInsight 로 upsert.

    Returns:
        IGAccountInsight 인스턴스 (성공) | None (권한 에러로 보류)

    호출 비용: 계정 1개 = 1 IG API call. 일 1회 호출이 적절.
    """
    from datetime import date, timedelta

    if account.status != IGAccountConnection.Status.ACTIVE:
        return None

    try:
        payload = IGInsightsClient.get_account_reach_by_follow_type(
            ig_user_id=account.external_account_id,
            access_token=account.access_token,
            period_days=period_days,
        )
    except InsightsPermissionError as e:
        logger.warning("account insight permission error account=%s: %s", account.id, e)
        account.mark_as_error(f"account insights permission: {e}")
        return None
    except InsightsAPIError as e:
        logger.warning("account insight api error account=%s: %s", account.id, e)
        return None

    # breakdown 파싱
    follower = 0
    non_follower = 0
    unknown = 0
    for entry in payload.get("data") or []:
        if entry.get("name") != "reach":
            continue
        total_value = entry.get("total_value") or {}
        for bd in total_value.get("breakdowns") or []:
            for r in bd.get("results") or []:
                dim_values = r.get("dimension_values") or []
                tag = (dim_values[0] if dim_values else "").upper()
                value = r.get("value") or 0
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = 0
                if tag == "FOLLOWER":
                    follower = value
                elif tag == "NON_FOLLOWER":
                    non_follower = value
                elif tag == "UNKNOWN":
                    unknown = value

    today = date.today()
    period_start = today - timedelta(days=period_days)
    insight, _ = IGAccountInsight.objects.update_or_create(
        account=account,
        period_days=period_days,
        defaults={
            "period_start": period_start,
            "period_end": today,
            "follower_reach": follower,
            "non_follower_reach": non_follower,
            "unknown_reach": unknown,
            "raw_payload": payload,
        },
    )
    return insight


def aggregate_media_insights(media_qs: Iterable[IGMedia]) -> dict:
    """
    여러 미디어를 묶었을 때의 합산 인사이트 — DB 단일 쿼리.

    Reach 는 단순합 (사용자 중복 발생 가능 → UI 에서 경고 툴팁).
    """
    from django.db.models import Sum, Avg, Count

    qs = IGMediaInsight.objects.filter(media__in=media_qs)
    agg = qs.aggregate(
        sum_reach=Sum("reach"),
        sum_likes=Sum("likes"),
        sum_comments=Sum("comments"),
        sum_shares=Sum("shares"),
        sum_saved=Sum("saved"),
        sum_interactions=Sum("total_interactions"),
        sum_views=Sum("views"),
        sum_impressions=Sum("impressions"),
        sum_follows=Sum("follows"),
        sum_profile_visits=Sum("profile_visits"),
        sum_paid_spend=Sum("paid_spend"),
        sum_paid_reach=Sum("paid_reach"),
        sum_paid_link_clicks=Sum("paid_link_clicks"),
        avg_er=Avg("engagement_rate"),
        avg_viral=Avg("viral_score"),
        n=Count("media"),
    )
    return {
        "media_count": agg["n"] or 0,
        "reach_sum": agg["sum_reach"] or 0,
        "reach_disclaimer": (
            "도달수는 사용자 중복이 발생할 수 있어, 단순 합산 수치는 실제 유니크 도달보다 높게 측정될 수 있습니다."
        ),
        "likes": agg["sum_likes"] or 0,
        "comments": agg["sum_comments"] or 0,
        "shares": agg["sum_shares"] or 0,
        "saved": agg["sum_saved"] or 0,
        "total_interactions": agg["sum_interactions"] or 0,
        "views": agg["sum_views"] or 0,
        "impressions": agg["sum_impressions"] or 0,
        "follows": agg["sum_follows"] or 0,
        "profile_visits": agg["sum_profile_visits"] or 0,
        "avg_engagement_rate": round(agg["avg_er"] or 0, 2),
        "avg_viral_score": round(agg["avg_viral"] or 0, 2),
        "paid": {
            "spend": float(agg["sum_paid_spend"]) if agg["sum_paid_spend"] is not None else None,
            "reach": agg["sum_paid_reach"],
            "link_clicks": agg["sum_paid_link_clicks"],
        },
    }
