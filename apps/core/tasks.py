"""Core operational Celery tasks.

GATE-0 backup observability. The actual backups run from HOST cron
(deploy/backups/pg_backup.sh = daily logical dump, pgBackRest = WAL PITR) so they
survive even when the app/broker is sick. This task only *watches* the continuous
WAL-archiving health that nothing else monitors in real time, using the existing DB
connection (no extra R2 credentials needed), and alerts via the existing Telegram bot.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import connection

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
