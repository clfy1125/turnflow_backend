"""apps.pages.tasks — Page 관련 비동기 작업.

현재 등록 태스크:
  - ``capture_reference_snapshot(page_id)`` — Playwright 캡쳐 후 ``Page.reference_snapshot`` 갱신.

각 태스크는 자체적으로 ``reference_snapshot_status`` 를 업데이트하며,
예외는 status="failed" 로 기록하고 raise — Celery 자동 retry 는 사용하지 않는다 (사용자 트리거 + 즉각 폴링).
"""
from __future__ import annotations

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="pages.capture_reference_snapshot",
    time_limit=120,        # 하드 limit (SIGKILL)
    soft_time_limit=90,    # 소프트 limit (SoftTimeLimitExceeded raise)
    autoretry_for=(),      # 자동 재시도 X — 명시적 status 로 표현
    acks_late=True,
)
def capture_reference_snapshot(self, page_id: int) -> dict:
    """레퍼런스 페이지의 모바일 미리보기를 Playwright 로 캡쳐.

    동작 흐름:
      1. Page 로드 + status="running" 저장
      2. is_public 검증 → 실패 시 SnapshotError
      3. capture_page_snapshot(slug) 호출
      4. 기존 reference_snapshot 파일 삭제 (있으면 R2/로컬 스토리지에서)
      5. 새 WebP 저장 + status="succeeded"

    실패 시 status="failed" + error 메시지 기록.  Celery exception 은 raise 하지 않음 —
    어드민 UI 가 status 폴링으로 결과를 판단.
    """
    from .models import Page
    from .services.snapshot import SnapshotError, capture_page_snapshot

    try:
        page = Page.objects.get(pk=page_id)
    except Page.DoesNotExist:
        logger.error("snapshot 대상 Page 없음: page_id=%s", page_id)
        return {"status": "failed", "error": "page_not_found"}

    page.reference_snapshot_status = Page.SnapshotStatus.RUNNING
    page.reference_snapshot_error = ""
    page.save(
        update_fields=[
            "reference_snapshot_status",
            "reference_snapshot_error",
            "updated_at",
        ]
    )

    try:
        if not page.is_public:
            raise SnapshotError("비공개 페이지는 캡쳐할 수 없습니다.")

        result = capture_page_snapshot(page.slug)

        # 기존 스냅샷 파일 정리 (R2/로컬 모두 default_storage 사용)
        old = page.reference_snapshot
        if old:
            try:
                old.delete(save=False)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "이전 snapshot 파일 삭제 실패 (무시): page=%s", page.slug
                )

        page.reference_snapshot.save(
            result.suggested_name, result.content_file, save=False
        )
        page.reference_snapshot_updated_at = timezone.now()
        page.reference_snapshot_status = Page.SnapshotStatus.SUCCEEDED
        page.reference_snapshot_error = ""
        page.save(
            update_fields=[
                "reference_snapshot",
                "reference_snapshot_updated_at",
                "reference_snapshot_status",
                "reference_snapshot_error",
                "updated_at",
            ]
        )
        logger.info(
            "snapshot 성공 — page=%s, elapsed=%ss, %dx%d",
            page.slug,
            result.elapsed_seconds,
            result.width,
            result.height,
        )
        return {
            "status": "succeeded",
            "url": page.reference_snapshot.url,
            "elapsed": result.elapsed_seconds,
        }

    except SnapshotError as e:
        page.reference_snapshot_status = Page.SnapshotStatus.FAILED
        page.reference_snapshot_error = str(e)[:500]
        page.save(
            update_fields=[
                "reference_snapshot_status",
                "reference_snapshot_error",
                "updated_at",
            ]
        )
        logger.warning("snapshot 실패 — page=%s: %s", page.slug, e)
        return {"status": "failed", "error": str(e)[:500]}

    except SoftTimeLimitExceeded:
        page.reference_snapshot_status = Page.SnapshotStatus.FAILED
        page.reference_snapshot_error = "soft_time_limit_exceeded (90s)"
        page.save(
            update_fields=[
                "reference_snapshot_status",
                "reference_snapshot_error",
                "updated_at",
            ]
        )
        logger.warning("snapshot soft timeout — page=%s", page.slug)
        return {"status": "failed", "error": "soft_time_limit"}

    except Exception as e:  # noqa: BLE001
        page.reference_snapshot_status = Page.SnapshotStatus.FAILED
        page.reference_snapshot_error = f"unexpected: {str(e)[:480]}"
        page.save(
            update_fields=[
                "reference_snapshot_status",
                "reference_snapshot_error",
                "updated_at",
            ]
        )
        logger.exception("snapshot 예상치 못한 예외 — page=%s", page.slug)
        raise
