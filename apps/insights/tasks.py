"""
Insights 동기화 Celery 태스크.

스케줄 (settings.CELERY_BEAT_SCHEDULE 에서 등록):
    insights-sync-active-accounts-media   매 30분 — 신규 미디어 메타데이터 발견
    insights-refresh-recent-insights       매 30분 — 최근 7일 미디어 인사이트 새로고침
    insights-refresh-old-insights          매일 03:00 (cron) — 그 외 미디어 인사이트 새로고침

호출량 추산 (대략):
    1 워크스페이스 = IG 계정 1개, 미디어 200건 가정.
    - media list: 200/50 = 4 호출
    - 최근 7일 (~15건) insights: 15 호출
    - 30분 주기 = 시간당 38 호출. 일 ~900 호출. IG limit 4800/시 대비 충분히 안전.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from apps.integrations.models import IGAccountConnection

from .models import IGMedia, MediaSyncJob
from .services import (
    InsightsAPIError,
    InsightsPermissionError,
    InsightsTransientError,
    sync_account_audience_insight,
    sync_account_media,
    sync_media_insights,
)

logger = logging.getLogger(__name__)


@shared_task(
    name="insights.sync_active_accounts_media",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def sync_active_accounts_media(self):
    """
    활성 IG 계정 전체에 대해 최신 미디어 메타데이터 동기화.

    이 태스크는 메타데이터만 동기화 (인사이트 X). 새 게시물 감지가 목적이라
    매 페이지 첫 결과가 이미 알고 있는 미디어면 다음 페이지로 안 넘어가도록
    구현하면 호출량을 더 줄일 수 있지만, 현재 구현은 안전하게 1~3 페이지 fetch.
    """
    accounts = IGAccountConnection.objects.filter(status=IGAccountConnection.Status.ACTIVE)
    total = 0
    errors = 0
    for account in accounts:
        try:
            res = sync_account_media(account, max_pages=3)
            total += res["fetched"]
        except InsightsPermissionError as e:
            errors += 1
            logger.warning("insights media sync permission error account=%s: %s", account.id, e)
            account.mark_as_error(f"insights media: {e}")
        except InsightsTransientError as e:
            errors += 1
            logger.warning("insights media sync transient account=%s: %s", account.id, e)
        except Exception:
            errors += 1
            logger.exception("insights media sync unexpected account=%s", account.id)
    logger.info("sync_active_accounts_media done fetched=%d errors=%d", total, errors)
    return {"fetched": total, "errors": errors}


@shared_task(
    name="insights.refresh_recent_insights",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def refresh_recent_insights(self):
    """
    최근 7일 게시물 + 한 번도 동기화 안 된 미디어 인사이트 새로고침.

    NOTE (v4.0.2): 단순 published_at 필터는 "오래된 게시물을 가진 신규 연동 계정"
    의 미디어를 영원히 NULL 로 남기는 버그가 있었음.
    `insights_last_synced_at IS NULL` OR 가지로 첫 동기화는 항상 처리하도록 보강.
    stale TTL 통과한 건 sync_media_insights 안에서 skip 되므로 호출 비용 미증가.
    """
    from datetime import timedelta
    from django.db.models import Q

    cutoff = timezone.now() - timedelta(days=7)
    queryset = (
        IGMedia.objects.filter(
            Q(published_at__gte=cutoff) | Q(insights_last_synced_at__isnull=True),
            account__status=IGAccountConnection.Status.ACTIVE,
        )
        .select_related("account", "insight")
        .order_by("-published_at")
    )
    processed = 0
    skipped = 0
    errors = 0
    for media in queryset.iterator(chunk_size=50):
        if media.is_insights_fresh():
            skipped += 1
            continue
        try:
            sync_media_insights(media, force=False)
            processed += 1
        except InsightsTransientError as e:
            errors += 1
            logger.warning("transient insight sync media=%s: %s", media.external_media_id, e)
        except Exception:
            errors += 1
            logger.exception("insight sync failed media=%s", media.external_media_id)
    logger.info(
        "refresh_recent_insights done processed=%d skipped=%d errors=%d",
        processed,
        skipped,
        errors,
    )
    return {"processed": processed, "skipped": skipped, "errors": errors}


@shared_task(
    name="insights.refresh_old_insights",
    bind=True,
    max_retries=1,
    default_retry_delay=600,
)
def refresh_old_insights(self):
    """
    7일 이상 미디어 인사이트 — 일 1회 새로고침.

    오래된 게시물은 수치 변동이 적어 빈번한 호출은 낭비. stale TTL 이
    24h 이므로 일 1회 호출이면 충분.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=7)
    queryset = (
        IGMedia.objects.filter(
            published_at__lt=cutoff,
            account__status=IGAccountConnection.Status.ACTIVE,
        )
        .select_related("account", "insight")
        .order_by("-published_at")
    )
    processed = 0
    skipped = 0
    errors = 0
    for media in queryset.iterator(chunk_size=100):
        if media.is_insights_fresh():
            skipped += 1
            continue
        try:
            sync_media_insights(media, force=False)
            processed += 1
        except InsightsTransientError as e:
            errors += 1
            logger.warning("transient insight sync media=%s: %s", media.external_media_id, e)
        except Exception:
            errors += 1
            logger.exception("insight sync failed media=%s", media.external_media_id)
    logger.info(
        "refresh_old_insights done processed=%d skipped=%d errors=%d",
        processed,
        skipped,
        errors,
    )
    return {"processed": processed, "skipped": skipped, "errors": errors}


