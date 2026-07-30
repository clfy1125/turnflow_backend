"""IG 프로필 통계 동기화 (팔로워·게시물 수).

분석 팝업이 "@reels_drgn · 팔로워 98,293 · 게시물 672개" 를 보여 주려면 연동 행에
통계가 있어야 한다. Graph 1콜(계정 메타)로 갱신하고, 실패하면 캐시값을 그대로 쓴다
(팝업이 절대 깨지지 않도록 fail-soft).
"""

from __future__ import annotations

import logging

from django.utils import timezone

from .pipeline.collect_official import CollectError, fetch_account_meta

logger = logging.getLogger(__name__)

STALE_HOURS = 6


def is_stale(connection, *, max_age_hours: int = STALE_HOURS) -> bool:
    if not connection.stats_synced_at:
        return True
    age = timezone.now() - connection.stats_synced_at
    return age.total_seconds() > max_age_hours * 3600


def apply_meta(connection, meta: dict) -> bool:
    """계정 메타 dict 를 연동 행에 반영. 변경이 있었으면 True."""
    if not meta:
        return False
    fields = []

    def _set(attr, value):
        if value is None:
            return
        if getattr(connection, attr) != value:
            setattr(connection, attr, value)
            fields.append(attr)

    _set("followers_count", meta.get("followers_count"))
    _set("follows_count", meta.get("follows_count"))
    _set("media_count", meta.get("media_count"))
    if meta.get("biography") is not None:
        _set("biography", (meta.get("biography") or "")[:2200])
    if meta.get("name"):
        _set("name", meta["name"][:255])

    connection.stats_synced_at = timezone.now()
    fields.append("stats_synced_at")
    connection.save(update_fields=[*fields, "updated_at"])
    return bool(fields)


def refresh_stats(connection, *, max_age_hours: int = STALE_HOURS, force: bool = False) -> dict:
    """필요하면 Graph 에서 통계를 새로 받아 반영. 반환: 사용한 메타(빈 dict 가능).

    fail-soft — 토큰 만료·네트워크 오류여도 예외를 올리지 않는다(팝업 표시가 우선).
    """
    if not force and not is_stale(connection, max_age_hours=max_age_hours):
        return {}
    try:
        token = connection.access_token
    except Exception:  # noqa: BLE001 - 복호화 실패(키 교체 등)
        logger.warning("insta_report: token decrypt failed conn=%s", connection.id)
        return {}
    if not token:
        return {}
    try:
        meta = fetch_account_meta(connection.external_account_id, token)
    except CollectError as e:
        logger.info("insta_report: profile stats refresh rejected conn=%s (%s)", connection.id, e)
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "insta_report: profile stats refresh failed conn=%s %s", connection.id, type(e).__name__
        )
        return {}
    apply_meta(connection, meta)
    return meta


def display_line(connection) -> str:
    """프론트가 그대로 써도 되는 한 줄 요약."""
    parts = [f"@{connection.username}"] if connection.username else []
    if connection.followers_count is not None:
        parts.append(f"팔로워 {connection.followers_count:,}")
    if connection.media_count is not None:
        parts.append(f"게시물 {connection.media_count:,}개")
    return " · ".join(parts)
