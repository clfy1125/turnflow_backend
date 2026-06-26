"""
Authentication Celery tasks — 사용자 계정 유지보수.

스케줄(config/settings/base.py CELERY_BEAT_SCHEDULE):
1. cleanup_unverified_accounts — 매일 KST 04:00, 미인증 상태로 N일(기본 1일) 경과한
   가입 계정을 정리(하드 삭제).

⚠️ 계정 삭제는 비가역적이므로 **다중 안전 장치** 를 둔다:
- 기능 플래그(UNVERIFIED_ACCOUNT_CLEANUP_ENABLED) 가 켜져 있을 때만 동작. 기본 OFF.
- dry-run(UNVERIFIED_ACCOUNT_CLEANUP_DRY_RUN) 기본 ON — 후보만 로그로 남기고 삭제하지 않음.
  운영 투입 전 며칠간 후보 로그를 관찰한 뒤 실삭제로 전환할 것.
- 관리자(is_staff/is_superuser) 와 워크스페이스 소유자는 절대 삭제 대상에서 제외.
- 사용 가능한 비밀번호가 없는 계정(소셜 로그인 계정)은 제외 — 정상 흐름상 OAuth 는 항상 인증됨.
- 개별 삭제를 try/except + transaction.atomic 으로 격리해 한 건 실패가 배치 전체를 막지 않게 한다.
  Workspace.owner 등 PROTECT FK 로 인한 ProtectedError 는 건너뛰고 로그만 남긴다.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

logger = logging.getLogger(__name__)

# 한 번의 배치에서 처리할 최대 후보 수 — 폭주 방어(이론상 미인증 폭증 시).
_MAX_BATCH = 1000


def _candidate_queryset(cutoff):
    """삭제 후보 QuerySet. 모든 안전 필터를 적용한다.

    - 미인증/활성 계정
    - 관리자(is_staff/is_superuser) 제외
    - cutoff 이전 가입(= N일 이상 경과)
    - 워크스페이스 소유자 제외(Workspace.owner 는 PROTECT)
    """
    User = get_user_model()
    return (
        User.objects.filter(
            is_email_verified=False,
            is_active=True,
            is_staff=False,
            is_superuser=False,
            date_joined__lt=cutoff,
        )
        .exclude(owned_workspaces__isnull=False)
        .order_by("date_joined")
    )


@shared_task(name="authentication.cleanup_unverified_accounts")
def cleanup_unverified_accounts() -> dict:
    """미인증 상태로 N일(기본 1일) 경과한 가입 계정을 정리한다.

    반환: {"enabled", "dry_run", "candidates", "deleted", "skipped", "failed"}
    멱등: 삭제된 계정은 다음 차수에 다시 조회되지 않는다.
    """
    if not settings.UNVERIFIED_ACCOUNT_CLEANUP_ENABLED:
        logger.info(
            "cleanup_unverified_accounts: disabled (UNVERIFIED_ACCOUNT_CLEANUP_ENABLED=False)"
        )
        return {
            "enabled": False,
            "dry_run": None,
            "candidates": 0,
            "deleted": 0,
            "skipped": 0,
            "failed": 0,
        }

    dry_run = settings.UNVERIFIED_ACCOUNT_CLEANUP_DRY_RUN
    retention_days = settings.UNVERIFIED_ACCOUNT_RETENTION_DAYS
    cutoff = timezone.now() - timezone.timedelta(days=retention_days)

    candidates = list(_candidate_queryset(cutoff)[:_MAX_BATCH])
    deleted = skipped = failed = 0

    for user in candidates:
        user_id, user_email = user.id, user.email

        # 소셜 로그인(사용 가능한 비밀번호 없음) 계정은 정리 대상에서 제외.
        if not user.has_usable_password():
            skipped += 1
            logger.info("cleanup_unverified_accounts: skip social account user=%s", user_id)
            continue

        if dry_run:
            logger.info(
                "cleanup_unverified_accounts[dry-run]: would delete user=%s email=%s joined=%s",
                user_id,
                user_email,
                user.date_joined.isoformat(),
            )
            continue

        try:
            with transaction.atomic():
                user.delete()
            deleted += 1
            logger.info(
                "cleanup_unverified_accounts: deleted user=%s email=%s", user_id, user_email
            )
        except ProtectedError:
            # 가입 후 워크스페이스 등 PROTECT 관계를 만든 계정 — 안전망. 건너뛴다.
            skipped += 1
            logger.warning(
                "cleanup_unverified_accounts: protected, skip user=%s email=%s",
                user_id,
                user_email,
            )
        except Exception:
            failed += 1
            logger.exception("cleanup_unverified_accounts: 삭제 중 오류 user=%s", user_id)

    summary = {
        "enabled": True,
        "dry_run": dry_run,
        "candidates": len(candidates),
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
    }

    log_fn = logger.error if failed else logger.info
    log_fn("cleanup_unverified_accounts: %s", summary)

    # 삭제/실패가 발생한 실행만 알림(베스트에포트). dry-run·무변경은 알림 생략.
    if not dry_run and (deleted or failed):
        try:
            from apps.core.telegram import send_telegram_notification

            icon = "🔴" if failed else "🧹"
            send_telegram_notification(
                f"{icon} *TurnFlow* 미인증 계정 정리: "
                f"삭제 {deleted} / 건너뜀 {skipped} / 실패 {failed} "
                f"(후보 {len(candidates)}, 보존 {retention_days}일)"
            )
        except Exception:
            logger.exception("cleanup_unverified_accounts: telegram 알림 실패")

    return summary
