"""P3f — 계정당 DM 발송 시간당 **백스톱** + Action Block 서킷 브레이커 (Redis 기반).

v4.3 역할 변경: 실제 발송 페이싱은 dm_pacer(계정별 지터 슬롯 직렬화 — 사설답장 평균 5.0s,
Send API 1~3s)가 담당한다. 이 모듈은 두 가지만 남긴다:
  1) check() — 시간당 백스톱(기본 740, 페이서 720 위·Meta 750 아래). 페이서가 정상이면
     절대 걸리지 않는 최후 방어선. (분당 캡·Redis flush 동결은 페이서가 대체해 제거.)
  2) Action Block(code 368) 서킷 브레이커 — 차단 중 재시도가 차단을 연장시키므로
     계정 발송을 에스컬레이팅 쿨다운(24h→×2, 상한 7d)으로 정지. DB 듀얼라이트(DR 생존).

설계 원칙:
- 멀티테넌트 정확성: 한도는 **IG 계정 단위**로 건다(Meta 한도가 계정당이므로).
- 발송을 "드롭"하지 않고 "지연(defer)" 한다 — 호출부는 차단 시 retry_after 만큼 뒤로 재스케줄.
- Redis 고정 윈도우 카운터(원자적 INCR) — 단순/견고. django_redis(/1) 사용.

플랜별 한도 테이블(_DEFAULT_LIMITS)은 캡 해제(IG_PRIVATE_REPLY_HOURLY_CAP=0) 시 시간당
폴백값으로만 쓰인다. (분당 값은 v4.3부터 DM 경로에서 미사용 — moderation 은 별도 네임스페이스.)
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
# plan per_hour 값이 이를 초과(pro=1200/enterprise=5000)해도 백스톱(기본 740)으로 강제 캡한다.
# 740 = 페이서 자연율(≈720/hr) 위, Meta 750 아래 — 페이서 정상 시 안 걸리는 최후 방어선.
# settings.IG_PRIVATE_REPLY_HOURLY_CAP 로 오버라이드(0/None 이면 캡 미적용).
PRIVATE_REPLY_HOURLY_CAP = 740


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
    """발송 1건이 허용되는지 검사하고 카운터를 소비한다 (v4.3 — 시간당 **백스톱** 전용).

    v4.3 부터 실제 페이싱은 dm_pacer(계정별 지터 슬롯 직렬화)가 담당한다. 여기는
    페이서 버그/우회 경로 대비 최후 방어선(시간당 740)만 남긴다:
      - 분당 버스트 캡 제거 — 페이서 간격(지터)이 버스트를 구조적으로 차단.
      - Redis flush fail-closed 동결 제거 — 페이서는 포인터가 유실돼도 원자 클레임이
        즉시 재직렬화하므로 과속이 불가능하다. 카운터 유실은 worker_ready 의
        rehydrate_from_db 가 DB 에서 재시드(백스톱 정확도 회복)한다.

    Returns Decision(allowed, retry_after, reason).
    차단되면 호출부는 발송하지 말고 retry_after 후 재시도(재스케줄)해야 한다.
    """
    if not ig_account_id:
        return Decision(allowed=True)  # 식별 불가 시 거버너 우회(안전 측 — 차단하지 않음)

    now = int(time.time())
    # 카운터 유실 감지(센티넬) — v4.3: 동결하지 않고 경고 + 재시드 유도만.
    if cache.get("dmrate:alive") is None:
        cache.set("dmrate:alive", 1, timeout=7 * 24 * 3600)
        logger.warning(
            "rate_governor: cache counters appear flushed — hourly backstop undercounts "
            "until rehydrate_from_db; dm_pacer keeps the physical rate safe"
        )

    per_hour, _per_minute = _limits_for(plan)
    # Meta 계정당 750/hr Private Reply 가 실제 물리 상한 → plan 과 무관하게 이 값(백스톱 740)을
    # 시간당 상한으로 사용한다. 0/None 이면 캡 해제 → plan per_hour 로 폴백.
    cap = getattr(settings, "IG_PRIVATE_REPLY_HOURLY_CAP", PRIVATE_REPLY_HOURLY_CAP)
    if cap and cap > 0:
        per_hour = cap

    hour_key = f"dmrate:h:{ig_account_id}:{now // 3600}"
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

    1) SentDMLog.submitted_at 윈도우 카운트로 dmrate:h 재시드 (시간당 백스톱 정확도 회복).
       (v4.3: 분당 카운터는 dm_pacer 가 대체해 제거 — 재시드 불필요.)
    2) DMAccountBlock 으로 dm:ab:cooldown/level 재시드 → 차단 계정 차단 유지.
    3) dmrate:alive 세팅 (+ 구버전 dmrate:reset_until 잔재 삭제).

    호출: dr_catchup STEP 0 + Celery worker_ready(active 사이트). 멱등.
    주의(DR failover): 사무실 DB 는 PITR ~RPO(1~2분) stale → 그만큼 적게 셀 수 있으나
    페이싱은 dm_pacer 가 구조적으로 보장(평균 5.0s)하므로 백스톱 오차는 위험하지 않다.
    """
    from datetime import datetime

    from django.db.models import Count

    from apps.integrations.models import DMAccountBlock, SentDMLog

    now = int(time.time())
    hour_epoch = now // 3600
    hour_start = datetime.fromtimestamp(hour_epoch * 3600, tz=UTC)
    acct_field = "campaign__ig_connection__external_account_id"

    seeded_h = 0
    for r in (
        SentDMLog.objects.filter(submitted_at__gte=hour_start)
        .values(acct_field)
        .annotate(n=Count("id"))
    ):
        acct = r[acct_field]
        if acct:
            cache.set(f"dmrate:h:{acct}:{hour_epoch}", r["n"], timeout=3700)
            seeded_h += 1

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

    cache.set("dmrate:alive", 1, timeout=7 * 24 * 3600)
    cache.delete("dmrate:reset_until")  # 구버전 fail-closed 잔재 정리 (v4.3 에서 동결 제거)
    logger.info("rate_governor rehydrated: hour_accts=%s action_blocks=%s", seeded_h, ab_count)
    return {"hour_accounts": seeded_h, "action_blocks": ab_count}
