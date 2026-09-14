"""Core operational Celery tasks.

두 가지 운영 감시가 산다.

1) GATE-0 backup observability.
2) 외부 LLM(DeepSeek) 계정 잔액 감시 — 2026-09-11 13:34 에 잔액이 0 이 되어 AI 페이지 생성
   53건이 실패하고 인스타 리포트 6건이 "AI 문장 없는 템플릿 판"으로 배달되기까지 **3일 동안
   아무도 몰랐다**. 사람이 대시보드를 들여다봐야만 아는 상태를 없애는 것이 이 감시의 목적.

GATE-0 backup observability. The actual backups run from HOST cron
(deploy/backups/pg_backup.sh = daily logical dump, pgBackRest = WAL PITR) so they
survive even when the app/broker is sick. This task only *watches* the continuous
WAL-archiving health that nothing else monitors in real time, using the existing DB
connection (no extra R2 credentials needed), and alerts via the existing Telegram bot.
"""

from __future__ import annotations

import logging

import requests
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from apps.core.telegram import send_telegram_notification

logger = logging.getLogger(__name__)

# Alert if WAL has not been archived within this many seconds (archive_timeout=60 → 5min = clearly stuck).
WAL_LAG_ALERT_SECONDS = 300


def read_archiver_status() -> dict:
    """pg_stat_archiver 한 줄을 dict 로 읽는다 — backup_health_check 와 /healthz/diag 공용.

    절대 raise 하지 않는다(측정 실패는 enabled=None+error 로). 반환 키:
      enabled(True/False/None), archived_count, failed_count, last_archived_time(iso/None),
      last_failed_time(iso/None), lag_seconds(int/None), broken(bool).
    """
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT archived_count,
                       failed_count,
                       last_archived_time,
                       last_failed_time,
                       EXTRACT(EPOCH FROM (now() - last_archived_time))::bigint AS lag_seconds
                FROM pg_stat_archiver
                """
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — 측정 실패(예: 권한/연결)
        return {"enabled": None, "error": str(exc)}

    if not row:
        return {"enabled": False}

    archived, failed, last_arch, last_fail, lag = row
    # 마지막 실패가 마지막 성공보다 최신 → 지금 아카이빙이 깨진 상태.
    broken = bool(last_fail and (not last_arch or last_fail > last_arch))
    return {
        "enabled": True,
        "archived_count": archived,
        "failed_count": failed,
        "last_archived_time": last_arch.isoformat() if last_arch else None,
        "last_failed_time": last_fail.isoformat() if last_fail else None,
        "lag_seconds": int(lag) if lag is not None else None,
        "broken": broken,
    }


@shared_task(queue="billing")
def backup_health_check() -> dict:
    """Monitor Layer-2 (WAL archiving) health via pg_stat_archiver. Beat: every 30 min.

    Alerts on: archiver failures, or WAL not archived for > WAL_LAG_ALERT_SECONDS.
    Returns a small status dict for logging/Flower. Never raises (best-effort monitor).
    """
    status: dict = {"ok": True, "checked": "pg_stat_archiver"}
    try:
        info = read_archiver_status()
        if info.get("enabled") is False:
            return {"ok": True, "note": "pg_stat_archiver empty (archiving not enabled yet)"}
        if info.get("enabled") is None:  # 측정 실패
            logger.warning("backup_health_check error: %s", info.get("error"))
            return {"ok": False, "error": info.get("error")}

        status.update(
            archived_count=info.get("archived_count"),
            failed_count=info.get("failed_count"),
            lag_seconds=info.get("lag_seconds"),
        )

        problems = []
        if info.get("broken"):
            problems.append(
                f"WAL archive FAILING (failed_count={info.get('failed_count')}, "
                f"last_failed={info.get('last_failed_time')})"
            )
        if info.get("last_archived_time") is None:
            problems.append("WAL never archived (archive_mode/archive_command not effective)")
        else:
            lag = info.get("lag_seconds")
            if lag is not None and lag > WAL_LAG_ALERT_SECONDS:
                problems.append(f"WAL archive lag {lag}s > {WAL_LAG_ALERT_SECONDS}s")

        if problems:
            status["ok"] = False
            send_telegram_notification(
                "🔴 *TurnFlow backup* WAL archiving problem:\n- " + "\n- ".join(problems)
            )
            logger.error("backup_health_check problems: %s", problems)
    except Exception as exc:  # noqa: BLE001 — monitor must never crash the worker
        logger.warning("backup_health_check error: %s", exc)
        status = {"ok": False, "error": str(exc)}
    return status


# ──────────────────────────────────────────────────────────────────────────────
# DeepSeek 잔액 감시
# ──────────────────────────────────────────────────────────────────────────────

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

# 마지막으로 "알린" 등급과 시각. 등급이 나빠지면 즉시 재알림, 같은 등급이면
# DEEPSEEK_BALANCE_REPEAT_HOURS 마다 1회만. 캐시가 날아가면 최악이 중복 1건이라 안전하다.
_ALERT_STATE_KEY = "ops:deepseek_balance:alert_state"
# 소진 속도 추정용 직전 표본 (ts, balance). 잔액이 늘면(충전) 표본을 초기화한다.
_SAMPLE_KEY = "ops:deepseek_balance:sample"
_STATE_TTL = 60 * 60 * 24 * 30  # 30일

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"
_LEVEL_RANK = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_CRIT: 2}


def fetch_deepseek_balance(timeout: int = 10) -> dict:
    """DeepSeek 계정 잔액 조회. **절대 raise 하지 않는다**(감시가 워커를 죽이면 안 된다).

    반환: {ok, available, balance_usd, currency, error}
      - ok=False 는 '조회 실패'지 '잔액 부족'이 아니다. 둘을 섞으면 네트워크 오류로
        가짜 🔴 가 나가고, 그게 반복되면 진짜 경보를 무시하게 된다.
    """
    key = (getattr(settings, "DEEPSEEK_API_KEY", "") or "").strip()
    if not key:
        return {"ok": False, "error": "DEEPSEEK_API_KEY 미설정", "balance_usd": None}
    try:
        r = requests.get(
            DEEPSEEK_BALANCE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if not r.ok:
            return {"ok": False, "error": f"HTTP {r.status_code}", "balance_usd": None}
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "balance_usd": None}

    infos = d.get("balance_infos") or []
    usd = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else {})
    try:
        balance = float(usd.get("total_balance"))
    except (TypeError, ValueError):
        balance = None
    return {
        "ok": True,
        "available": bool(d.get("is_available")),
        "balance_usd": balance,
        "currency": usd.get("currency") or "USD",
        "error": "",
    }


def _classify(info: dict, warn: float, crit: float) -> str:
    """잔액 → 등급. is_available=False 는 금액과 무관하게 crit(호출이 이미 402 로 거절된다)."""
    if not info.get("available"):
        return LEVEL_CRIT
    bal = info.get("balance_usd")
    if bal is None:
        return LEVEL_OK  # 금액 파싱 실패인데 available=True 면 서비스는 되는 상태
    if bal <= crit:
        return LEVEL_CRIT
    if bal <= warn:
        return LEVEL_WARN
    return LEVEL_OK


def _runway(balance: float | None) -> tuple[float | None, float | None]:
    """직전 표본과 비교해 (일당 소진액, 남은 일수) 추정. 표본 없거나 충전이면 (None, None)."""
    now_ts = timezone.now().timestamp()
    prev = cache.get(_SAMPLE_KEY)
    if balance is not None:
        cache.set(_SAMPLE_KEY, {"ts": now_ts, "balance": balance}, _STATE_TTL)
    if not prev or balance is None:
        return None, None
    elapsed_days = (now_ts - float(prev.get("ts", now_ts))) / 86400
    spent = float(prev.get("balance", 0)) - balance
    if elapsed_days < 0.02 or spent <= 0:  # 30분 미만이거나 충전 → 추정 불가
        return None, None
    per_day = spent / elapsed_days
    return per_day, (balance / per_day if per_day > 0 else None)


@shared_task(queue="billing")
def check_deepseek_balance() -> dict:
    """DeepSeek 잔액 감시 → 임계 이하면 Telegram 경보. Beat/tick: 3시간마다.

    경보 규칙(이 감시의 핵심은 "울려야 할 때만 운다"):
      - 등급이 **나빠질 때** 즉시 1회
      - 같은 등급이 이어지면 DEEPSEEK_BALANCE_REPEAT_HOURS(기본 24h) 마다 1회만
      - **경보가 나갔던 경우에만** 회복(🟢) 을 알린다 — 조용히 지나간 건 조용히 끝낸다
    """
    warn = float(getattr(settings, "DEEPSEEK_BALANCE_WARN_USD", 10.0))
    crit = float(getattr(settings, "DEEPSEEK_BALANCE_CRIT_USD", 3.0))
    repeat_h = int(getattr(settings, "DEEPSEEK_BALANCE_REPEAT_HOURS", 24))

    info = fetch_deepseek_balance()
    if not info.get("ok"):
        # 조회 실패는 경보하지 않는다(가짜 🔴 방지). 로그만 남기고 다음 주기에 재시도.
        logger.warning("check_deepseek_balance 조회 실패: %s", info.get("error"))
        return {"ok": False, "error": info.get("error")}

    balance = info.get("balance_usd")
    level = _classify(info, warn, crit)
    per_day, days_left = _runway(balance)

    state = cache.get(_ALERT_STATE_KEY) or {}
    last_level = state.get("level", LEVEL_OK)
    last_ts = float(state.get("ts", 0))
    now_ts = timezone.now().timestamp()

    result = {
        "ok": True,
        "balance_usd": balance,
        "level": level,
        "per_day_usd": round(per_day, 3) if per_day else None,
        "days_left": round(days_left, 1) if days_left else None,
        "alerted": False,
    }

    if level == LEVEL_OK:
        if _LEVEL_RANK[last_level] > 0:  # 경보가 나갔던 에피소드만 회복을 알린다
            send_telegram_notification(
                f"🟢 *DeepSeek 잔액 회복* — ${balance:,.2f} (임계 ${warn:,.0f})"
            )
            result["alerted"] = True
        cache.set(_ALERT_STATE_KEY, {"level": LEVEL_OK, "ts": now_ts}, _STATE_TTL)
        return result

    worsened = _LEVEL_RANK[level] > _LEVEL_RANK[last_level]
    stale = (now_ts - last_ts) >= repeat_h * 3600
    if not (worsened or stale):
        return result

    icon = "🔴" if level == LEVEL_CRIT else "🟡"
    head = "소진 (호출이 402 로 거절됩니다)" if not info.get("available") else "부족"
    lines = [f"{icon} *DeepSeek 잔액 {head}* — 현재 **${balance:,.2f}**"]
    if per_day:
        lines.append(
            f"소진 속도 ≈ ${per_day:,.2f}/일"
            + (f" → 잔여 약 {days_left:.1f}일" if days_left else "")
        )
    lines.append(f"임계: 경고 ${warn:,.0f} / 긴급 ${crit:,.0f}")
    lines.append("영향: AI 페이지 생성(bio_remake) · 인스타 리포트 문장 합성")
    lines.append("충전: https://platform.deepseek.com/top_up")
    send_telegram_notification("\n".join(lines))
    cache.set(_ALERT_STATE_KEY, {"level": level, "ts": now_ts}, _STATE_TTL)
    result["alerted"] = True
    logger.warning("DeepSeek 잔액 경보: level=%s balance=%s", level, balance)
    return result
