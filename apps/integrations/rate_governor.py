"""P3f — 계정당/플랜별 DM 발송 안전속도 거버너 (Redis 기반).

WHY: 합산 동시성을 올리면(목표 ~10-20K DM/분, 수천 계정) 한 IG 계정을 Meta 안전속도 이상으로
과속 발송해 밴/스로틀당할 위험이 생긴다. Meta 는 "짧은 시간 버스트"에 민감하므로(예: 3분에 10건)
**시간당 상한(플랜 quota)** 과 **분당 상한(버스트/밴 안전)** 을 동시에 본다.

설계 원칙:
- 멀티테넌트 정확성: 한도는 **IG 계정 단위**로 건다(Meta 한도가 계정당이므로).
- 발송을 "드롭"하지 않고 "지연(defer)" 한다 — 호출부는 차단 시 retry_after 만큼 뒤로 재스케줄.
- Redis 고정 윈도우 카운터(원자적 INCR) — 단순/견고. django_redis(/1) 사용.

플랜별 한도(기본값, settings.DM_RATE_LIMITS 로 오버라이드 가능):
    free       : 시간당 60,  분당 8     (무료 = DM 자동화 500건/월 수준의 보수적 페이싱)
    starter/pro/enterprise: 점증 (유료 = 고처리)

⚠️ 이 모듈은 순수 유틸이다. 실제 발송 경로(send_dm_task) 에 wiring 하는 5줄은
   SERVER_RUNBOOK.md 의 P3f 절차를 따라 추가 + 테스트할 것.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

# (per_hour, per_minute) — settings.DM_RATE_LIMITS 로 오버라이드.
_DEFAULT_LIMITS = {
    "free": (60, 8),
    "starter": (300, 20),
    "pro": (1200, 40),
    "enterprise": (5000, 80),
}


def _limits_for(plan: str) -> tuple[int, int]:
    table = getattr(settings, "DM_RATE_LIMITS", None) or _DEFAULT_LIMITS
    return tuple(table.get(plan, table.get("free", (60, 8))))  # type: ignore[return-value]


@dataclass
class Decision:
    allowed: bool
    retry_after: int = 0      # 차단 시 호출부가 기다렸다 재시도할 초
    reason: str = ""


def _incr_window(key: str, ttl: int) -> int:
    """원자적 고정 윈도우 카운터. 첫 증가 시 TTL 설정."""
    try:
        val = cache.incr(key)
    except ValueError:
        # 키 없음 → 1 로 초기화 + TTL. (django_redis: add 후 incr)
        cache.add(key, 0, timeout=ttl)
        val = cache.incr(key)
    if val == 1:
        try:
            cache.expire(key, ttl)  # django-redis 4.x+
        except Exception:
            pass
    return val


def check(ig_account_id: str, plan: str = "free") -> Decision:
    """발송 1건이 허용되는지 검사하고 카운터를 소비한다.

    Returns Decision(allowed, retry_after, reason).
    차단되면 호출부는 발송하지 말고 retry_after 후 재시도(재스케줄)해야 한다.
    """
    if not ig_account_id:
        return Decision(allowed=True)  # 식별 불가 시 거버너 우회(안전 측 — 차단하지 않음)

    per_hour, per_minute = _limits_for(plan)
    now = int(time.time())
    hour_key = f"dmrate:h:{ig_account_id}:{now // 3600}"
    min_key = f"dmrate:m:{ig_account_id}:{now // 60}"

    # 분당(버스트) 먼저 — 더 짧은 윈도우.
    m = _incr_window(min_key, ttl=70)
    if m > per_minute:
        return Decision(False, retry_after=60 - (now % 60) + 1, reason="per_minute")

    h = _incr_window(hour_key, ttl=3700)
    if h > per_hour:
        return Decision(False, retry_after=3600 - (now % 3600) + 1, reason="per_hour")

    return Decision(allowed=True)
