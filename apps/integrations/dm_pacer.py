"""DM 스무스 페이서 (v4.3) — 계정별 토큰버킷/리키버킷 + 지터.

WHY(설계 배경 — 2026-07-09 결정):
기존 방식(시간당 캡 700 소진 → 다음 시각경계까지 defer)은
  (a) 윈도우 경계에 발송이 몰리고(버스트),
  (b) DR/Redis 유실 시 카운터 복원 오차를 흡수할 마진(750-700)이 필요했으며,
  (c) 캠페인별 200/hr 소프트캡 등 레이어가 겹쳐 복잡했다.
대신 **발송 간격 자체를 계정 단위로 직렬화**하면:
  - 사설답장 물리한도(Meta 750/hr)를 구조적으로 초과 불가 (평균 5.0s ≈ 720/hr)
  - Redis 유실 시에도 원자적 슬롯 클레임이 즉시 재직렬화 → DR 마진/동결 불필요
    (최악의 경우 포인터 리셋 = 다음 1건이 즉시 나가는 것뿐, 과속 불가)
  - 정확히 N초 간격은 그 자체가 봇 지문 → 간격마다 uniform 지터로 흔든다

버킷 (Meta 레이트리밋 버킷과 1:1 — 실측/공식문서 확인 2026-07-09):
  - PRIVATE_REPLY (케이스 A): 댓글 기반 오프닝 DM (recipient.comment_id).
    Meta 한도 750 calls/hour/계정 → 기본 3~7s 지터 (평균 5.0s ≈ 720/hr).
  - SEND_API (케이스 B): 유저 개시 스레드 응답 — reward/재안내/스토리답장 (recipient.id).
    Meta 한도 100 calls/sec (시간당 캡 없음) → 기본 1~3s 지터 (봇 지문 회피 목적).

핵심 메커니즘 — 원자적 슬롯 클레임 (Redis Lua):
    slot = max(포인터, now); 포인터 = slot + jitter_gap
동시에 몇 건이 클레임해도 각자 서로 다른 슬롯을 받아 자동 직렬화된다(별도 큐 인프라 불필요).
클레임된 슬롯은 SentDMLog.next_retry_at 에 기록(재시작 안전) + 로그별 claimed 플래그로
재진입 시 재클레임을 막는다(포인터 폭주 방지). 에러 백오프 재시도는 플래그가 없어
새 슬롯을 받는다(새 POST 시도 = 새 페이싱 — Meta 가 보는 것과 일치).

주의: Meta 는 발송 응답에 사용량 헤더를 주지 않는다(실측 확인) — self-pacing 이 유일한 진실원.
시간당 740 백스톱(rate_governor)은 페이서 버그 대비 최후 방어선으로만 유지(페이서 720 위·Meta 750 아래).
"""

from __future__ import annotations

import logging
import random
import time

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q

logger = logging.getLogger(__name__)

# 버킷 식별자 (캐시 키에 들어가므로 짧게)
BUCKET_PRIVATE_REPLY = "pr"
BUCKET_SEND_API = "sa"

# Fix 2 — 포인터 자가치유: 포인터가 '실제 마지막 예약'보다 이만큼(초) 넘게 앞서 있으면
# 삭제/일시중지로 생긴 빈 슬롯(홀)로 보고 회수한다. 클레임 write-lag(인플라이트 ~동시성×gap)를
# 흡수할 만큼 넉넉히 잡아 활성 발송 중 오탐 리셋을 방지한다.
POINTER_RECLAIM_SLACK_SECONDS = 300

# 버킷별 기본 지터 범위(초). settings.DM_PACER_* 로 오버라이드.
#  - pr: 평균 5.0s ≈ 720/hr < Meta 750/hr (마진 ~4%)
#  - sa: Meta 캡과 무관 — 봇 지문 회피용 소간격
_DEFAULT_RANGES = {
    BUCKET_PRIVATE_REPLY: (3.0, 7.0),
    BUCKET_SEND_API: (1.0, 3.0),
}

# 슬롯이 이 이내로 임박했으면 "지금 발송"으로 취급 (celery countdown 오차 흡수)
GRACE_SECONDS = 2.0

