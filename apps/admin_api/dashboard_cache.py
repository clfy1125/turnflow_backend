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

from apps.admin_api.roles import ROLE_FULL, resolve_admin_role

logger = logging.getLogger(__name__)

# ── 캐시 우회(?refresh=1) ─────────────────────────────────────────────
# 방문/가입 적재에는 무효화 훅이 없어 신규 유입은 TTL 만큼 늦게 보인다. 화면의 '새로고침'
# 버튼이 이 파라미터를 실어 보내면 그 요청만 캐시를 건너뛰고 재계산 후 다시 적재한다.
_TRUTHY = {"1", "true", "yes", "y", "on"}
# 응답 헤더로 캐시 여부를 알린다 (브라우저 JS 노출은 settings 의 CORS_EXPOSE_HEADERS).
CACHE_HEADER = "X-Cache"
CACHE_HIT = "HIT"
CACHE_MISS = "MISS"
CACHE_BYPASS = "BYPASS"


def wants_cache_bypass(request) -> bool:
    """``?refresh=1`` 로 캐시 우회를 요청했고, **그럴 권한이 있는지**.

    ``full`` 역할만 허용한다 — 제한 역할(marketing_viewer, 외주)이 무한 새로고침으로
    가장 비싼 집계(period=all)를 반복 재계산시키는 경로를 열지 않기 위함이다.
    권한이 없으면 **403 이 아니라 조용히 무시**하고 캐시된 응답을 준다(비필수 기능이라
    실패로 다룰 이유가 없다). 프론트는 이 역할에 새로고침 버튼을 숨기면 된다.
    """
    raw = (request.query_params.get("refresh") or "").strip().lower()
    if raw not in _TRUTHY:
        return False
    return resolve_admin_role(request) == ROLE_FULL


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