@shared_task(
    name="insights.refresh_account_audience_insights",
    bind=True,
    max_retries=1,
    default_retry_delay=600,
)
def refresh_account_audience_insights(self, period_days: int = 30):
    """
    활성 IG 계정 전체의 follow_type breakdown reach 를 일 1회 새로고침.

    "고인물 콘텐츠" 진단 룰 (rule_account_followers_dominant) 의 근거 데이터.
    1 계정 = 1 IG API call.
    """
    accounts = IGAccountConnection.objects.filter(status=IGAccountConnection.Status.ACTIVE)
    processed = 0
    errors = 0
    for account in accounts:
        try:
            res = sync_account_audience_insight(account, period_days=period_days)
            if res is not None:
                processed += 1
        except InsightsTransientError as e:
            errors += 1
            logger.warning("account insight transient account=%s: %s", account.id, e)
        except Exception:
            errors += 1
            logger.exception("account insight unexpected account=%s", account.id)
    logger.info(
        "refresh_account_audience_insights processed=%d errors=%d", processed, errors
    )
    return {"processed": processed, "errors": errors}


@shared_task(name="insights.bootstrap_account", bind=True)
def bootstrap_account(self, account_id: str):
    """
    신규 IG 연동 또는 권한 추가 재연동 직후 1회 자동 호출.

    OAuth callback view 에서 IGAccountConnection 저장 직후 enqueue.
    프론트가 별도로 sync 트리거할 필요 없이, 연동 직후 바로:
        1) 모든 미디어 메타데이터 동기화
        2) 모든 미디어 인사이트 동기화 (insights scope 보유 시)
        3) 계정 단위 청중 인사이트 (follow_type breakdown)

    호출 비용: 계정 1개 + 미디어 N건 기준 약 (N/50 + N + 1) IG API call.
    예: 미디어 200건 = 4 + 200 + 1 = 205회 호출. 일 1회만 발생.
    """
    try:
        account = IGAccountConnection.objects.get(id=account_id)
    except IGAccountConnection.DoesNotExist:
        logger.error("bootstrap_account: account not found %s", account_id)
        return {"status": "not_found"}

    if account.status != IGAccountConnection.Status.ACTIVE:
        return {"status": "skipped", "reason": "not active"}

    has_insights_scope = "instagram_business_manage_insights" in (account.scopes or [])

    # 1) 메타데이터 동기화 (insights scope 없어도 가능 - basic 권한)
    try:
        meta_res = sync_account_media(account, max_pages=10)
        logger.info(
            "bootstrap %s metadata: fetched=%d created=%d",
            account.username,
            meta_res.get("fetched", 0),
            meta_res.get("created", 0),
        )
    except InsightsPermissionError as e:
        logger.warning("bootstrap %s metadata permission: %s", account.username, e)
        return {"status": "failed", "stage": "metadata", "error": str(e)}
    except Exception:
        logger.exception("bootstrap %s metadata failed", account.username)
        return {"status": "failed", "stage": "metadata"}

    if not has_insights_scope:
        logger.info(
            "bootstrap %s: insights scope 없음 — 메타데이터만 동기화하고 종료",
            account.username,
        )
        return {"status": "partial", "reason": "no insights scope", "media": meta_res}

    # 2) 모든 미디어 인사이트 동기화
    media_qs = IGMedia.objects.filter(account=account).order_by("-published_at")
    total = media_qs.count()
    processed = 0
    errors = 0
    for media in media_qs.iterator(chunk_size=50):
        try:
            sync_media_insights(media, force=True)
            processed += 1
        except InsightsTransientError as e:
            errors += 1
            logger.warning(
                "bootstrap %s insight transient media=%s: %s",
                account.username,
                media.external_media_id,
                e,
            )
        except Exception:
            errors += 1
            logger.exception(
                "bootstrap %s insight failed media=%s",
                account.username,
                media.external_media_id,
            )
    logger.info(
        "bootstrap %s insights: %d/%d processed errors=%d",
        account.username,
        processed,
        total,
        errors,
    )

    # 3) 계정 단위 청중 인사이트
    try:
        sync_account_audience_insight(account, period_days=30)
    except Exception:
        logger.exception("bootstrap %s audience insight failed", account.username)

    return {
        "status": "succeeded",
        "media_total": total,
        "insights_processed": processed,
        "insights_errors": errors,
    }


