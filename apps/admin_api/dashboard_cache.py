"""apps/admin_api/dashboard_cache.py — 어드민 대시보드 응답 캐시 키 + 무효화 (MKT-11).

키 문자열을 **여기 한 곳**에 둔다. 예전엔 뷰 모듈 안에 있어서 "쓰는 쪽"(대시보드 뷰)만
알고 "무효화해야 하는 쪽"(채널 링크 CRUD)은 알지 못했다 — 그래서 링크를 저장해도 최대
5분간 채널별 성과 표에 나타나지 않았다(캐시 히트).

⚠️ ``cache.clear()`` 금지 — Redis 를 다른 기능과 공유한다. 전체 flush 는 DM 발송의
rate_governor fail-closed 센티넬을 날려 **1시간 발송 정지**를 유발한다
(apps/integrations/rate_governor.py 참고). 반드시 아래처럼 키를 특정해 삭제할 것.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── 마케팅 대시보드 ───────────────────────────────────────────────────
MKT_CACHE_KEY_TMPL = "admin:dash:mkt:{period}"
MKT_CACHE_KEY_CUSTOM_TMPL = "admin:dash:mkt:custom:{start}:{end}"
# 기간 무관 고정 패널 — period 별 응답이 공유하는 단일 키 (계산 1회)
MKT_CACHE_KEY_SNAPSHOT = "admin:dash:mkt:snapshot"
# 프리셋 period (ALLOWED_PERIODS 와 같은 집합 — 늘어나면 함께 고칠 것)
MKT_CACHE_PRESETS = ("7d", "30d", "90d", "all")


def bust_marketing_dashboard_cache(*, reason: str = "") -> None:
    """마케팅 대시보드 캐시 무효화 — 채널 링크 CRUD 직후 호출.

    프리셋 4키 + 커스텀 범위 키(패턴 삭제 가능한 백엔드에서만)를 지운다.
    ``snapshot`` 은 **일부러 남긴다** — 기간 무관 누적치라 링크 생성과 무관하고,
    매번 지우면 비싼 재계산이 반복된다.

    행동 직후 그 결과를 확인하는 흐름이라(링크 저장 → 표에서 확인) 여기서만은 5분 지연이
    "동작 안 함"으로 읽힌다.
    """
    keys = [MKT_CACHE_KEY_TMPL.format(period=p) for p in MKT_CACHE_PRESETS]
    cache.delete_many(keys)
    # django-redis 는 delete_pattern 을 제공한다(SCAN+DEL — flush 아님). 없는 백엔드면 생략:
    # 커스텀 범위는 조회 빈도가 낮아 다음 만료까지 기다려도 실무상 문제 없다.
    delete_pattern = getattr(cache, "delete_pattern", None)
    if callable(delete_pattern):
        try:
            delete_pattern(MKT_CACHE_KEY_CUSTOM_TMPL.format(start="*", end="*"))
        except Exception:  # noqa: BLE001 — 캐시 무효화 실패가 본 요청을 깨뜨리면 안 된다
            logger.warning("[admin-cache] 커스텀 범위 키 패턴 삭제 실패 (무시)", exc_info=True)
    logger.info("[admin-cache] 마케팅 대시보드 캐시 무효화 reason=%s", reason or "-")
