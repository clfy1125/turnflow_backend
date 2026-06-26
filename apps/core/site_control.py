"""SiteControl 헬퍼 — active_site 판정 + 스케줄러 heartbeat.

``is_active_site()`` 는 핫패스(미들웨어 / Celery prerun / tick)에서 호출되므로 5초 캐시한다.
단 캐시(Redis) 장애가 게이트를 깨면 안 되므로:
  - 캐시 miss → DB 조회 후 캐시 갱신(+장기 last-known-good 도 갱신)
  - DB 조회 실패 → last-known-good 으로 폴백(active 사이트가 DB blip 에 503 나는 것 방지)
  - 둘 다 없으면 보수적으로 passive 취급(split-brain 방지 우선)

상세: DR_IMPLEMENTATION_PLAN.md §5.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_CACHE_KEY = "dr:site_state"
_LKG_KEY = "dr:site_state:lkg"  # last-known-good (DB 장애 폴백용, 장기 TTL)
_CACHE_TTL = 5
_LKG_TTL = 3600
_HEARTBEAT_KEY = "dr:scheduler:last_tick"

# DB·LKG 모두 없을 때의 보수적 기본값 — 이 서버를 passive 로 간주(쓰기 차단).
_UNKNOWN_STATE = {
    "active_site": "__unknown__",
    "epoch": 0,
    "mode": "maintenance",
    "restore_complete": False,
}


def _safe_cache_get(key):
    try:
        return cache.get(key)
    except Exception:  # noqa: BLE001 — 캐시 장애는 무시
        return None


def _safe_cache_set(key, value, ttl):
    try:
        cache.set(key, value, timeout=ttl)
    except Exception:  # noqa: BLE001
        pass


def _load_from_db() -> dict:
    from apps.core.models import SiteControl

    sc = SiteControl.load()
    return {
        "active_site": sc.active_site,
        "epoch": sc.epoch,
        "mode": sc.mode,
        "restore_complete": sc.restore_complete,
    }


def get_site_state(*, use_cache: bool = True) -> dict:
    """현재 SiteControl 상태 dict. 5초 캐시 + DB + last-known-good 폴백."""
    if use_cache:
        cached = _safe_cache_get(_CACHE_KEY)
        if cached is not None:
            return cached
    try:
        state = _load_from_db()
        _safe_cache_set(_CACHE_KEY, state, _CACHE_TTL)
        _safe_cache_set(_LKG_KEY, state, _LKG_TTL)
        return state
    except Exception:  # noqa: BLE001 — DB blip: 마지막 정상값으로 폴백
        lkg = _safe_cache_get(_LKG_KEY)
        if lkg is not None:
            logger.warning("site state DB load failed; using last-known-good")
            return lkg
        logger.error("site state unavailable (no DB, no LKG); treating as passive")
        return dict(_UNKNOWN_STATE)


def invalidate_site_state_cache() -> None:
    """promote/demote 직후 호출 — 새 상태가 즉시 반영되도록 캐시 무효화."""
    try:
        cache.delete(_CACHE_KEY)
    except Exception:  # noqa: BLE001
        pass


def is_active_site() -> bool:
    """이 서버가 권위(write/Celery/scheduler 허용) 사이트인가.

    상태 불명(DB 미가용/테이블 부재 — 예: 마이그레이션 직전 배포 윈도우)일 때는 **fail-open**
    (active 로 간주)한다. 근거: 이 DR 은 단일 공유 DB 모델이라 'DB 를 못 읽는 사이트'는 어차피
    서빙도 못 한다. split-brain 의 실질 방어는 운영 펜싱 + epoch + CF LB 이지 이 게이트의
    unknown 처리가 아니다. unknown 을 passive 로 막으면 신규 배포 시 자기유발 503/태스크 드랍만 난다.
    반면 상태가 '명확히 다른 active_site' 면 passive 로 차단(정상 펜싱).
    """
    state = get_site_state()
    active = state.get("active_site")
    if active in (None, "", "__unknown__"):
        return True  # 상태 불명 → fail-open
    return active == settings.SITE_ID and state.get("mode") == "live"


# ─────────────────────────────────────────────────────────────
# 스케줄러 dead-man heartbeat
# ─────────────────────────────────────────────────────────────
def touch_scheduler_heartbeat() -> None:
    _safe_cache_set(_HEARTBEAT_KEY, int(time.time()), 3600)


def scheduler_heartbeat_fresh(max_age: int = 180) -> bool:
    ts = _safe_cache_get(_HEARTBEAT_KEY)
    if ts is None:
        return False
    try:
        return (int(time.time()) - int(ts)) <= max_age
    except (TypeError, ValueError):
        return False
