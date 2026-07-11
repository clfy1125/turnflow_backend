"""apps/admin_api/views/dashboard_ops.py — 어드민 운영(Operations) 대시보드 집계.

라우팅: ``GET /api/v1/admin/dashboard/operations/`` (``IsAdminUser``, is_staff=True).

운영자가 30~60초 폴링으로 서비스 건강 상태를 한 화면에서 감시하는 엔드포인트.
- ``status_summary``: DM/IG연동/스팸필터/빌링 4개 서브시스템 신호등 + overall(worst-of)
- ``action_required``: 고정 순서 조치 목록 (count=0 항목도 포함 — 프론트 고정 레이아웃)
- ``dm_quality`` / ``spam``: 윈도우 집계 + 제로필(zero-fill) 시계열
- ``recent_errors``: DM 실패 / 결제 실패 / 스팸 숨김 실패 3종 병합 (timestamp desc, 최대 20)
- ``risk_accounts``: 토큰 상태 × 24h 도착률 스코어링 상위 5

정책:
- 모든 카운트는 **전사(GLOBAL)** 집계 — request.user 워크스페이스 필터 없음.
- 임계값/상태 판정 규칙의 단일 소스는 :mod:`apps.admin_api.dashboard_constants`.
- ``delivery_rate`` 는 :func:`apps.admin_api.views.dashboard._delivery_rate` 공식 재사용:
  ``(delivered+read) / (accepted+delivered+read+failed_no_trace)``.
- 응답은 Redis 에 ``OPS_DASHBOARD_CACHE_TTL``(30s) 캐시 (키 ``admin:dash:ops:{window}``).
  payload 의 ``generated_at`` 으로 프론트가 신선도를 표시할 수 있다.
- 읽기 전용이라 AdminActionLog 감사 기록 없음 (감사는 mutation 전용 관례).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.functions import TruncHour, TruncMinute
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.dashboard_constants import (
    DM_DELIVERY_CRITICAL_THRESHOLD,
    DM_DELIVERY_WARNING_THRESHOLD,
    DM_MIN_SAMPLE_FOR_STATUS,
    IG_EXPIRED_CRITICAL_COUNT,
    OPS_DASHBOARD_CACHE_TTL,
    PAST_DUE_CRITICAL_COUNT,
    PAYMENT_FAILED_WARNING_COUNT,
    QUEUE_WINDOW_RISK_HOURS,
    RECENT_ERRORS_LIMIT,
    RISK_ACCOUNTS_LIMIT,
    RISK_REPEATED_PARAM_ERRORS_COUNT,
    SPAM_HIDE_FAILED_CRITICAL_COUNT,
    SPAM_HIDE_FAILED_WARNING_COUNT,
    STUCK_SUBMITTING_MINUTES,
    TOKEN_EXPIRING_SOON_HOURS,
    WEBHOOK_BACKLOG_CRITICAL_MINUTES,
    WEBHOOK_BACKLOG_STALE_MINUTES,
)
from apps.admin_api.serializers.dashboard_ops import AdminOpsDashboardSerializer

# delivery_rate 표준 공식 재사용 (dm-verification/stats 와 동일 정의 — 복제 금지)
from apps.admin_api.views.dashboard import _accepted_or_after, _delivery_rate
from apps.billing.models import (
    PaymentHistory,
    PaymentStatus,
    SubscriptionStatus,
    TossWebhookLog,
    UserSubscription,
)
from apps.integrations.models import IGAccountConnection, SentDMLog, SpamCommentLog

logger = logging.getLogger(__name__)

ALLOWED_WINDOWS = ("1h", "24h", "today")
CACHE_KEY_TMPL = "admin:dash:ops:{window}"

# ── 상태 집합 ────────────────────────────────────────────────────────
# succeeded: 도착 확정 (+ legacy sent)
DM_SUCCEEDED_STATUSES = (
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.SENT,  # legacy
)
# failed: 분류된 실패 4종 (+ legacy failed)
DM_FAILED_STATUSES = (
    SentDMLog.Status.FAILED_TOKEN,
    SentDMLog.Status.FAILED_WINDOW,
    SentDMLog.Status.FAILED_PARAM,
    SentDMLog.Status.FAILED_NO_TRACE,
    SentDMLog.Status.FAILED,  # legacy
)
# 스팸 통계: CLEAN(멱등 장부) 제외
SPAM_DETECTED_STATUSES = (
    SpamCommentLog.Status.DETECTED,
    SpamCommentLog.Status.HIDDEN,
    SpamCommentLog.Status.FAILED,
)

_STATUS_RANK = {"ok": 0, "warning": 1, "critical": 2}


# ── 윈도우/시계열 헬퍼 ───────────────────────────────────────────────


def _window_bounds(window: str, now) -> tuple[datetime, str]:
    """window 파라미터 → (since, series granularity).

    - "1h": now-1h, 5분 버킷("5m")
    - "24h": now-24h, 시간 버킷("hour")
    - "today": Asia/Seoul 자정 → now, 시간 버킷("hour")
    """
    if window == "1h":
        return now - timedelta(hours=1), "5m"
    if window == "today":
        today_start = timezone.make_aware(
            datetime.combine(timezone.localdate(), datetime.min.time()),
            timezone.get_current_timezone(),
        )
        return today_start, "hour"
    return now - timedelta(hours=24), "hour"


def _floor_bucket(dt, granularity: str):
    """aware datetime 을 로컬(Asia/Seoul) 기준 버킷 시작 시각으로 내림."""
    local = timezone.localtime(dt)
    if granularity == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    return local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0)


def _zero_filled_series(rows: dict, since, now, granularity: str, fields: tuple) -> list[dict]:
    """[floor(since), floor(now)] 구간을 granularity 간격으로 제로필한 버킷 리스트."""
    step = timedelta(hours=1) if granularity == "hour" else timedelta(minutes=5)
    buckets = []
    cur = _floor_bucket(since, granularity)
    end = _floor_bucket(now, granularity)
    while cur <= end:
        row = rows.get(cur)
        item = {"ts": cur.isoformat()}
        for f in fields:
            item[f] = row[f] if row else 0
        buckets.append(item)
        cur += step
    return buckets


def _bucketize(qs_rows, granularity: str, fields: tuple) -> dict:
    """Trunc groupby 결과를 로컬 버킷 키로 재집계 (1h 윈도우는 분→5분 내림 합산)."""
    agg: dict = {}
    for row in qs_rows:
        key = _floor_bucket(row["bucket"], granularity)
        slot = agg.setdefault(key, dict.fromkeys(fields, 0))
        for f in fields:
            slot[f] += row[f]
    return agg


# ── 집계 헬퍼 (모두 (now, since) 시그니처) ───────────────────────────


def _dm_quality(now, since, granularity: str) -> tuple[dict, dict]:
    """DM 발송 품질 블록 + 원시 집계(dict) 반환 (status_summary 재사용용)."""
    dm_agg = SentDMLog.objects.filter(created_at__gte=since).aggregate(
        requested=Count("id"),
        accepted=Count("id", filter=Q(status=SentDMLog.Status.ACCEPTED)),
        delivered=Count("id", filter=Q(status=SentDMLog.Status.DELIVERED)),
        read=Count("id", filter=Q(status=SentDMLog.Status.READ)),
        legacy_sent=Count("id", filter=Q(status=SentDMLog.Status.SENT)),
        failed_token=Count("id", filter=Q(status=SentDMLog.Status.FAILED_TOKEN)),
        failed_window=Count("id", filter=Q(status=SentDMLog.Status.FAILED_WINDOW)),
        failed_param=Count("id", filter=Q(status=SentDMLog.Status.FAILED_PARAM)),
        failed_no_trace=Count("id", filter=Q(status=SentDMLog.Status.FAILED_NO_TRACE)),
        legacy_failed=Count("id", filter=Q(status=SentDMLog.Status.FAILED)),
        skipped=Count("id", filter=Q(status=SentDMLog.Status.SKIPPED)),
        queued=Count("id", filter=Q(status=SentDMLog.Status.QUEUED)),
        submitting=Count("id", filter=Q(status=SentDMLog.Status.SUBMITTING)),
    )

    trunc = TruncHour if granularity == "hour" else TruncMinute
    fields = ("requested", "succeeded", "failed", "skipped")
    series_rows = (
        SentDMLog.objects.filter(created_at__gte=since)
        .annotate(bucket=trunc("created_at"))
        .values("bucket")
        .annotate(
            requested=Count("id"),
            succeeded=Count("id", filter=Q(status__in=DM_SUCCEEDED_STATUSES)),
            failed=Count("id", filter=Q(status__in=DM_FAILED_STATUSES)),
            skipped=Count("id", filter=Q(status=SentDMLog.Status.SKIPPED)),
        )
        .order_by("bucket")
    )
    buckets = _zero_filled_series(
        _bucketize(series_rows, granularity, fields), since, now, granularity, fields
    )

    block = {
        "requested": dm_agg["requested"],
        "succeeded": dm_agg["delivered"] + dm_agg["read"] + dm_agg["legacy_sent"],
        "accepted_pending": dm_agg["accepted"],
        "failed": (
            dm_agg["failed_token"]
            + dm_agg["failed_window"]
            + dm_agg["failed_param"]
            + dm_agg["failed_no_trace"]
            + dm_agg["legacy_failed"]
        ),
        "skipped": dm_agg["skipped"],
        "queued": dm_agg["queued"],
        "submitting": dm_agg["submitting"],
        "delivery_rate": _delivery_rate(dm_agg),
        "series": {"granularity": granularity, "buckets": buckets},
    }
    return block, dm_agg


def _ig_connections(now) -> dict:
    expiring_cutoff = now + timedelta(hours=TOKEN_EXPIRING_SOON_HOURS)
    agg = IGAccountConnection.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status=IGAccountConnection.Status.ACTIVE)),
        expired=Count("id", filter=Q(status=IGAccountConnection.Status.EXPIRED)),
        revoked=Count("id", filter=Q(status=IGAccountConnection.Status.REVOKED)),
        error=Count("id", filter=Q(status=IGAccountConnection.Status.ERROR)),
        # 경계: token_expires_at == now+24h 도 포함(<=). 이미 지난 토큰(<= now)은 제외.
        expiring_24h=Count(
            "id",
            filter=Q(
                status=IGAccountConnection.Status.ACTIVE,
                token_expires_at__gt=now,
                token_expires_at__lte=expiring_cutoff,
            ),
        ),
        soft_deactivated=Count("id", filter=Q(is_active=False)),
    )
    return {
        "total": agg["total"],
        "by_status": {
            "active": agg["active"],
            "expired": agg["expired"],
            "revoked": agg["revoked"],
            "error": agg["error"],
        },
        "expiring_24h": agg["expiring_24h"],
        "soft_deactivated": agg["soft_deactivated"],
    }


def _spam(now, since, granularity: str) -> dict:
    agg = SpamCommentLog.objects.filter(created_at__gte=since).aggregate(
        checked=Count("id"),
        detected=Count("id", filter=Q(status__in=SPAM_DETECTED_STATUSES)),
        hidden=Count("id", filter=Q(status=SpamCommentLog.Status.HIDDEN)),
        failed=Count("id", filter=Q(status=SpamCommentLog.Status.FAILED)),
    )

    trunc = TruncHour if granularity == "hour" else TruncMinute
    fields = ("detected", "hidden")
    series_rows = (
        SpamCommentLog.objects.filter(created_at__gte=since)
        .annotate(bucket=trunc("created_at"))
        .values("bucket")
        .annotate(
            detected=Count("id", filter=Q(status__in=SPAM_DETECTED_STATUSES)),
            hidden=Count("id", filter=Q(status=SpamCommentLog.Status.HIDDEN)),
        )
        .order_by("bucket")
    )
    buckets = _zero_filled_series(
        _bucketize(series_rows, granularity, fields), since, now, granularity, fields
    )

    top_categories = [
        {"category": row["spam_category"] or "uncategorized", "count": row["c"]}
        for row in (
            SpamCommentLog.objects.filter(created_at__gte=since, status__in=SPAM_DETECTED_STATUSES)
            .values("spam_category")
            .annotate(c=Count("id"))
            .order_by("-c")[:5]
        )
    ]

    return {
        "checked": agg["checked"],
        "detected": agg["detected"],
        "hidden": agg["hidden"],
        "failed": agg["failed"],
        "series": {"granularity": granularity, "buckets": buckets},
        "top_categories": top_categories,
    }


def _count_queue_window_risk(now, risk_hours: int) -> int:
    """QUEUED 중 메시징 윈도우 만료까지 risk_hours 이내인 건수.

    AdminDMBacklogView(views/autodm.py) 의 window_risk 로직을 그대로 복제한다
    (comment_id 있으면 7일, 없으면 24h 윈도우; created_at 순 스캔 상한 2000).
    autodm.py 는 안정 파일이라 리팩터링하지 않는다 — 로직 변경 시 양쪽 동기화 필요.
    """
    risk_cut = timedelta(hours=risk_hours)
    count = 0
    queued = SentDMLog.objects.filter(status=SentDMLog.Status.QUEUED)
    for cid, created in queued.order_by("created_at").values_list("comment_id", "created_at")[
        :2000
    ]:
        window = timedelta(days=7) if cid else timedelta(hours=24)
        if (created + window) - now <= risk_cut:
            count += 1
    return count


def _action_required(now, since, dm_agg: dict, ig_block: dict, billing: dict) -> list[dict]:
    """고정 순서 조치 목록 — count=0 항목도 포함(프론트 고정 레이아웃).

    severity 규칙: count == 0 → "ok", count >= 1 → "warning".
    """
    since_iso = timezone.localtime(since).isoformat()
    stuck_cutoff = now - timedelta(minutes=STUCK_SUBMITTING_MINUTES)
    stuck_submitting = SentDMLog.objects.filter(
        status=SentDMLog.Status.SUBMITTING, created_at__lt=stuck_cutoff
    ).count()
    queued_window_risk = _count_queue_window_risk(now, QUEUE_WINDOW_RISK_HOURS)
    ig_review = UserSubscription.objects.filter(ig_activation_review_needed=True).count()

    items = [
        (
            "expired_tokens",
            "토큰 만료 IG 계정",
            ig_block["by_status"]["expired"],
            {"page": "/auto-dm/ig-connections", "params": {"status": "expired"}},
        ),
        (
            "expiring_tokens_24h",
            "24h 내 토큰 만료 예정",
            ig_block["expiring_24h"],
            # 프론트 힌트 — 목록 API 에 필터 추가 전까지는 안내용 파라미터
            {"page": "/auto-dm/ig-connections", "params": {"expiring_within_hours": "24"}},
        ),
        (
            "failed_param_recent",
            "파라미터 오류 실패 (윈도우)",
            dm_agg["failed_param"],
            {"page": "/auto-dm/logs", "params": {"status": "failed_param", "since": since_iso}},
        ),
        (
            "failed_no_trace_recent",
            "도착 미확인 (윈도우)",
            dm_agg["failed_no_trace"],
            {"page": "/auto-dm/logs", "params": {"status": "failed_no_trace", "since": since_iso}},
        ),
        (
            "stuck_submitting",
            f"SUBMITTING {STUCK_SUBMITTING_MINUTES}분+ 정체",
            stuck_submitting,
            {"page": "/auto-dm/logs", "params": {"status": "submitting"}},
        ),
        (
            "queued_window_risk",
            f"윈도우 만료 임박 대기건 ({QUEUE_WINDOW_RISK_HOURS}h)",
            queued_window_risk,
            {"page": "/auto-dm/backlog", "params": {}},
        ),
        (
            "past_due_subscriptions",
            "결제 연체(past_due) 구독",
            billing["past_due"],
            {"page": "/users", "params": {"subscription_status": "past_due"}},
        ),
        (
            "ig_activation_review",
            "IG 활성 계정 재선택 필요",
            ig_review,
            {"page": "/users", "params": {"ig_activation_review": "true"}},
        ),
        (
            "unprocessed_webhooks",
            f"미처리 토스 웹훅 ({WEBHOOK_BACKLOG_STALE_MINUTES}분+)",
            billing["webhook_backlog"],
            {"page": None, "params": {}},
        ),
    ]
    return [
        {
            "key": key,
            "label": label,
            "count": count,
            "severity": "ok" if count == 0 else "warning",
            "link": link,
        }
        for key, label, count, link in items
    ]


def _recent_errors(since) -> list[dict]:
    """DM 실패 / 결제 실패 / 스팸 숨김 실패 3종 병합 (timestamp desc, 최대 20)."""
    errors: list[dict] = []

    dm_failures = (
        SentDMLog.objects.filter(created_at__gte=since, status__in=DM_FAILED_STATUSES)
        .select_related("campaign__ig_connection")
        .order_by("-created_at")[:RECENT_ERRORS_LIMIT]
    )
    for log in dm_failures:
        conn = getattr(log.campaign, "ig_connection", None)
        detail = f"{log.status}: {(log.error_message or '')[:200]}".rstrip(": ")
        errors.append(
            {
                "type": "dm_failure",
                "timestamp": timezone.localtime(log.created_at).isoformat(),
                "subject": getattr(conn, "username", "") or "",
                "detail": detail,
                "ref_id": str(log.id),
                "link": {"page": "/auto-dm/logs", "params": {"id": str(log.id)}},
            }
        )

    payment_failures = (
        PaymentHistory.objects.filter(created_at__gte=since, status=PaymentStatus.FAILED)
        .select_related("user")
        .order_by("-created_at")[:RECENT_ERRORS_LIMIT]
    )
    for p in payment_failures:
        detail = f"{p.failure_code}: {p.failure_message}".strip(": ") or "결제 실패"
        errors.append(
            {
                "type": "payment_failure",
                "timestamp": timezone.localtime(p.created_at).isoformat(),
                "subject": p.user.email,
                "detail": detail[:200],
                "ref_id": str(p.id),
                "link": {"page": "/users", "params": {"id": p.user_id}},
            }
        )

    spam_failures = (
        SpamCommentLog.objects.filter(created_at__gte=since, status=SpamCommentLog.Status.FAILED)
        .select_related("spam_filter__ig_connection")
        .order_by("-created_at")[:RECENT_ERRORS_LIMIT]
    )
    for s in spam_failures:
        conn = getattr(s.spam_filter, "ig_connection", None)
        errors.append(
            {
                "type": "spam_hide_failure",
                "timestamp": timezone.localtime(s.created_at).isoformat(),
                "subject": getattr(conn, "username", "") or "",
                "detail": (s.error_message or "숨김 처리 실패")[:200],
                "ref_id": str(s.id),
                "link": {"page": None, "params": {}},
            }
        )

    errors.sort(key=lambda e: e["timestamp"], reverse=True)
    return errors[:RECENT_ERRORS_LIMIT]


def _risk_accounts(now) -> list[dict]:
    """토큰 상태 × 24h DM 품질 병합 스코어링 → 상위 RISK_ACCOUNTS_LIMIT.

    점수: token_expired=3 / critical_delivery_rate(<0.75, 표본>=20)=3 /
          low_delivery_rate(<0.90, 표본>=20)=2 / token_expiring_24h=2 /
          repeated_param_errors(failed_param>=5)=1.
    정렬: (-score, delivery_rate 오름차순 — 표본 없으면 후순위).
    """
    last_24h = now - timedelta(hours=24)

    # 소스 1: 24h per-connection DM 집계 (ACTIVE 한정 없음 — 만료 계정도 평가)
    dm_metrics: dict = {}
    per_conn = (
        SentDMLog.objects.filter(created_at__gte=last_24h)
        .values("campaign__ig_connection_id")
        .annotate(
            accepted=Count("id", filter=Q(status=SentDMLog.Status.ACCEPTED)),
            delivered=Count("id", filter=Q(status=SentDMLog.Status.DELIVERED)),
            read=Count("id", filter=Q(status=SentDMLog.Status.READ)),
            failed_no_trace=Count("id", filter=Q(status=SentDMLog.Status.FAILED_NO_TRACE)),
            failed_param=Count("id", filter=Q(status=SentDMLog.Status.FAILED_PARAM)),
        )
    )
    for row in per_conn:
        sample = _accepted_or_after(row)
        dm_metrics[row["campaign__ig_connection_id"]] = {
            "sample": sample,
            "rate": _delivery_rate(row) if sample else None,
            "failed_param": row["failed_param"],
        }

    # 소스 2: 토큰 상태 (만료 or 24h 내 만료 예정 ACTIVE)
    expiring_cutoff = now + timedelta(hours=TOKEN_EXPIRING_SOON_HOURS)
    token_state: dict = {}
    token_rows = IGAccountConnection.objects.filter(
        Q(status=IGAccountConnection.Status.EXPIRED)
        | Q(
            status=IGAccountConnection.Status.ACTIVE,
            token_expires_at__gt=now,
            token_expires_at__lte=expiring_cutoff,
        )
    ).values("id", "status")
    for row in token_rows:
        token_state[row["id"]] = row["status"]

    scored = []
    for conn_id in set(dm_metrics) | set(token_state):
        m = dm_metrics.get(conn_id, {"sample": 0, "rate": None, "failed_param": 0})
        score = 0
        reasons = []
        t_status = token_state.get(conn_id)
        if t_status == IGAccountConnection.Status.EXPIRED:
            score += 3
            reasons.append("token_expired")
        elif t_status == IGAccountConnection.Status.ACTIVE:
            score += 2
            reasons.append("token_expiring_24h")
        if m["sample"] >= DM_MIN_SAMPLE_FOR_STATUS and m["rate"] is not None:
            if m["rate"] < DM_DELIVERY_CRITICAL_THRESHOLD:
                score += 3
                reasons.append("critical_delivery_rate")
            elif m["rate"] < DM_DELIVERY_WARNING_THRESHOLD:
                score += 2
                reasons.append("low_delivery_rate")
        if m["failed_param"] >= RISK_REPEATED_PARAM_ERRORS_COUNT:
            score += 1
            reasons.append("repeated_param_errors")
        if score <= 0:
            continue
        scored.append((conn_id, score, reasons, m))

    scored.sort(key=lambda t: (-t[1], t[3]["rate"] if t[3]["rate"] is not None else 2.0))
    top = scored[:RISK_ACCOUNTS_LIMIT]
    if not top:
        return []

    # 표시 필드 일괄 조회 (owner email / username / 토큰 상태)
    display = {
        row["id"]: row
        for row in IGAccountConnection.objects.filter(id__in=[t[0] for t in top]).values(
            "id", "username", "status", "token_expires_at", "workspace__owner__email"
        )
    }
    result = []
    for conn_id, score, reasons, m in top:
        d = display.get(conn_id, {})
        expires = d.get("token_expires_at")
        result.append(
            {
                "ig_connection_id": str(conn_id),
                "username": d.get("username") or "",
                "owner_email": d.get("workspace__owner__email") or "",
                "risk_score": score,
                "reasons": reasons,
                "metrics": {
                    "delivery_rate_24h": m["rate"],
                    "failed_param_24h": m["failed_param"],
                    "token_expires_at": (
                        timezone.localtime(expires).isoformat() if expires else None
                    ),
                    "status": d.get("status") or "",
                },
            }
        )
    return result


def _billing_signals(now, since) -> dict:
    """빌링 서브시스템 신호 (status_summary/action_required 공유)."""
    backlog_cutoff = now - timedelta(minutes=WEBHOOK_BACKLOG_STALE_MINUTES)
    critical_cutoff = now - timedelta(minutes=WEBHOOK_BACKLOG_CRITICAL_MINUTES)
    return {
        "failed_payments": PaymentHistory.objects.filter(
            created_at__gte=since, status=PaymentStatus.FAILED
        ).count(),
        "past_due": UserSubscription.objects.filter(status=SubscriptionStatus.PAST_DUE).count(),
        "webhook_backlog": TossWebhookLog.objects.filter(
            processed=False, created_at__lt=backlog_cutoff
        ).count(),
        "webhook_stale_critical": TossWebhookLog.objects.filter(
            processed=False, created_at__lt=critical_cutoff
        ).exists(),
    }


def _status_summary(dm_agg: dict, dm_rate: float, ig_block: dict, spam_block: dict, billing: dict):
    """이미 계산된 수치로 서브시스템 신호등 판정 — 규칙은 dashboard_constants 도크스트링 참고."""
    # dm: 표본 미달이면 판정 안 함(ok + insufficient_sample). 경계는 strict < .
    sample = _accepted_or_after(dm_agg)
    insufficient = sample < DM_MIN_SAMPLE_FOR_STATUS
    if insufficient:
        dm_status = "ok"
    elif dm_rate < DM_DELIVERY_CRITICAL_THRESHOLD:
        dm_status = "critical"
    elif dm_rate < DM_DELIVERY_WARNING_THRESHOLD:
        dm_status = "warning"
    else:
        dm_status = "ok"

    expired = ig_block["by_status"]["expired"]
    expiring = ig_block["expiring_24h"]
    if expired >= IG_EXPIRED_CRITICAL_COUNT:
        ig_status = "critical"
    elif expired + expiring > 0:
        ig_status = "warning"
    else:
        ig_status = "ok"

    hide_failed = spam_block["failed"]
    if hide_failed >= SPAM_HIDE_FAILED_CRITICAL_COUNT:
        spam_status = "critical"
    elif hide_failed >= SPAM_HIDE_FAILED_WARNING_COUNT:
        spam_status = "warning"
    else:
        spam_status = "ok"

    if billing["past_due"] >= PAST_DUE_CRITICAL_COUNT or billing["webhook_stale_critical"]:
        billing_status = "critical"
    elif (
        billing["failed_payments"] >= PAYMENT_FAILED_WARNING_COUNT
        or billing["past_due"] >= 1
        or billing["webhook_backlog"] >= 1
    ):
        billing_status = "warning"
    else:
        billing_status = "ok"

    statuses = [dm_status, ig_status, spam_status, billing_status]
    overall = max(statuses, key=lambda s: _STATUS_RANK[s])
    return {
        "overall": overall,
        "subsystems": {
            "dm": {
                "status": dm_status,
                "delivery_rate": dm_rate,
                "sample": sample,
                "insufficient_sample": insufficient,
            },
            "ig_connections": {
                "status": ig_status,
                "expired": expired,
                "expiring_24h": expiring,
            },
            "spam_filter": {"status": spam_status, "hide_failed": hide_failed},
            "billing": {
                "status": billing_status,
                "failed_payments": billing["failed_payments"],
                "past_due": billing["past_due"],
                "webhook_backlog": billing["webhook_backlog"],
            },
        },
    }


class AdminOpsDashboardView(APIView):
    """어드민 운영 대시보드 집계 (단일 GET, Redis 30s 캐시)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminOpsDashboardSerializer

    @extend_schema(
        tags=["admin-dashboard"],
        summary="[관리자] 운영 대시보드 집계",
        description="""
## 개요
운영자 관제 화면용 **전사(GLOBAL) 운영 지표**를 단일 호출로 반환합니다.
DM 발송 품질 / IG 연동 / 스팸 필터 / 빌링 4개 서브시스템의 신호등(`status_summary`),
고정 순서 조치 목록(`action_required`), 제로필 시계열(`dm_quality.series`, `spam.series`),
최근 오류 3종 병합(`recent_errors`), 위험 계정 스코어링(`risk_accounts`)을 포함합니다.

## 사용 시나리오
- 백오피스 운영 대시보드에서 30~60초 간격 폴링으로 상태 갱신
- `status_summary.overall` 색상으로 즉각적 건강 상태 파악, `action_required` 로 드릴다운
- `recent_errors` / `risk_accounts` 로 개별 계정·결제 문제 즉시 추적

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만 접근)
- 미인증 401, 일반 사용자(비스태프) 403.

## 비즈니스 로직
- **전수 집계**: request.user 소속 워크스페이스로 필터하지 않습니다 (백오피스 전역).
- `window`: `1h`(5분 버킷) / `24h`(시간 버킷, 기본) / `today`(Asia/Seoul 자정~현재, 시간 버킷).
  잘못된 값은 **400** — 레거시 `/metrics/overview/` 의 `since` 폴백과 달리 엄격 검증.
- `delivery_rate` 는 `/integrations/dm-verification/stats/` 와 동일 공식:
  `(delivered+read) / (accepted+delivered+read+failed_no_trace)`.
- **상태 판정 임계값** (단일 소스: `apps/admin_api/dashboard_constants.py`):

| 서브시스템 | warning | critical |
|---|---|---|
| dm | rate < 0.90 (표본 ≥ 20) | rate < 0.75 (표본 ≥ 20) |
| ig_connections | expired+expiring_24h ≥ 1 | expired ≥ 10 |
| spam_filter | 윈도우 내 숨김 실패 ≥ 1 | ≥ 10 |
| billing | 결제실패 ≥ 1 or past_due ≥ 1 or 웹훅백로그 ≥ 1 | past_due ≥ 10 or 30분+ 미처리 웹훅 |

  경계: 비율은 strict `<` (rate==0.90 → ok, rate==0.75 → warning),
  토큰 만료 컷오프는 `<=` (token_expires_at == now+24h → expiring 포함).
  표본(accepted_or_after) < 20 이면 dm 은 판정하지 않고 `ok` + `insufficient_sample=true`.
- `action_required` 는 **고정 순서 배열**이며 count=0 항목도 포함합니다 (프론트 고정 레이아웃).
  severity: count==0 → ok, count≥1 → warning. `link.page`/`link.params` 는 백오피스
  화면 라우팅 힌트입니다 (`expiring_within_hours` 는 목록 API 필터 추가 전까지 안내용).
- `series` 버킷은 빈 구간을 0 으로 제로필합니다 (ts 는 Asia/Seoul 로컬 ISO 8601).
- 응답은 Redis 에 **30초 캐시**됩니다 (키 `admin:dash:ops:{window}`) —
  `generated_at` 으로 신선도를 표시하세요.

## 주의사항
- IG access_token / 토스 빌링키 등 비밀값은 절대 직렬화하지 않습니다.
- 읽기 전용 — AdminActionLog 감사 기록 없음.
- `queued_window_risk` 는 QUEUED 스캔 상한 2000 건 (AdminDMBacklogView 와 동일 로직).

### 요청 예시
```bash
curl -H "Authorization: Bearer <staff_token>" \\
  "https://api.example.com/api/v1/admin/dashboard/operations/?window=24h"
```
        """,
        parameters=[
            OpenApiParameter(
                name="window",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=list(ALLOWED_WINDOWS),
                description="집계 윈도우. 1h(5분 버킷) / 24h(기본, 시간 버킷) / "
                "today(Asia/Seoul 자정~현재). 그 외 값은 400.",
            ),
        ],
        responses={
            200: AdminOpsDashboardSerializer,
            400: OpenApiResponse(
                description="잘못된 window 값 — "
                '{"success": false, "error": {"code": 400, "message": "...", '
                '"details": {"allowed": ["1h","24h","today"]}}}'
            ),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자(is_staff) 권한 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                response_only=True,
                value={
                    "window": "24h",
                    "since": "2026-07-10T14:00:00+09:00",
                    "generated_at": "2026-07-11T14:00:03+09:00",
                    "status_summary": {
                        "overall": "warning",
                        "subsystems": {
                            "dm": {
                                "status": "ok",
                                "delivery_rate": 0.9932,
                                "sample": 1423,
                                "insufficient_sample": False,
                            },
                            "ig_connections": {
                                "status": "warning",
                                "expired": 3,
                                "expiring_24h": 2,
                            },
                            "spam_filter": {"status": "ok", "hide_failed": 0},
                            "billing": {
                                "status": "warning",
                                "failed_payments": 1,
                                "past_due": 2,
                                "webhook_backlog": 0,
                            },
                        },
                    },
                    "action_required": [
                        {
                            "key": "expired_tokens",
                            "label": "토큰 만료 IG 계정",
                            "count": 3,
                            "severity": "warning",
                            "link": {
                                "page": "/auto-dm/ig-connections",
                                "params": {"status": "expired"},
                            },
                        },
                        {
                            "key": "unprocessed_webhooks",
                            "label": "미처리 토스 웹훅 (10분+)",
                            "count": 0,
                            "severity": "ok",
                            "link": {"page": None, "params": {}},
                        },
                    ],
                    "dm_quality": {
                        "requested": 1500,
                        "succeeded": 1410,
                        "accepted_pending": 13,
                        "failed": 47,
                        "skipped": 25,
                        "queued": 5,
                        "submitting": 0,
                        "delivery_rate": 0.9932,
                        "series": {
                            "granularity": "hour",
                            "buckets": [
                                {
                                    "ts": "2026-07-10T14:00:00+09:00",
                                    "requested": 63,
                                    "succeeded": 60,
                                    "failed": 2,
                                    "skipped": 1,
                                }
                            ],
                        },
                    },
                    "ig_connections": {
                        "total": 451,
                        "by_status": {"active": 410, "expired": 23, "revoked": 15, "error": 3},
                        "expiring_24h": 2,
                        "soft_deactivated": 6,
                    },
                    "spam": {
                        "checked": 4200,
                        "detected": 130,
                        "hidden": 110,
                        "failed": 2,
                        "series": {
                            "granularity": "hour",
                            "buckets": [
                                {"ts": "2026-07-10T14:00:00+09:00", "detected": 6, "hidden": 5}
                            ],
                        },
                        "top_categories": [{"category": "promo", "count": 61}],
                    },
                    "recent_errors": [
                        {
                            "type": "dm_failure",
                            "timestamp": "2026-07-11T13:55:00+09:00",
                            "subject": "brand_official",
                            "detail": "failed_param: (#100) Param recipient...",
                            "ref_id": "5b1f0c2e-0000-4a00-9c00-000000000001",
                            "link": {
                                "page": "/auto-dm/logs",
                                "params": {"id": "5b1f0c2e-0000-4a00-9c00-000000000001"},
                            },
                        }
                    ],
                    "risk_accounts": [
                        {
                            "ig_connection_id": "5b1f0c2e-0000-4a00-9c00-000000000002",
                            "username": "shop_kr",
                            "owner_email": "o@x.com",
                            "risk_score": 5,
                            "reasons": ["token_expired", "low_delivery_rate"],
                            "metrics": {
                                "delivery_rate_24h": 0.71,
                                "failed_param_24h": 6,
                                "token_expires_at": None,
                                "status": "expired",
                            },
                        }
                    ],
                },
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        request_id = getattr(request, "id", "") or ""
        window = request.query_params.get("window", "24h")
        if window not in ALLOWED_WINDOWS:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": f"잘못된 window 값입니다: {window!r}",
                        "details": {"allowed": list(ALLOWED_WINDOWS)},
                    },
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        cache_key = CACHE_KEY_TMPL.format(window=window)
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        now = timezone.now()
        since, granularity = _window_bounds(window, now)

        dm_block, dm_agg = _dm_quality(now, since, granularity)
        ig_block = _ig_connections(now)
        spam_block = _spam(now, since, granularity)
        billing = _billing_signals(now, since)

        payload = {
            "window": window,
            "since": timezone.localtime(since).isoformat(),
            "generated_at": timezone.localtime(now).isoformat(),
            "status_summary": _status_summary(
                dm_agg, dm_block["delivery_rate"], ig_block, spam_block, billing
            ),
            "action_required": _action_required(now, since, dm_agg, ig_block, billing),
            "dm_quality": dm_block,
            "ig_connections": ig_block,
            "spam": spam_block,
            "recent_errors": _recent_errors(since),
            "risk_accounts": _risk_accounts(now),
        }

        data = AdminOpsDashboardSerializer(payload).data
        cache.set(cache_key, data, OPS_DASHBOARD_CACHE_TTL)

        logger.info(
            "[admin-dash-ops] req=%s window=%s overall=%s dm_rate=%s",
            request_id,
            window,
            payload["status_summary"]["overall"],
            dm_block["delivery_rate"],
        )
        return Response(data)
