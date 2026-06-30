"""
Celery configuration for Instagram Service Backend
"""

import logging
import os

from celery import Celery
from celery.exceptions import Reject
from celery.signals import task_postrun, task_prerun, worker_ready

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("instagram_service")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

_logger = logging.getLogger(__name__)


@task_prerun.connect
def _dr_active_site_gate(task_id=None, task=None, **kwargs):
    """DR split-brain 방지 — passive 사이트에서는 모든 태스크를 거부(requeue=False).

    active 사이트(콜로)에서는 no-op. failover 후 office 가 active 면 office 워커가 정상 실행.
    게이트 평가 자체가 실패하면 통과(가용성 우선; 미들웨어/거버너가 2차 방어). 멱등 태스크라
    전환 경계의 짧은 중복/드랍은 무손실.  (DR_IMPLEMENTATION_PLAN.md §5.4)
    """
    try:
        from apps.core.site_control import is_active_site

        passive = not is_active_site()
    except Exception:  # noqa: BLE001 — 게이트 오류는 통과
        return
    if passive:
        _logger.warning("DR gate: rejecting task %s on passive site", getattr(task, "name", "?"))
        raise Reject(requeue=False)


@worker_ready.connect
def _dr_rehydrate_governor(**kwargs):
    """워커 부팅 시(예: Redis 재시작/failover 후) 거버너 카운터를 DB 에서 재수화.

    active 사이트에서만 수행. 이로써 'Redis 손실 → 최대 1h DM 동결' 함정 없이 즉시 정확 재개.
    (DR_IMPLEMENTATION_PLAN.md §7.1)
    """
    try:
        from apps.core.site_control import is_active_site
        from apps.integrations.rate_governor import rehydrate_from_db

        if is_active_site():
            rehydrate_from_db()
    except Exception:  # noqa: BLE001 — best-effort
        _logger.exception("worker_ready governor rehydrate failed")


@task_postrun.connect
def _dr_worker_heartbeat(**kwargs):
    """워커가 태스크를 실제 consume 했음을 알리는 heartbeat(/healthz/diag 가 읽음).

    active 사이트에서 큐가 흐르는지(워커 stall 감지) 신호. best-effort — 무트래픽이면 stale
    로 보이나, 감지기가 queue_depth 와 상관(빈 큐 + stale = 무해)으로 처리하므로 오탐 아님.
    (DR_IMPLEMENTATION_PLAN.md §5, 계획서 Phase A1)
    """
    try:
        from apps.core.site_control import touch_worker_heartbeat

        touch_worker_heartbeat()
    except Exception:  # noqa: BLE001 — best-effort
        pass


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
