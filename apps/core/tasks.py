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


@shared_task(queue="billing")
def backup_health_check() -> dict:
    """Monitor Layer-2 (WAL archiving) health via pg_stat_archiver. Beat: every 30 min.

    Alerts on: archiver failures, or WAL not archived for > WAL_LAG_ALERT_SECONDS.
    Returns a small status dict for logging/Flower. Never raises (best-effort monitor).
    """
    status: dict = {"ok": True, "checked": "pg_stat_archiver"}
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

        if not row:
            return {"ok": True, "note": "pg_stat_archiver empty (archiving not enabled yet)"}

        archived, failed, last_arch, last_fail, lag = row
        status.update(
            archived_count=archived,
            failed_count=failed,
            lag_seconds=lag,
        )

        problems = []
        # A recent failure that is newer than the last success → archiving is broken now.
        if last_fail and (not last_arch or last_fail > last_arch):
            problems.append(f"WAL archive FAILING (failed_count={failed}, last_failed={last_fail})")
        # Archiving stalled.
        if last_arch is None:
            problems.append("WAL never archived (archive_mode/archive_command not effective)")
        elif lag is not None and lag > WAL_LAG_ALERT_SECONDS:
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