# slot = max(pointer, now); pointer = slot + gap. ms 정수 연산(Lua number→integer 절삭 회피).
# TTL = 포인터가 now 로부터 떨어진 거리 + 1h 여유 — 대형 백로그에서도 포인터가 만료로
# 리셋되지 않게 한다(리셋돼도 과속은 아니지만 예약과 신규 클레임이 겹칠 수 있음).
_LUA_CLAIM = """
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local now = tonumber(ARGV[1])
local gap = tonumber(ARGV[2])
local slot = nxt
if now > slot then slot = now end
redis.call('SET', KEYS[1], slot + gap, 'PX', math.floor(slot + gap - now + 3600000))
return slot
"""


def bucket_for_log(log) -> str:
    """이 로그가 소비할 Meta 레이트리밋 버킷.

    ★ send_dm_task 의 발송 라우팅 분기와 반드시 동일해야 한다:
    comment_id 있음 + parent_log 없음 → Private Reply(케이스 A), 그 외 → Send API(케이스 B).
    """
    if log.comment_id and log.parent_log_id is None:
        return BUCKET_PRIVATE_REPLY
    return BUCKET_SEND_API


def _range_for(bucket: str) -> tuple[float, float]:
    if bucket == BUCKET_PRIVATE_REPLY:
        lo = getattr(settings, "DM_PACER_PRIVATE_REPLY_MIN_S", None)
        hi = getattr(settings, "DM_PACER_PRIVATE_REPLY_MAX_S", None)
    else:
        lo = getattr(settings, "DM_PACER_SEND_API_MIN_S", None)
        hi = getattr(settings, "DM_PACER_SEND_API_MAX_S", None)
    d_lo, d_hi = _DEFAULT_RANGES[bucket]
    lo = float(lo) if lo else d_lo
    hi = float(hi) if hi else d_hi
    if hi < lo:  # 설정 실수 방어
        lo, hi = d_lo, d_hi
    return lo, hi


def _pointer_key(ig_account_id: str, bucket: str) -> str:
    return f"dmpace:{bucket}:{ig_account_id}"


def _flag_key(log_id) -> str:
    return f"dmpace:claimed:{log_id}"


def claim_slot(ig_account_id: str, bucket: str) -> float:
    """계정×버킷의 다음 발송 슬롯(epoch seconds)을 원자적으로 예약해 반환.

    반환값이 now 이하이면 즉시 발송 가능(빈 버킷). Redis(Lua) 우선, 실패 시
    django cache 폴백(원자성 없음 — 테스트/로컬 안전망, 프로덕션은 Redis).
    """
    lo, hi = _range_for(bucket)
    gap_ms = int(random.uniform(lo, hi) * 1000)
    now_ms = int(time.time() * 1000)
    key = _pointer_key(ig_account_id, bucket)

    try:
        from django_redis import get_redis_connection

        conn = get_redis_connection("default")
        slot_ms = int(conn.eval(_LUA_CLAIM, 1, key, now_ms, gap_ms))
    except Exception:  # noqa: BLE001 — 캐시 폴백 (LocMem 등)
        nxt = cache.get(key)
        slot_ms = max(int(nxt or 0), now_ms)
        ttl = int((slot_ms + gap_ms - now_ms) / 1000) + 3600
        cache.set(key, slot_ms + gap_ms, timeout=ttl)

    return slot_ms / 1000.0


def peek_next_slot(ig_account_id: str, bucket: str) -> float | None:
    """포인터(다음 클레임이 받을 슬롯 하한, epoch seconds) 읽기 전용 조회 — 게이지/ETA 용.

    포인터 없음(유휴 버킷) 이면 None. 클레임하지 않는다(부작용 없음).
    """
    key = _pointer_key(ig_account_id, bucket)
    try:
        from django_redis import get_redis_connection

        conn = get_redis_connection("default")
        raw = conn.get(key)
        if raw is None:
            return None
        return int(raw) / 1000.0
    except Exception:  # noqa: BLE001
        val = cache.get(key)
        return (int(val) / 1000.0) if val else None


def avg_gap_seconds(bucket: str) -> float:
    """버킷 평균 발송 간격(초) — 미클레임 백로그의 ETA 추정용."""
    lo, hi = _range_for(bucket)
    return (lo + hi) / 2.0


