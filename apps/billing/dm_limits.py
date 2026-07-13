"""
DM 월간 발송 한도 (플랜 기반) — 정의의 단일 소스.

- 한도: SubscriptionPlan.features["dm_monthly_limit"] (-1 = 무제한).
  플랜은 워크스페이스 owner 의 UserSubscription 기준 (멀티 워크스페이스 우회 방지를 위해
  카운트도 owner 스코프). 관리자(is_staff/superuser)는 무제한.
- 사용량: SentDMLog 캘린더월 집계, SENT_FOR_QUOTA_STATUSES 만 소진
  (사용자에게 보이는 compute_monthly_usage 수치와 동일 정의).
  **집계 단위 = (캠페인 × 수신자 Instagram ID) 고유쌍** (v4.2 — "사람 단위" 과금):
  같은 캠페인에서 한 사람에게 여러 번 발송(follow-gate opening+reward, 재안내)해도 1로
  카운트하고, 같은 사람이 서로 다른 캠페인에서 받으면 2로 카운트한다.
- fail-open: '카운트' 실패 시 발송을 막지 않는다 (DM 무손실 원칙).
  '플랜 조회' 실패는 free(200) 취급 (_resolve_plan_name 과 동일한 보수성).
"""

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_DM_MONTHLY_LIMIT = 200  # free/basic 기본 — 플랜 조회 실패 시 보수적 폴백


def get_dm_monthly_limit(owner) -> int:
    """owner 의 이번 달 DM 발송 한도. -1 = 무제한."""
    from apps.integrations.campaign_stats import is_admin_user

    if is_admin_user(owner):
        return -1
    try:
        from .subscription_utils import get_user_plan

        plan = get_user_plan(owner)
        limit = plan.features.get("dm_monthly_limit", DEFAULT_DM_MONTHLY_LIMIT)
        return int(limit)
    except Exception:  # noqa: BLE001 - 플랜 조회 실패는 보수적으로 free 취급
        logger.exception("get_dm_monthly_limit: 플랜 조회 실패 — free 한도 적용")
        return DEFAULT_DM_MONTHLY_LIMIT


def count_owner_dms_this_month(owner, now=None) -> int:
    """owner 의 모든 워크스페이스에 걸친 이번 달 quota 소진 DM 수.

    v4.2 — "사람 단위" 과금: 단순 로그 count 가 아니라 **(캠페인 × 수신자) 고유쌍** 수를
    센다. 같은 캠페인 안에서 한 수신자에게 여러 DM 이 나가도(opening+reward, 재안내) 1이고,
    같은 수신자가 다른 캠페인에서 받으면 각각 1씩 잡힌다. distinct 는 인덱스
    dm_log_recipient_status_idx(recipient_user_id, status, ...) 로 커버된다.
    """
    from apps.integrations.campaign_stats import SENT_FOR_QUOTA_STATUSES, _month_bounds
    from apps.integrations.models import SentDMLog

    start, end = _month_bounds(now)
    return (
        SentDMLog.objects.filter(
            campaign__ig_connection__workspace__owner=owner,
            created_at__gte=start,
            created_at__lt=end,
            status__in=SENT_FOR_QUOTA_STATUSES,
        )
        .values("campaign_id", "recipient_user_id")
        .distinct()
        .count()
    )


def count_owner_dms_since(owner, since) -> int:
    """owner 의 특정 시점(since) 이후 quota 소진 DM 수 — 환불 심사용.

    집계 정의(SENT_FOR_QUOTA_STATUSES + (캠페인 × 수신자) 고유쌍)는
    count_owner_dms_this_month 과 동일하되, 창을 '캘린더월'이 아니라 'since 이후'로
    잡는다. 환불 심사는 해당 결제(paid_at) 이후 사용량만 봐야 하기 때문이다.
    """
    from apps.integrations.campaign_stats import SENT_FOR_QUOTA_STATUSES
    from apps.integrations.models import SentDMLog

    return (
        SentDMLog.objects.filter(
            campaign__ig_connection__workspace__owner=owner,
            created_at__gte=since,
            status__in=SENT_FOR_QUOTA_STATUSES,
        )
        .values("campaign_id", "recipient_user_id")
        .distinct()
        .count()
    )


def _quota_hit_cache_key(owner_id, now=None) -> str:
    local = timezone.localtime(now or timezone.now())
    return f"dmquota:hit:{owner_id}:{local:%Y%m}"


def check_dm_quota(owner) -> tuple[bool, int, int]:
    """DM 발송 가능 여부. Returns (allowed, used, limit).

    - 무제한(-1)이면 COUNT 쿼리 자체를 생략 (pro/admin 대량 발송 경로 무부하).
    - 한도 도달이 확인되면 월말까지 캐시 플래그를 세워 웹훅 폭주 시 COUNT 반복을 줄인다.
    - COUNT/캐시 예외는 fail-open (발송 허용).
    """
    limit = get_dm_monthly_limit(owner)
    if limit == -1:
        return True, 0, -1

    try:
        cache_key = _quota_hit_cache_key(owner.id)
        if cache.get(cache_key):
            return False, limit, limit

        used = count_owner_dms_this_month(owner)
        if used >= limit:
            # 월말까지 남은 시간만큼 플래그 (초과 후 재계산 방지)
            from apps.integrations.campaign_stats import _month_bounds

            _, month_end = _month_bounds()
            ttl = max(int((month_end - timezone.now()).total_seconds()), 60)
            cache.set(cache_key, True, timeout=ttl)
            return False, used, limit
        return True, used, limit
    except Exception:  # noqa: BLE001 - 카운트 실패는 fail-open (무손실 원칙)
        logger.exception("check_dm_quota: 사용량 집계 실패 — fail-open 발송 허용")
        return True, 0, limit


def notify_quota_reached_once(owner, used: int, limit: int) -> None:
    """한도 최초 도달 시 오너당 월 1회 운영 알림 (best-effort)."""
    try:
        local = timezone.localtime()
        notify_key = f"dmquota:notified:{owner.id}:{local:%Y%m}"
        if cache.get(notify_key):
            return
        cache.set(notify_key, True, timeout=60 * 60 * 24 * 32)

        from apps.core.telegram import send_telegram_notification

        send_telegram_notification(
            f"📮 DM 월 한도 도달\n- user: {owner.email}\n- 사용: {used}/{limit}\n"
            f"→ 이후 발송은 SKIPPED 처리 (프로 업그레이드 시 되살림 가능)"
        )
    except Exception:  # noqa: BLE001
        logger.exception("notify_quota_reached_once 실패")
