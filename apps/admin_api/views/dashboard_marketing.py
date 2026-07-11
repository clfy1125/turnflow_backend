"""apps/admin_api/views/dashboard_marketing.py — 어드민 마케팅 대시보드 집계.

라우팅: ``GET /api/v1/admin/dashboard/marketing/`` (``IsAdminUser``, is_staff=True).

방문→가입→활성화→유료 퍼널, 채널별 성과, 업셀 후보, 기능별 사용 통계,
플랜 분포, MRR 을 단일 호출로 반환한다. 기간 비교(KPI)는 전부
``{current, previous, delta_pct}`` 구조.

핵심 의미론 (결정 근거는 설계 문서 §3):
- **퍼널 = 가입 코호트(signup_cohort)**: 단계 2~5 는 ``date_joined ∈ 기간`` 인 유저가
  "현재까지" 해당 단계에 도달했는지로 센다 (기간-활동 카운트는 모집단이 섞여 100% 초과
  전환율이 나올 수 있음). 1단계(방문)만 기간-이벤트 기준.
- ``first_page_published`` 는 **근사치** — 공개 시각 미기록이라 첫 *공개* 페이지의
  ``created_at`` 을 대용한다. 코호트 단계("현재 공개 페이지 보유")는 정확.
- ``paid_conversions`` 는 유저별 **첫 PAID PaymentHistory 의 paid_at** 기준 —
  ``pro_activated_at`` 은 환불 시 null 처리되어 부적합 (tasks.py:935).
- **MRR 은 point-in-time 라이브 계산** — 과거 시점 재구성이 불가하므로
  ``mrr.previous = null``. (스냅샷 테이블 도입 트리거: p95 지연 > 1s 또는 MRR 히스토리
  필요 시 ``DailyMetricsSnapshot`` 추가 검토.)
- 어트리뷰션(apps.analytics — 병렬 워크스트림)은 **guarded import**: 앱이 아직 없으면
  ``attribution_available=false`` 로 visits/channels 만 0/빈 값 강등, 나머지는 정상 동작.
- 레퍼럴 오버레이: ReferralRedemption 보유 유저는 저장된 채널과 무관하게 조회 시점에
  channel="referral" 로 분류 (코드 사용이 가입 이후라 가입 시점 저장 불가).
- 업셀 후보의 DM 사용량은 **실제 과금 정의**(billing.dm_limits) 재사용 —
  SENT_FOR_QUOTA_STATUSES + (캠페인 × 수신자) 고유쌍, 캘린더월(_month_bounds).
- 모든 카운트는 전사(GLOBAL). 응답은 Redis 5분 캐시 (키 ``admin:dash:mkt:{period}``).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, Exists, Min, OuterRef, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.dashboard_constants import (
    MARKETING_DASHBOARD_CACHE_TTL,
    TOP_PAGES_LIMIT,
    UPSELL_CANDIDATES_LIMIT,
    UPSELL_CLICKS_HIGH,
    UPSELL_CLICKS_MID,
    UPSELL_DM_RATIO_HIGH,
    UPSELL_DM_RATIO_MID,
    UPSELL_MULTI_IG_MIN,
    UPSELL_SPAM_HEAVY,
)
from apps.admin_api.serializers.dashboard_marketing import AdminMarketingDashboardSerializer
from apps.billing.dm_limits import DEFAULT_DM_MONTHLY_LIMIT
from apps.billing.models import (
    EXTRA_IG_ACCOUNT_PRICE,
    PaymentHistory,
    PaymentStatus,
    ReferralRedemption,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)
from apps.integrations.campaign_stats import SENT_FOR_QUOTA_STATUSES, _month_bounds
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog, SpamCommentLog
from apps.pages.models import BlockClick, Page, PageView

logger = logging.getLogger(__name__)

User = get_user_model()

# ── 어트리뷰션 서브시스템 (병렬 워크스트림 apps.analytics) — guarded import ──
# 앱이 아직 없거나(ImportError) 파일은 있는데 INSTALLED_APPS 미등록(RuntimeError)이어도
# 이 모듈은 깨지지 않고 attribution_available=false 로 강등된다.
# 어트리뷰션 앱 경로가 apps.analytics 와 다르게 확정되면 여기 한 곳만 바꾸면 된다.
try:
    from apps.analytics.models import LandingVisit, SignupAttribution

    ATTRIBUTION_AVAILABLE = True
except (ImportError, RuntimeError):
    LandingVisit = None
    SignupAttribution = None
    ATTRIBUTION_AVAILABLE = False

ALLOWED_PERIODS = {"7d": 7, "30d": 30, "90d": 90}
CACHE_KEY_TMPL = "admin:dash:mkt:{period}"
CACHE_KEY_CUSTOM_TMPL = "admin:dash:mkt:custom:{start}:{end}"
MAX_CUSTOM_SPAN_DAYS = 366  # 커스텀 범위 상한 (초과 시 400)

TARGET_UPSELL_PLANS = ("free", "basic")
COHORT_SNAPSHOT_WARN_ROWS = 50_000  # 코호트가 이보다 크면 스냅샷 테이블 전환 검토 로그

# 활성화 판정에 쓰는 스팸 차단 상태 (FAILED 는 차단 실패라 제외)
_SPAM_BLOCKED_STATUSES = (SpamCommentLog.Status.DETECTED, SpamCommentLog.Status.HIDDEN)


# ── 공통 헬퍼 ────────────────────────────────────────────────────────


def _delta_metric(current: int, previous: int) -> dict:
    """{current, previous, delta_pct} — previous == 0 이면 delta_pct = null."""
    delta = round((current - previous) / previous * 100, 1) if previous else None
    return {"current": current, "previous": previous, "delta_pct": delta}


def _rate(numer: int, denom: int) -> float | None:
    return round(numer / denom, 4) if denom else None


def _cohort_qs(start, end):
    """가입 코호트 + 단계 도달 플래그 annotate (Exists 서브쿼리 — 단일 쿼리)."""
    has_ig = Exists(IGAccountConnection.objects.filter(workspace__owner=OuterRef("pk")))
    has_page = Exists(Page.objects.filter(user=OuterRef("pk"), is_public=True))
    has_camp = Exists(AutoDMCampaign.objects.filter(ig_connection__workspace__owner=OuterRef("pk")))
    has_paid = Exists(PaymentHistory.objects.filter(user=OuterRef("pk"), status=PaymentStatus.PAID))
    return User.objects.filter(date_joined__gte=start, date_joined__lt=end).annotate(
        ig=has_ig, pg=has_page, cp=has_camp, pd=has_paid
    )


def _cohort_agg(start, end) -> dict:
    """코호트 단계 도달 집계 (funnel/kpi 공용) — activated = page ∪ campaign."""
    return _cohort_qs(start, end).aggregate(
        signups=Count("id"),
        ig_connected=Count("id", filter=Q(ig=True)),
        page_published=Count("id", filter=Q(pg=True)),
        dm_campaign=Count("id", filter=Q(cp=True)),
        both=Count("id", filter=Q(pg=True, cp=True)),
        activated=Count("id", filter=Q(pg=True) | Q(cp=True)),
        paid=Count("id", filter=Q(pd=True)),
    )


def _count_first_in_window(qs, group_field: str, ts_field: str, start, end) -> int:
    """그룹별 최초 이벤트(Min(ts))가 기간 내인 그룹 수 — 'first X in period' KPI."""
    return (
        qs.values(group_field)
        .annotate(first=Min(ts_field))
        .filter(first__gte=start, first__lt=end)
        .count()
    )


def _signups_count(start, end) -> int:
    return User.objects.filter(date_joined__gte=start, date_joined__lt=end).count()


# ── 커스텀 범위 / 일별 추이(trends) ─────────────────────────────────────


def _local_midnight(d: date) -> datetime:
    """로컬(Asia/Seoul) 날짜(date) → 그 날 자정의 aware datetime."""
    return timezone.make_aware(
        datetime.combine(d, datetime.min.time()), timezone.get_current_timezone()
    )


def _parse_custom_range(start_raw: str, end_raw: str) -> tuple[date, date]:
    """커스텀 start/end (YYYY-MM-DD) 파싱 + 검증. 실패 시 ValueError(사유).

    end < start / span > MAX_CUSTOM_SPAN_DAYS → ValueError.
    """
    try:
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("start/end 는 YYYY-MM-DD 형식이어야 합니다") from exc
    if end < start:
        raise ValueError("end 는 start 이후여야 합니다")
    if (end - start).days + 1 > MAX_CUSTOM_SPAN_DAYS:
        raise ValueError(f"범위는 최대 {MAX_CUSTOM_SPAN_DAYS}일까지 허용됩니다")
    return start, end


def _local_date_counts(qs, ts_field: str) -> dict:
    """qs 를 로컬(Asia/Seoul) 날짜(TruncDate) 별로 Count → {date: count}."""
    tz = timezone.get_current_timezone()
    return {
        row["d"]: row["c"]
        for row in (
            qs.annotate(d=TruncDate(ts_field, tzinfo=tz)).values("d").annotate(c=Count("id"))
        )
        if row["d"] is not None
    }


def _first_paid_local_date_counts(start, end) -> dict:
    """유저별 첫 PAID paid_at 의 로컬 날짜 → {date: count} (범위 내만).

    KPI 의 first-paid 집합(_count_first_in_window)과 동일 소스를 날짜별로 분해한 것.
    Min(paid_at) 을 유저별로 구해 범위 내인 것만 로컬 날짜로 버킷팅 — 별도 무거운
    스캔 없이 group-by 한 번으로 계산.
    """
    tz = timezone.get_current_timezone()
    counts: dict = {}
    rows = (
        PaymentHistory.objects.filter(status=PaymentStatus.PAID)
        .values("user_id")
        .annotate(first=Min("paid_at"))
        .filter(first__gte=start, first__lt=end)
    )
    for row in rows:
        d = timezone.localtime(row["first"], tz).date()
        counts[d] = counts.get(d, 0) + 1
    return counts


def _trends(start, end) -> dict:
    """현재 기간(range.current) 을 로컬 날짜 단위로 zero-fill 한 일별 추이.

    지표별 1쿼리(TruncDate group-by), 파이썬에서 날짜→버킷 병합.
    - signups: User.date_joined
    - paid: 유저별 첫 PAID paid_at (KPI first-paid 재사용)
    - dm_delivered: SentDMLog status in (delivered, read), created_at
    - page_views: PageView.viewed_at
    - page_clicks: BlockClick.clicked_at
    - visits: LandingVisit.created_at — 어트리뷰션 미탑재 시 전부 0
    """
    signups = _local_date_counts(
        User.objects.filter(date_joined__gte=start, date_joined__lt=end), "date_joined"
    )
    paid = _first_paid_local_date_counts(start, end)
    dm_delivered = _local_date_counts(
        SentDMLog.objects.filter(
            created_at__gte=start,
            created_at__lt=end,
            status__in=(SentDMLog.Status.DELIVERED, SentDMLog.Status.READ),
        ),
        "created_at",
    )
    page_views = _local_date_counts(
        PageView.objects.filter(viewed_at__gte=start, viewed_at__lt=end), "viewed_at"
    )
    page_clicks = _local_date_counts(
        BlockClick.objects.filter(clicked_at__gte=start, clicked_at__lt=end), "clicked_at"
    )
    if ATTRIBUTION_AVAILABLE:
        visits = _local_date_counts(
            LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end), "created_at"
        )
    else:
        visits = {}

    # zero-fill: [start 로컬 날짜, end 로컬 날짜) — end 는 미포함 상한이므로 하루 뺀 날까지
    buckets = []
    cur = timezone.localtime(start).date()
    last = timezone.localtime(end - timedelta(microseconds=1)).date()
    while cur <= last:
        buckets.append(
            {
                "date": cur.isoformat(),
                "signups": signups.get(cur, 0),
                "paid": paid.get(cur, 0),
                "dm_delivered": dm_delivered.get(cur, 0),
                "page_views": page_views.get(cur, 0),
                "page_clicks": page_clicks.get(cur, 0),
                "visits": visits.get(cur, 0),
            }
        )
        cur += timedelta(days=1)
    return {"granularity": "day", "buckets": buckets}


def _visit_counts(start, end) -> tuple[int, int]:
    """(visits, unique_visitors) — 어트리뷰션 미탑재 시 (0, 0)."""
    if not ATTRIBUTION_AVAILABLE:
        return 0, 0
    qs = LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end)
    return qs.count(), qs.order_by().values("visitor_id").distinct().count()


# ── KPI ──────────────────────────────────────────────────────────────


def _kpis(cur: tuple, prev: tuple, mrr_total: int) -> dict:
    """모든 KPI 를 {current, previous, delta_pct} 로. cur/prev = (start, end)."""

    def first_ig(w):
        return _count_first_in_window(
            IGAccountConnection.objects.all(), "workspace__owner_id", "created_at", *w
        )

    def first_page(w):
        # ⚠ 근사 — 공개 시각 미기록: 첫 '공개' 페이지의 created_at 기준 (모듈 도크스트링)
        return _count_first_in_window(
            Page.objects.filter(is_public=True), "user_id", "created_at", *w
        )

    def first_campaign(w):
        return _count_first_in_window(
            AutoDMCampaign.objects.all(),
            "ig_connection__workspace__owner_id",
            "created_at",
            *w,
        )

    def first_paid(w):
        return _count_first_in_window(
            PaymentHistory.objects.filter(status=PaymentStatus.PAID), "user_id", "paid_at", *w
        )

    visits_cur, uniq_cur = _visit_counts(*cur)
    visits_prev, uniq_prev = _visit_counts(*prev)

    mrr = _delta_metric(mrr_total, 0)
    # MRR 은 point-in-time — 과거 시점 재구성 불가 → previous/delta 는 항상 null
    mrr.update({"previous": None, "delta_pct": None, "currency": "KRW"})

    return {
        "visits": _delta_metric(visits_cur, visits_prev),
        "unique_visitors": _delta_metric(uniq_cur, uniq_prev),
        "signups": _delta_metric(_signups_count(*cur), _signups_count(*prev)),
        "ig_connected": _delta_metric(first_ig(cur), first_ig(prev)),
        "first_page_published": _delta_metric(first_page(cur), first_page(prev)),
        "first_dm_campaign": _delta_metric(first_campaign(cur), first_campaign(prev)),
        "paid_conversions": _delta_metric(first_paid(cur), first_paid(prev)),
        "mrr": mrr,
    }


# ── 퍼널 ─────────────────────────────────────────────────────────────


def _funnel(agg: dict, visits_current: int) -> dict:
    """가입 코호트 퍼널 — activated 는 병렬 브랜치(page ∪ campaign)."""
    signups = agg["signups"]
    if signups > COHORT_SNAPSHOT_WARN_ROWS:
        logger.warning(
            "[admin-dash-mkt] cohort %s rows > %s — 스냅샷 테이블 전환 검토 필요",
            signups,
            COHORT_SNAPSHOT_WARN_ROWS,
        )
    stages = [
        {
            "key": "visit",
            "count": visits_current,
            "rate_from_previous": None,
            "rate_from_signups": None,
        },
        {
            "key": "signup",
            "count": signups,
            "rate_from_previous": _rate(signups, visits_current),
            "rate_from_signups": 1.0 if signups else None,
        },
        {
            "key": "ig_connected",
            "count": agg["ig_connected"],
            "rate_from_previous": _rate(agg["ig_connected"], signups),
            "rate_from_signups": _rate(agg["ig_connected"], signups),
        },
        {
            "key": "activated",
            "count": agg["activated"],
            # 페이지 공개는 IG 연결이 불필요하므로(비선형) rate_from_signups 병기
            "rate_from_previous": _rate(agg["activated"], agg["ig_connected"]),
            "rate_from_signups": _rate(agg["activated"], signups),
            "branches": {
                "page_published": agg["page_published"],
                "dm_campaign_created": agg["dm_campaign"],
                "both": agg["both"],
            },
        },
        {
            "key": "paid",
            "count": agg["paid"],
            "rate_from_previous": _rate(agg["paid"], agg["activated"]),
            "rate_from_signups": _rate(agg["paid"], signups),
        },
    ]
    return {"semantics": "signup_cohort", "stages": stages}


# ── 채널 ─────────────────────────────────────────────────────────────


def _channels(start, end) -> dict:
    """채널별 성과 — SignupAttribution 기준, 레퍼럴 오버레이 적용.

    - 어트리뷰션 없는 코호트 가입자는 "unknown" 행 (행 합계 == 코호트 가입자 수).
    - ReferralRedemption 보유 유저는 저장 채널과 무관하게 "referral" (조회 시점 오버레이).
    - referral_codes 는 billing 소스라 어트리뷰션 미탑재여도 항상 채워진다.
    """
    rows: list[dict] = []
    if ATTRIBUTION_AVAILABLE:
        flag_rows = list(_cohort_qs(start, end).values_list("id", "ig", "pg", "cp", "pd"))
        user_ids = [r[0] for r in flag_rows]
        attr_map = dict(
            SignupAttribution.objects.filter(user_id__in=user_ids).values_list("user_id", "channel")
        )
        referral_users = set(
            ReferralRedemption.objects.filter(user_id__in=user_ids).values_list(
                "user_id", flat=True
            )
        )
        per_channel: dict = defaultdict(lambda: {"signups": 0, "activated": 0, "paid": 0})
        for uid, _ig, pg, cp, pd in flag_rows:
            channel = "referral" if uid in referral_users else attr_map.get(uid, "unknown")
            slot = per_channel[channel]
            slot["signups"] += 1
            if pg or cp:
                slot["activated"] += 1
            if pd:
                slot["paid"] += 1

        visits_by_channel = {
            r["channel"]: r["v"]
            for r in LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end)
            .values("channel")
            .annotate(v=Count("id"))
        }
        for channel in set(per_channel) | set(visits_by_channel):
            slot = per_channel.get(channel, {"signups": 0, "activated": 0, "paid": 0})
            visits = visits_by_channel.get(channel, 0)
            rows.append(
                {
                    "channel": channel,
                    "visits": visits,
                    "signups": slot["signups"],
                    "signup_rate": _rate(slot["signups"], visits),
                    "activated": slot["activated"],
                    "activation_rate": _rate(slot["activated"], slot["signups"]),
                    "paid": slot["paid"],
                    "paid_rate": _rate(slot["paid"], slot["signups"]),
                }
            )
        rows.sort(key=lambda r: (-r["signups"], -r["visits"], r["channel"]))

    referral_codes = [
        {
            "code": r["referral_code__code"],
            "redemptions": r["redemptions"],
            "converted": r["converted"],
            "conversion_rate": _rate(r["converted"], r["redemptions"]),
        }
        for r in (
            ReferralRedemption.objects.filter(trial_started_at__gte=start, trial_started_at__lt=end)
            .values("referral_code__code")
            .annotate(
                redemptions=Count("id"),
                converted=Count("id", filter=Q(converted_to_paid=True)),
            )
            .order_by("-redemptions")
        )
    ]
    return {"rows": rows, "referral_codes": referral_codes}


# ── 업셀 후보 ────────────────────────────────────────────────────────


def _upsell_candidates(now) -> list[dict]:
    """free/basic 오너 대상 업셀 스코어링 상위 UPSELL_CANDIDATES_LIMIT(10).

    DM 사용량은 실제 과금 정의(billing.dm_limits)와 동일:
    캘린더월(_month_bounds) 내 SENT_FOR_QUOTA_STATUSES 의 (캠페인 × 수신자) 고유쌍.
    한도는 플랜별 1회만 조회 (SubscriptionPlan.features.dm_monthly_limit,
    없으면 DEFAULT_DM_MONTHLY_LIMIT=200).
    """
    month_start, month_end = _month_bounds(now)
    since_30d = now - timedelta(days=30)

    # 1) DM 쿼터 사용량 — (owner, campaign, recipient) distinct 쌍 → 오너별 Counter.
    #    free/basic 월 한도(≈200)로 행 수가 바운드되어 파이썬 집계로 충분.
    pair_rows = (
        SentDMLog.objects.filter(
            created_at__gte=month_start,
            created_at__lt=month_end,
            status__in=SENT_FOR_QUOTA_STATUSES,
            campaign__ig_connection__workspace__owner__subscription__plan__name__in=(
                TARGET_UPSELL_PLANS
            ),
        )
        .order_by()  # Meta.ordering(-created_at)이 SELECT DISTINCT 에 끼어들면 고유쌍이 깨진다
        .values_list(
            "campaign__ig_connection__workspace__owner_id", "campaign_id", "recipient_user_id"
        )
        .distinct()
    )
    dm_used = Counter(owner_id for owner_id, _cid, _rid in pair_rows)

    # 2) 최근 30d 페이지 클릭 상위
    clicks_map = {
        r["page__user_id"]: r["c"]
        for r in BlockClick.objects.filter(
            clicked_at__gte=since_30d,
            page__user__subscription__plan__name__in=TARGET_UPSELL_PLANS,
        )
        .values("page__user_id")
        .annotate(c=Count("id"))
        .order_by("-c")[:200]
    }

    # 3) 최근 30d 스팸 차단 상위
    spam_map = {
        r["spam_filter__ig_connection__workspace__owner_id"]: r["c"]
        for r in SpamCommentLog.objects.filter(
            created_at__gte=since_30d,
            status__in=_SPAM_BLOCKED_STATUSES,
            spam_filter__ig_connection__workspace__owner__subscription__plan__name__in=(
                TARGET_UPSELL_PLANS
            ),
        )
        .values("spam_filter__ig_connection__workspace__owner_id")
        .annotate(c=Count("id"))
        .order_by("-c")[:200]
    }

    # 4) 복수 활성 IG 연동
    multi_ig_map = {
        r["workspace__owner_id"]: r["n"]
        for r in IGAccountConnection.objects.filter(
            status=IGAccountConnection.Status.ACTIVE,
            is_active=True,
            workspace__owner__subscription__plan__name__in=TARGET_UPSELL_PLANS,
        )
        .values("workspace__owner_id")
        .annotate(n=Count("id"))
        .filter(n__gte=UPSELL_MULTI_IG_MIN)
    }

    owner_ids = set(dm_used) | set(clicks_map) | set(spam_map) | set(multi_ig_map)
    if not owner_ids:
        return []

    # 한도는 플랜별 1회 조회, 오너→플랜 매핑은 단일 쿼리
    limit_by_plan = {
        row["name"]: int((row["features"] or {}).get("dm_monthly_limit", DEFAULT_DM_MONTHLY_LIMIT))
        for row in SubscriptionPlan.objects.filter(name__in=TARGET_UPSELL_PLANS).values(
            "name", "features"
        )
    }
    plan_by_owner = dict(
        UserSubscription.objects.filter(
            user_id__in=owner_ids, plan__name__in=TARGET_UPSELL_PLANS
        ).values_list("user_id", "plan__name")
    )

    scored = []
    for owner_id in owner_ids:
        plan_name = plan_by_owner.get(owner_id)
        if plan_name is None:
            continue  # 소스 조회와 사이 구독 변경 경합 방어
        limit = limit_by_plan.get(plan_name, DEFAULT_DM_MONTHLY_LIMIT)
        used = dm_used.get(owner_id, 0)
        ratio = round(used / limit, 4) if limit > 0 else None
        clicks = clicks_map.get(owner_id, 0)
        spam = spam_map.get(owner_id, 0)

        score = 0
        reasons = []
        if ratio is not None and ratio >= UPSELL_DM_RATIO_HIGH:
            score += 3
            reasons.append("dm_quota_80pct")
        elif ratio is not None and ratio >= UPSELL_DM_RATIO_MID:
            score += 2
            reasons.append("dm_quota_50pct")
        if clicks >= UPSELL_CLICKS_HIGH:
            score += 2
            reasons.append("high_page_traffic")
        elif clicks >= UPSELL_CLICKS_MID:
            score += 1
            reasons.append("high_page_traffic")
        if spam >= UPSELL_SPAM_HEAVY:
            score += 1
            reasons.append("heavy_spam_filtering")
        if owner_id in multi_ig_map:
            score += 2
            reasons.append("multiple_ig_connections")
        if score <= 0:
            continue
        scored.append((owner_id, score, reasons, used, limit, ratio, clicks, spam))

    scored.sort(key=lambda t: (-t[1], -(t[5] or 0.0), -t[6]))
    top = scored[:UPSELL_CANDIDATES_LIMIT]
    if not top:
        return []

    top_ids = [t[0] for t in top]
    display = {
        row["id"]: row
        for row in User.objects.filter(id__in=top_ids).values(
            "id", "email", "subscription__plan__name"
        )
    }
    # 표시용 정확한 활성 IG 연동 수 (multi_ig_map 은 >=2 만 담고 있음)
    ig_counts = {
        r["workspace__owner_id"]: r["n"]
        for r in IGAccountConnection.objects.filter(
            status=IGAccountConnection.Status.ACTIVE,
            is_active=True,
            workspace__owner_id__in=top_ids,
        )
        .values("workspace__owner_id")
        .annotate(n=Count("id"))
    }

    result = []
    for owner_id, score, reasons, used, limit, ratio, clicks, spam in top:
        d = display.get(owner_id, {})
        result.append(
            {
                "user_id": owner_id,
                "email": d.get("email") or "",
                "plan": d.get("subscription__plan__name") or "",
                "score": score,
                "reasons": reasons,
                "metrics": {
                    "dm_used_month": used,
                    "dm_limit": limit,
                    "dm_usage_ratio": ratio,
                    "page_clicks_30d": clicks,
                    "spam_blocked_30d": spam,
                    "active_ig_connections": ig_counts.get(owner_id, 0),
                },
                "link": {"page": f"/users/{owner_id}", "params": {}},
            }
        )
    return result


# ── 기능별 통계 ──────────────────────────────────────────────────────


def _feature_stats(cur: tuple, prev: tuple) -> dict:
    start, end = cur

    # biolink — new_public_pages 는 created_at 근사 (공개 시각 미기록)
    def new_public_pages(w):
        return Page.objects.filter(
            is_public=True, created_at__gte=w[0], created_at__lt=w[1]
        ).count()

    def page_views(w):
        return PageView.objects.filter(viewed_at__gte=w[0], viewed_at__lt=w[1]).count()

    def block_clicks(w):
        return BlockClick.objects.filter(clicked_at__gte=w[0], clicked_at__lt=w[1]).count()

    views_cur = page_views(cur)
    clicks_cur = block_clicks(cur)

    top_page_rows = list(
        PageView.objects.filter(viewed_at__gte=start, viewed_at__lt=end)
        .values("page_id", "page__slug", "page__title")
        .annotate(v=Count("id"))
        .order_by("-v")[:TOP_PAGES_LIMIT]
    )
    top_page_ids = [r["page_id"] for r in top_page_rows]
    top_clicks = {
        r["page_id"]: r["c"]
        for r in BlockClick.objects.filter(
            clicked_at__gte=start, clicked_at__lt=end, page_id__in=top_page_ids
        )
        .values("page_id")
        .annotate(c=Count("id"))
    }
    top_pages = [
        {
            "slug": r["page__slug"],
            "title": r["page__title"] or "",
            "views": r["v"],
            "clicks": top_clicks.get(r["page_id"], 0),
        }
        for r in top_page_rows
    ]

    # dm — delivery_rate 는 표준 공식 (기간 내)
    def campaigns_created(w):
        return AutoDMCampaign.objects.filter(created_at__gte=w[0], created_at__lt=w[1]).count()

    def dm_agg(w):
        return SentDMLog.objects.filter(created_at__gte=w[0], created_at__lt=w[1]).aggregate(
            requested=Count("id"),
            accepted=Count("id", filter=Q(status=SentDMLog.Status.ACCEPTED)),
            delivered=Count("id", filter=Q(status=SentDMLog.Status.DELIVERED)),
            read=Count("id", filter=Q(status=SentDMLog.Status.READ)),
            failed_no_trace=Count("id", filter=Q(status=SentDMLog.Status.FAILED_NO_TRACE)),
        )

    from apps.admin_api.views.dashboard import _delivery_rate  # 표준 공식 재사용

    dm_cur, dm_prev = dm_agg(cur), dm_agg(prev)

    # spam
    def spam_counts(w):
        return SpamCommentLog.objects.filter(created_at__gte=w[0], created_at__lt=w[1]).aggregate(
            detected=Count(
                "id",
                filter=Q(
                    status__in=(
                        SpamCommentLog.Status.DETECTED,
                        SpamCommentLog.Status.HIDDEN,
                        SpamCommentLog.Status.FAILED,
                    )
                ),
            ),
            hidden=Count("id", filter=Q(status=SpamCommentLog.Status.HIDDEN)),
        )

    spam_cur, spam_prev = spam_counts(cur), spam_counts(prev)

    # trials — started = 레퍼럴 트라이얼 + 카드등록 트라이얼(trial_used_at).
    # converted/conversion_rate 는 '레퍼럴 코호트'만 대상 (카드 트라이얼 전환은 전용
    # 플래그가 없어 추적 불가 — 문서화된 한계).
    def referral_started(w):
        return ReferralRedemption.objects.filter(
            trial_started_at__gte=w[0], trial_started_at__lt=w[1]
        )

    def card_trials(w):
        return UserSubscription.objects.filter(
            trial_used_at__gte=w[0], trial_used_at__lt=w[1]
        ).count()

    ref_cur_count = referral_started(cur).count()
    started_cur = ref_cur_count + card_trials(cur)
    started_prev = referral_started(prev).count() + card_trials(prev)
    converted = referral_started(cur).filter(converted_to_paid=True).count()

    return {
        "biolink": {
            "public_pages_total": Page.objects.filter(is_public=True).count(),
            "new_public_pages": _delta_metric(new_public_pages(cur), new_public_pages(prev)),
            "views": _delta_metric(views_cur, page_views(prev)),
            "clicks": _delta_metric(clicks_cur, block_clicks(prev)),
            "ctr": round(clicks_cur / views_cur, 4) if views_cur else 0.0,
            "top_pages": top_pages,
        },
        "dm": {
            "campaigns_created": _delta_metric(campaigns_created(cur), campaigns_created(prev)),
            "requested": _delta_metric(dm_cur["requested"], dm_prev["requested"]),
            "delivered": _delta_metric(
                dm_cur["delivered"] + dm_cur["read"], dm_prev["delivered"] + dm_prev["read"]
            ),
            "delivery_rate": _delivery_rate(dm_cur),
        },
        "spam": {
            "detected": _delta_metric(spam_cur["detected"], spam_prev["detected"]),
            "hidden": _delta_metric(spam_cur["hidden"], spam_prev["hidden"]),
        },
        "trials": {
            "started": _delta_metric(started_cur, started_prev),
            "converted": converted,
            "conversion_rate": _rate(converted, ref_cur_count),
        },
    }


# ── 플랜 분포 / MRR ──────────────────────────────────────────────────


def _plan_distribution() -> list[dict]:
    """전 플랜(비활성 포함), sort_order 순 — /metrics/overview/ 의 by_plan 패턴 재사용."""
    per_plan_status: dict = defaultdict(dict)
    for row in UserSubscription.objects.values("plan_id", "status").annotate(c=Count("id")):
        per_plan_status[row["plan_id"]][row["status"]] = row["c"]

    rows = []
    for plan in SubscriptionPlan.objects.all().order_by("sort_order", "name"):
        by_status = per_plan_status.get(plan.id, {})
        rows.append(
            {
                "name": plan.name,
                "display_name": plan.display_name,
                "total": sum(by_status.values()),
                "active": by_status.get(SubscriptionStatus.ACTIVE, 0),
                "trialing": by_status.get(SubscriptionStatus.TRIALING, 0),
                "past_due": by_status.get(SubscriptionStatus.PAST_DUE, 0),
                "cancelled": by_status.get(SubscriptionStatus.CANCELLED, 0),
            }
        )
    return rows


def _mrr_breakdown() -> dict:
    """point-in-time MRR — ACTIVE 유료 구독만 (TRIALING 은 현금 미발생이라 제외).

    by_plan 의 mrr 은 기본료 합(스냅샷 우선, 없으면 현재 판매가) — 추가 IG 계정 매출은
    extra_ig_accounts 블록으로 분리 (EXTRA_IG_ACCOUNT_PRICE=9,900원/월).
    admin 플랜은 운영용 내부 계정이라 매출이 아님 → 제외 (plan_distribution 에는 표시됨).
    """
    subs = UserSubscription.objects.filter(
        status=SubscriptionStatus.ACTIVE, plan__isnull=False
    ).exclude(plan__name__in=("free", "admin"))

    by_plan = []
    base_total = 0
    for row in (
        subs.values("plan__name", "plan__display_name", "plan__sort_order")
        .annotate(
            subscribers=Count("id"),
            base=Sum(Coalesce("monthly_amount_snapshot", "plan__monthly_price")),
        )
        .order_by("plan__sort_order", "plan__name")
    ):
        base = row["base"] or 0
        base_total += base
        by_plan.append(
            {
                "name": row["plan__name"],
                "display_name": row["plan__display_name"],
                "subscribers": row["subscribers"],
                "mrr": base,
            }
        )

    extra_count = subs.filter(plan__name="pro").aggregate(n=Sum("extra_ig_accounts"))["n"] or 0
    extra_mrr = extra_count * EXTRA_IG_ACCOUNT_PRICE
    return {
        "total": base_total + extra_mrr,
        "by_plan": by_plan,
        "extra_ig_accounts": {
            "count": extra_count,
            "unit_price": EXTRA_IG_ACCOUNT_PRICE,
            "mrr": extra_mrr,
        },
    }


class AdminMarketingDashboardView(APIView):
    """어드민 마케팅 대시보드 집계 (단일 GET, Redis 5분 캐시)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminMarketingDashboardSerializer

    @extend_schema(
        tags=["admin-dashboard"],
        summary="[관리자] 마케팅 대시보드 집계",
        description="""
## 개요
마케팅/그로스 관점의 **전사(GLOBAL) 지표**를 단일 호출로 반환합니다.
KPI(기간 비교), 가입 코호트 퍼널, 채널별 성과, 업셀 후보, 기능별 사용 통계,
플랜 분포, MRR 브레이크다운을 포함합니다.

## 사용 시나리오
- 백오피스 마케팅 대시보드 진입 시 1회 호출 + 기간 토글(7d/30d/90d) 시 재호출
- 캠페인 집행 후 채널별 방문→가입→활성화→유료 전환 효율 비교
- `upsell_candidates` 로 CS/세일즈가 업그레이드 제안 대상 선별

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만 접근)
- 미인증 401, 일반 사용자(비스태프) 403.

## 비즈니스 로직
- **전수 집계**: request.user 소속 워크스페이스로 필터하지 않습니다.
- `period`: `7d` / `30d`(기본) / `90d`. current = [now-N일, now), previous = [now-2N일, now-N일).
  잘못된 값은 **400**. 모든 KPI 는 `{current, previous, delta_pct}` (previous==0 → delta null).
- **커스텀 범위**: `start=YYYY-MM-DD` + `end=YYYY-MM-DD` (Asia/Seoul 로컬 날짜) 를 함께 주면
  `period` 무시하고 커스텀 집계 — `period` 응답은 `"custom"`. current = [start 자정, end+1일 자정),
  previous = **직전 동일 길이 구간** `[start-span, start)` (span = current 길이). **검증(400)**:
  start/end 중 하나만·파싱 불가·`end < start`·span > 366일 → `details.reason`.
- **`trends`(신규, 항상 포함)**: current 기간 전체를 **로컬 날짜 단위로 zero-fill** 한 일별 버킷.
  각 버킷 = `{date(로컬 YYYY-MM-DD), signups, paid, dm_delivered, page_views, page_clicks, visits}`.
  signups=User.date_joined, paid=유저별 첫 PAID paid_at(KPI first-paid 재사용),
  dm_delivered=SentDMLog(delivered/read), page_views=PageView, page_clicks=BlockClick,
  visits=LandingVisit(어트리뷰션 미탑재 시 0). 지표별 1쿼리(TruncDate group-by, Asia/Seoul).
- **퍼널 = 가입 코호트(signup_cohort)**: 2~5단계는 `date_joined ∈ 기간` 유저가 "현재까지"
  단계에 도달했는지 기준 (기간-활동 카운트는 모집단 혼합으로 100% 초과 전환율 가능 → 배제).
  1단계(visit)만 기간-이벤트. `activated` 는 병렬 브랜치 합집합
  (`page_published ∪ dm_campaign_created`, 페이지 공개는 IG 연결 불필요 — 비선형).
- `first_page_published` / `new_public_pages` 는 **근사** — 공개 시각 미기록이라 첫 공개
  페이지의 `created_at` 을 대용합니다.
- `paid_conversions` 는 유저별 **첫 PAID PaymentHistory.paid_at** 기준 —
  `pro_activated_at` 은 환불 시 null 처리되어 부적합.
- **MRR 은 조회 시점 라이브 계산**: ACTIVE 유료 구독의
  `Coalesce(monthly_amount_snapshot, plan.monthly_price)` 합 + 추가 IG 계정
  (`extra_ig_accounts × 9,900원`). TRIALING·free·admin(운영용 내부 플랜) 제외.
  과거 재구성 불가 → `mrr.previous = null`.
- **어트리뷰션 강등**: 트래킹 서브시스템(apps.analytics) 미탑재 시
  `attribution_available=false` — `visits`/`unique_visitors` 는 0, `channels.rows` 는 빈
  배열로 강등되고 나머지 블록은 정상 동작합니다.
- **레퍼럴 오버레이**: `ReferralRedemption` 보유 유저는 저장 채널과 무관하게
  `channel="referral"` 로 재분류 (코드 사용이 가입 이후 발생하므로 조회 시점 오버레이).
- `upsell_candidates` 의 DM 사용량은 **실제 과금 정의** 재사용 —
  캘린더월 내 SENT_FOR_QUOTA_STATUSES 의 (캠페인 × 수신자) 고유쌍, 한도는
  `SubscriptionPlan.features.dm_monthly_limit`(기본 200). 점수: 쿼터 80%+ → +3,
  50%+ → +2, 클릭 500+/100+ → +2/+1, 스팸차단 50+ → +1, 활성 IG 2개+ → +2.
- `trials.converted`/`conversion_rate` 는 **레퍼럴 코호트만** 대상 (카드등록 트라이얼
  전환은 전용 플래그 부재로 미추적 — started 에는 포함).
- 응답은 Redis 에 **300초(5분) 캐시** (프리셋 키 `admin:dash:mkt:{period}`,
  커스텀 키 `admin:dash:mkt:custom:{start}:{end}`).

## 주의사항
- 결제/토큰 비밀값은 직렬화하지 않습니다. 읽기 전용 — 감사 로그 없음.
- 코호트가 5만 행을 넘으면 경고 로그 (스냅샷 테이블 전환 트리거).
- p95 지연 > 1s 또는 MRR 히스토리 필요 시 `DailyMetricsSnapshot` 도입 검토 (뷰 도크스트링).

### 요청 예시
```bash
# 프리셋
curl -H "Authorization: Bearer <staff_token>" \\
  "https://api.example.com/api/v1/admin/dashboard/marketing/?period=30d"
# 커스텀 범위 (Asia/Seoul 로컬 날짜, period 무시, previous=직전 동일 길이)
curl -H "Authorization: Bearer <staff_token>" \\
  "https://api.example.com/api/v1/admin/dashboard/marketing/?start=2026-06-01&end=2026-06-30"
```
        """,
        parameters=[
            OpenApiParameter(
                name="period",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=list(ALLOWED_PERIODS),
                description="집계 기간. 7d / 30d(기본) / 90d. 그 외 값은 400. "
                "start&end 를 함께 주면 무시됩니다.",
            ),
            OpenApiParameter(
                name="start",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="커스텀 범위 시작일 (YYYY-MM-DD, Asia/Seoul 로컬 날짜). "
                "end 와 함께 주면 period 무시. 단독 사용 시 400.",
            ),
            OpenApiParameter(
                name="end",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="커스텀 범위 종료일 (YYYY-MM-DD, 포함). span 최대 366일. "
                "end < start / 파싱불가 / 단독 사용 시 400.",
            ),
        ],
        responses={
            200: AdminMarketingDashboardSerializer,
            400: OpenApiResponse(
                description="잘못된 period 값 — "
                '{"success": false, "error": {"code": 400, "message": "...", '
                '"details": {"allowed": ["7d","30d","90d"]}}} '
                "또는 잘못된 커스텀 범위(하나만/역순/파싱불가/span>366) — "
                '{"success": false, "error": {"code": 400, "message": "...", '
                '"details": {"reason": "..."}}}'
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
                    "period": "30d",
                    "range": {
                        "current_start": "2026-06-11T14:00:00+09:00",
                        "current_end": "2026-07-11T14:00:00+09:00",
                        "previous_start": "2026-05-12T14:00:00+09:00",
                        "previous_end": "2026-06-11T14:00:00+09:00",
                    },
                    "generated_at": "2026-07-11T14:00:03+09:00",
                    "attribution_available": True,
                    "kpis": {
                        "visits": {"current": 5400, "previous": 4100, "delta_pct": 31.7},
                        "unique_visitors": {"current": 3900, "previous": 3000, "delta_pct": 30.0},
                        "signups": {"current": 210, "previous": 180, "delta_pct": 16.7},
                        "ig_connected": {"current": 95, "previous": 70, "delta_pct": 35.7},
                        "first_page_published": {
                            "current": 60,
                            "previous": 44,
                            "delta_pct": 36.4,
                        },
                        "first_dm_campaign": {"current": 48, "previous": 39, "delta_pct": 23.1},
                        "paid_conversions": {"current": 18, "previous": 11, "delta_pct": 63.6},
                        "mrr": {
                            "current": 2085000,
                            "previous": None,
                            "delta_pct": None,
                            "currency": "KRW",
                        },
                    },
                    "funnel": {
                        "semantics": "signup_cohort",
                        "stages": [
                            {
                                "key": "visit",
                                "count": 5400,
                                "rate_from_previous": None,
                                "rate_from_signups": None,
                            },
                            {
                                "key": "signup",
                                "count": 210,
                                "rate_from_previous": 0.0389,
                                "rate_from_signups": 1.0,
                            },
                            {
                                "key": "ig_connected",
                                "count": 102,
                                "rate_from_previous": 0.4857,
                                "rate_from_signups": 0.4857,
                            },
                            {
                                "key": "activated",
                                "count": 88,
                                "rate_from_previous": 0.8627,
                                "rate_from_signups": 0.419,
                                "branches": {
                                    "page_published": 55,
                                    "dm_campaign_created": 51,
                                    "both": 18,
                                },
                            },
                            {
                                "key": "paid",
                                "count": 12,
                                "rate_from_previous": 0.1364,
                                "rate_from_signups": 0.0571,
                            },
                        ],
                    },
                    "trends": {
                        "granularity": "day",
                        "buckets": [
                            {
                                "date": "2026-06-11",
                                "signups": 12,
                                "paid": 2,
                                "dm_delivered": 340,
                                "page_views": 210,
                                "page_clicks": 45,
                                "visits": 180,
                            },
                            {
                                "date": "2026-06-12",
                                "signups": 8,
                                "paid": 0,
                                "dm_delivered": 402,
                                "page_views": 260,
                                "page_clicks": 51,
                                "visits": 205,
                            },
                        ],
                    },
                    "channels": {
                        "rows": [
                            {
                                "channel": "instagram",
                                "visits": 2100,
                                "signups": 90,
                                "signup_rate": 0.0429,
                                "activated": 41,
                                "activation_rate": 0.4556,
                                "paid": 7,
                                "paid_rate": 0.0778,
                            },
                            {
                                "channel": "unknown",
                                "visits": 0,
                                "signups": 35,
                                "signup_rate": None,
                                "activated": 9,
                                "activation_rate": 0.2571,
                                "paid": 1,
                                "paid_rate": 0.0286,
                            },
                        ],
                        "referral_codes": [
                            {
                                "code": "CREATOR10",
                                "redemptions": 14,
                                "converted": 3,
                                "conversion_rate": 0.2143,
                            }
                        ],
                    },
                    "upsell_candidates": [
                        {
                            "user_id": 812,
                            "email": "heavy@user.com",
                            "plan": "free",
                            "score": 6,
                            "reasons": ["dm_quota_80pct", "multiple_ig_connections"],
                            "metrics": {
                                "dm_used_month": 168,
                                "dm_limit": 200,
                                "dm_usage_ratio": 0.84,
                                "page_clicks_30d": 640,
                                "spam_blocked_30d": 12,
                                "active_ig_connections": 2,
                            },
                            "link": {"page": "/users/812", "params": {}},
                        }
                    ],
                    "feature_stats": {
                        "biolink": {
                            "public_pages_total": 1450,
                            "new_public_pages": {"current": 88, "previous": 71, "delta_pct": 23.9},
                            "views": {"current": 41000, "previous": 33000, "delta_pct": 24.2},
                            "clicks": {"current": 9800, "previous": 8100, "delta_pct": 21.0},
                            "ctr": 0.239,
                            "top_pages": [
                                {
                                    "slug": "minacoach",
                                    "title": "미나코치",
                                    "views": 4100,
                                    "clicks": 1900,
                                }
                            ],
                        },
                        "dm": {
                            "campaigns_created": {
                                "current": 120,
                                "previous": 95,
                                "delta_pct": 26.3,
                            },
                            "requested": {"current": 44000, "previous": 36000, "delta_pct": 22.2},
                            "delivered": {"current": 43100, "previous": 35200, "delta_pct": 22.4},
                            "delivery_rate": 0.9925,
                        },
                        "spam": {
                            "detected": {"current": 3100, "previous": 2500, "delta_pct": 24.0},
                            "hidden": {"current": 2700, "previous": 2200, "delta_pct": 22.7},
                        },
                        "trials": {
                            "started": {"current": 25, "previous": 30, "delta_pct": -16.7},
                            "converted": 6,
                            "conversion_rate": 0.24,
                        },
                    },
                    "plan_distribution": [
                        {
                            "name": "free",
                            "display_name": "무료",
                            "total": 1100,
                            "active": 1080,
                            "trialing": 0,
                            "past_due": 0,
                            "cancelled": 20,
                        }
                    ],
                    "mrr_breakdown": {
                        "total": 2085000,
                        "by_plan": [
                            {
                                "name": "pro",
                                "display_name": "프로",
                                "subscribers": 130,
                                "mrr": 1937000,
                            }
                        ],
                        "extra_ig_accounts": {"count": 15, "unit_price": 9900, "mrr": 148500},
                    },
                },
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        request_id = getattr(request, "id", "") or ""
        now = timezone.now()
        start_raw = request.query_params.get("start")
        end_raw = request.query_params.get("end")
        custom = bool(start_raw or end_raw)

        if custom:
            if not (start_raw and end_raw):
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 400,
                            "message": "커스텀 범위는 start 와 end 를 모두 지정해야 합니다",
                            "details": {"reason": "start 와 end 를 함께 제공하세요"},
                        },
                    },
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            try:
                start_d, end_d = _parse_custom_range(start_raw, end_raw)
            except ValueError as exc:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 400,
                            "message": "잘못된 커스텀 범위입니다",
                            "details": {"reason": str(exc)},
                        },
                    },
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            period = "custom"
            # current = [start 자정, end+1일 자정), previous = 직전 동일 길이 [start-span, start)
            cur_start = _local_midnight(start_d)
            cur_end = _local_midnight(end_d + timedelta(days=1))
            span = cur_end - cur_start
            cur = (cur_start, cur_end)
            prev = (cur_start - span, cur_start)
            cache_key = CACHE_KEY_CUSTOM_TMPL.format(start=start_raw, end=end_raw)
        else:
            period = request.query_params.get("period", "30d")
            if period not in ALLOWED_PERIODS:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": 400,
                            "message": f"잘못된 period 값입니다: {period!r}",
                            "details": {"allowed": list(ALLOWED_PERIODS)},
                        },
                    },
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            days = ALLOWED_PERIODS[period]
            cur = (now - timedelta(days=days), now)
            prev = (now - timedelta(days=days * 2), now - timedelta(days=days))
            cache_key = CACHE_KEY_TMPL.format(period=period)

        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        mrr_breakdown = _mrr_breakdown()
        cohort = _cohort_agg(*cur)
        visits_current, _uniq = _visit_counts(*cur)

        payload = {
            "period": period,
            "range": {
                "current_start": timezone.localtime(cur[0]).isoformat(),
                "current_end": timezone.localtime(cur[1]).isoformat(),
                "previous_start": timezone.localtime(prev[0]).isoformat(),
                "previous_end": timezone.localtime(prev[1]).isoformat(),
            },
            "generated_at": timezone.localtime(now).isoformat(),
            "attribution_available": ATTRIBUTION_AVAILABLE,
            "kpis": _kpis(cur, prev, mrr_breakdown["total"]),
            "funnel": _funnel(cohort, visits_current),
            "trends": _trends(*cur),
            "channels": _channels(*cur),
            "upsell_candidates": _upsell_candidates(now),
            "feature_stats": _feature_stats(cur, prev),
            "plan_distribution": _plan_distribution(),
            "mrr_breakdown": mrr_breakdown,
        }

        data = AdminMarketingDashboardSerializer(payload).data
        cache.set(cache_key, data, MARKETING_DASHBOARD_CACHE_TTL)

        logger.info(
            "[admin-dash-mkt] req=%s period=%s signups=%s mrr=%s attribution=%s",
            request_id,
            period,
            cohort["signups"],
            mrr_breakdown["total"],
            ATTRIBUTION_AVAILABLE,
        )
        return Response(data)