@shared_task(name="insights.run_sync_job", bind=True)
def run_sync_job(self, job_id: str):
    """
    사용자 트리거 MediaSyncJob 실행.

    프론트가 강제 새로고침 버튼을 누르면 API 가 MediaSyncJob 을 생성하고
    이 태스크를 enqueue. 진행률은 동일 row 의 processed/total 컬럼으로 노출.
    """
    try:
        job = MediaSyncJob.objects.select_related("account").get(id=job_id)
    except MediaSyncJob.DoesNotExist:
        logger.error("sync job not found: %s", job_id)
        return

    job.status = MediaSyncJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    try:
        if job.scope == MediaSyncJob.Scope.METADATA_ONLY:
            res = sync_account_media(job.account, max_pages=10)
            job.total = res["fetched"]
            job.processed = res["fetched"]
        else:
            from datetime import timedelta

            from django.db.models import Q

            qs = IGMedia.objects.filter(account=job.account).order_by("-published_at")
            if job.scope == MediaSyncJob.Scope.INSIGHTS_RECENT:
                # 최근 7일 게시물 OR 한 번도 동기화 안 된 미디어 (= 첫 동기화 보장)
                cutoff = timezone.now() - timedelta(days=7)
                qs = qs.filter(
                    Q(published_at__gte=cutoff) | Q(insights_last_synced_at__isnull=True)
                )

            job.total = qs.count()
            job.save(update_fields=["total"])

            processed = 0
            err = 0
            for media in qs.iterator(chunk_size=50):
                try:
                    sync_media_insights(media, force=True)
                except (InsightsPermissionError, InsightsAPIError) as e:
                    err += 1
                    logger.warning("user sync media=%s: %s", media.external_media_id, e)
                except Exception:
                    err += 1
                    logger.exception("user sync media=%s", media.external_media_id)
                processed += 1
                if processed % 10 == 0:
                    MediaSyncJob.objects.filter(id=job.id).update(
                        processed=processed, error_count=err
                    )
            job.processed = processed
            job.error_count = err

        job.status = MediaSyncJob.Status.SUCCEEDED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "processed", "error_count", "finished_at"])
    except Exception as e:
        logger.exception("sync job failed: %s", job.id)
        job.status = MediaSyncJob.Status.FAILED
        job.error_message = str(e)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_message", "finished_at"])