def bucket_q(bucket: str) -> Q:
    """SentDMLog 쿼리셋을 버킷으로 필터하는 Q (send_dm_task 라우팅·bucket_for_log 와 동일 규칙).

    pr(Private Reply): comment_id 있음 + parent_log 없음. sa(Send API): 그 외.
    ETA 계산(verification_views)과 포인터 회수(reconcile)가 공유해 정의 divergence 방지.
    """
    if bucket == BUCKET_PRIVATE_REPLY:
        return Q(parent_log__isnull=True) & ~Q(comment_id="")
    return Q(comment_id="") | Q(parent_log__isnull=False)


# slack 만큼 넘는 phantom 만 회수(activebusy 오탐 방지). floor(=max(now, 마지막 예약 slot))로 당김.
_LUA_RECLAIM = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local floor = tonumber(ARGV[1])
local slack = tonumber(ARGV[2])
if cur > floor + slack then
    redis.call('SET', KEYS[1], floor, 'PX', 3700000)
    return cur - floor
end
return 0
"""


def iter_active_pointers():
    """활성 페이서 포인터(dmpace:pr:* / dmpace:sa:*)를 SCAN 해 (bucket, account_id) 산출.

    Redis 전용(포인터는 get_redis_connection 로 raw 키 저장). Redis 미가용(LocMem 등)이면
    아무것도 산출하지 않는다 — reconcile 은 프로덕션(Redis) 에서만 의미가 있다.
    """
    try:
        from django_redis import get_redis_connection

        conn = get_redis_connection("default")
    except Exception:  # noqa: BLE001
        return
    for bucket in (BUCKET_PRIVATE_REPLY, BUCKET_SEND_API):
        prefix = f"dmpace:{bucket}:"
        try:
            for raw in conn.scan_iter(match=f"{prefix}*", count=200):
                key = raw.decode() if isinstance(raw, bytes) else raw
                acct = key[key.rfind(prefix) + len(prefix) :]
                if acct:
                    yield bucket, acct
        except Exception:  # noqa: BLE001
            continue


def reclaim_pointer(ig_account_id: str, bucket: str, floor_ts: float) -> float:
    """포인터가 floor_ts + slack 보다 앞서 있으면 floor_ts 로 원자적으로 당기고 회수 초를 반환.

    floor_ts = max(now, 이 계정×버킷의 아직 대기중인 마지막 예약 슬롯). 즉 실제 예약 뒤로는
    절대 당기지 않고(충돌 방지), now 아래로도 안 내려간다(과속 불가). 회수 안 하면 0.0.
    """
    key = _pointer_key(ig_account_id, bucket)
    floor_ms = int(floor_ts * 1000)
    slack_ms = int(POINTER_RECLAIM_SLACK_SECONDS * 1000)
    try:
        from django_redis import get_redis_connection

        conn = get_redis_connection("default")
        reclaimed_ms = int(conn.eval(_LUA_RECLAIM, 1, key, floor_ms, slack_ms))
    except Exception:  # noqa: BLE001
        return 0.0
    return reclaimed_ms / 1000.0


def pacer_gate(ig_account_id: str, log):
    """발송 직전 페이싱 게이트.

    Returns:
        None                — 지금 발송 진행 (빈 버킷 즉시 슬롯 / 예약 슬롯 도래).
        (wait_s, bucket)    — wait_s 초 뒤 슬롯 — 호출부는 defer(QUEUED+next_retry_at).

    재진입 규약: 클레임 시 로그별 플래그(dmpace:claimed:{log_id})에 슬롯을 저장한다.
    재진입(리큐 워커) 시 플래그가 있으면 **재클레임하지 않고** 그 슬롯을 따른다
    (재클레임하면 포인터가 이중 전진해 대기열 끝으로 밀림). 플래그가 없으면(에러 백오프
    재시도·Redis 유실) 새 슬롯을 클레임한다 — 새 POST 시도 = 새 페이싱이므로 올바르다.
    """
    bucket = bucket_for_log(log)
    flag_key = _flag_key(log.id)
    now = time.time()

    claimed = cache.get(flag_key)
    if claimed is not None:
        wait = float(claimed) - now
        if wait > GRACE_SECONDS:
            return (wait, bucket)  # 예약 슬롯 미도래 — 재클레임 없이 대기 유지
        cache.delete(flag_key)  # 슬롯 도래 — 소비하고 발송 진행
        return None

    slot = claim_slot(ig_account_id, bucket)
    wait = slot - now
    if wait > GRACE_SECONDS:
        cache.set(flag_key, slot, timeout=int(wait) + 3600)
        return (wait, bucket)
    return None  # 빈/여유 버킷 — 즉시 발송
