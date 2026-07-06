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

import logging
import time
from dataclasses import dataclass
from datetime import UTC

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# (per_hour, per_minute) — settings.DM_RATE_LIMITS 로 오버라이드.
# 미지 플랜명은 free 페이싱으로 폴백되므로 신규 플랜은 반드시 여기 명시할 것.
# starter/enterprise 는 구 workspace.plan 오버라이드 호환용으로 잔존.
_DEFAULT_LIMITS = {
    "free": (60, 8),
    "basic": (300, 20),  # 월 200건 한도라 페이싱은 여유롭게 (starter급)
    "starter": (300, 20),
    "pro": (1200, 40),
    "admin": (5000, 80),  # 내부 운영 계정 — enterprise급
    "enterprise": (5000, 80),
}

# Meta Graph API 물리 한도: 게시물/릴스 댓글 Private Reply 는 계정당 750 calls/hour.
# plan per_hour 값이 이를 초과(pro=1200/enterprise=5000)해도 안전마진(기본 700)으로 강제 캡한다.
# settings.IG_PRIVATE_REPLY_HOURLY_CAP 로 오버라이드(0/None 이면 캡 미적용).
PRIVATE_REPLY_HOURLY_CAP = 700


def _limits_for(plan: str) -> tuple[int, int]:
    table = getattr(settings, "DM_RATE_LIMITS", None) or _DEFAULT_LIMITS
    return tuple(table.get(plan, table.get("free", (60, 8))))  # type: ignore[return-value]


@dataclass
class Decision:
    allowed: bool
    retry_after: int = 0  # 차단 시 호출부가 기다렸다 재시도할 초
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


# ───────────────────────────────────────────────────────────────
# Action Block 서킷 브레이커 (P4) — 계정별 에스컬레이팅 쿨다운
# ───────────────────────────────────────────────────────────────
# Meta code 368("temporarily blocked for policies violations") 같은 차단 신호가 오면,
# 그 계정의 모든 DM 발송을 일정 시간 'Meta 로 보내지 않고' defer 한다.
# (검색 근거: 1차 차단 24-48h, 차단 중 재시도는 차단 기간을 '연장'시킨다 → 짧은 재개 금지.)
# 반복 차단 시 쿨다운을 ×2 로 늘려(에스컬레이션) 상한까지. 쿨다운 만료 시 자동 재개.
_ACTION_BLOCK_BASE_HOURS = 24
_ACTION_BLOCK_MAX_DAYS = 7


def _ab_keys(ig_account_id: str) -> tuple[str, str]:
    return f"dm:ab:cooldown:{ig_account_id}", f"dm:ab:level:{ig_account_id}"


def action_block_cooldown_remaining(ig_account_id: str) -> int:
    """현재 Action Block 쿨다운 잔여 초. 쿨다운 중이 아니면 0.

    값에 만료 epoch 를 저장하므로 어떤 캐시 백엔드(LocMem/Redis)에서도 동일하게 동작한다.
    """
    if not ig_account_id:
        return 0
    cd_key, _ = _ab_keys(ig_account_id)
    until = cache.get(cd_key)
    if until is None:
        # 캐시 miss(예: Redis flush/DR failover) → DB(DMAccountBlock) 폴백 + 캐시 재프라임.
        until = _restore_action_block_from_db(ig_account_id)
        if until is None:
            return 0
    remaining = int(until) - int(time.time())
    return remaining if remaining > 0 else 0


