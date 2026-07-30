"""인스타 성장 리포트 Celery 태스크.

큐: `reports` (전용) — 1건이 13~18분 걸리므로 dm_send/webhook 큐를 막지 않게 분리한다.
워커는 `-Q reports -c 2 --max-tasks-per-child=1` 로 띄운다(영상 임시파일 + Chromium 회수).

⚠️ 자동 재시도 없음. 1건에 실비 $0.4~0.6 이 들어가므로 재시도는 사용자 명시 요청으로만.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from .models import InstagramReport, ReportErrorCode, ReportStatus

logger = logging.getLogger(__name__)

# 태스크 자체 상한(전역 30분보다 길게) — 최악의 경우(추출 재시도 + 재합성 4회)를 덮는다.
TASK_SOFT_TIME_LIMIT = 40 * 60
TASK_TIME_LIMIT = 45 * 60

# 스위퍼: 이 시간을 넘겨 running 인 잡은 죽은 것으로 본다(워커 OOM·강제종료 등).
STALE_MINUTES = 60


@shared_task(
    name="insta_reports.generate_report",
    bind=True,
    soft_time_limit=TASK_SOFT_TIME_LIMIT,
    time_limit=TASK_TIME_LIMIT,
    acks_late=True,
    max_retries=0,
)
def generate_insta_report(self, report_id: str) -> str:
    """리포트 1건 생성 (S1 수집 → PDF). 완료 시 알림 메일 발송."""
    from . import service  # 지연 임포트 — 파이프라인 모듈 로딩을 워커에서만

    report = (
        InstagramReport.objects.filter(pk=report_id)
        .select_related("ig_connection", "workspace", "requested_by")
        .first()
    )
    if report is None:
        logger.warning("insta_reports: report not found id=%s", report_id)
        return "not_found"
    if report.is_terminal:
        # 중복 디스패치 방어 — 이미 끝난 잡은 다시 돌리지 않는다(중복 과금 차단).
        logger.info("insta_reports: already terminal id=%s status=%s", report_id, report.status)
        return f"skip:{report.status}"

    try:
        service.generate(report)
    except service.ReportFailure as e:
        report.mark_failed(e.code, e.detail)
        logger.warning(
            "insta_reports: failed id=%s code=%s detail=%s", report_id, e.code, e.detail[:200]
        )
        return f"failed:{e.code}"
    except SoftTimeLimitExceeded:
        report.mark_failed(ReportErrorCode.TIMEOUT, f"soft time limit {TASK_SOFT_TIME_LIMIT}s")
        logger.error("insta_reports: timeout id=%s", report_id)
        return "failed:TIMEOUT"
    except Exception as e:  # noqa: BLE001 - 어떤 예외도 잡을 실패로 기록해야 한다
        report.mark_failed(ReportErrorCode.INTERNAL, f"{type(e).__name__}: {e}")
        logger.exception("insta_reports: internal error id=%s", report_id)
        return "failed:INTERNAL"

    _notify_ready(report)
    return "succeeded"


def _notify_ready(report) -> None:
    """완료 알림 메일. 실패해도 리포트 결과에는 영향 없음."""
    user = report.requested_by
    if user is None or not getattr(user, "email", ""):
        return
    try:
        from apps.emails.tasks import send_insta_report_ready_email

        send_insta_report_ready_email.delay(user.id, str(report.id))
    except Exception:  # noqa: BLE001
        logger.warning("insta_reports: ready email enqueue failed id=%s", report.id)


@shared_task(name="insta_reports.sweep_stale")
def sweep_stale_reports() -> str:
    """죽은 잡 정리 — running 이 STALE_MINUTES 를 넘긴 리포트를 실패로 확정.

    워커가 OOM·재시작으로 사라지면 잡이 영원히 running 으로 남아 사용자가 새 리포트를
    만들 수 없다(동시 생성 1건 제한). 이용 횟수는 차감하지 않는다.
    """
    cutoff = timezone.now() - timedelta(minutes=STALE_MINUTES)
    stale = InstagramReport.objects.filter(
        status__in=[ReportStatus.QUEUED, ReportStatus.RUNNING],
        created_at__lt=cutoff,
    )
    n = 0
    for report in stale:
        report.mark_failed(
            ReportErrorCode.TIMEOUT, f"스위퍼: {STALE_MINUTES}분 초과 (stage={report.stage})"
        )
        n += 1
    if n:
        logger.warning("insta_reports: swept %s stale reports", n)
    return f"swept:{n}"


@shared_task(name="insta_reports.purge_caches")
def purge_report_caches(days: int = 90) -> str:
    """오래된 AI 캐시 정리.

    - 댓글 분류 캐시(`ReportAiCache`): 90일 경과분 삭제 (댓글 원문은 저장하지 않지만
      id→분류 매핑도 오래되면 무의미)
    - 영상 피처 캐시는 유지 — 재분석 비용 절감의 핵심이고 개인정보를 담지 않는다.
    - **리포트 PDF·집계는 삭제하지 않는다** (제품 결정: 계속 보관, 2026-07-29).
    """
    from .models import ReportAiCache

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = ReportAiCache.objects.filter(created_at__lt=cutoff).delete()
    logger.info("insta_reports: purged %s ai cache rows older than %sd", deleted, days)
    return f"purged:{deleted}"
