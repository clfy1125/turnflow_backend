"""내부 컨트롤플레인 — 외부 스케줄러 tick.

POST /api/v1/internal/scheduler/tick
  외부 Cron(CF Cron primary + Google Cloud Scheduler secondary)이 60초마다 호출한다.
  실제 due 판단은 DB(ScheduledJob.next_due_at)에서 하고, due 잡을 Celery 로 **enqueue만** 한다
  (태스크 본문은 인라인 실행하지 않음 → tick 은 절대 블록되지 않는다).

  단일 발사 보장: ``select_for_update(skip_locked=True)`` 로 due 행을 잠그고 같은 트랜잭션에서
  ``next_due_at`` 을 전진시킨다 → 동시 tick(CF + GCS)에도 '윈도우당 정확히 1회' 가 DB 불변식.

  active_site 게이트: SITE_ID != active_site 면 409(아무것도 fire 안 함). active_site 불명 → 503.

상세: DR_IMPLEMENTATION_PLAN.md §6.2.
"""

from __future__ import annotations

import logging
import urllib.request

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.internal_auth import verify_scheduler_request
from apps.core.site_control import get_site_state, touch_scheduler_heartbeat

logger = logging.getLogger(__name__)


def _err(code: int, reason: str, **extra):
    return JsonResponse(
        {
            "success": False,
            "error": {"code": code, "message": reason, "details": {"reason": reason, **extra}},
        },
        status=code,
    )


def _send(app, task_name: str, queue):
    if queue:
        app.send_task(task_name, queue=queue)
    else:
        app.send_task(task_name)


def _run_due_jobs():
    """due 잡을 잠그고 next_due_at 전진 후 enqueue. (fired keys, skipped count) 반환."""
    from apps.core.models import ScheduledJob
    from config.celery import app as celery_app

    now = timezone.now()
    fired: list[str] = []

    with transaction.atomic():
        # skip_locked: 다른 tick 이 잠근 행은 이 tick 의 집합에서 제외 → 이중 발사 없음.
        due = list(
            ScheduledJob.objects.select_for_update(skip_locked=True).filter(
                enabled=True, next_due_at__lte=now
            )
        )
        for job in due:
            job.next_due_at = job.compute_next_due(after=now)
            job.last_run_at = now
            job.last_status = "enqueued"
            job.save(update_fields=["next_due_at", "last_run_at", "last_status", "updated_at"])
            # 커밋 후 enqueue (롤백 시 유령 enqueue 방지)
            transaction.on_commit(
                lambda tn=job.task, q=(job.queue or None): _send(celery_app, tn, q)
            )
            fired.append(job.key)

    return fired, 0


def _ping_healthchecks():
    url = getattr(settings, "HEALTHCHECKS_TICK_URL", "") or ""
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=3)  # noqa: S310 (고정 모니터 URL)
    except Exception:  # noqa: BLE001 — best-effort
        pass


@csrf_exempt
@require_POST
def scheduler_tick(request):
    ok, reason = verify_scheduler_request(request)
    if not ok:
        code = 403 if reason in ("bad_secret", "ip_not_allowed") else 503
        return _err(code, reason)

    state = get_site_state()
    if not state.get("active_site") or state.get("active_site") == "__unknown__":
        return _err(503, "active_site_unknown")
    if state.get("active_site") != settings.SITE_ID or state.get("mode") != "live":
        return _err(409, "not_active_site", active_site=state.get("active_site"))

    try:
        fired, skipped = _run_due_jobs()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler_tick: due-job run failed")
        return _err(500, "tick_failed")

    touch_scheduler_heartbeat()
    _ping_healthchecks()
    return JsonResponse({"fired": fired, "skipped": skipped, "now": timezone.now().isoformat()})
