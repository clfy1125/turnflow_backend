"""Auto DM 캠페인 목록/요약용 통계 집계 헬퍼 (조회 고도화 v4.1).

프론트엔드 조회 고도화 요청(docs/backend-auto-dm-list-enhancements.md)을 위한 공유 로직.
목록 항목 enrichment, 요약 엔드포인트, 월간 사용량을 한 곳에서 계산해
N+1 통계 호출을 제거하고 정의를 단일화한다.

정의 출처:
  - delivery_rate: verification_views.stats / admin_api `_build_stats` 와 동일
    (확정도착 = delivered+read, 모수 = accepted+delivered+read+failed_no_trace)
  - needs_attention: dm_frontend_actions 의 severity=error 상태 + failed_no_trace
  - 월간 사용량: SentDMLog 에서 캘린더월(Asia/Seoul) 직접 집계 (UsageCounter 는 발송 시
    증가되지 않아 stale → 정확도를 위해 로그를 직접 센다).
    한도는 owner 구독 플랜 features.dm_monthly_limit (billing.dm_limits 와 동일 정의).
"""

from __future__ import annotations

from django.db.models import Count, Max, Q
from django.utils import timezone

from .models import AutoDMCampaign, SentDMLog

# ── 상태 집합 (delivery_rate / delivered_count 계산용) ──────────────────────────
# 확정 도착(사용자에게 "도착함"이라 보고 가능 + 읽음). legacy "sent" 는 모수와 분자
# 양쪽에서 빠지므로 delivery_rate 정의(_build_stats)와 일치시키기 위해 제외한다.
CONFIRMED_DELIVERED_STATUSES = [
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.RECOVERY_DELIVERED,  # 복구 재전송 성공 = 확정 도착
]
# delivery_rate 모수: ACCEPTED 진입 이후 종결된 건 (도착/읽음/도착미확인 포함)
ACCEPTED_OR_AFTER_STATUSES = [
    SentDMLog.Status.ACCEPTED,
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.FAILED_NO_TRACE,
    SentDMLog.Status.RECOVERY_DELIVERED,  # 실제 발송+도착했으므로 분자·분모 양쪽 포함
]
# 사용자 조치가 필요한 상태 (severity=error + 도착미확인 자가점검).
# error: 토큰만료(재연동) / 24h윈도우만료 / 파라미터오류.  warning: 도착미확인.
NEEDS_ATTENTION_STATUSES = [
    SentDMLog.Status.FAILED_TOKEN,
    SentDMLog.Status.FAILED_WINDOW,
    SentDMLog.Status.FAILED_PARAM,
    SentDMLog.Status.FAILED_NO_TRACE,
]
# 월간 사용량(quota) 으로 카운트할 상태: 실제로 Meta 에 발송 요청이 접수된 건.
# accepted 이후 + legacy sent. queued/submitting/skipped/rate_limited/거부성 실패는 제외
# (발송 전 단계이거나 발송 자체가 안 일어났으므로 quota 미소진).
SENT_FOR_QUOTA_STATUSES = [
    SentDMLog.Status.ACCEPTED,
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.FAILED_NO_TRACE,
    SentDMLog.Status.SENT,  # legacy
    SentDMLog.Status.RECOVERY_DELIVERED,  # 실제 Meta 발송 소비 → 쿼터 집계
]

# annotate 결과를 담는 임시 속성명 (모델 필드와 충돌 안 나게 언더스코어 프리픽스)
_ANNO_CONFIRMED = "_confirmed_delivered"
_ANNO_ACCEPTED = "_accepted_or_after"
_ANNO_NEEDS = "_needs_attention"
_ANNO_LAST = "_last_sent_at"


def annotate_campaign_stats(qs):
    """campaign queryset 에 per-campaign dm_logs 집계를 annotate (목록 N+1 제거).

    한 번의 LEFT JOIN + 조건부 집계로 모든 캠페인의 통계를 계산한다.
    부모/자식 로그를 모두 포함한다(전체 발송 그림 = canonical _build_stats 와 동일 범위).
    """
    return qs.annotate(
        **{
            _ANNO_CONFIRMED: Count(
                "dm_logs", filter=Q(dm_logs__status__in=CONFIRMED_DELIVERED_STATUSES)
            ),
            _ANNO_ACCEPTED: Count(
                "dm_logs", filter=Q(dm_logs__status__in=ACCEPTED_OR_AFTER_STATUSES)
            ),
            _ANNO_NEEDS: Count("dm_logs", filter=Q(dm_logs__status__in=NEEDS_ATTENTION_STATUSES)),
            _ANNO_LAST: Max("dm_logs__created_at"),
        }
    )


