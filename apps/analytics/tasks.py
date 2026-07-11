"""
analytics Celery tasks — 랜딩 방문 데이터 유지보수.

스케줄(config/settings/base.py CELERY_BEAT_SCHEDULE):
1. cleanup_landing_visits — 매일 KST 03:30, 보존기간(기본 180일) 초과 LandingVisit 배치 삭제.

SignupAttribution 은 TTL 없음 (사용자당 1행 업무 기록, user 삭제 시 CASCADE).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# 한 번에 지우는 pk 청크 크기 — 거대 단일 DELETE(락/WAL 폭증) 방지
_CHUNK_SIZE = 10_000


@shared_task(name="analytics.cleanup_landing_visits")
def cleanup_landing_visits() -> dict:
    """LANDING_VISIT_RETENTION_DAYS(기본 180일) 초과 LandingVisit 배치 삭제 (10k 청크, pk 커서).

    반환: {"retention_days", "deleted"}
    멱등: 이미 지워진 행은 다음 차수에 조회되지 않는다.
    """
    from .models import LandingVisit

    retention_days = settings.LANDING_VISIT_RETENTION_DAYS
    cutoff = timezone.now() - timedelta(days=retention_days)

    total_deleted = 0
    while True:
        pks = list(
            LandingVisit.objects.filter(created_at__lt=cutoff).values_list("pk", flat=True)[
                :_CHUNK_SIZE
            ]
        )
        if not pks:
            break
        deleted, _ = LandingVisit.objects.filter(pk__in=pks).delete()
        total_deleted += deleted

    summary = {"retention_days": retention_days, "deleted": total_deleted}
    logger.info("cleanup_landing_visits: %s", summary)
    return summary