def trip_action_block(ig_account_id: str, base_hours: int = None, max_days: int = None) -> int:
    """Action Block 감지 시 계정 쿨다운 설정(에스컬레이팅).

    Returns:
        새로 설정한 쿨다운 초(= 새 트립). 이미 쿨다운 중이면 0(동시 368 폭주 시 중복 트립 무시).
    """
    if not ig_account_id:
        return 0
    if action_block_cooldown_remaining(ig_account_id) > 0:
        return 0  # 이미 쿨다운 중 — 에스컬레이션/재설정 안 함

    base_hours = base_hours or getattr(
        settings, "DM_ACTION_BLOCK_BASE_COOLDOWN_HOURS", _ACTION_BLOCK_BASE_HOURS
    )
    max_days = max_days or getattr(
        settings, "DM_ACTION_BLOCK_MAX_COOLDOWN_DAYS", _ACTION_BLOCK_MAX_DAYS
    )
    cd_key, lvl_key = _ab_keys(ig_account_id)
    # 레벨(반복 차단 횟수)은 30일 유지 → 반복 차단일수록 쿨다운이 길어진다.
    # Redis 손실에도 에스컬레이션이 유지되도록 캐시·DB 중 큰 레벨을 기준으로 +1.
    level = max(int(cache.get(lvl_key) or 0), _db_action_block_level(ig_account_id)) + 1
    cache.set(lvl_key, level, timeout=30 * 24 * 3600)
    cooldown = min(int(base_hours) * 3600 * (2 ** (level - 1)), int(max_days) * 24 * 3600)
    until_epoch = int(time.time()) + cooldown
    cache.set(cd_key, until_epoch, timeout=cooldown)
    # ★ 듀얼라이트(DR): DB 영속화 → Redis flush/DR failover 에도 차단 유지(차단 연장 사고 방지).
    _persist_action_block_to_db(ig_account_id, until_epoch, level)
    return int(cooldown)