def compute_campaign_enrichment(obj: AutoDMCampaign) -> dict:
    """캠페인 1건의 enrichment dict 계산.

    annotate_campaign_stats 로 annotate 된 인스턴스면 그 값을 쓰고(추가 쿼리 0),
    아니면 그 캠페인 로그를 즉석 집계한다(단건 경로 — pause/resume 등에서 안전 fallback).
    """
    confirmed = getattr(obj, _ANNO_CONFIRMED, None)
    if confirmed is None:
        agg = obj.dm_logs.aggregate(
            confirmed=Count("id", filter=Q(status__in=CONFIRMED_DELIVERED_STATUSES)),
            accepted=Count("id", filter=Q(status__in=ACCEPTED_OR_AFTER_STATUSES)),
            needs=Count("id", filter=Q(status__in=NEEDS_ATTENTION_STATUSES)),
            last=Max("created_at"),
        )
        confirmed = agg["confirmed"]
        accepted = agg["accepted"]
        needs = agg["needs"]
        last = agg["last"]
    else:
        accepted = getattr(obj, _ANNO_ACCEPTED, 0) or 0
        needs = getattr(obj, _ANNO_NEEDS, 0) or 0
        last = getattr(obj, _ANNO_LAST, None)

    delivery_rate = round(confirmed / accepted, 4) if accepted else 0.0
    return {
        "delivered_count": confirmed,
        "delivery_rate": delivery_rate,
        "needs_attention_count": needs,
        "last_sent_at": last,
        # 게시물 썸네일 = 캠페인 media_url (목록 응답에서 Graph API 로 best-effort 보강됨)
        "thumbnail_url": obj.media_url or None,
    }


def build_counts(campaign_qs) -> dict:
    """상태별 캠페인 개수 + total. (단일 group-by 쿼리)"""
    rows = campaign_qs.values("status").annotate(n=Count("id"))
    by_status = {row["status"]: row["n"] for row in rows}
    counts = {s: by_status.get(s, 0) for s in AutoDMCampaign.Status.values}
    counts["total"] = sum(by_status.values())
    return counts


def _month_bounds(now=None):
    """현재 시각이 속한 캘린더월의 [start, next_month_start) 경계 (서버 타임존 기준, aware)."""
    local = timezone.localtime(now or timezone.now())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def is_admin_user(user) -> bool:
    """관리자 모드 여부 (DRF IsAdminUser 와 동일 기준: is_staff). superuser 도 포함."""
    return bool(user and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)))


def compute_monthly_usage(workspace, now=None, *, user=None) -> dict:
    """워크스페이스의 이번 달 DM 사용량 + 한도.

    한도는 workspace.owner 의 구독 플랜 features.dm_monthly_limit (-1=무제한) —
    발송 게이트(billing.dm_limits.check_dm_quota)와 동일 정의.
    요청자가 **관리자(is_staff/superuser)** 면 플랜과 무관하게 무제한(-1)으로 본다.
    사용량은 SentDMLog 에서 캘린더월 범위를 직접 집계(quota 소진 상태만).
    주의: 표시 수치는 이 워크스페이스 범위이고, enforcement 는 owner 전체 범위다
    (플랜이 유저 단위이므로 멀티 워크스페이스 분산 우회를 막기 위함).
    """
    from apps.billing.dm_limits import get_dm_monthly_limit

    start, end = _month_bounds(now)
    # v4.2 — 과금 정의(billing.dm_limits)와 동일하게 (캠페인 × 수신자) 고유쌍으로 집계한다.
    sent_this_month = (
        SentDMLog.objects.filter(
            campaign__ig_connection__workspace=workspace,
            created_at__gte=start,
            created_at__lt=end,
            status__in=SENT_FOR_QUOTA_STATUSES,
        )
        .values("campaign_id", "recipient_user_id")
        .distinct()
        .count()
    )

    if is_admin_user(user):
        limit = -1  # 관리자 모드 → 무제한
    else:
        limit = get_dm_monthly_limit(workspace.owner)
    is_unlimited = limit == -1
    return {
        "sent_this_month": sent_this_month,
        "monthly_free_limit": limit,  # -1 = 무제한
        "remaining_this_month": (None if is_unlimited else max(limit - sent_this_month, 0)),
        "is_over_limit": (False if is_unlimited else sent_this_month >= limit),
        "period_start": start,
        "period_end": end,
    }


def build_delivery_summary(campaign_qs) -> dict:
    """목록 범위 전체의 발송 요약 (delivery_rate / needs_attention 합).

    campaign_qs 에 연결된 모든 dm_logs 를 가로질러 집계한다.
    """
    agg = SentDMLog.objects.filter(campaign__in=campaign_qs).aggregate(
        confirmed=Count("id", filter=Q(status__in=CONFIRMED_DELIVERED_STATUSES)),
        accepted=Count("id", filter=Q(status__in=ACCEPTED_OR_AFTER_STATUSES)),
        needs=Count("id", filter=Q(status__in=NEEDS_ATTENTION_STATUSES)),
        delivered_or_sent=Count("id", filter=Q(status__in=SentDMLog.DELIVERED_STATUSES)),
        last=Max("created_at"),
    )
    confirmed = agg["confirmed"]
    accepted = agg["accepted"]
    total_attempt = SentDMLog.objects.filter(campaign__in=campaign_qs).count()
    delivery_rate = round(confirmed / accepted, 4) if accepted else 0.0
    success_rate = round(agg["delivered_or_sent"] / total_attempt, 4) if total_attempt else 0.0
    return {
        "total_sent": agg["delivered_or_sent"],
        "delivery_rate": delivery_rate,
        "success_rate": success_rate,
        "needs_attention_total": agg["needs"],
        "_last_activity_at": agg["last"],
    }
