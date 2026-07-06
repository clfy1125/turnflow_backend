"""DR 감지 보조 — Celery 큐 깊이 + deferred DM 적체 (회색지대 신호).

`/healthz/diag` 가 호출한다. 모두 best-effort(예외 → 빈값/None), 핫패스 아님(감지기가 60s 폴).
"앱이 200 을 주는데 일이 안 흐르는" 정체(stall)를 감지기에 노출하는 게 목적이다.

상세: DR 계획서 Phase A1, DR_IMPLEMENTATION_PLAN.md §5.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# 워커가 consume 하는 Redis 리스트 키 == 큐 이름. CELERY_TASK_ROUTES 의 큐 + 기본 큐.
# (config/settings/base.py CELERY_TASK_ROUTES 와 일치시킬 것)
_KNOWN_QUEUES = ("dm_send", "webhook_followup", "verify", "snapshot", "billing", "ai_jobs", "celery")


def queue_depths() -> dict[str, int]:
    """브로커(Redis DB0)의 큐별 LLEN. 측정 실패 시 빈 dict.

    dm_send 가 단조 증가하면서 worker_heartbeat 도 stale → 워커 죽고 앱만 응답하는 회색지대.
    """
    client = None
    try:
        import redis  # redis-py (django_redis/celery 의존성으로 항상 설치됨)

        client = redis.from_url(settings.CELERY_BROKER_URL)
        out: dict[str, int] = {}
        for q in _KNOWN_QUEUES:
            try:
                out[q] = int(client.llen(q))
            except Exception:  # noqa: BLE001 — 개별 큐 실패는 건너뜀
                continue
        return out
    except Exception:  # noqa: BLE001
        logger.debug("queue_depths failed", exc_info=True)
        return {}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def oldest_due_deferred_dm_age_s() -> int | None:
    """재시도 시각이 이미 지났는데 아직 QUEUED 인 가장 오래된 DM 의 '밀린 시간'(초).

    이 값이 크면 deferred requeue 파이프라인이 멈춘 것(워커 stall) — 회색지대 신호.
    적체 없으면 0, 측정 불가면 None.
    """
    try:
        from django.db.models import Min
        from django.utils import timezone

        from apps.integrations.models import SentDMLog

        now = timezone.now()
        oldest = SentDMLog.objects.filter(
            status=SentDMLog.Status.QUEUED, next_retry_at__lte=now
        ).aggregate(m=Min("next_retry_at"))["m"]
        if oldest is None:
            return 0
        return max(0, int((now - oldest).total_seconds()))
    except Exception:  # noqa: BLE001
        logger.debug("oldest_due_deferred_dm_age_s failed", exc_info=True)
        return None