def check(ig_account_id: str, plan: str = "free") -> Decision:
    """발송 1건이 허용되는지 검사하고 카운터를 소비한다.

    Returns Decision(allowed, retry_after, reason).
    차단되면 호출부는 발송하지 말고 retry_after 후 재시도(재스케줄)해야 한다.
    """
    if not ig_account_id:
        return Decision(allowed=True)  # 식별 불가 시 거버너 우회(안전 측 — 차단하지 않음)

    # ★ P8: Redis flush/재시작 감지 → fail-closed (순간 과발송·밴 방지).
    # 장기 TTL 센티넬(dmrate:alive)이 사라졌다 = 캐시 저장소가 비워졌다(카운터 전부 0으로 리셋).
    # 이 경우 '이번 시각 경계까지 전 계정 차단'으로 고정한다(요구사항: 1시간 동안 최대치 고정).
    # dmrate:reset_until 에 차단 종료 epoch 를 박아 그 시간까지 모든 check 가 차단되도록 한다.
    now = int(time.time())
    reset_until = cache.get("dmrate:reset_until")
    if reset_until and now < int(reset_until):
        return Decision(
            False, retry_after=int(reset_until) - now + 1, reason="redis_reset_failclosed"
        )
    if cache.get("dmrate:alive") is None:
        boundary = (now // 3600 + 1) * 3600  # 다음 시각 경계
        cache.set("dmrate:alive", 1, timeout=7 * 24 * 3600)
        cache.set("dmrate:reset_until", boundary, timeout=max(boundary - now + 5, 5))
        return Decision(False, retry_after=boundary - now + 1, reason="redis_reset_failclosed")

    per_hour, per_minute = _limits_for(plan)
    # Meta 계정당 750/hr Private Reply 가 실제 물리 상한 → plan 과 무관하게 이 값(안전마진 700)을
    # 시간당 상한으로 사용한다. 분당(per_minute)은 plan 별 버스트 스무더로 유지(밴 회피용).
    # 플랜별 시간당 차등이 필요하면 settings.DM_RATE_LIMITS + IG_PRIVATE_REPLY_HOURLY_CAP 로 조정.
    cap = getattr(settings, "IG_PRIVATE_REPLY_HOURLY_CAP", PRIVATE_REPLY_HOURLY_CAP)
    if cap and cap > 0:
        per_hour = cap
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


# ───────────────────────────────────────────────────────────────
# 수동 모더레이션(댓글 숨김/복원) 남용 방지 — 버튼 연타로 Meta quota 소진 방지
# ───────────────────────────────────────────────────────────────
# 숨김/복원은 사용자가 버튼으로 직접 호출한다. 연타하면 (a) 같은 댓글을 hide↔unhide 로
# 왔다갔다 하며 무의미한 호출을 반복하거나 (b) 계정 전체 Meta Graph 호출을 급격히 소진해
# 밴/스로틀당할 위험이 있다. DM 발송과 달리 사용자 요청이라 '지연' 대신 즉시 429 로 거절한다.
# 두 겹으로 막는다:
#   (1) 같은 (댓글, 액션) 쿨다운: 더블클릭/같은 버튼 연타를 흡수. hide 직후 unhide '정정'은
#       액션이 달라 쿨다운을 공유하지 않으므로 허용된다(오탐 되돌리기 UX 보존).
#   (2) 계정당 분/시 상한: 지속적 연타로 계정 quota 를 소진하지 못하게 버스트/누적 캡.
# DM 거버너와 별도 네임스페이스(mod:*). settings.MODERATION_RATE_LIMITS 로 오버라이드.
_MODERATION_DEFAULTS = {
    "per_comment_cooldown": 10,  # 같은 (댓글, 액션) 재요청 최소 간격(초) — 더블클릭 흡수
    "per_minute": 20,  # 계정당 분당 모더레이션 액션(버스트 상한)
    "per_hour": 300,  # 계정당 시간당 모더레이션 액션(누적 상한, Meta 안전마진)
}


def _moderation_limits() -> dict:
    cfg = getattr(settings, "MODERATION_RATE_LIMITS", None) or {}
    return {**_MODERATION_DEFAULTS, **cfg}


def moderation_action_check(ig_account_id: str, comment_id: str, action: str) -> Decision:
    """댓글 숨김/복원 1회가 허용되는지 검사하고 카운터를 소비한다(버튼 연타 방지).

    Args:
        ig_account_id: IG 계정 식별자(external_account_id). Meta 한도가 계정당이므로 스코프 기준.
        comment_id: 대상 댓글 ID. 같은 댓글의 같은 액션 더블클릭을 쿨다운으로 흡수.
        action: "hide" | "unhide". 같은 댓글이라도 다른 액션은 쿨다운을 공유하지 않아
            hide 직후 unhide 정정은 허용된다.

    Returns:
        Decision(allowed, retry_after, reason). 식별 불가/캐시 미가용 시엔 통과(사용자
        액션을 임의로 막지 않는 안전 측).
    """
    if not ig_account_id:
        return Decision(allowed=True)

    lim = _moderation_limits()
    now = int(time.time())

    # (1) 같은 (댓글, 액션) 쿨다운 — 더블클릭/연타 흡수. cache.add 는 키가 없을 때만 True.
    #     쿨다운에 걸리면 카운터를 소비하지 않고 즉시 거절(연타가 계정 quota 를 먹지 않게).
    cooldown = int(lim.get("per_comment_cooldown", 0) or 0)
    if comment_id and cooldown > 0:
        if not cache.add(f"mod:cd:{action}:{comment_id}", now, timeout=cooldown):
            return Decision(False, retry_after=cooldown, reason="per_comment_cooldown")

    # (2) 계정당 분/시 상한 — 지속 연타로 Meta quota 소진 방지(분당 먼저: 더 짧은 윈도우).
    per_minute = int(lim.get("per_minute", 0) or 0)
    per_hour = int(lim.get("per_hour", 0) or 0)
    if per_minute > 0:
        m = _incr_window(f"mod:m:{ig_account_id}:{now // 60}", ttl=70)
        if m > per_minute:
            return Decision(False, retry_after=60 - (now % 60) + 1, reason="per_minute")
    if per_hour > 0:
        h = _incr_window(f"mod:h:{ig_account_id}:{now // 3600}", ttl=3700)
        if h > per_hour:
            return Decision(False, retry_after=3600 - (now % 3600) + 1, reason="per_hour")

    return Decision(allowed=True)


# ───────────────────────────────────────────────────────────────
# DR — Action Block DB 영속화 + Redis 손실 후 DB 재수화 (#2 Option C)
# 설계: DR_IMPLEMENTATION_PLAN.md §7.1, §7.2.
# ───────────────────────────────────────────────────────────────
def _db_action_block_level(ig_account_id: str) -> int:
    """DMAccountBlock.level (없으면 0). trip 시 에스컬레이션 보존용."""
    try:
        from apps.integrations.models import DMAccountBlock

        row = DMAccountBlock.objects.filter(external_account_id=ig_account_id).only("level").first()
        return int(row.level) if row else 0
    except Exception:  # noqa: BLE001 — best-effort
        return 0


def _persist_action_block_to_db(ig_account_id: str, until_epoch: int, level: int) -> None:
    """trip 시 DB 영속화(듀얼라이트). best-effort — 실패해도 캐시 쿨다운은 이미 적용됨."""
    try:
        from datetime import datetime

        from apps.integrations.models import DMAccountBlock

        now_dt = datetime.fromtimestamp(int(time.time()), tz=UTC)
        DMAccountBlock.objects.update_or_create(
            external_account_id=ig_account_id,
            defaults={
                "cooldown_until": datetime.fromtimestamp(int(until_epoch), tz=UTC),
                "level": int(level),
                "last_tripped_at": now_dt,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("action block DB persist failed for %s", ig_account_id)


def _restore_action_block_from_db(ig_account_id: str):
    """캐시 miss 시 DMAccountBlock 에서 쿨다운 복원 + 캐시 재프라임. until(epoch) 또는 None."""
    try:
        from apps.integrations.models import DMAccountBlock

        row = DMAccountBlock.objects.filter(external_account_id=ig_account_id).first()
    except Exception:  # noqa: BLE001
        return None
    if not row or not row.cooldown_until:
        return None
    until = int(row.cooldown_until.timestamp())
    ttl = until - int(time.time())
    if ttl <= 0:
        return None
    cd_key, lvl_key = _ab_keys(ig_account_id)
    cache.set(cd_key, until, timeout=ttl)
    cache.set(lvl_key, int(row.level), timeout=30 * 24 * 3600)
    return until


def rehydrate_from_db() -> dict:
    """Redis 손실/DR failover 후 거버너 상태를 DB 에서 재구성하고 즉시 재개.

    1) SentDMLog.submitted_at 윈도우 카운트로 dmrate:h/m 재시드 → '최대 1h 동결' 함정 제거.
    2) DMAccountBlock 으로 dm:ab:cooldown/level 재시드 → 차단 계정 차단 유지.
    3) dmrate:alive 세팅 + dmrate:reset_until 삭제 → fail-closed 동결 해제.

    호출: dr_catchup STEP 0 + Celery worker_ready(active 사이트). 멱등.
    주의(DR failover): 사무실 DB 는 PITR ~RPO(1~2분) stale → 그만큼 적게 셀 수 있으나
    700(Meta 750 마진 50) + 분당 캡이 흡수. 동일서버 Redis 재시작은 DB 최신 → 100% 정확.
    """
    from datetime import datetime

    from django.db.models import Count

    from apps.integrations.models import DMAccountBlock, SentDMLog

    now = int(time.time())
    hour_epoch, min_epoch = now // 3600, now // 60
    hour_start = datetime.fromtimestamp(hour_epoch * 3600, tz=UTC)
    min_start = datetime.fromtimestamp(min_epoch * 60, tz=UTC)
    acct_field = "campaign__ig_connection__external_account_id"

    seeded_h = seeded_m = 0
    for r in (
        SentDMLog.objects.filter(submitted_at__gte=hour_start)
        .values(acct_field)
        .annotate(n=Count("id"))
    ):
        acct = r[acct_field]
        if acct:
            cache.set(f"dmrate:h:{acct}:{hour_epoch}", r["n"], timeout=3700)
            seeded_h += 1
    for r in (
        SentDMLog.objects.filter(submitted_at__gte=min_start)
        .values(acct_field)
        .annotate(n=Count("id"))
    ):
        acct = r[acct_field]
        if acct:
            cache.set(f"dmrate:m:{acct}:{min_epoch}", r["n"], timeout=70)
            seeded_m += 1

    ab_count = 0
    now_dt = datetime.fromtimestamp(now, tz=UTC)
    for row in DMAccountBlock.objects.filter(cooldown_until__gt=now_dt):
        until = int(row.cooldown_until.timestamp())
        ttl = until - now
        if ttl <= 0:
            continue
        cd_key, lvl_key = _ab_keys(row.external_account_id)
        cache.set(cd_key, until, timeout=ttl)
        cache.set(lvl_key, int(row.level), timeout=30 * 24 * 3600)
        ab_count += 1

    # 동결 해제: 카운터를 DB 로 정확히 복원했으므로 fail-closed 가 불필요.
    cache.set("dmrate:alive", 1, timeout=7 * 24 * 3600)
    cache.delete("dmrate:reset_until")
    logger.info(
        "rate_governor rehydrated: hour_accts=%s min_accts=%s action_blocks=%s",
        seeded_h,
        seeded_m,
        ab_count,
    )
    return {"hour_accounts": seeded_h, "minute_accounts": seeded_m, "action_blocks": ab_count}
