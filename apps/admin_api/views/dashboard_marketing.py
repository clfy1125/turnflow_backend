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
- ``period=all``(R-1): current = [서비스 최초 가입 시각, now), **직전 기간 없음** —
  ``range.previous_* = null`` + 모든 delta 지표 ``previous/delta_pct = null``
  (빈 구간을 previous 로 넘기면 "직전 0명"으로 오독됨). 캐시 TTL 은 15분.
- ``snapshot``(R-2): 상단 고정 패널 — **기간 파라미터와 무관한 전체 기간 누적**.
  별도 캐시 키(``admin:dash:mkt:snapshot``)라 모든 period 응답이 같은 값을 공유한다.
- 무료체험 집계(R-4)는 **카드 등록 완료**(billing_key_issued_at) 체험만 —
  어드민이 수동 부여한 무카드 계정을 전환 실적에서 제외한다 (퍼널/채널 공통).
- 모든 카운트는 전사(GLOBAL). 응답은 Redis 5분 캐시 (키 ``admin:dash:mkt:{period}``).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, Exists, F, Max, Min, OuterRef, Q, Sum
from django.db.models.functions import Coalesce, TruncDate, TruncWeek
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.dashboard_constants import (
    CANCEL_REASONS_TOP,
    CHANNEL_CAMPAIGNS_LIMIT,
    CHECKOUT_ATTRIBUTION_WINDOW_DAYS,
    COHORT_MAX_PERIODS,
    COHORT_SUBSCRIPTION_MONTHS,
    COHORT_USAGE_WEEKS,
    CUSTOMER_ACTIONS_LIMIT,
    DORMANT_IDLE_DAYS,
    MARKETING_DASHBOARD_ALL_CACHE_TTL,
    MARKETING_DASHBOARD_CACHE_TTL,
    MARKETING_DASHBOARD_SNAPSHOT_CACHE_TTL,
    ONBOARDING_SAMPLE_LIMIT,
    POST_PAYMENT_WINDOW_DAYS,
    RECENT_CANCELLATIONS_LIMIT,
    RECENT_CHURN_WINDOW_DAYS,
    TOP_PAGES_LIMIT,
    TRENDS_DAY_MAX_SPAN_DAYS,
    TRENDS_WEEK_MAX_SPAN_DAYS,
    UPSELL_CANDIDATES_LIMIT,
    UPSELL_CLICKS_HIGH,
    UPSELL_CLICKS_MID,
    UPSELL_DM_RATIO_HIGH,
    UPSELL_DM_RATIO_MID,
    UPSELL_MULTI_IG_MIN,
    UPSELL_SPAM_HEAVY,
)
from apps.admin_api.pii import apply_pii_policy
from apps.admin_api.roles import resolve_admin_role
from apps.admin_api.serializers.dashboard_marketing import AdminMarketingDashboardSerializer
from apps.ai_jobs.models import AiJob
from apps.billing.dm_limits import DEFAULT_DM_MONTHLY_LIMIT
from apps.billing.models import (
    EXTRA_IG_ACCOUNT_PRICE,
    DailyPaidCohortSnapshot,
    DailySubscriptionSnapshot,
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

# ── 결제 진입 텔레메트리 (apps.analytics.CheckoutEvent) — 별도 guarded import ──
# 마이그레이션 전(프론트 이벤트 미전송)이어도 대시보드는 entry_paths_available=false 로
# 강등되어 정상 동작한다.
try:
    from apps.analytics.models import CheckoutEvent

    CHECKOUT_EVENT_AVAILABLE = True
except (ImportError, RuntimeError):
    CheckoutEvent = None
    CHECKOUT_EVENT_AVAILABLE = False

# ── 구독 취소 텔레메트리 (apps.analytics.CancellationEvent) — guarded import ──
# 해지 사유/취소 방어는 프론트 이벤트 의존 → 미탑재 시 cancel_reasons_available=false 강등.
try:
    from apps.analytics.models import CancellationEvent

    CANCELLATION_EVENT_AVAILABLE = True
except (ImportError, RuntimeError):
    CancellationEvent = None
    CANCELLATION_EVENT_AVAILABLE = False

# AI 페이지 생성으로 간주하는 AiJob.job_type (라벨링·DM폼 등 비-페이지 작업 제외)
AI_PAGE_JOB_TYPES = ("bio_remake", "theme_generation", "copy_generation", "external_import")

# created_breakdown(M-1)의 'AI 생성' 판정 job_type — external_import 는 'imported' 로
# 별도 분류되므로 여기서 제외 (imported > ai > manual 우선순위와 짝).
AI_CREATED_JOB_TYPES = ("bio_remake", "theme_generation", "copy_generation")

# paid_conversions KPI 정의 문자열 (M-2 — 프론트 툴팁 정본)
PAID_CONVERSIONS_DEFINITION = (
    "기간 내 실제 결제(Toss PAID)가 처음 발생한 회원 수 · 체험·쿠폰 미결제 제외"
)
PAID_PLAN_NO_PAYMENT_DEFINITION = (
    "기간 내 체험(카드등록)·쿠폰(레퍼럴)으로 유료 플랜을 시작했고 "
    "현재까지 실제 결제(Toss PAID) 이력이 없는 회원 수"
)

# N-3: trials 비율 2종의 정의 문자열 (프론트 i-아이콘 툴팁 정본).
# 우리 빌링 모델에서 체험 종료 후 '유료플랜 유지'는 결제 성공을 통해서만 일어나므로
# 두 비율 모두 실결제(PAID) 기준 — 차이는 모집단(레퍼럴 한정 vs 전체 체험).
TRIALS_CONVERSION_FORMULA = (
    "기간 내 시작한 레퍼럴(쿠폰) 체험 중 실제 결제(Toss PAID)로 전환된 비율 — "
    "전환 마킹은 결제 성공 시점(converted_to_paid), 카드등록 체험은 분모·분자에서 제외"
)
TRIALS_PAID_CONVERSION_FORMULA = (
    "기간 내 시작한 전체 체험(레퍼럴+카드등록, 회원 중복 제거) 중 "
    "현재까지 실제 결제(Toss PAID)가 발생한 회원 비율"
)
# P-1: 종료 시점 기준 전환율 — 체험 길이가 가변(기본 30일 + 쿠폰 보너스 trial_days)이라
# 시작 코호트 분모는 '아직 안 끝난' 체험이 섞여 과소평가되는 것을 정정하는 대표 지표.
TRIALS_ENDED_CONVERSION_FORMULA = (
    "이 기간에 무료체험이 끝난(만료·중도 해지·체험 중 결제 전환 포함) 고객 중 "
    "실제 결제(Toss PAID)로 이어진 비율 · 체험 길이가 가변이라 시작이 아닌 "
    "종료 시점 기준으로 집계 · 전환 판정은 조회 시점"
)

# period → 일수. "all"(R-1) = 서비스 오픈부터 now 까지라 고정 일수가 없어 None,
# 이 경우에만 previous 구간을 만들지 않는다(prev=None → delta 전부 null).
ALLOWED_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "all": None}
PERIOD_ALL = "all"
CACHE_KEY_TMPL = "admin:dash:mkt:{period}"
CACHE_KEY_CUSTOM_TMPL = "admin:dash:mkt:custom:{start}:{end}"
# R-2: 기간 무관 고정 패널 — period 별 응답이 공유하는 단일 키 (계산 1회)
CACHE_KEY_SNAPSHOT = "admin:dash:mkt:snapshot"
MAX_CUSTOM_SPAN_DAYS = 366  # 커스텀 범위 상한 (초과 시 400)

TARGET_UPSELL_PLANS = ("free", "basic")
COHORT_SNAPSHOT_WARN_ROWS = 50_000  # 코호트가 이보다 크면 스냅샷 테이블 전환 검토 로그

# 채널 키 → 한국어 라벨 (available_channels 라벨 + 드롭다운 표기용). 없는 키는 그대로 표기.
CHANNEL_LABELS = {
    "meta_ads": "Meta 광고",
    "google_ads": "Google 광고",
    "naver_ads": "Naver 광고",
    "tiktok_ads": "틱톡 광고",
    "kakao_ads": "카카오 광고",
    "x_ads": "X(트위터) 광고",
    "linkedin_ads": "링크드인 광고",
    # P-5/Q-5: "인스타 오가닉" 난해 피드백 → 최종 "인스타그램 유입" (키는 저장 계약이라 불변)
    "instagram_organic": "인스타그램 유입",
    "youtube_organic": "유튜브",
    "search_organic": "검색",
    "referral": "레퍼럴",
    "influencer": "인플루언서",
    "direct": "다이렉트",
    "unknown": "미분류",
    "paid_other": "기타 광고",
    "facebook_organic": "페이스북",
    "blog_organic": "블로그",
    "threads_organic": "스레드",
    "tiktok_organic": "틱톡",
    "other_campaign": "기타 캠페인",
    "other_referral": "기타 외부",
}

# 퍼널 노드 라벨 (고정 한국어)
_FUNNEL_NODE_LABELS = {
    "visit": "방문자",  # 고유 방문자(distinct visitor_id) — 세션 수 아님
    "signup": "가입",
    "ig_connected": "IG 연동",
    "dm_campaign": "DM 캠페인",
    "page_created": "페이지 생성",
    "page_published": "페이지 공개",
    # R-3: 분기 4노드를 대체하는 단일 활성화 노드 (분기는 branches 에 그대로 남아 팝업용)
    "activated": "활성화 유저",
    "paid": "유료플랜 전환",  # N-1: 무료체험+실결제 합산으로 재정의 (breakdown 으로 분해)
}

# R-4: '무료체험 진행 중' 판정 — 카드 등록 완료(billing_key_issued_at) 건만.
# 어드민이 수동 부여한 무카드 유료플랜 계정은 전환 실적에서 제외한다.
_TRIAL_PLAN_EXCLUDE = ("free", "admin")

# 활성화 판정에 쓰는 스팸 차단 상태 (FAILED 는 차단 실패라 제외)
_SPAM_BLOCKED_STATUSES = (SpamCommentLog.Status.DETECTED, SpamCommentLog.Status.HIDDEN)

# 결제 진입 트리거(CheckoutEvent.trigger_feature) → 한국어 라벨.
# 프론트 정의 어휘라 없는 키는 원문 표기 (라벨 강제 금지).
TRIGGER_FEATURE_LABELS = {
    "dm_limit": "DM 한도 초과",
    "dm_unlimited": "DM 무제한",
    "page_limit": "페이지 개수 제한",
    "badge_removal": "배지 제거",
    "spam_advanced": "스팸 방어 고급",
    "multi_ig": "다계정 연결",
    "ai_page": "AI 페이지 생성",
    "analytics_export": "분석 다운로드",
    "pricing_direct": "가격표 직접 진입",
    "direct": "가격표 직접 진입",
    "": "미지정",
}

# 온보딩 이탈 세그먼트 라벨/설명 (고정 순서 배열로 응답).
_ONBOARDING_SEGMENTS = [
    ("no_action", "가입 후 무행동", "IG 연동·페이지 생성·DM 캠페인 어느 것도 없음"),
    ("ig_no_campaign", "IG 연동 후 캠페인 없음", "IG 는 연동했으나 DM 캠페인을 만들지 않음"),
    (
        "page_created_not_published",
        "페이지 생성 후 미공개",
        "페이지를 만들었으나 공개(is_public)하지 않음",
    ),
    ("campaign_no_send", "캠페인 생성 후 미발송", "DM 캠페인을 만들었으나 발송 로그가 없음"),
]

# 결제 후 관찰 기능 (paid_at 이후 POST_PAYMENT_WINDOW_DAYS 내 실제 사용 여부).
_POST_PAYMENT_FEATURES = [
    ("dm_send", "DM 발송"),
    ("page_created", "페이지 생성"),
    ("ai_page", "페이지 AI 기능"),
    ("spam_used", "스팸 방어 사용"),
    ("extra_ig", "추가 IG 연동"),
]

# 해지 사유(CancellationEvent.reason) → 한국어 라벨. 없는 키는 원문 표기.
CANCEL_REASON_LABELS = {
    "price": "가격 부담",
    "low_usage": "사용 빈도 낮음",
    "no_effect": "효과를 못 느낌",
    "hard_setup": "세팅 어려움",
    "missing_feature": "원하는 기능 없음",
    "ig_error": "인스타 연동/DM 오류",
    "switched": "다른 서비스 사용",
    "paused": "잠시 중단",
    "other": "기타",
    "": "미지정",
}


# ── 공통 헬퍼 ────────────────────────────────────────────────────────


def _delta_metric(current: int, previous: int | None) -> dict:
    """{current, previous, delta_pct} — previous == 0 이면 delta_pct = null.

    previous is None(= period=all, 비교할 직전 기간 자체가 없음)이면 previous 도 null —
    0 으로 내려보내면 프론트가 "직전 기간에 0명이었다"로 오독한다 (R-1).
    """
    if previous is None:
        return {"current": current, "previous": None, "delta_pct": None}
    delta = round((current - previous) / previous * 100, 1) if previous else None
    return {"current": current, "previous": previous, "delta_pct": delta}


def _rate(numer: int, denom: int) -> float | None:
    return round(numer / denom, 4) if denom else None


def _cohort_qs(start, end):
    """가입 코호트 + 단계 도달 플래그 annotate (Exists 서브쿼리 — 단일 쿼리).

    pgc = 페이지 '생성'(공개 여부 무관), pg = 페이지 '공개'(is_public=True).
    바이오링크 갈래는 생성→공개 2단계라 둘 다 필요.
    cur_plan / cur_status / cur_card 는 **현재 구독**(User.subscription, OneToOne 이라
    행 증식 없음)의 플랜명·상태·빌링키 발급 시각 — 체험/실결제의 플랜별 분해(R-4)와
    '카드 등록 완료' 판정에 쓴다. 구독이 없으면 셋 다 None.
    """
    has_ig = Exists(IGAccountConnection.objects.filter(workspace__owner=OuterRef("pk")))
    has_any_page = Exists(Page.objects.filter(user=OuterRef("pk")))
    has_page = Exists(Page.objects.filter(user=OuterRef("pk"), is_public=True))
    has_camp = Exists(AutoDMCampaign.objects.filter(ig_connection__workspace__owner=OuterRef("pk")))
    has_paid = Exists(PaymentHistory.objects.filter(user=OuterRef("pk"), status=PaymentStatus.PAID))
    return User.objects.filter(date_joined__gte=start, date_joined__lt=end).annotate(
        ig=has_ig,
        pgc=has_any_page,
        pg=has_page,
        cp=has_camp,
        pd=has_paid,
        cur_plan=F("subscription__plan__name"),
        cur_status=F("subscription__status"),
        cur_card=F("subscription__billing_key_issued_at"),
    )


# 현재 유료플랜 무료체험 중 (플랜 free/admin 제외) — 카드 등록 여부는 별도 조건
_Q_TRIALING = Q(subscription__status=SubscriptionStatus.TRIALING) & ~Q(
    subscription__plan__name__in=_TRIAL_PLAN_EXCLUDE
)
# R-4: 카드 등록 완료 체험만 '유료플랜 전환'으로 인정
_Q_TRIAL_CARD = _Q_TRIALING & Q(subscription__billing_key_issued_at__isnull=False)
_Q_TRIAL_NO_CARD = _Q_TRIALING & Q(subscription__billing_key_issued_at__isnull=True)


def _trial_flags(cur_plan, cur_status, cur_card) -> tuple[bool, bool]:
    """(카드 등록 체험 중, 카드 미등록 체험 중) — flag_rows 파이썬 판정용.

    _Q_TRIAL_CARD / _Q_TRIAL_NO_CARD 와 동일 규칙 (집계 SQL 과 정의 일치 필수).
    """
    trialing = (
        cur_status == SubscriptionStatus.TRIALING
        and cur_plan not in _TRIAL_PLAN_EXCLUDE
        # cur_plan is None(구독 없음)이면 cur_status 도 None 이라 위에서 이미 False
    )
    return trialing and cur_card is not None, trialing and cur_card is None


def _cohort_agg(start, end) -> dict:
    """코호트 단계 도달 집계 (funnel/kpi 공용) — activated = page ∪ campaign.

    paid = 실결제(PAID 이력), trial_only = 현재 체험 중(카드 등록 완료) & 미결제 —
    퍼널 conversion 노드(유료플랜 전환)의 count = paid + trial_only (N-1 + R-4).
    pro_trial/basic_trial/pro_paid/basic_paid 는 conversion.breakdown 3분할용
    (현재 구독 플랜 기준), trial_no_card 는 카드 미등록으로 전환에서 제외된 인원.
    """
    return _cohort_qs(start, end).aggregate(
        signups=Count("id"),
        ig_connected=Count("id", filter=Q(ig=True)),
        page_created=Count("id", filter=Q(pgc=True)),
        page_published=Count("id", filter=Q(pg=True)),
        dm_campaign=Count("id", filter=Q(cp=True)),
        both=Count("id", filter=Q(pg=True, cp=True)),
        activated=Count("id", filter=Q(pg=True) | Q(cp=True)),
        paid=Count("id", filter=Q(pd=True)),
        trial_only=Count("id", filter=_Q_TRIAL_CARD & Q(pd=False)),
        pro_trial=Count("id", filter=_Q_TRIAL_CARD & Q(pd=False, subscription__plan__name="pro")),
        basic_trial=Count(
            "id", filter=_Q_TRIAL_CARD & Q(pd=False, subscription__plan__name="basic")
        ),
        pro_paid=Count("id", filter=Q(pd=True, subscription__plan__name="pro")),
        basic_paid=Count("id", filter=Q(pd=True, subscription__plan__name="basic")),
        trial_no_card=Count("id", filter=_Q_TRIAL_NO_CARD & Q(pd=False)),
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


def _service_start(now):
    """period=all 의 current_start — 가장 이른 User.date_joined (회원 0명이면 now)."""
    return User.objects.aggregate(first=Min("date_joined"))["first"] or now


# ── 커스텀 범위 / 일별 추이(trends) ─────────────────────────────────────


def _local_midnight(d: date) -> datetime:
    """로컬(Asia/Seoul) 날짜(date) → 그 날 자정의 aware datetime."""
    return timezone.make_aware(
        datetime.combine(d, datetime.min.time()), timezone.get_current_timezone()
    )


def _month_add(d: date, k: int) -> date:
    """월초일 d 에 k 개월 더한 월초일 (월 산술 — timedelta 로는 불가)."""
    total = d.year * 12 + (d.month - 1) + k
    return date(total // 12, total % 12 + 1, 1)


def _trends_granularity(start, end) -> str:
    """R-5: 구간 길이에 따른 trends 버킷 단위 — day / week(월요일) / month(1일).

    period=all 이면 구간이 수백 일이 되어 일별 버킷 × 채널 분해로 응답이 급증하므로
    자동 상향한다 (임계값은 dashboard_constants).
    """
    span_days = max(1, (end - start).days)
    if span_days <= TRENDS_DAY_MAX_SPAN_DAYS:
        return "day"
    if span_days <= TRENDS_WEEK_MAX_SPAN_DAYS:
        return "week"
    return "month"


def _bucket_of(d: date, granularity: str) -> date:
    """로컬 날짜 → 버킷 시작일 (week=그 주 월요일, month=그 달 1일)."""
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    if granularity == "month":
        return d.replace(day=1)
    return d


def _bucket_next(d: date, granularity: str) -> date:
    """제로필 순회용 — 다음 버킷 시작일."""
    if granularity == "week":
        return d + timedelta(days=7)
    if granularity == "month":
        return _month_add(d, 1)
    return d + timedelta(days=1)


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


def _first_paid_local_dates(start, end) -> dict:
    """유저별 첫 PAID paid_at 의 로컬 날짜 → {user_id: date} (범위 내만).

    KPI 의 first-paid 집합(_count_first_in_window)과 동일 소스 — trends 의 일별 paid
    총량(Counter)과 채널 분해(Q-1 by_channel) 양쪽에 재사용한다.
    """
    tz = timezone.get_current_timezone()
    rows = (
        PaymentHistory.objects.filter(status=PaymentStatus.PAID)
        .values("user_id")
        .annotate(first=Min("paid_at"))
        .filter(first__gte=start, first__lt=end)
    )
    return {row["user_id"]: timezone.localtime(row["first"], tz).date() for row in rows}


def _trend_channel_of(uid, attr_map: dict, referral_users) -> str:
    """trends 채널 귀속 — 채널별 성과 표와 동일 규칙 (저장 채널 + referral 오버라이드)."""
    return "referral" if uid in referral_users else attr_map.get(uid, "unknown")


def _trends(start, end) -> dict:
    """현재 기간(range.current) 을 로컬 날짜 단위로 zero-fill 한 추이.

    버킷 단위는 구간 길이로 자동 결정(R-5): day / week(월요일 시작) / month(1일 시작).
    `date` 는 버킷 **시작일**, 마지막 버킷은 진행 중(미완결)이어도 그대로 내려간다.
    지표별 1쿼리(TruncDate group-by), 파이썬에서 날짜→버킷 병합.
    - signups: User.date_joined
    - paid: 유저별 첫 PAID paid_at (KPI first-paid 재사용)
    - dm_delivered: SentDMLog status in (delivered, read), created_at
    - page_views: PageView.viewed_at
    - page_clicks: BlockClick.clicked_at
    - visits: LandingVisit.created_at (세션 단위 행 수 — kpis.visits 와 동일)
    - activated(Q-1): 그 버킷에 DM 캠페인 생성 or 페이지 공개(공개 페이지 created_at 근사)한
      고유 회원 수 (**버킷 단위 user dedupe** — 주별이면 같은 주 중복 활동은 1명,
      가입 시기 무관 이벤트 기준)
    - by_channel(Q-1): visits/signups/activated/paid 4지표의 채널 분해 —
      귀속은 채널별 성과 표와 동일(저장 채널 + 제휴코드 사용자 referral 오버라이드),
      visits 만 방문 자체의 저장 채널. Σ by_channel == 총량. 전부 0인 채널은 생략.
    """
    tz = timezone.get_current_timezone()
    granularity = _trends_granularity(start, end)

    def _bk(d: date) -> date:
        return _bucket_of(d, granularity)

    def _roll(day_counts: dict) -> Counter:
        """{로컬 날짜: n} → {버킷 시작일: Σn} (day 단위면 그대로)."""
        rolled: Counter = Counter()
        for d, n in day_counts.items():
            rolled[_bk(d)] += n
        return rolled

    # signups — (uid, 버킷) 필요 (채널 분해용)
    signup_rows = [
        (r[0], _bk(r[1]))
        for r in User.objects.filter(date_joined__gte=start, date_joined__lt=end)
        .annotate(d=TruncDate("date_joined", tzinfo=tz))
        .values_list("id", "d")
        if r[1] is not None
    ]
    signups = Counter(b for _uid, b in signup_rows)

    # paid — {uid: 버킷}
    first_paid_map = {uid: _bk(d) for uid, d in _first_paid_local_dates(start, end).items()}
    paid = Counter(first_paid_map.values())

    # activated — 그 버킷에 캠페인 생성 ∪ 공개 페이지 생성 유저 (버킷 → set, 버킷 내 dedupe)
    activated_by_bucket: dict = defaultdict(set)
    for owner_id, d in (
        AutoDMCampaign.objects.filter(created_at__gte=start, created_at__lt=end)
        .annotate(d=TruncDate("created_at", tzinfo=tz))
        .values_list("ig_connection__workspace__owner_id", "d")
    ):
        if d is not None:
            activated_by_bucket[_bk(d)].add(owner_id)
    for user_id, d in (
        Page.objects.filter(is_public=True, created_at__gte=start, created_at__lt=end)
        .annotate(d=TruncDate("created_at", tzinfo=tz))
        .values_list("user_id", "d")
    ):
        if d is not None:
            activated_by_bucket[_bk(d)].add(user_id)

    dm_delivered = _roll(
        _local_date_counts(
            SentDMLog.objects.filter(
                created_at__gte=start,
                created_at__lt=end,
                status__in=(SentDMLog.Status.DELIVERED, SentDMLog.Status.READ),
            ),
            "created_at",
        )
    )
    page_views = _roll(
        _local_date_counts(
            PageView.objects.filter(viewed_at__gte=start, viewed_at__lt=end), "viewed_at"
        )
    )
    page_clicks = _roll(
        _local_date_counts(
            BlockClick.objects.filter(clicked_at__gte=start, clicked_at__lt=end), "clicked_at"
        )
    )

    # visits — 총량(세션) + 채널 분해 {(버킷, channel): n}
    visits: Counter = Counter()
    visits_by_bucket_channel: Counter = Counter()
    if ATTRIBUTION_AVAILABLE:
        for r in (
            LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end)
            .annotate(d=TruncDate("created_at", tzinfo=tz))
            .values("d", "channel")
            .annotate(c=Count("id"))
        ):
            if r["d"] is None:
                continue
            bucket = _bk(r["d"])
            visits[bucket] += r["c"]
            visits_by_bucket_channel[(bucket, r["channel"])] += r["c"]

    # 유저 채널 귀속 맵 — 관련 유저만 조회 (signups + activated + paid)
    involved = {uid for uid, _b in signup_rows} | set(first_paid_map)
    for users in activated_by_bucket.values():
        involved |= users
    attr_map: dict = {}
    referral_users: set = set()
    if ATTRIBUTION_AVAILABLE and involved:
        attr_map = dict(
            SignupAttribution.objects.filter(user_id__in=involved).values_list("user_id", "channel")
        )
        referral_users = set(
            ReferralRedemption.objects.filter(user_id__in=involved).values_list(
                "user_id", flat=True
            )
        )

    # (버킷, channel) 슬라이스 집계 — Σ by_channel == 총량 보장 (모든 유저가 채널 보유)
    slice_key = defaultdict(lambda: {"visits": 0, "signups": 0, "activated": 0, "paid": 0})
    for (bucket, channel), n in visits_by_bucket_channel.items():
        slice_key[(bucket, channel)]["visits"] += n
    for uid, bucket in signup_rows:
        slice_key[(bucket, _trend_channel_of(uid, attr_map, referral_users))]["signups"] += 1
    for bucket, users in activated_by_bucket.items():
        for uid in users:
            slice_key[(bucket, _trend_channel_of(uid, attr_map, referral_users))]["activated"] += 1
    for uid, bucket in first_paid_map.items():
        slice_key[(bucket, _trend_channel_of(uid, attr_map, referral_users))]["paid"] += 1

    by_channel_by_bucket: dict = defaultdict(dict)
    for (bucket, channel), slice_ in slice_key.items():
        if any(slice_.values()):  # 전부 0인 채널은 생략 (프론트 0 처리)
            by_channel_by_bucket[bucket][channel] = slice_

    # zero-fill: [start 버킷, end 버킷] — end 는 미포함 상한이므로 하루 뺀 날의 버킷까지
    buckets = []
    cur = _bk(timezone.localtime(start).date())
    last = _bk(timezone.localtime(end - timedelta(microseconds=1)).date())
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
                "activated": len(activated_by_bucket.get(cur, ())),
                "by_channel": by_channel_by_bucket.get(cur, {}),
            }
        )
        cur = _bucket_next(cur, granularity)
    return {"granularity": granularity, "buckets": buckets}


def _visit_counts(start, end) -> tuple[int, int]:
    """(visits, unique_visitors) — 어트리뷰션 미탑재 시 (0, 0)."""
    if not ATTRIBUTION_AVAILABLE:
        return 0, 0
    qs = LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end)
    return qs.count(), qs.order_by().values("visitor_id").distinct().count()


# ── KPI ──────────────────────────────────────────────────────────────


def _kpis(cur: tuple, prev: tuple | None, mrr_total: int) -> dict:
    """모든 KPI 를 {current, previous, delta_pct} 로. cur/prev = (start, end).

    prev is None(period=all — 비교할 직전 기간 없음)이면 previous/delta_pct 전부 null.
    """

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

    def _prev(fn):
        """직전 기간 값 — 비교 구간이 없으면 None (→ _delta_metric 이 null 처리)."""
        return None if prev is None else fn(prev)

    visits_cur, uniq_cur = _visit_counts(*cur)
    visits_prev, uniq_prev = (None, None) if prev is None else _visit_counts(*prev)

    mrr = _delta_metric(mrr_total, 0)
    # MRR 은 point-in-time — 과거 시점 재구성 불가 → previous/delta 는 항상 null
    mrr.update({"previous": None, "delta_pct": None, "currency": "KRW"})

    # M-2: 정의 문자열 동봉 — 실결제(PAID)만 카운트, 체험·쿠폰 미결제 제외를 명시
    paid_conversions = _delta_metric(first_paid(cur), _prev(first_paid))
    paid_conversions["definition"] = PAID_CONVERSIONS_DEFINITION

    return {
        "visits": _delta_metric(visits_cur, visits_prev),
        "unique_visitors": _delta_metric(uniq_cur, uniq_prev),
        "signups": _delta_metric(_signups_count(*cur), _prev(lambda w: _signups_count(*w))),
        "ig_connected": _delta_metric(first_ig(cur), _prev(first_ig)),
        "first_page_published": _delta_metric(first_page(cur), _prev(first_page)),
        "first_dm_campaign": _delta_metric(first_campaign(cur), _prev(first_campaign)),
        "paid_conversions": paid_conversions,
        "mrr": mrr,
    }


# ── 퍼널 ─────────────────────────────────────────────────────────────


def _funnel_node(key: str, count: int, numer: int, denom: int, rate_of, formula) -> dict:
    """퍼널 노드 1개 — {key, label, count, rate, rate_of, formula}.

    rate = numer/denom (denom 0 → null). rate_of = 분모 노드 key(또는 null),
    formula = 한국어 정의 문자열 (M-6 — 모든 노드에서 non-null, 프론트 툴팁 정본).
    """
    return {
        "key": key,
        "label": _FUNNEL_NODE_LABELS[key],
        "count": count,
        "rate": _rate(numer, denom),
        "rate_of": rate_of,
        "formula": formula,
    }


def _build_funnel_variant(counts: dict, visitors: int) -> dict:
    """counts(+visitors) → {head, branches, activation, activation_overlap, conversion}.

    counts keys: signups, ig_connected, page_created, page_published, dm_campaign,
    activated(page ∪ campaign), both(page ∩ campaign), paid(실결제),
    trial_only/pro_trial/basic_trial(카드 등록 체험 중 & 미결제), pro_paid/basic_paid,
    trial_no_card(카드 미등록 체험 — 전환에서 제외된 인원).
    - head: 방문자(visit, **고유 방문자** distinct visitor_id — 세션 수는 kpis.visits)
      → 가입(signup, rate=signups/visitors, visitors 0 → null)
    - branch dm: IG 연동(ig/signups) → DM 캠페인(dm/ig)
    - branch biolink: 페이지 생성(created/signups) → 페이지 공개(published/created)
      ※ R-3 이후 프론트 퍼널은 branches 를 숨기고 '자세히 보기' 팝업에서 재사용한다.
        (구조는 유지 — 제거 금지)
    - activation(R-3): 분기 4노드를 대체하는 단일 노드 = activated / signups.
      activation_overlap.both = 페이지 공개 AND DM 캠페인 둘 다인 인원 (중복 제거 구성용).
    - conversion(N-1 + R-4): **유료플랜 전환** = 카드 등록 체험 중 + 실결제
      (count = paid + trial_only), **rate 분모는 activated**(방문→가입→활성화→유료 직렬),
      breakdown = {pro_trial, basic_trial, pro_paid, basic_paid, other} (합 == count).
    """
    signups = counts["signups"]
    ig = counts["ig_connected"]
    page_created = counts["page_created"]
    page = counts["page_published"]
    dm = counts["dm_campaign"]
    activated = counts.get("activated", 0)
    both = counts.get("both", 0)
    paid = counts["paid"]
    trial_only = counts.get("trial_only", 0)
    pro_trial = counts.get("pro_trial", 0)
    basic_trial = counts.get("basic_trial", 0)
    pro_paid = counts.get("pro_paid", 0)
    basic_paid = counts.get("basic_paid", 0)
    plan_total = paid + trial_only
    # other = 해지 후 free 강등 등으로 pro/basic 어디에도 안 잡히는 잔여 (합계 보정용)
    other = plan_total - (pro_trial + basic_trial + pro_paid + basic_paid)

    # M-6: 모든 노드에 한국어 정의(formula)를 채운다 — 프론트 툴팁 정본 (null 금지)
    head = [
        _funnel_node(
            "visit",
            visitors,
            0,
            0,
            None,
            "기간 내 랜딩 고유 방문자 수(distinct visitor_id, 브라우저 단위) — "
            "재방문 세션은 1명으로 집계, 유일하게 기간-이벤트 기준",
        ),
        _funnel_node(
            "signup",
            signups,
            signups,
            visitors,
            "visit",
            "기간 내 가입한 회원 수(가입 코호트) ÷ 고유 방문자 수 × 100",
        ),
    ]
    branches = [
        {
            "key": "dm",
            "label": "DM 자동화",
            "steps": [
                _funnel_node(
                    "ig_connected",
                    ig,
                    ig,
                    signups,
                    "signup",
                    "가입 코호트 중 IG 계정을 연동한 회원 수 ÷ 가입 수 × 100 "
                    "(도달 여부는 현재까지 기준)",
                ),
                _funnel_node(
                    "dm_campaign",
                    dm,
                    dm,
                    ig,
                    "ig_connected",
                    "가입 코호트 중 DM 캠페인을 만든 회원 수 ÷ IG 연동 수 × 100 "
                    "(도달 여부는 현재까지 기준)",
                ),
            ],
        },
        {
            "key": "biolink",
            "label": "바이오링크",
            "steps": [
                _funnel_node(
                    "page_created",
                    page_created,
                    page_created,
                    signups,
                    "signup",
                    "가입 코호트 중 페이지를 만든 회원 수(공개 여부 무관) ÷ 가입 수 × 100 "
                    "(도달 여부는 현재까지 기준)",
                ),
                _funnel_node(
                    "page_published",
                    page,
                    page,
                    page_created,
                    "page_created",
                    "가입 코호트 중 페이지를 공개한 회원 수 ÷ 페이지 생성 수 × 100 "
                    "(도달 여부는 현재까지 기준)",
                ),
            ],
        },
    ]
    activation = _funnel_node(
        "activated",
        activated,
        activated,
        signups,
        "signup",
        "DM 캠페인 1개 이상 생성 또는 페이지 공개 ÷ 이 기간 가입자 (중복 제거) · "
        "도달 여부는 현재까지 기준",
    )
    conversion = _funnel_node(
        "paid",
        plan_total,
        plan_total,
        activated,
        "activated",
        "가입 코호트 중 유료플랜(무료체험+실결제) 진입 ÷ 활성화 유저 × 100 · "
        "무료체험은 **카드 등록 완료** 건만(어드민 수동 부여 제외), "
        "실결제=실제 결제(Toss PAID) 발생",
    )
    conversion["breakdown"] = {
        "pro_trial": pro_trial,
        "basic_trial": basic_trial,
        "pro_paid": pro_paid,
        "basic_paid": basic_paid,
        "other": other,
    }
    # 화면 비노출 — 카드 미등록으로 전환에서 빠진 체험 인원 (검증/로그용)
    conversion["excluded_no_card"] = counts.get("trial_no_card", 0)
    return {
        "head": head,
        "branches": branches,
        "activation": activation,
        "activation_overlap": {"both": both},
        "conversion": conversion,
    }


def _funnel(all_counts: dict, visitors_all: int, channel_variants: list[tuple]) -> dict:
    """가입 코호트 분기 퍼널 — variants.all + 채널별 variant (드롭다운용, 미리 계산).

    - all_counts: _cohort_agg(*cur) (signups/ig_connected/page_published/dm_campaign/paid 사용)
    - visitors_all: 전체 현재 기간 고유 방문자 수 (distinct visitor_id)
    - channel_variants: [(channel_key, counts_dict, visitors), ...] signups desc 정렬됨.
      비어 있으면(어트리뷰션 미탑재) available_channels 는 all 만.
    """
    signups = all_counts["signups"]
    if signups > COHORT_SNAPSHOT_WARN_ROWS:
        logger.warning(
            "[admin-dash-mkt] cohort %s rows > %s — 스냅샷 테이블 전환 검토 필요",
            signups,
            COHORT_SNAPSHOT_WARN_ROWS,
        )

    available_channels = [{"value": "all", "label": "전체 채널"}]
    variants = {"all": _build_funnel_variant(all_counts, visitors_all)}
    for channel, counts, visitors in channel_variants:
        available_channels.append({"value": channel, "label": CHANNEL_LABELS.get(channel, channel)})
        variants[channel] = _build_funnel_variant(counts, visitors)

    return {
        "semantics": "signup_cohort",
        "available_channels": available_channels,
        "variants": variants,
    }


# ── 채널 ─────────────────────────────────────────────────────────────


def _cohort_flags(start, end) -> tuple:
    """가입 코호트 flag_rows + 어트리뷰션 맵 (채널/퍼널 공용, 중복 쿼리 방지).

    반환: (flag_rows, attr_map, attr_utm, referral_users, visits_by_channel)
    - flag_rows: [(id, ig, pgc, pg, cp, pd, cur_plan, cur_status, cur_card), ...]
      (_cohort_qs values_list) pgc=페이지 생성(공개 무관), pg=페이지 공개, pd=실결제,
      cur_* = 현재 구독의 플랜명/상태/빌링키 발급 시각 (체험 판정은 _trial_flags).
    - attr_map: {user_id: channel} (SignupAttribution)
    - attr_utm: {user_id: (utm_campaign, utm_content)} — N-2 캠페인 분해용
    - referral_users: {user_id: 제휴코드 문자열} (ReferralRedemption 보유 — 조회 시점
      오버레이. `uid in referral_users` 멤버십 판정 + Q-4 referral 채널 코드 단위 분해 겸용)
    - visits_by_channel: {channel: 고유 방문자 수(distinct visitor_id)} — 세션 수 아님
    어트리뷰션 미탑재 시 전부 빈 값.
    """
    if not ATTRIBUTION_AVAILABLE:
        return [], {}, {}, {}, {}
    flag_rows = list(
        _cohort_qs(start, end).values_list(
            "id", "ig", "pgc", "pg", "cp", "pd", "cur_plan", "cur_status", "cur_card"
        )
    )
    user_ids = [r[0] for r in flag_rows]
    attr_map: dict = {}
    attr_utm: dict = {}
    for uid, channel, camp, content in SignupAttribution.objects.filter(
        user_id__in=user_ids
    ).values_list("user_id", "channel", "utm_campaign", "utm_content"):
        attr_map[uid] = channel
        attr_utm[uid] = (camp or "", content or "")
    referral_users = dict(
        ReferralRedemption.objects.filter(user_id__in=user_ids).values_list(
            "user_id", "referral_code__code"
        )
    )
    visits_by_channel = {
        r["channel"]: r["v"]
        for r in LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end)
        .values("channel")
        .annotate(v=Count("visitor_id", distinct=True))
    }
    return flag_rows, attr_map, attr_utm, referral_users, visits_by_channel


def _channel_of(uid, attr_map: dict, referral_users) -> str:
    """유저 채널 판정 — ReferralRedemption 보유는 저장 채널과 무관하게 'referral' 오버레이.

    referral_users 는 {user_id: 코드} dict (멤버십 판정만 사용 — set 처럼 동작).
    """
    return "referral" if uid in referral_users else attr_map.get(uid, "unknown")


def _funnel_channel_variants(flags: tuple) -> list[tuple]:
    """채널별 퍼널 counts 집계 → [(channel, counts, visitors), ...] (signups desc).

    counts keys 는 _cohort_agg 와 동일 축 (activated/both/플랜별 분해 포함) —
    _build_funnel_variant 가 all variant 와 같은 구조를 만들 수 있어야 한다 (R-3/R-4).
    signups>0 인 채널만 (드롭다운/available_channels 대상). 어트리뷰션 미탑재 시 빈 리스트.
    """
    flag_rows, attr_map, _attr_utm, referral_users, visits_by_channel = flags
    per_channel: dict = defaultdict(
        lambda: {
            "signups": 0,
            "ig_connected": 0,
            "page_created": 0,
            "page_published": 0,
            "dm_campaign": 0,
            "activated": 0,
            "both": 0,
            "paid": 0,
            "trial_only": 0,
            "pro_trial": 0,
            "basic_trial": 0,
            "pro_paid": 0,
            "basic_paid": 0,
            "trial_no_card": 0,
        }
    )
    for uid, ig, pgc, pg, cp, pd, cur_plan, cur_status, cur_card in flag_rows:
        tr, tr_no_card = _trial_flags(cur_plan, cur_status, cur_card)
        channel = _channel_of(uid, attr_map, referral_users)
        slot = per_channel[channel]
        slot["signups"] += 1
        if ig:
            slot["ig_connected"] += 1
        if pgc:
            slot["page_created"] += 1
        if pg:
            slot["page_published"] += 1
        if cp:
            slot["dm_campaign"] += 1
        if pg or cp:
            slot["activated"] += 1
        if pg and cp:
            slot["both"] += 1
        if pd:
            slot["paid"] += 1
            if cur_plan == "pro":
                slot["pro_paid"] += 1
            elif cur_plan == "basic":
                slot["basic_paid"] += 1
        elif tr:
            slot["trial_only"] += 1
            if cur_plan == "pro":
                slot["pro_trial"] += 1
            elif cur_plan == "basic":
                slot["basic_trial"] += 1
        elif tr_no_card:
            slot["trial_no_card"] += 1

    variants = [
        (channel, counts, visits_by_channel.get(channel, 0))
        for channel, counts in per_channel.items()
        if counts["signups"] > 0
    ]
    variants.sort(key=lambda t: (-t[1]["signups"], t[0]))
    return variants


def _channels(start, end, flags: tuple | None = None) -> dict:
    """채널별 성과 — SignupAttribution 기준, 레퍼럴 오버레이 적용.

    - 어트리뷰션 없는 코호트 가입자는 "unknown" 행 (행 합계 == 코호트 가입자 수).
    - ReferralRedemption 보유 유저는 저장 채널과 무관하게 "referral" (조회 시점 오버레이).
    - referral_codes 는 billing 소스라 어트리뷰션 미탑재여도 항상 채워진다.
    - flags: 뷰에서 미리 계산한 _cohort_flags(start,end) 결과 (funnel 과 중복 쿼리 방지).
    - paid 는 **실결제(첫 PAID 이력)** 만 — 체험 미포함 (N-4). free_trial(현재 체험 중 &
      미결제)은 별도 컬럼 — R-4 이후 **카드 등록 완료 체험만** (퍼널 conversion 과 동일 정의).
    - campaigns(N-2): 채널 행 하위의 (utm_campaign × utm_content) 조합별 분해.
      방문(=고유 방문자, distinct visitor_id)=LandingVisit 저장 utm, 가입측=SignupAttribution
      저장 utm (레퍼럴 오버레이 유저도 자신의 저장 utm 조합으로 referral 채널 아래 분해).
      utm 없는 유입은 ("", "") 한 행. paid(+free_trial) desc → signups desc → visits desc
      정렬 상위 CHANNEL_CAMPAIGNS_LIMIT 개만 — 잘리면 campaigns_truncated=true.
    - referral_overlap(P-3): 원래 이 채널로 저장됐으나 제휴코드 사용(레퍼럴 오버레이)으로
      referral 행으로 이동한 코호트 인원 수 — 오버레이는 배타적(중복 집계 없음)이라
      원 채널이 그만큼 과소 집계되는 것을 보정 표기하기 위한 필드. referral 행은 항상 0.
    """
    if flags is None:
        flags = _cohort_flags(start, end)
    flag_rows, attr_map, attr_utm, referral_users, visits_by_channel = flags

    rows: list[dict] = []
    if ATTRIBUTION_AVAILABLE:
        # 비순차 제품 특성 반영 — 단일 '활성화' 대신 분기 단계별 컬럼:
        # IG 연동 / DM 캠페인 (DM 갈래) · 페이지 생성 / 페이지 공개 (바이오링크 갈래).
        def _empty_slot() -> dict:
            return {
                "signups": 0,
                "ig_connected": 0,
                "dm_campaign": 0,
                "page_created": 0,
                "page_published": 0,
                "paid": 0,
                "free_trial": 0,
            }

        per_channel: dict = defaultdict(_empty_slot)
        # (channel, utm_campaign, utm_content) 조합별 슬롯 — N-2 캠페인 분해
        per_combo: dict = defaultdict(_empty_slot)
        # P-3: 저장 채널 → referral 로 이동한 인원 (원 채널 과소 집계 보정 표기용)
        referral_overlap: dict = defaultdict(int)
        for uid, ig, pgc, pg, cp, pd, cur_plan, cur_status, cur_card in flag_rows:
            tr, _tr_no_card = _trial_flags(cur_plan, cur_status, cur_card)
            channel = _channel_of(uid, attr_map, referral_users)
            if channel == "referral":
                referral_overlap[attr_map.get(uid, "unknown")] += 1
                # Q-4: referral 채널의 의미 있는 세부 축은 원래 방문 utm 이 아니라
                # 사용한 제휴코드 — utm_campaign 자리에 코드 문자열
                # (channels.referral_codes[].code 와 정확히 일치, 프론트 조인 키)
                camp, content = referral_users.get(uid) or "", ""
            else:
                camp, content = attr_utm.get(uid, ("", ""))
            for slot in (per_channel[channel], per_combo[(channel, camp, content)]):
                slot["signups"] += 1
                if ig:
                    slot["ig_connected"] += 1
                if cp:
                    slot["dm_campaign"] += 1
                if pgc:
                    slot["page_created"] += 1
                if pg:
                    slot["page_published"] += 1
                if pd:
                    slot["paid"] += 1
                if tr and not pd:
                    slot["free_trial"] += 1

        # 조합별 고유 방문자 수 — LandingVisit 저장 utm 기준 (채널도 저장 시점 파생값)
        visits_by_combo = {
            (r["channel"], r["utm_campaign"] or "", r["utm_content"] or ""): r["v"]
            for r in LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end)
            .values("channel", "utm_campaign", "utm_content")
            .annotate(v=Count("visitor_id", distinct=True))
        }
        combos_by_channel: dict = defaultdict(set)
        for channel, camp, content in set(per_combo) | set(visits_by_combo):
            combos_by_channel[channel].add((camp, content))

        def _campaign_rows(channel: str) -> tuple[list[dict], bool]:
            items = []
            for camp, content in combos_by_channel.get(channel, ()):
                slot = per_combo.get((channel, camp, content)) or _empty_slot()
                visits = visits_by_combo.get((channel, camp, content), 0)
                items.append(
                    {
                        "utm_campaign": camp,
                        "utm_content": content,
                        "visits": visits,
                        "signups": slot["signups"],
                        "ig_connected": slot["ig_connected"],
                        "dm_campaign": slot["dm_campaign"],
                        "page_created": slot["page_created"],
                        "page_published": slot["page_published"],
                        "paid": slot["paid"],
                        "free_trial": slot["free_trial"],
                        "paid_rate": _rate(slot["paid"], slot["signups"]),
                    }
                )
            items.sort(
                key=lambda c: (
                    -(c["paid"] + c["free_trial"]),
                    -c["signups"],
                    -c["visits"],
                    c["utm_campaign"],
                    c["utm_content"],
                )
            )
            truncated = len(items) > CHANNEL_CAMPAIGNS_LIMIT
            return items[:CHANNEL_CAMPAIGNS_LIMIT], truncated

        # 전원이 referral 로 이동해 잔여 멤버·방문이 없는 채널도 overlap 표기를 위해 행 생성
        for channel in set(per_channel) | set(visits_by_channel) | set(referral_overlap):
            slot = per_channel.get(channel) or _empty_slot()
            visits = visits_by_channel.get(channel, 0)
            campaigns, truncated = _campaign_rows(channel)
            rows.append(
                {
                    "channel": channel,
                    "visits": visits,
                    "signups": slot["signups"],
                    "signup_rate": _rate(slot["signups"], visits),
                    "ig_connected": slot["ig_connected"],
                    "dm_campaign": slot["dm_campaign"],
                    "page_created": slot["page_created"],
                    "page_published": slot["page_published"],
                    "paid": slot["paid"],
                    "free_trial": slot["free_trial"],
                    "paid_rate": _rate(slot["paid"], slot["signups"]),
                    "campaigns": campaigns,
                    "campaigns_truncated": truncated,
                    "referral_overlap": referral_overlap.get(channel, 0),
                }
            )
        rows.sort(key=lambda r: (-r["signups"], -r["visits"], r["channel"]))

    referral_codes = [
        {
            "code": r["referral_code__code"],
            "description": r["referral_code__description"] or "",
            "redemptions": r["redemptions"],
            "converted": r["converted"],
            "conversion_rate": _rate(r["converted"], r["redemptions"]),
        }
        for r in (
            ReferralRedemption.objects.filter(trial_started_at__gte=start, trial_started_at__lt=end)
            .values("referral_code__code", "referral_code__description")
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


def _trials_ended(start, end) -> dict:
    """P-1: '이 기간에 무료체험이 끝난' 회원 수 + 그중 실결제 유지 수 (유저 dedupe).

    종료 시점 판정:
    - 쿠폰(레퍼럴): ReferralRedemption 이 내구 기록.
      · 전환자 → min(trial_ends_at, converted_at) — 체험 중 결제하면 그 시점에 종료
      · 미전환 → trial_ends_at — 중도 해지(취소 예약)여도 체험은 예정일까지 유효하므로
        예정일에 '결제 없이 종료'로 카운트
    - 카드등록: 내구 종료 기록이 없어 재구성 (trial_used_at 보유 유저 한정).
      · 전환자 → 첫 PAID paid_at (갱신 배치가 체험 종료 시점에 첫 과금)
      · 미전환(PAID 이력 전무) → cancelled_at — 만료 다운그레이드 시각(≈종료 +1h 이내,
        _downgrade_to_free 가 current_period_end 를 지우므로 이것이 유일한 흔적)
    - 진행 중(TRIALING)·취소 예약 상태로 아직 예정일 전인 체험은 미종료 → 분모 제외.
    - 전환 판정은 조회 시점 기준(converted_to_paid / PAID 이력) — 퍼널과 동일 의미론.
    """
    ended_conv: set = set()
    ended_nonconv: set = set()

    # ① 쿠폰 체험 — 종료 시점이 기간과 겹칠 수 있는 행만 당겨 파이썬 판정
    coupon_rows = ReferralRedemption.objects.filter(
        Q(trial_ends_at__gte=start, trial_ends_at__lt=end)
        | Q(converted_at__gte=start, converted_at__lt=end)
    ).values_list("user_id", "trial_ends_at", "converted_to_paid", "converted_at")
    for uid, ends, conv, conv_at in coupon_rows:
        moment = min(ends, conv_at) if (conv and conv_at) else ends
        if start <= moment < end:
            (ended_conv if conv else ended_nonconv).add(uid)

    # ② 카드 체험 — trial_used_at 보유 유저 한정 재구성
    card_users = dict(
        UserSubscription.objects.filter(trial_used_at__isnull=False).values_list(
            "user_id", "cancelled_at"
        )
    )
    if card_users:
        first_paid = {
            r["user_id"]: r["first"]
            for r in PaymentHistory.objects.filter(
                user_id__in=card_users, status=PaymentStatus.PAID
            )
            .values("user_id")
            .annotate(first=Min("paid_at"))
        }
        for uid, cancelled_at in card_users.items():
            paid_at = first_paid.get(uid)
            if paid_at is not None:
                if start <= paid_at < end:
                    ended_conv.add(uid)
            elif cancelled_at is not None and start <= cancelled_at < end:
                ended_nonconv.add(uid)

    ended_nonconv -= ended_conv  # 유저 dedupe — 전환이 우선
    ended = len(ended_conv | ended_nonconv)
    return {
        "ended": ended,
        "ended_converted": len(ended_conv),
        "ended_conversion_rate": _rate(len(ended_conv), ended),
        "ended_conversion_formula": TRIALS_ENDED_CONVERSION_FORMULA,
    }


def _feature_stats(cur: tuple, prev: tuple | None) -> dict:
    """기능별 사용 통계. prev is None(period=all)이면 delta 계열 previous 전부 null."""
    start, end = cur

    def _prev(fn):
        return None if prev is None else fn(prev)

    # biolink — new_public_pages 는 created_at 근사 (공개 시각 미기록)
    def new_public_pages(w):
        return Page.objects.filter(
            is_public=True, created_at__gte=w[0], created_at__lt=w[1]
        ).count()

    # M-1: 생성 방식 분해 — 모집단은 new_public_pages 와 동일(기간 내 created_at 공개 페이지)
    # → ai + imported + manual == new_public_pages.current 항상 성립.
    # 우선순위 imported > ai > manual (임포트 후 AI 리메이크한 페이지는 imported 로 1회만).
    def created_breakdown(w):
        has_ai_job = Exists(
            AiJob.objects.filter(
                page=OuterRef("pk"),
                status=AiJob.Status.SUCCEEDED,
                job_type__in=AI_CREATED_JOB_TYPES,
            )
        )
        agg = (
            Page.objects.filter(is_public=True, created_at__gte=w[0], created_at__lt=w[1])
            .annotate(ai_ok=has_ai_job)
            .aggregate(
                total=Count("id"),
                imported=Count("id", filter=~Q(import_source="")),
                ai=Count("id", filter=Q(import_source="") & Q(ai_ok=True)),
            )
        )
        return {
            "ai": agg["ai"],
            "imported": agg["imported"],
            "manual": agg["total"] - agg["imported"] - agg["ai"],
        }

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

    dm_cur = dm_agg(cur)
    dm_prev = None if prev is None else dm_agg(prev)

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

    spam_cur = spam_counts(cur)
    spam_prev = None if prev is None else spam_counts(prev)

    # trials — started = 레퍼럴 트라이얼 + 카드등록 트라이얼(trial_used_at).
    # converted/conversion_rate 는 '레퍼럴 코호트'만 대상 (카드 트라이얼 전환은 전용
    # 플래그가 없어 추적 불가 — 문서화된 한계).
    def referral_started(w):
        return ReferralRedemption.objects.filter(
            trial_started_at__gte=w[0], trial_started_at__lt=w[1]
        )

    def card_trial_qs(w):
        return UserSubscription.objects.filter(trial_used_at__gte=w[0], trial_used_at__lt=w[1])

    ref_cur_count = referral_started(cur).count()
    started_cur = ref_cur_count + card_trial_qs(cur).count()
    started_prev = _prev(lambda w: referral_started(w).count() + card_trial_qs(w).count())
    converted = referral_started(cur).filter(converted_to_paid=True).count()

    # N-3: 현재 체험 진행 중 수 — 조회 시점 TRIALING 유료플랜 구독 (상태 전이는
    # billing.handle_trial_expiry 가 유지하므로 status 가 운영상 진실).
    trials_active = (
        UserSubscription.objects.filter(status=SubscriptionStatus.TRIALING)
        .exclude(plan__name__in=("free", "admin"))
        .count()
    )
    # N-3: 유료전환율(실결제 기준) — 기간 내 체험 시작자 전체(레퍼럴+카드, 유저 dedupe)
    # 중 현재까지 PAID 결제가 발생한 회원 비율. started(이벤트 합산)와 달리 분모를
    # 회원 단위로 dedupe 한다.
    starter_uids = set(referral_started(cur).values_list("user_id", flat=True)) | set(
        card_trial_qs(cur).values_list("user_id", flat=True)
    )
    paid_starters = 0
    if starter_uids:
        paid_starters = (
            PaymentHistory.objects.filter(user_id__in=starter_uids, status=PaymentStatus.PAID)
            .order_by()
            .values("user_id")
            .distinct()
            .count()
        )

    # ── 기능별 '사용자 수' (마케팅 관점: 발송량보다 활성 사용자 수가 중요) ──
    # 각각 기간 내 해당 기능을 실제로 쓴 고유 회원 수. distinct+values 는 Meta.ordering
    # 누수 방지를 위해 반드시 .order_by() 로 정렬 제거.
    def page_users(w):
        return (
            Page.objects.filter(is_public=True, created_at__gte=w[0], created_at__lt=w[1])
            .order_by()
            .values("user_id")
            .distinct()
            .count()
        )

    def dm_campaign_users(w):
        return (
            AutoDMCampaign.objects.filter(created_at__gte=w[0], created_at__lt=w[1])
            .order_by()
            .values("ig_connection__workspace__owner_id")
            .distinct()
            .count()
        )

    def spam_users(w):
        return (
            SpamCommentLog.objects.filter(created_at__gte=w[0], created_at__lt=w[1])
            .order_by()
            .values("spam_filter__ig_connection__workspace__owner_id")
            .distinct()
            .count()
        )

    return {
        "biolink": {
            "public_pages_total": Page.objects.filter(is_public=True).count(),
            "new_public_pages": _delta_metric(new_public_pages(cur), _prev(new_public_pages)),
            "created_breakdown": created_breakdown(cur),
            "active_users": _delta_metric(page_users(cur), _prev(page_users)),
            "views": _delta_metric(views_cur, _prev(page_views)),
            "clicks": _delta_metric(clicks_cur, _prev(block_clicks)),
            "ctr": round(clicks_cur / views_cur, 4) if views_cur else 0.0,
            "top_pages": top_pages,
        },
        "dm": {
            "campaigns_created": _delta_metric(campaigns_created(cur), _prev(campaigns_created)),
            "active_users": _delta_metric(dm_campaign_users(cur), _prev(dm_campaign_users)),
            "requested": _delta_metric(
                dm_cur["requested"], dm_prev["requested"] if dm_prev else None
            ),
            "delivered": _delta_metric(
                dm_cur["delivered"] + dm_cur["read"],
                (dm_prev["delivered"] + dm_prev["read"]) if dm_prev else None,
            ),
            "delivery_rate": _delivery_rate(dm_cur),
        },
        "spam": {
            "active_users": _delta_metric(spam_users(cur), _prev(spam_users)),
            "detected": _delta_metric(
                spam_cur["detected"], spam_prev["detected"] if spam_prev else None
            ),
            "hidden": _delta_metric(spam_cur["hidden"], spam_prev["hidden"] if spam_prev else None),
        },
        "trials": {
            "started": _delta_metric(started_cur, started_prev),
            "active": trials_active,
            "converted": converted,
            "conversion_rate": _rate(converted, ref_cur_count),
            "conversion_formula": TRIALS_CONVERSION_FORMULA,
            "paid_conversion_rate": _rate(paid_starters, len(starter_uids)),
            "paid_conversion_formula": TRIALS_PAID_CONVERSION_FORMULA,
            # P-1: 종료 시점 기준 대표 지표 (ended/ended_converted/rate/formula)
            **_trials_ended(*cur),
        },
    }


# ── 플랜 분포 / MRR ──────────────────────────────────────────────────


def _plan_distribution() -> list[dict]:
    """전 플랜(비활성 포함), sort_order 순 — /metrics/overview/ 의 by_plan 패턴 재사용.

    admin 플랜은 운영용 내부 계정이라 마케팅과 무관 → 제외 (MRR 과 동일 정책).
    """
    per_plan_status: dict = defaultdict(dict)
    for row in UserSubscription.objects.values("plan_id", "status").annotate(c=Count("id")):
        per_plan_status[row["plan_id"]][row["status"]] = row["c"]

    rows = []
    for plan in SubscriptionPlan.objects.exclude(name="admin").order_by("sort_order", "name"):
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


# ── 온보딩 이탈자 ─────────────────────────────────────────────────────


def _sample_users(qs, limit: int) -> list[dict]:
    """가입 코호트 샘플 회원 → [{user_id, email, joined_at, link}] (최근 가입 순)."""
    return [
        {
            "user_id": u["id"],
            "email": u["email"] or "",
            "joined_at": timezone.localtime(u["date_joined"]).isoformat(),
            "link": {"page": f"/users/{u['id']}", "params": {}},
        }
        for u in qs.order_by("-date_joined").values("id", "email", "date_joined")[:limit]
    ]


def _onboarding_dropoffs(start, end) -> dict:
    """가입 코호트의 단계별 이탈 세그먼트 (마케팅/CS 액션 리스트).

    측정 가능한 4개 세그먼트를 count + 최근 샘플 회원(ONBOARDING_SAMPLE_LIMIT)으로 반환.
    '유료 기능 시도 후 미결제'는 프론트 CheckoutEvent 텔레메트리 의존 →
    paywall_no_payment 세그먼트로 별도 처리(미탑재 시 available=false 강등).
    """
    has_ig = Exists(IGAccountConnection.objects.filter(workspace__owner=OuterRef("pk")))
    has_any_page = Exists(Page.objects.filter(user=OuterRef("pk")))
    has_pub = Exists(Page.objects.filter(user=OuterRef("pk"), is_public=True))
    has_camp = Exists(AutoDMCampaign.objects.filter(ig_connection__workspace__owner=OuterRef("pk")))
    has_sent = Exists(
        SentDMLog.objects.filter(campaign__ig_connection__workspace__owner=OuterRef("pk"))
    )
    cohort = User.objects.filter(date_joined__gte=start, date_joined__lt=end).annotate(
        ig=has_ig, pgc=has_any_page, pg=has_pub, cp=has_camp, sent=has_sent
    )

    seg_filters = {
        "no_action": Q(ig=False, pgc=False, cp=False),
        "ig_no_campaign": Q(ig=True, cp=False),
        "page_created_not_published": Q(pgc=True, pg=False),
        "campaign_no_send": Q(cp=True, sent=False),
    }

    segments = []
    for key, label, desc in _ONBOARDING_SEGMENTS:
        seg_qs = cohort.filter(seg_filters[key])
        segments.append(
            {
                "key": key,
                "label": label,
                "description": desc,
                "count": seg_qs.count(),
                "available": True,
                "samples": _sample_users(seg_qs, ONBOARDING_SAMPLE_LIMIT),
            }
        )

    # 유료 기능 시도 후 미결제 — 프론트 CheckoutEvent 의존 (미탑재 시 강등)
    paywall_seg = {
        "key": "paywall_no_payment",
        "label": "유료 기능 시도 후 미결제",
        "description": "유료 제한 모달을 봤으나(paywall_viewed) 결제하지 않음",
        "count": 0,
        "available": CHECKOUT_EVENT_AVAILABLE,
        "samples": [],
    }
    if CHECKOUT_EVENT_AVAILABLE:
        paywalled = set(
            CheckoutEvent.objects.filter(
                created_at__gte=start,
                created_at__lt=end,
                event="paywall_viewed",
                user__date_joined__gte=start,
                user__date_joined__lt=end,
            )
            .order_by()
            .values_list("user_id", flat=True)
            .distinct()
        )
        paywalled.discard(None)
        if paywalled:
            paid_ids = set(
                PaymentHistory.objects.filter(user_id__in=paywalled, status=PaymentStatus.PAID)
                .order_by()
                .values_list("user_id", flat=True)
                .distinct()
            )
            no_pay = paywalled - paid_ids
            paywall_seg["count"] = len(no_pay)
            paywall_seg["samples"] = _sample_users(
                User.objects.filter(id__in=no_pay), ONBOARDING_SAMPLE_LIMIT
            )
    segments.append(paywall_seg)

    return {"cohort_signups": cohort.count(), "segments": segments}


# ── 유료 전환 분석 (플랜 / 진입 경로 / 결제 후 사용) ───────────────────


def _post_payment_usage(first_paid: dict) -> list[dict]:
    """결제 후 POST_PAYMENT_WINDOW_DAYS 내 기능별 '사용한 유저 수'.

    기능별 1쿼리(전환자 대상, 최소 paid_at 이후)로 이벤트를 당겨와 유저별 창 안인지
    파이썬에서 판정 (전환자는 소수라 바운드됨).
    """
    if not first_paid:
        return [{"key": k, "label": lbl, "users": 0} for k, lbl in _POST_PAYMENT_FEATURES]

    features: dict = {k: set() for k, _ in _POST_PAYMENT_FEATURES}
    window = timedelta(days=POST_PAYMENT_WINDOW_DAYS)
    min_paid = min(first_paid.values())
    user_ids = list(first_paid)

    def _bucket(rows, feature_key):
        for owner_id, ts in rows:
            paid_at = first_paid.get(owner_id)
            if paid_at and paid_at <= ts < paid_at + window:
                features[feature_key].add(owner_id)

    _bucket(
        SentDMLog.objects.filter(
            campaign__ig_connection__workspace__owner_id__in=user_ids,
            created_at__gte=min_paid,
        ).values_list("campaign__ig_connection__workspace__owner_id", "created_at"),
        "dm_send",
    )
    _bucket(
        Page.objects.filter(user_id__in=user_ids, created_at__gte=min_paid).values_list(
            "user_id", "created_at"
        ),
        "page_created",
    )
    _bucket(
        AiJob.objects.filter(
            user_id__in=user_ids,
            job_type__in=AI_PAGE_JOB_TYPES,
            status=AiJob.Status.SUCCEEDED,
            created_at__gte=min_paid,
        ).values_list("user_id", "created_at"),
        "ai_page",
    )
    _bucket(
        SpamCommentLog.objects.filter(
            spam_filter__ig_connection__workspace__owner_id__in=user_ids,
            status__in=_SPAM_BLOCKED_STATUSES,
            created_at__gte=min_paid,
        ).values_list("spam_filter__ig_connection__workspace__owner_id", "created_at"),
        "spam_used",
    )
    _bucket(
        IGAccountConnection.objects.filter(
            workspace__owner_id__in=user_ids, created_at__gte=min_paid
        ).values_list("workspace__owner_id", "created_at"),
        "extra_ig",
    )
    return [
        {"key": k, "label": lbl, "users": len(features[k])} for k, lbl in _POST_PAYMENT_FEATURES
    ]


def _entry_paths(first_paid: dict) -> tuple[list[dict], bool]:
    """유저별 첫 PAID 이전 CHECKOUT_ATTRIBUTION_WINDOW_DAYS 내 '마지막 트리거'를 진입 경로로 귀속.

    반환: (rows, available). CheckoutEvent 미탑재 → ([], False).
    이벤트는 있으나 귀속 0건 → ([], True). rows = [{key, label, count}] (count desc).
    """
    if not CHECKOUT_EVENT_AVAILABLE:
        return [], False
    if not first_paid:
        return [], True

    window = timedelta(days=CHECKOUT_ATTRIBUTION_WINDOW_DAYS)
    user_ids = list(first_paid)
    min_paid = min(first_paid.values())
    latest: dict = {}  # user_id → (ts, trigger_feature)
    rows = CheckoutEvent.objects.filter(
        user_id__in=user_ids,
        event__in=("paywall_viewed", "checkout_started"),
        created_at__gte=min_paid - window,
    ).values_list("user_id", "created_at", "trigger_feature")
    for uid, ts, trig in rows:
        paid_at = first_paid.get(uid)
        if not paid_at or not (paid_at - window <= ts <= paid_at):
            continue
        best = latest.get(uid)
        if best is None or ts > best[0]:
            latest[uid] = (ts, trig or "")

    counter = Counter(trig for _ts, trig in latest.values())
    entry_paths = [
        {"key": trig, "label": TRIGGER_FEATURE_LABELS.get(trig, trig or "미지정"), "count": c}
        for trig, c in counter.most_common()
    ]
    return entry_paths, True


def _paid_plan_no_payment(start, end) -> dict:
    """M-2 동반 지표 — 기간 내 체험·쿠폰으로 유료 플랜을 시작했으나 미결제인 회원 수.

    진입 경로 2종의 합집합(유저 dedupe):
    - 쿠폰(레퍼럴): ReferralRedemption.trial_started_at ∈ 기간
    - 체험(카드등록): UserSubscription.trial_used_at ∈ 기간
    '미결제' = 조회 시점까지 PAID PaymentHistory 가 전혀 없음 (체험 중 전환자는 자동 제외).
    referral_trial/card_trial 은 경로별 분해 — 한 회원이 두 경로 모두 탄 경우(드묾)
    양쪽에 잡혀 합이 count 를 넘을 수 있다.
    """
    referral_uids = set(
        ReferralRedemption.objects.filter(
            trial_started_at__gte=start, trial_started_at__lt=end
        ).values_list("user_id", flat=True)
    )
    card_uids = set(
        UserSubscription.objects.filter(
            trial_used_at__gte=start, trial_used_at__lt=end
        ).values_list("user_id", flat=True)
    )
    trial_uids = referral_uids | card_uids
    paid_ever: set = set()
    if trial_uids:
        paid_ever = set(
            PaymentHistory.objects.filter(user_id__in=trial_uids, status=PaymentStatus.PAID)
            .order_by()
            .values_list("user_id", flat=True)
            .distinct()
        )
    return {
        "count": len(trial_uids - paid_ever),
        "referral_trial": len(referral_uids - paid_ever),
        "card_trial": len(card_uids - paid_ever),
        "definition": PAID_PLAN_NO_PAYMENT_DEFINITION,
    }


def _paid_conversion_analysis(cur) -> dict:
    """유료 전환을 3축으로 분해 — 선택 플랜 / 결제 진입 경로 / 결제 후 사용.

    '무엇 때문에 결제했나'를 단정하지 않고, (1) 선택 플랜 (2) 결제 진입 경로
    (프론트 CheckoutEvent 의존 — 미탑재 시 강등) (3) 결제 후
    POST_PAYMENT_WINDOW_DAYS 내 실제 사용 기능(사용자 수)을 분리해 제공한다.
    admin 플랜은 운영용 내부 계정이라 by_plan 에서 제외.
    M-2: paid_plan_no_payment (체험·쿠폰 유료플랜 미결제 회원 수) 동반 제공 —
    어드민이 "실결제 N명 / 무료 유료플랜 M명"을 함께 본다.
    """
    start, end = cur
    first_paid = {
        row["user_id"]: row["first"]
        for row in PaymentHistory.objects.filter(status=PaymentStatus.PAID)
        .values("user_id")
        .annotate(first=Min("paid_at"))
        .filter(first__gte=start, first__lt=end)
    }
    user_ids = list(first_paid)

    by_plan = []
    if user_ids:
        for row in (
            UserSubscription.objects.filter(user_id__in=user_ids)
            .exclude(plan__name__in=("free", "admin"))
            .values("plan__name", "plan__display_name")
            .annotate(c=Count("id"))
            .order_by("-c", "plan__name")
        ):
            by_plan.append(
                {
                    "name": row["plan__name"],
                    "display_name": row["plan__display_name"],
                    "count": row["c"],
                }
            )

    entry_paths, entry_available = _entry_paths(first_paid)
    return {
        "total": len(first_paid),
        "by_plan": by_plan,
        "paid_plan_no_payment": _paid_plan_no_payment(start, end),
        "post_payment_usage": _post_payment_usage(first_paid),
        "entry_paths": entry_paths,
        "entry_paths_available": entry_available,
        "post_payment_window_days": POST_PAYMENT_WINDOW_DAYS,
    }


# ── 구독 유지·해지 분석 (왜 계속 남고, 왜 떠나는가) ──────────────────────

_PAID_EXCLUDE = ("free", "admin")  # 유료 구독 판정 시 제외 (free=무료, admin=운영용)


def _billed_amount_sum(subs) -> int:
    """구독 QS 의 월 청구액 합 (원) — Coalesce(snapshot, plan.monthly_price) + pro 추가 IG."""
    agg = subs.aggregate(
        base=Sum(Coalesce("monthly_amount_snapshot", "plan__monthly_price")),
        extra=Sum("extra_ig_accounts", filter=Q(plan__name="pro")),
    )
    return (agg["base"] or 0) + (agg["extra"] or 0) * EXTRA_IG_ACCOUNT_PRICE


def _cancel_reasons(start, end) -> tuple[list[dict], bool]:
    """해지 사유 TOP N (CancellationEvent.reason). 미탑재 시 ([], False)."""
    if not CANCELLATION_EVENT_AVAILABLE:
        return [], False
    rows = (
        CancellationEvent.objects.filter(
            created_at__gte=start, created_at__lt=end, event="cancel_reason_submitted"
        )
        .values("reason")
        .annotate(c=Count("id"))
        .order_by("-c")[:CANCEL_REASONS_TOP]
    )
    reasons = [
        {
            "key": r["reason"],
            "label": CANCEL_REASON_LABELS.get(r["reason"], r["reason"] or "미지정"),
            "count": r["c"],
        }
        for r in rows
    ]
    return reasons, True


def _cancel_defense(start, end) -> dict | None:
    """취소 방어 — 취소 버튼 클릭 대비 유지(중단/철회) 선택. 이벤트 미탑재/0 시 None."""
    if not CANCELLATION_EVENT_AVAILABLE:
        return None
    qs = CancellationEvent.objects.filter(created_at__gte=start, created_at__lt=end)
    tries = qs.filter(event="cancel_button_clicked").order_by().values("user_id").distinct().count()
    if tries == 0:
        return None
    retained = (
        qs.filter(event__in=("subscription_cancel_aborted", "subscription_resumed"))
        .order_by()
        .values("user_id")
        .distinct()
        .count()
    )
    return {"tries": tries, "retained": retained, "defense_rate": _rate(retained, tries)}


def _recent_cancellations(now) -> list[dict]:
    """최근 취소 예약(해지 위험) 고객 — CANCELLED + 유료 + 기간 남음. CS 액션용.

    아직 되살릴 수 있는 고객이라 최근 사용(7일 DM/30일 클릭)과 사유를 함께 제공한다.
    """
    subs = list(
        UserSubscription.objects.filter(
            status=SubscriptionStatus.CANCELLED, current_period_end__gt=now
        )
        .exclude(plan__name__in=_PAID_EXCLUDE)
        .select_related("user", "plan")
        .order_by("-cancelled_at")[:RECENT_CANCELLATIONS_LIMIT]
    )
    if not subs:
        return []
    owner_ids = [s.user_id for s in subs]
    since_7d, since_30d = now - timedelta(days=7), now - timedelta(days=30)
    dm_7d = {
        r["campaign__ig_connection__workspace__owner_id"]: r["c"]
        for r in SentDMLog.objects.filter(
            campaign__ig_connection__workspace__owner_id__in=owner_ids, created_at__gte=since_7d
        )
        .values("campaign__ig_connection__workspace__owner_id")
        .annotate(c=Count("id"))
    }
    clicks_30d = {
        r["page__user_id"]: r["c"]
        for r in BlockClick.objects.filter(page__user_id__in=owner_ids, clicked_at__gte=since_30d)
        .values("page__user_id")
        .annotate(c=Count("id"))
    }
    reason_by_user: dict = {}
    if CANCELLATION_EVENT_AVAILABLE:
        for ev in CancellationEvent.objects.filter(
            user_id__in=owner_ids, event="cancel_reason_submitted"
        ).order_by("user_id", "-created_at"):
            reason_by_user.setdefault(ev.user_id, ev.reason)

    result = []
    for s in subs:
        amount = s.monthly_amount_snapshot or (s.plan.monthly_price or 0)
        if s.plan.name == "pro":
            amount += (s.extra_ig_accounts or 0) * EXTRA_IG_ACCOUNT_PRICE
        days_remaining = max(0, (s.current_period_end - now).days) if s.current_period_end else None
        reason = reason_by_user.get(s.user_id, "")
        result.append(
            {
                "user_id": s.user_id,
                "email": s.user.email or "",
                "plan": s.plan.display_name,
                "monthly_amount": amount,
                "days_remaining": days_remaining,
                "cancelled_at": (
                    timezone.localtime(s.cancelled_at).isoformat() if s.cancelled_at else None
                ),
                "reason": reason,
                "reason_label": CANCEL_REASON_LABELS.get(reason, reason) if reason else "",
                "recent_dm_7d": dm_7d.get(s.user_id, 0),
                "recent_clicks_30d": clicks_30d.get(s.user_id, 0),
                "link": {"page": f"/users/{s.user_id}", "params": {}},
            }
        )
    return result


def _subscription_retention(cur) -> dict:
    """구독 유지·해지 분석 — '왜 계속 남고, 왜 떠나는가'.

    ⚠ 스냅샷 테이블 부재로 시점 재구성 불가 → 유지/해지율은 **근사**(basis=approx_no_snapshot):
    - denom = 기간 시작 전 첫 결제(첫 PAID paid_at < start) 유료 고객
    - numer = 그중 현재도 유료 ACTIVE(free/admin 아님) 유지 → 코호트 대비 '현재 생존' 비율
    현재-상태 카운트(취소 예약/past_due/at-risk MRR)는 정확. 실현 해지는 실제 결제 이력이 있는
    회원만 카운트(트라이얼 만료 다운그레이드 오집계 방지) — 단 다운그레이드 시 금액이 소거되어
    '실현 해지 MRR'은 복구 불가 → at_risk_mrr(취소 예약+past_due 월 금액)로 대체 제시.
    """
    start, now = cur

    paying_before = set(
        PaymentHistory.objects.filter(status=PaymentStatus.PAID)
        .values("user_id")
        .annotate(first=Min("paid_at"))
        .filter(first__lt=start)
        .values_list("user_id", flat=True)
    )
    retained = 0
    if paying_before:
        retained = (
            UserSubscription.objects.filter(
                user_id__in=paying_before, status=SubscriptionStatus.ACTIVE
            )
            .exclude(plan__name__in=_PAID_EXCLUDE)
            .count()
        )
    denom = len(paying_before)
    retention_rate = _rate(retained, denom)
    churn_rate = round(1 - retention_rate, 4) if retention_rate is not None else None

    paid_active = UserSubscription.objects.filter(status=SubscriptionStatus.ACTIVE).exclude(
        plan__name__in=_PAID_EXCLUDE
    )
    cancel_scheduled_qs = UserSubscription.objects.filter(
        status=SubscriptionStatus.CANCELLED, current_period_end__gt=now
    ).exclude(plan__name__in=_PAID_EXCLUDE)
    past_due_qs = UserSubscription.objects.filter(status=SubscriptionStatus.PAST_DUE).exclude(
        plan__name__in=_PAID_EXCLUDE
    )

    at_risk_mrr = _billed_amount_sum(cancel_scheduled_qs) + _billed_amount_sum(past_due_qs)

    # 실현 해지: 기간 내 free 로 다운그레이드된 것 중 '실제 결제 이력 보유'만
    # (트라이얼 만료 다운그레이드도 cancelled_at 이 찍히므로 결제 이력으로 필터).
    churned_candidates = set(
        UserSubscription.objects.filter(
            plan__name="free", cancelled_at__gte=start, cancelled_at__lt=now
        ).values_list("user_id", flat=True)
    )
    realized_churn = 0
    if churned_candidates:
        ever_paid = set(
            PaymentHistory.objects.filter(user_id__in=churned_candidates, status=PaymentStatus.PAID)
            .values_list("user_id", flat=True)
            .distinct()
        )
        realized_churn = len(churned_candidates & ever_paid)

    new_paid_users = set(
        PaymentHistory.objects.filter(status=PaymentStatus.PAID)
        .values("user_id")
        .annotate(first=Min("paid_at"))
        .filter(first__gte=start, first__lt=now)
        .values_list("user_id", flat=True)
    )
    new_mrr = (
        _billed_amount_sum(paid_active.filter(user_id__in=new_paid_users)) if new_paid_users else 0
    )
    current_mrr = _billed_amount_sum(paid_active)

    cancel_reasons, reasons_available = _cancel_reasons(start, now)
    # P-4: 일별 스냅샷(billing.snapshot_daily_metrics) 적재 시작일 — 이 날짜 이후 기간은
    # 향후 정확 계산(basis="snapshot") 전환 대상. 아직 미적재면 null.
    first_snapshot = (
        DailySubscriptionSnapshot.objects.order_by("snapshot_date")
        .values_list("snapshot_date", flat=True)
        .first()
    )
    return {
        "basis": "approx_no_snapshot",
        "snapshot_since": first_snapshot.isoformat() if first_snapshot else None,
        "window_days": max(1, (now - start).days),
        "retention_rate": retention_rate,
        "churn_rate": churn_rate,
        "paying_now": paid_active.count(),
        "cancel_scheduled": cancel_scheduled_qs.count(),
        "payment_failed": past_due_qs.count(),
        "realized_churn": realized_churn,
        "at_risk_mrr": at_risk_mrr,
        "mrr_movement": {
            "new_mrr": new_mrr,
            "at_risk_mrr": at_risk_mrr,
            "current_mrr": current_mrr,
            "note": "업/다운그레이드·실현 해지 MRR 은 스냅샷 테이블 도입 후 정확 산출 "
            "(다운그레이드 시 금액 스냅샷이 소거됨)",
        },
        "cancel_reasons": cancel_reasons,
        "cancel_reasons_available": reasons_available,
        "cancel_defense": _cancel_defense(start, now),
        "recent_cancellations": _recent_cancellations(now),
    }


# ── 코호트 분석 (Q-2) — 기간 필터와 무관한 고정 창 ─────────────────────


def _subscription_cohorts(now) -> dict:
    """구독 유지 코호트 (첫 PAID 월 × M+1..M+5 유지율).

    values[i] = 코호트월 +(i+1)개월 시점(월초일)에도 유료 ACTIVE(free/admin 제외)인 비율.
    - 시점별 소스: DailyPaidCohortSnapshot 에 그 날짜(+3일 관용) 스냅샷이 있으면 사용,
      없으면 **현재 상태 역산 근사** — 현재 유료 유지 중이거나 cancelled_at(다운그레이드
      시각)이 시점 이후면 그 시점엔 유지로 간주. 전 값이 스냅샷이면 basis="snapshot",
      하나라도 근사면 "approx" (프론트 "근사" 배지 기준).
    - 아직 도래하지 않은 시점은 생략 (배열이 짧아짐). 일시정지(paused)는 스냅샷 정의
      (ACTIVE only)와 일관되게 유지로 세지 않는다.
    """
    today = timezone.localdate(now)
    this_month = today.replace(day=1)
    months = [_month_add(this_month, -i) for i in range(COHORT_SUBSCRIPTION_MONTHS - 1, -1, -1)]
    span_start = _local_midnight(months[0])

    cohort_users: dict = defaultdict(set)
    for row in (
        PaymentHistory.objects.filter(status=PaymentStatus.PAID)
        .values("user_id")
        .annotate(first=Min("paid_at"))
        .filter(first__gte=span_start)
    ):
        cohort_users[timezone.localtime(row["first"]).date().replace(day=1)].add(row["user_id"])

    all_uids = set().union(*cohort_users.values()) if cohort_users else set()
    currently_paying: set = set()
    cancelled_at_map: dict = {}
    if all_uids:
        for uid, status_, plan_name, cancelled_at in UserSubscription.objects.filter(
            user_id__in=all_uids
        ).values_list("user_id", "status", "plan__name", "cancelled_at"):
            if status_ == SubscriptionStatus.ACTIVE and plan_name not in ("free", "admin"):
                currently_paying.add(uid)
            cancelled_at_map[uid] = cancelled_at

    rows = []
    used_approx = False
    emitted_any = False
    for m in months:
        users = cohort_users.get(m, set())
        size = len(users)
        values: list = []
        for i in range(COHORT_MAX_PERIODS):
            checkpoint = _month_add(m, i + 1)
            if checkpoint > today or size == 0:
                break
            snap = (
                DailyPaidCohortSnapshot.objects.filter(
                    cohort_month=m,
                    snapshot_date__gte=checkpoint,
                    snapshot_date__lte=checkpoint + timedelta(days=3),
                )
                .order_by("snapshot_date")
                .first()
            )
            if snap is not None:
                values.append(round(min(snap.paying_users, size) / size, 4))
            else:
                used_approx = True
                checkpoint_dt = _local_midnight(checkpoint)
                retained = sum(
                    1
                    for uid in users
                    if uid in currently_paying
                    or (cancelled_at_map.get(uid) and cancelled_at_map[uid] > checkpoint_dt)
                )
                values.append(round(retained / size, 4))
            emitted_any = True
        rows.append({"cohort": m.strftime("%Y-%m"), "size": size, "values": values})

    basis = "snapshot" if emitted_any and not used_approx else "approx"
    return {
        "unit": "month",
        "max_periods": COHORT_MAX_PERIODS,
        "basis": basis,
        "rows": rows,
    }


def _usage_cohorts(now) -> dict:
    """제품 사용 코호트 (가입 주 × W+1..W+5 사용률) — 이벤트 로그 소급이라 항상 정확.

    코호트 = 가입 주(월요일 시작, 로컬). '사용' = 그 주에 DM 캠페인 생성 · DM 발송 발생 ·
    페이지 생성/공개(공개 시각 미기록이라 created_at) 중 1개 이상. values 는 **완결된
    주**만 포함 (진행 중인 주는 부분 집계라 생략 — 배열이 짧아짐).
    """
    tz = timezone.get_current_timezone()
    today = timezone.localdate(now)
    this_monday = today - timedelta(days=today.weekday())
    weeks = [this_monday - timedelta(weeks=i) for i in range(COHORT_USAGE_WEEKS - 1, -1, -1)]
    span_start = _local_midnight(weeks[0])

    cohort_users: dict = defaultdict(set)
    for uid, d in (
        User.objects.filter(date_joined__gte=span_start)
        .annotate(d=TruncDate("date_joined", tzinfo=tz))
        .values_list("id", "d")
    ):
        if d is not None:
            cohort_users[d - timedelta(days=d.weekday())].add(uid)

    # 주별 사용 유저 — 이벤트별 (owner, 주) distinct 를 DB 에서 dedupe 해 작게 당김
    week_users: dict = defaultdict(set)

    def _collect(qs, user_field: str, ts_field: str):
        for uid, w in (
            qs.annotate(w=TruncWeek(ts_field, tzinfo=tz)).values_list(user_field, "w").distinct()
        ):
            if w is not None:
                week_users[timezone.localtime(w, tz).date()].add(uid)

    _collect(
        AutoDMCampaign.objects.filter(created_at__gte=span_start),
        "ig_connection__workspace__owner_id",
        "created_at",
    )
    _collect(
        SentDMLog.objects.filter(created_at__gte=span_start),
        "campaign__ig_connection__workspace__owner_id",
        "created_at",
    )
    _collect(Page.objects.filter(created_at__gte=span_start), "user_id", "created_at")

    rows = []
    for w in weeks:
        users = cohort_users.get(w, set())
        size = len(users)
        values: list = []
        for i in range(COHORT_MAX_PERIODS):
            target = w + timedelta(weeks=i + 1)
            if target >= this_monday or size == 0:  # 진행 중/미도래 주는 생략
                break
            values.append(round(len(users & week_users.get(target, set())) / size, 4))
        rows.append({"cohort": w.isoformat(), "size": size, "values": values})

    return {"unit": "week", "max_periods": COHORT_MAX_PERIODS, "rows": rows}


def _cohorts(now) -> dict:
    """Q-2: 코호트 분석 매트릭스 2종 — 기간 필터와 무관 (항상 최근 6개월/6주)."""
    return {"subscription": _subscription_cohorts(now), "usage": _usage_cohorts(now)}


# ── 고객 액션 리스트 (Q-3) — 기간 필터와 무관한 현재 스냅샷 ─────────────


def _sub_monthly_amount(sub) -> int:
    """구독 월 청구액 (원) — 스냅샷 우선 + pro 추가 IG (recent_cancellations 와 동일 공식)."""
    amount = sub.monthly_amount_snapshot or (sub.plan.monthly_price or 0)
    if sub.plan.name == "pro":
        amount += (sub.extra_ig_accounts or 0) * EXTRA_IG_ACCOUNT_PRICE
    return amount


def _payment_failed_actions(now) -> list[dict]:
    """① 결제 실패 — PAST_DUE 유료 구독 (dunning 진행/소진 상태 포함)."""
    from apps.billing.tasks import MAX_RENEWAL_ATTEMPTS

    subs = list(
        UserSubscription.objects.filter(status=SubscriptionStatus.PAST_DUE)
        .exclude(plan__name__in=("free", "admin"))
        .select_related("user", "plan")
    )
    if not subs:
        return []
    failed_at_map = {
        r["user_id"]: r["last"]
        for r in PaymentHistory.objects.filter(
            user_id__in=[s.user_id for s in subs], status=PaymentStatus.FAILED
        )
        .values("user_id")
        .annotate(last=Max("created_at"))
    }
    rows = []
    for s in subs:
        if s.next_billing_retry_at:
            retry_status = "scheduled"
        elif s.renewal_attempts >= MAX_RENEWAL_ATTEMPTS:
            retry_status = "exhausted"
        else:
            retry_status = "none"
        failed_at = failed_at_map.get(s.user_id)
        rows.append(
            {
                "user_id": s.user_id,
                "email": s.user.email or "",
                "plan": s.plan.name,
                "plan_display": s.plan.display_name,
                "amount": _sub_monthly_amount(s),
                "failed_at": timezone.localtime(failed_at).isoformat() if failed_at else None,
                "reason": s.last_billing_error or "",
                "retry_status": retry_status,
                "next_retry_at": (
                    timezone.localtime(s.next_billing_retry_at).isoformat()
                    if s.next_billing_retry_at
                    else None
                ),
                "retry_count": s.renewal_attempts,
                "retry_max": MAX_RENEWAL_ATTEMPTS,
                "link": {"page": f"/users/{s.user_id}", "params": {}},
            }
        )
    rows.sort(key=lambda r: r["failed_at"] or "", reverse=True)
    return rows[:CUSTOMER_ACTIONS_LIMIT]


def _dormant_actions(now) -> list[dict]:
    """② 장기 미사용 — 유료 ACTIVE 구독인데 DORMANT_IDLE_DAYS(30일)+ 기능 미사용.

    '기능 사용' = DM 발송 발생 · DM 캠페인 생성 · 페이지 공개/수정(updated_at) ·
    페이지 클릭 발생 중 아무거나. 활동 이력이 전혀 없으면 last_active_at=null +
    idle_days=첫 결제(없으면 구독 생성) 후 경과일. 미사용 오래된 순.
    """
    subs = list(
        UserSubscription.objects.filter(status=SubscriptionStatus.ACTIVE)
        .exclude(plan__name__in=("free", "admin"))
        .select_related("user", "plan")
    )
    if not subs:
        return []
    owner_ids = [s.user_id for s in subs]

    def _last_map(qs, user_field: str, ts_field: str) -> dict:
        return {
            r[user_field]: r["last"] for r in qs.values(user_field).annotate(last=Max(ts_field))
        }

    dm_last = _last_map(
        SentDMLog.objects.filter(campaign__ig_connection__workspace__owner_id__in=owner_ids),
        "campaign__ig_connection__workspace__owner_id",
        "created_at",
    )
    camp_last = _last_map(
        AutoDMCampaign.objects.filter(ig_connection__workspace__owner_id__in=owner_ids),
        "ig_connection__workspace__owner_id",
        "created_at",
    )
    page_last = _last_map(Page.objects.filter(user_id__in=owner_ids), "user_id", "updated_at")
    click_last = _last_map(
        BlockClick.objects.filter(page__user_id__in=owner_ids), "page__user_id", "clicked_at"
    )
    first_paid = {
        r["user_id"]: r["first"]
        for r in PaymentHistory.objects.filter(user_id__in=owner_ids, status=PaymentStatus.PAID)
        .values("user_id")
        .annotate(first=Min("paid_at"))
    }

    since_30d = now - timedelta(days=30)
    rows = []
    for s in subs:
        candidates = [m.get(s.user_id) for m in (dm_last, camp_last, page_last, click_last)]
        candidates = [c for c in candidates if c is not None]
        last_active = max(candidates) if candidates else None
        anchor = last_active or first_paid.get(s.user_id) or s.created_at
        idle_days = max(0, (now - anchor).days)
        if idle_days < DORMANT_IDLE_DAYS:
            continue
        rows.append(
            {
                "user_id": s.user_id,
                "email": s.user.email or "",
                "plan": s.plan.name,
                "plan_display": s.plan.display_name,
                "last_active_at": (
                    timezone.localtime(last_active).isoformat() if last_active else None
                ),
                "idle_days": idle_days,
                "dm_30d": 0,  # 정의상 활동이면 dormant 제외 — 아래에서 실측으로 덮음
                "page_clicks_30d": 0,
                "link": {"page": f"/users/{s.user_id}", "params": {}},
            }
        )
    rows.sort(key=lambda r: -r["idle_days"])
    rows = rows[:CUSTOMER_ACTIONS_LIMIT]

    # 숏리스트만 30d 실측 (정의상 0이지만 표기 일관성 위해 계산해 덮음)
    short_ids = [r["user_id"] for r in rows]
    if short_ids:
        dm_30d = {
            r["campaign__ig_connection__workspace__owner_id"]: r["c"]
            for r in SentDMLog.objects.filter(
                campaign__ig_connection__workspace__owner_id__in=short_ids,
                created_at__gte=since_30d,
            )
            .values("campaign__ig_connection__workspace__owner_id")
            .annotate(c=Count("id"))
        }
        clicks_30d = {
            r["page__user_id"]: r["c"]
            for r in BlockClick.objects.filter(
                page__user_id__in=short_ids, clicked_at__gte=since_30d
            )
            .values("page__user_id")
            .annotate(c=Count("id"))
        }
        for r in rows:
            r["dm_30d"] = dm_30d.get(r["user_id"], 0)
            r["page_clicks_30d"] = clicks_30d.get(r["user_id"], 0)
    return rows


def _recent_churn_actions(now) -> list[dict]:
    """③ 최근 해지 — RECENT_CHURN_WINDOW_DAYS(30일) 내 해지 '완료'(free 다운그레이드) +
    실제 결제 이력 보유 (트라이얼 만료-only 다운그레이드 제외 — 실이탈만, 윈백 대상).

    다운그레이드가 이전 플랜/금액을 소거하므로: plan = CancellationEvent.from_plan
    best-effort(없으면 ""), monthly_amount = 마지막 PAID 결제 금액.
    recent_cancellations(취소 예약, 아직 유료)와는 상호 배타 — 여기는 완료분만.
    """
    since = now - timedelta(days=RECENT_CHURN_WINDOW_DAYS)
    subs = list(
        UserSubscription.objects.filter(
            plan__name="free", cancelled_at__gte=since, cancelled_at__lte=now
        ).select_related("user")
    )
    if not subs:
        return []
    user_ids = [s.user_id for s in subs]

    # 실결제 이력 (첫/마지막 결제 — tenure·마지막 월액)
    pay_rows = PaymentHistory.objects.filter(
        user_id__in=user_ids, status=PaymentStatus.PAID
    ).values_list("user_id", "amount", "paid_at")
    first_paid: dict = {}
    last_paid: dict = {}  # uid -> (paid_at, amount)
    for uid, amount, paid_at in pay_rows:
        if paid_at is None:
            continue
        if uid not in first_paid or paid_at < first_paid[uid]:
            first_paid[uid] = paid_at
        if uid not in last_paid or paid_at > last_paid[uid][0]:
            last_paid[uid] = (paid_at, amount)

    reason_map: dict = {}
    from_plan_map: dict = {}
    if CANCELLATION_EVENT_AVAILABLE:
        for ev in CancellationEvent.objects.filter(user_id__in=user_ids).order_by(
            "user_id", "-created_at"
        ):
            if ev.event == "cancel_reason_submitted":
                reason_map.setdefault(ev.user_id, ev.reason)
            if ev.from_plan:
                from_plan_map.setdefault(ev.user_id, ev.from_plan)
    display_by_plan = dict(SubscriptionPlan.objects.values_list("name", "display_name"))

    rows = []
    for s in subs:
        if s.user_id not in first_paid:
            continue  # 결제 이력 없는 다운그레이드(트라이얼 만료 등) — 실이탈 아님
        reason = reason_map.get(s.user_id, "")
        plan_name = from_plan_map.get(s.user_id, "")
        tenure_days = max(0, (s.cancelled_at - first_paid[s.user_id]).days)
        rows.append(
            {
                "user_id": s.user_id,
                "email": s.user.email or "",
                "plan": plan_name,
                "plan_display": display_by_plan.get(plan_name, plan_name or ""),
                "churned_at": timezone.localtime(s.cancelled_at).isoformat(),
                "reason": reason,
                "reason_label": CANCEL_REASON_LABELS.get(reason, reason) if reason else "",
                "tenure_months": tenure_days // 30,
                "monthly_amount": last_paid[s.user_id][1],
                "link": {"page": f"/users/{s.user_id}", "params": {}},
            }
        )
    rows.sort(key=lambda r: r["churned_at"], reverse=True)
    return rows[:CUSTOMER_ACTIONS_LIMIT]


def _customer_actions(now) -> dict:
    """Q-3: 고객 액션 리스트 3종 — 기간 필터와 무관 (현재 기준 스냅샷, 각 최대 20건)."""
    return {
        "payment_failed": _payment_failed_actions(now),
        "dormant": _dormant_actions(now),
        "recent_churn": _recent_churn_actions(now),
    }


# ── 고정 패널 스냅샷 (R-2) — 기간 파라미터와 무관한 전체 기간 누적 ──────


def _plan_count_rows(subs) -> tuple[list[dict], int]:
    """구독 QS → ([{name, display_name, count}], total) — Σ count == total 보장."""
    rows = [
        {
            "name": r["plan__name"],
            "display_name": r["plan__display_name"],
            "count": r["c"],
        }
        for r in (
            subs.values("plan__name", "plan__display_name", "plan__sort_order")
            .annotate(c=Count("id"))
            .order_by("plan__sort_order", "plan__name")
        )
    ]
    return rows, sum(r["count"] for r in rows)


def _snapshot(now) -> dict:
    """상단 고정 패널 — **전체 기간 누적**, period/커스텀 범위와 무관 (R-2).

    - paying: 실제 결제(PAID) 이력이 있고 **현재 유료 구독이 살아있는**(ACTIVE) 회원 수.
      PAST_DUE(결제 실패 dunning 중)는 **제외** — customer_actions.payment_failed 에
      별도로 잡히고, '실제 결제 인원'의 의미를 흐리지 않기 위함 (R-7 ②).
      by_plan 은 **현재 구독 플랜** 기준 (Σ == total).
    - trialing: 조회 시점 TRIALING + 유료플랜 + **카드 등록 완료**
      (billing_key_issued_at — 어드민 수동 부여 무카드 계정 제외).
      feature_stats.trials.active(카드 필터 없음) 보다 작거나 같은 것이 정상.
    - visitors: 전체 기간 고유 방문자(distinct visitor_id). 어트리뷰션 미탑재 시 0.
    - signups: 누적 가입 회원 수 (_signups_count 와 동일 정책 — 별도 필터 없음).
    - activated: **가입 시기 무관**, 공개 페이지 보유 ∪ DM 캠페인 보유 고유 회원 수.
      period=all 의 funnel.activation.count(코호트=전체)와 일치해야 하므로 판정 축을
      _cohort_qs(pg/cp)와 동일하게 맞춘다.
    """
    paid_user_ids = PaymentHistory.objects.filter(status=PaymentStatus.PAID).values("user_id")
    paying_rows, paying_total = _plan_count_rows(
        UserSubscription.objects.filter(
            user_id__in=paid_user_ids, status=SubscriptionStatus.ACTIVE
        ).exclude(plan__name__in=_PAID_EXCLUDE)
    )
    trial_rows, trial_total = _plan_count_rows(
        UserSubscription.objects.filter(
            status=SubscriptionStatus.TRIALING, billing_key_issued_at__isnull=False
        ).exclude(plan__name__in=_PAID_EXCLUDE)
    )

    visitors = 0
    if ATTRIBUTION_AVAILABLE:
        visitors = LandingVisit.objects.order_by().values("visitor_id").distinct().count()

    page_owners = set(
        Page.objects.filter(is_public=True).order_by().values_list("user_id", flat=True).distinct()
    )
    campaign_owners = set(
        AutoDMCampaign.objects.order_by()
        .values_list("ig_connection__workspace__owner_id", flat=True)
        .distinct()
    )

    return {
        "as_of": timezone.localtime(now).isoformat(),
        "paying": {"total": paying_total, "by_plan": paying_rows},
        "trialing": {"total": trial_total, "by_plan": trial_rows},
        "visitors": visitors,
        "signups": User.objects.count(),
        "activated": len(page_owners | campaign_owners),
    }


def _snapshot_cached(now) -> dict:
    """R-6 ②: 기간과 무관하므로 별도 캐시 키 — 모든 period 응답이 계산 1회를 공유."""
    cached = cache.get(CACHE_KEY_SNAPSHOT)
    if cached is not None:
        return cached
    data = _snapshot(now)
    cache.set(CACHE_KEY_SNAPSHOT, data, MARKETING_DASHBOARD_SNAPSHOT_CACHE_TTL)
    return data


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
- `period`: `7d` / `30d`(기본) / `90d` / `all`. current = [now-N일, now),
  previous = [now-2N일, now-N일). 잘못된 값은 **400**.
  모든 KPI 는 `{current, previous, delta_pct}` (previous==0 → delta null).
- **`period=all`(R-1, 전체 기간)**: current = [**서비스 최초 가입 시각**(가장 이른
  `User.date_joined`, 회원 0명이면 now), now). **직전 기간을 만들지 않습니다** —
  `range.previous_start/previous_end = null`, 모든 delta 계열은
  `previous: null` + `delta_pct: null` (빈 구간을 previous 로 주면 "직전 0건"으로
  오독되므로 의도적으로 null). 커스텀 범위(`start`&`end`)는 지금처럼 직전 동일 길이 비교 유지.
  계산량이 가장 크고 분 단위로 값이 변하지 않아 캐시 TTL 은 **900초(15분)**.
- **`snapshot`(R-2, 상단 고정 패널)**: **전체 기간 누적, 기간 파라미터와 무관** —
  `period=7d` 응답에도 `period=all` 응답에도 같은 값이 들어갑니다
  (별도 캐시 키 `admin:dash:mkt:snapshot`, TTL 900초 → 모든 period 가 계산 1회를 공유).
  `{as_of, paying{total,by_plan}, trialing{total,by_plan}, visitors, signups, activated}`.
  · `paying` = 실결제(PAID) 이력 보유 + **현재 유료 ACTIVE** 구독 회원 수
  (**PAST_DUE 제외** — `customer_actions.payment_failed` 에 별도 집계, free/admin 제외),
  `by_plan` 은 현재 구독 플랜 기준 (Σ == total).
  · `trialing` = 조회 시점 TRIALING + 유료플랜 + **카드 등록 완료**(billing_key_issued_at) —
  카드 필터가 없는 `feature_stats.trials.active` 보다 작거나 같은 것이 정상.
  · `visitors` = 전체 기간 고유 방문자(distinct visitor_id, 미탑재 시 0),
  `signups` = 누적 가입 회원 수, `activated` = **가입 시기 무관** 공개 페이지 ∪ DM 캠페인
  보유 고유 회원 수. `funnel.activation.count`(이 기간 가입 코호트)와는 정의가 다르며
  **period=all 에서만 두 값이 일치**합니다.
- **커스텀 범위**: `start=YYYY-MM-DD` + `end=YYYY-MM-DD` (Asia/Seoul 로컬 날짜) 를 함께 주면
  `period` 무시하고 커스텀 집계 — `period` 응답은 `"custom"`. current = [start 자정, end+1일 자정),
  previous = **직전 동일 길이 구간** `[start-span, start)` (span = current 길이). **검증(400)**:
  start/end 중 하나만·파싱 불가·`end < start`·span > 366일 → `details.reason`.
- **`trends.granularity`(R-5)**: 현재 구간이 길면 버킷 단위를 자동 상향합니다 —
  **≤120일 `"day"` / ≤400일 `"week"`(월요일 시작) / 그 이상 `"month"`(1일 시작)**.
  `date` 는 버킷 **시작일**, 필드 구성·`Σ by_channel == 버킷 총량` 규칙은 일별과 동일하며
  마지막 버킷이 진행 중(미완결)이어도 그대로 내려갑니다. `activated` 는 **버킷 단위 dedupe**
  (주별이면 같은 주 중복 활동은 1명). 프론트는 이 값을 읽어 받은 단위 그대로 렌더하세요.
- **`trends`(신규, 항상 포함)**: current 기간 전체를 **로컬 날짜 단위로 zero-fill** 한 일별 버킷.
  각 버킷 = `{date(로컬 YYYY-MM-DD), signups, paid, dm_delivered, page_views, page_clicks, visits}`.
  signups=User.date_joined, paid=유저별 첫 PAID paid_at(KPI first-paid 재사용),
  dm_delivered=SentDMLog(delivered/read), page_views=PageView, page_clicks=BlockClick,
  visits=LandingVisit **행 수(세션 단위)** — 트래픽 볼륨 차트용이라 퍼널/채널의 고유 방문자와
  단위가 다름 (어트리뷰션 미탑재 시 0). 지표별 1쿼리(TruncDate group-by, Asia/Seoul).
- **퍼널 = 가입 코호트(signup_cohort), 분기 구조**: `date_joined ∈ 기간` 유저가 "현재까지"
  단계에 도달했는지 기준 (기간-활동 카운트는 모집단 혼합으로 100% 초과 전환율 가능 → 배제).
  visit 만 기간-이벤트이며 **고유 방문자(distinct visitor_id) 단위** — 재방문 세션은 1명으로
  집계, 세션 수는 `kpis.visits` 로 별도 제공(방문자는 브라우저 localStorage 단위라 기기/브라우저가
  다르면 별개로 집계됨). 공통 head(방문자→가입) 이후 2갈래 병렬 분기 → 유료 전환 수렴:
  분기 A(DM 자동화) = IG 연동 → DM 캠페인(IG 연동이 전제라 순차),
  분기 B(바이오링크) = 페이지 생성 → 페이지 공개(IG 불필요, 가입에서 바로 — 비선형).
  각 노드 = `{key, label, count, rate, rate_of, formula}`. rate: signup=가입/고유 방문자,
  ig_connected=ig/가입, dm_campaign=dm/ig, page_created=생성/가입, page_published=공개/생성,
  paid=유료플랜 전환/가입(수렴이라 가입 대비).
- **퍼널 activation 노드(R-3)**: 분기 4노드(IG 연동/DM 캠페인/페이지 생성/페이지 공개)를
  대체하는 **단일 '활성화 유저' 노드** — `variants[*].activation =
  {key:"activated", label:"활성화 유저", count, rate, rate_of:"signup", formula}`.
  count = 가입 코호트 중 **공개 페이지 보유 ∪ DM 캠페인 보유**(중복 제거),
  rate = activated / signups (분모 0 → null). `variants[*].activation_overlap.both` =
  둘 다 보유한 인원 → 프론트가 `dm_only = dm_campaign - both`,
  `page_published_only = page_published - both` 로 중복 제거 구성을 계산합니다.
  `branches` 는 **그대로 유지**(퍼널에서 숨기고 '자세히 보기' 팝업에서 재사용). 채널
  variant 전부에 동일하게 들어갑니다.
- **퍼널 conversion 노드(N-1 + R-4)**: label="유료플랜 전환", `count` = **카드 등록 체험 중
  + 실결제**, **`rate_of` = `"activated"`**(방문→가입→활성화→유료 직렬이라 분모가 활성화
  유저로 변경). `breakdown = {pro_trial, basic_trial, pro_paid, basic_paid, other}` —
  **모든 값의 합 == count** 보장. pro_trial/basic_trial = 현재 TRIALING · 해당 플랜 ·
  **카드 등록 완료**(billing_key_issued_at) · PAID 이력 없음 (basic_trial 은 체험이 프로
  전용인 현 정책에선 사실상 항상 0이지만 합계식 안정성을 위해 **키는 항상 포함** — 0이면
  프론트에서 행 생략), pro_paid/basic_paid = PAID 이력 보유 회원의 **현재 구독 플랜** 분해,
  other = 해지 후 free 강등 등 잔여(보통 0). **카드 미등록 체험자는 breakdown 뿐 아니라
  `count` 에서도 제외**되며(어드민 수동 부여 계정이 전환 실적으로 잡히는 문제 제거),
  제외 인원은 `conversion.excluded_no_card`(화면 비노출, 검증용)로 함께 내려갑니다.
  체험이 만료돼 free 로 강등된 회원은 어느 쪽에도 안 잡힘(현재 상태 기준).
  ⚠ 같은 정의 변경이 `channels.rows[].free_trial` / `campaigns[].free_trial` 에도
  적용됩니다(카드 등록 체험만).
- **채널별 퍼널 variant(미리 계산)**: `funnel.variants` 에 `all` + signups>0 인 채널별 variant 를
  담아 응답 (드롭다운 전환 시 재요청 불필요). `available_channels` 는 `all` + 각 채널
  (signups desc). 어트리뷰션 미탑재 시 `all` 만.
- `first_page_published` / `new_public_pages` 는 **근사** — 공개 시각 미기록이라 첫 공개
  페이지의 `created_at` 을 대용합니다.
- `paid_conversions` 는 유저별 **첫 PAID PaymentHistory.paid_at** 기준 — **실결제만**
  카운트(체험·쿠폰 미결제 제외), `definition` 필드에 한국어 정의 동봉(M-2).
  `pro_activated_at` 은 환불 시 null 처리되어 부적합.
- **퍼널 노드 `formula`(M-6)**: 모든 노드에서 한국어 정의로 채워짐(non-null) — 프론트
  툴팁의 정본. 코호트 단계는 "(도달 여부는 현재까지 기준)" 명시.
- **`feature_stats.biolink.created_breakdown`(M-1)**: 기간 내 생성 공개 페이지의 생성 방식
  분해 `{ai, imported, manual}` — 모집단은 `new_public_pages.current` 와 동일하므로 합이
  항상 일치. 우선순위 imported(import_source 있음) > ai(성공 AiJob 보유) > manual.
- **`channels.referral_codes[].description`(M-3)**: 제휴 코드 내부 메모
  (ReferralCode.description) 노출 — 어떤 제휴인지 식별용.
- **`paid_conversion_analysis.paid_plan_no_payment`(M-2)**: 기간 내 체험(카드등록)·쿠폰
  (레퍼럴)으로 유료 플랜을 시작했으나 현재까지 미결제인 회원 수
  `{count, referral_trial, card_trial, definition}` — "실결제 N명 / 무료 유료플랜 M명" 병기용.
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
- **`channels.rows`(분기 컬럼)**: 단일 '활성화' 대신 비순차 분기 단계별 컬럼 —
  `ig_connected`/`dm_campaign`(DM 갈래) · `page_created`/`page_published`(바이오링크 갈래).
  `paid` 는 **실결제(첫 PAID 이력)만** — 체험 미포함(N-4). `free_trial`(현재 체험 중·미결제)
  별도 컬럼 제공.
- **`channels.rows[].visits` = 고유 방문자(distinct visitor_id)**: 채널/캠페인 행의 `visits`
  와 퍼널 head 의 visit 노드는 전부 **사람(브라우저) 단위** — 같은 방문자의 재방문 세션은
  1로 집계됩니다. `signup_rate = signups / 고유 방문자`. 세션(이벤트) 수가 필요하면
  `kpis.visits` / `trends.buckets[].visits` 를 사용하세요. 한 방문자가 여러 채널로 유입되면
  채널별로 각각 1씩 잡히므로 채널 합계 ≥ 전체 고유 방문자일 수 있습니다.
- **`channels.rows[].campaigns`(N-2)**: 채널 하위 (utm_campaign × utm_content) 조합별 분해 —
  채널 행과 동일 축(visits/signups/ig_connected/dm_campaign/page_created/page_published/
  paid/free_trial/paid_rate). 방문(고유 방문자)=LandingVisit 저장 utm,
  가입측=SignupAttribution 저장 utm.
  utm 없는 유입은 `utm_campaign=""` 한 행. (paid+free_trial) desc → signups desc →
  visits desc 정렬 상위 **CHANNEL_CAMPAIGNS_LIMIT(10)** 개만 — 잘리면
  `campaigns_truncated=true`.
- **`feature_stats.trials` 확장(N-3)**: `active`(조회 시점 TRIALING 유료플랜 구독 수,
  point-in-time) + `paid_conversion_rate`(기간 내 체험 시작자 전체(레퍼럴+카드, 회원 dedupe)
  중 실결제 발생 비율) + 비율 2종의 한국어 계산식 `conversion_formula` /
  `paid_conversion_formula`.
- **`feature_stats.trials` 종료 기준(P-1, 대표 지표)**: `ended`(이 기간에 무료체험이 끝난
  고객 수 — 만료·중도 해지·체험 중 결제 전환, 쿠폰+카드 전체, 유저 dedupe) /
  `ended_converted`(그중 실결제 유지) / `ended_conversion_rate` / `ended_conversion_formula`.
  체험 길이가 가변(기본 30일+쿠폰 보너스)이라 시작 코호트 분모의 과소평가를 정정 —
  종료 시점: 쿠폰=min(trial_ends_at, converted_at), 카드 전환=첫 PAID paid_at,
  카드 미전환=만료 다운그레이드 시각(cancelled_at, ≈종료+1h). 진행 중 체험은 분모 제외.
- **`channels.rows[].referral_overlap`(P-3)**: 원래 이 채널로 저장됐으나 제휴코드 사용
  (레퍼럴 오버레이)으로 referral 행에 배타 집계된 코호트 인원 수 — 원 채널 과소 집계의
  보정 표기용 (중복 집계는 없음).
- **`subscription_retention.snapshot_since`(P-4)**: 일별 구독 스냅샷 적재 시작일
  (YYYY-MM-DD, 미적재 시 null). 스냅샷 이력이 충분히 쌓이면 유지·해지 분석이
  `basis="snapshot"`(정확 계산)으로 전환될 예정 — 그 전까지 `approx_no_snapshot`.
- **`trends.buckets[].activated` + `by_channel`(Q-1)**: activated = 그날 DM 캠페인 생성
  or 페이지 공개한 고유 회원 수(일별 dedupe). by_channel = 채널 키 →
  `{visits, signups, activated, paid}` — 귀속은 채널별 성과 표와 동일 규칙
  (저장 채널 + referral 오버라이드), visits 만 방문 자체의 저장 채널(세션 단위).
  각 지표 Σ(채널) == 버킷 총량, 전부 0인 채널은 생략. 주별 합산은 프론트에서.
- **`cohorts`(Q-2, 기간 필터 무관)**: `subscription`(첫 결제 월 × M+1..M+5 유료 유지율 —
  시점별로 일별 스냅샷 우선, 없으면 현재 상태 역산 근사 → 하나라도 근사면
  `basis="approx"`) + `usage`(가입 주(월요일) × W+1..W+5 기능 사용률 — 이벤트 로그
  소급이라 항상 정확, 진행 중인 주는 생략). 도래 전 기간은 values 에서 생략.
- **`customer_actions`(Q-3, 기간 필터 무관, 각 20건)**: `payment_failed`(PAST_DUE 유료
  구독 + dunning 재시도 상태 D+1/D+3/D+5, retry_max=3) / `dormant`(유료 ACTIVE 인데
  30일+ 기능 미사용 — DM 발송·캠페인 생성·페이지 공개/수정·페이지 클릭 기준) /
  `recent_churn`(30일 내 해지 완료 + 실결제 이력 — 취소예약(recent_cancellations)과
  상호 배타. 해지 전 플랜은 CancellationEvent.from_plan best-effort).
- **referral 채널의 `campaigns[]`(Q-4)**: referral 행의 세부 축은 원래 방문 utm 이 아니라
  **사용한 제휴코드** — `utm_campaign` 자리에 코드 문자열(`referral_codes[].code` 와
  정확히 일치, 프론트 조인 키), `utm_content` 는 항상 "".
- **`feature_stats.*.active_users`(신규)**: 기능별 발송량과 별개로 **실제 사용한 고유 회원 수**
  (페이지 공개/DM 캠페인 생성/스팸 방어 사용 사용자) — 마케팅 활성도 지표.
- **`onboarding_dropoffs`(신규)**: 가입 코호트의 단계별 이탈 세그먼트 + 최근 샘플 회원
  (CS 드릴다운). 측정 4종(무행동/IG후캠페인없음/페이지생성후미공개/캠페인생성후미발송) +
  `paywall_no_payment`(유료 기능 시도 후 미결제 — CheckoutEvent 의존, 미탑재 시 available=false).
- **`paid_conversion_analysis`(신규)**: 유료 전환을 3축 분리 — (1) `by_plan` 선택 플랜별
  전환자 수(현재 구독 플랜, admin/free 제외) (2) `entry_paths` 결제 진입 경로(업그레이드
  트리거, `CheckoutEvent` 귀속 — 미수집 시 `entry_paths_available=false`) (3)
  `post_payment_usage` 결제 후 7일 내 실제 사용 기능별 유저 수. **'무엇 때문에 결제했나'를
  단정하지 않고** 진입 경로/사용을 분리 제시.
- 응답은 Redis 에 **300초(5분) 캐시** (프리셋 키 `admin:dash:mkt:{period}`,
  커스텀 키 `admin:dash:mkt:custom:{start}:{end}`). 단 `period=all` 은 **900초(15분)**,
  `snapshot` 은 별도 키 `admin:dash:mkt:snapshot` 에 **900초** (모든 period 공유).

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
                description="집계 기간. 7d / 30d(기본) / 90d / all(전체 기간). 그 외 값은 400. "
                "all 은 서비스 최초 가입 시각부터 now 까지이며 직전 기간 비교가 없습니다"
                "(range.previous_*=null, delta 전부 null). start&end 를 함께 주면 무시됩니다.",
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
                '"details": {"allowed": ["7d","30d","90d","all"]}}} '
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
                    "snapshot": {
                        "as_of": "2026-07-11T14:00:03+09:00",
                        "paying": {
                            "total": 87,
                            "by_plan": [
                                {"name": "basic", "display_name": "베이직", "count": 29},
                                {"name": "pro", "display_name": "프로", "count": 58},
                            ],
                        },
                        "trialing": {
                            "total": 41,
                            "by_plan": [{"name": "pro", "display_name": "프로", "count": 41}],
                        },
                        "visitors": 24180,
                        "signups": 1842,
                        "activated": 612,
                    },
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
                        "paid_conversions": {
                            "current": 18,
                            "previous": 11,
                            "delta_pct": 63.6,
                            "definition": PAID_CONVERSIONS_DEFINITION,
                        },
                        "mrr": {
                            "current": 2085000,
                            "previous": None,
                            "delta_pct": None,
                            "currency": "KRW",
                        },
                    },
                    "funnel": {
                        "semantics": "signup_cohort",
                        "available_channels": [
                            {"value": "all", "label": "전체 채널"},
                            {"value": "instagram_organic", "label": "인스타그램 유입"},
                            {"value": "unknown", "label": "미분류"},
                        ],
                        "variants": {
                            "all": {
                                "head": [
                                    {
                                        "key": "visit",
                                        "label": "방문자",
                                        "count": 3900,
                                        "rate": None,
                                        "rate_of": None,
                                        "formula": "기간 내 랜딩 고유 방문자 수(distinct "
                                        "visitor_id, 브라우저 단위) — 재방문 세션은 1명으로 "
                                        "집계, 유일하게 기간-이벤트 기준",
                                    },
                                    {
                                        "key": "signup",
                                        "label": "가입",
                                        "count": 210,
                                        "rate": 0.0538,
                                        "rate_of": "visit",
                                        "formula": "기간 내 가입한 회원 수(가입 코호트) ÷ "
                                        "고유 방문자 수 × 100",
                                    },
                                ],
                                "branches": [
                                    {
                                        "key": "dm",
                                        "label": "DM 자동화",
                                        "steps": [
                                            {
                                                "key": "ig_connected",
                                                "label": "IG 연동",
                                                "count": 102,
                                                "rate": 0.4857,
                                                "rate_of": "signup",
                                                "formula": "IG 연동 수 ÷ 가입 수 × 100",
                                            },
                                            {
                                                "key": "dm_campaign",
                                                "label": "DM 캠페인",
                                                "count": 51,
                                                "rate": 0.5,
                                                "rate_of": "ig_connected",
                                                "formula": "DM 캠페인 수 ÷ IG 연동 수 × 100",
                                            },
                                        ],
                                    },
                                    {
                                        "key": "biolink",
                                        "label": "바이오링크",
                                        "steps": [
                                            {
                                                "key": "page_created",
                                                "label": "페이지 생성",
                                                "count": 70,
                                                "rate": 0.3333,
                                                "rate_of": "signup",
                                                "formula": "페이지 생성 수 ÷ 가입 수 × 100",
                                            },
                                            {
                                                "key": "page_published",
                                                "label": "페이지 공개",
                                                "count": 55,
                                                "rate": 0.7857,
                                                "rate_of": "page_created",
                                                "formula": "페이지 공개 수 ÷ 페이지 생성 수 × 100",
                                            },
                                        ],
                                    },
                                ],
                                "activation": {
                                    "key": "activated",
                                    "label": "활성화 유저",
                                    "count": 96,
                                    "rate": 0.4571,
                                    "rate_of": "signup",
                                    "formula": "DM 캠페인 1개 이상 생성 또는 페이지 공개 ÷ "
                                    "이 기간 가입자 (중복 제거) · 도달 여부는 현재까지 기준",
                                },
                                "activation_overlap": {"both": 21},
                                "conversion": {
                                    "key": "paid",
                                    "label": "유료플랜 전환",
                                    "count": 18,
                                    "rate": 0.1875,
                                    "rate_of": "activated",
                                    "formula": "가입 코호트 중 유료플랜(무료체험+실결제) 진입"
                                    " ÷ 활성화 유저 × 100 · 무료체험은 **카드 등록 완료** "
                                    "건만(어드민 수동 부여 제외), 실결제=실제 결제(Toss PAID) 발생",
                                    "breakdown": {
                                        "pro_trial": 11,
                                        "basic_trial": 0,
                                        "pro_paid": 5,
                                        "basic_paid": 2,
                                        "other": 0,
                                    },
                                    "excluded_no_card": 1,
                                },
                            }
                        },
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
                                "activated": 6,
                                "by_channel": {
                                    "instagram_organic": {
                                        "visits": 140,
                                        "signups": 7,
                                        "activated": 3,
                                        "paid": 1,
                                    },
                                    "search_organic": {
                                        "visits": 40,
                                        "signups": 3,
                                        "activated": 2,
                                        "paid": 1,
                                    },
                                    "unknown": {
                                        "visits": 0,
                                        "signups": 2,
                                        "activated": 1,
                                        "paid": 0,
                                    },
                                },
                            },
                            {
                                "date": "2026-06-12",
                                "signups": 8,
                                "paid": 0,
                                "dm_delivered": 402,
                                "page_views": 260,
                                "page_clicks": 51,
                                "visits": 205,
                                "activated": 4,
                                "by_channel": {
                                    "instagram_organic": {
                                        "visits": 205,
                                        "signups": 8,
                                        "activated": 4,
                                        "paid": 0,
                                    }
                                },
                            },
                        ],
                    },
                    "cohorts": {
                        "subscription": {
                            "unit": "month",
                            "max_periods": 5,
                            "basis": "approx",
                            "rows": [
                                {
                                    "cohort": "2026-02",
                                    "size": 9,
                                    "values": [0.89, 0.78, 0.72, 0.67, 0.61],
                                },
                                {"cohort": "2026-07", "size": 13, "values": []},
                            ],
                        },
                        "usage": {
                            "unit": "week",
                            "max_periods": 5,
                            "rows": [
                                {
                                    "cohort": "2026-06-15",
                                    "size": 118,
                                    "values": [0.62, 0.49, 0.42, 0.38, 0.34],
                                },
                                {"cohort": "2026-07-20", "size": 96, "values": []},
                            ],
                        },
                    },
                    "customer_actions": {
                        "payment_failed": [
                            {
                                "user_id": 8901,
                                "email": "a@b.com",
                                "plan": "pro",
                                "plan_display": "프로",
                                "amount": 14900,
                                "failed_at": "2026-07-25T09:00:00+09:00",
                                "reason": "카드 한도 초과",
                                "retry_status": "scheduled",
                                "next_retry_at": "2026-07-27T09:00:00+09:00",
                                "retry_count": 1,
                                "retry_max": 3,
                                "link": {"page": "/users/8901", "params": {}},
                            }
                        ],
                        "dormant": [
                            {
                                "user_id": 8911,
                                "email": "c@d.com",
                                "plan": "pro",
                                "plan_display": "프로",
                                "last_active_at": "2026-06-10T00:00:00+09:00",
                                "idle_days": 45,
                                "dm_30d": 0,
                                "page_clicks_30d": 0,
                                "link": {"page": "/users/8911", "params": {}},
                            }
                        ],
                        "recent_churn": [
                            {
                                "user_id": 8921,
                                "email": "e@f.com",
                                "plan": "pro",
                                "plan_display": "프로",
                                "churned_at": "2026-07-20T00:00:00+09:00",
                                "reason": "price",
                                "reason_label": "가격 부담",
                                "tenure_months": 4,
                                "monthly_amount": 14900,
                                "link": {"page": "/users/8921", "params": {}},
                            }
                        ],
                    },
                    "channels": {
                        "rows": [
                            {
                                "channel": "instagram_organic",
                                "visits": 2100,
                                "signups": 90,
                                "signup_rate": 0.0429,
                                "ig_connected": 44,
                                "dm_campaign": 22,
                                "page_created": 38,
                                "page_published": 30,
                                "paid": 7,
                                "free_trial": 11,
                                "paid_rate": 0.0778,
                                "campaigns": [
                                    {
                                        "utm_campaign": "2026_spring",
                                        "utm_content": "banner_a",
                                        "visits": 300,
                                        "signups": 12,
                                        "ig_connected": 7,
                                        "dm_campaign": 4,
                                        "page_created": 5,
                                        "page_published": 4,
                                        "paid": 2,
                                        "free_trial": 3,
                                        "paid_rate": 0.1667,
                                    },
                                    {
                                        "utm_campaign": "",
                                        "utm_content": "",
                                        "visits": 1800,
                                        "signups": 78,
                                        "ig_connected": 37,
                                        "dm_campaign": 18,
                                        "page_created": 33,
                                        "page_published": 26,
                                        "paid": 5,
                                        "free_trial": 8,
                                        "paid_rate": 0.0641,
                                    },
                                ],
                                "campaigns_truncated": False,
                                "referral_overlap": 5,
                            },
                            {
                                "channel": "unknown",
                                "visits": 0,
                                "signups": 35,
                                "signup_rate": None,
                                "ig_connected": 6,
                                "dm_campaign": 3,
                                "page_created": 8,
                                "page_published": 5,
                                "paid": 1,
                                "free_trial": 2,
                                "paid_rate": 0.0286,
                                "campaigns": [
                                    {
                                        "utm_campaign": "",
                                        "utm_content": "",
                                        "visits": 0,
                                        "signups": 35,
                                        "ig_connected": 6,
                                        "dm_campaign": 3,
                                        "page_created": 8,
                                        "page_published": 5,
                                        "paid": 1,
                                        "free_trial": 2,
                                        "paid_rate": 0.0286,
                                    }
                                ],
                                "campaigns_truncated": False,
                                "referral_overlap": 0,
                            },
                        ],
                        "referral_codes": [
                            {
                                "code": "CREATOR10",
                                "description": "A 인플루언서 릴스 협찬",
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
                            "created_breakdown": {"ai": 30, "imported": 12, "manual": 46},
                            "active_users": {"current": 74, "previous": 60, "delta_pct": 23.3},
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
                            "active_users": {"current": 52, "previous": 41, "delta_pct": 26.8},
                            "requested": {"current": 44000, "previous": 36000, "delta_pct": 22.2},
                            "delivered": {"current": 43100, "previous": 35200, "delta_pct": 22.4},
                            "delivery_rate": 0.9925,
                        },
                        "spam": {
                            "active_users": {"current": 18, "previous": 15, "delta_pct": 20.0},
                            "detected": {"current": 3100, "previous": 2500, "delta_pct": 24.0},
                            "hidden": {"current": 2700, "previous": 2200, "delta_pct": 22.7},
                        },
                        "trials": {
                            "started": {"current": 25, "previous": 30, "delta_pct": -16.7},
                            "active": 14,
                            "converted": 6,
                            "conversion_rate": 0.24,
                            "conversion_formula": TRIALS_CONVERSION_FORMULA,
                            "paid_conversion_rate": 0.28,
                            "paid_conversion_formula": TRIALS_PAID_CONVERSION_FORMULA,
                            "ended": 22,
                            "ended_converted": 7,
                            "ended_conversion_rate": 0.3182,
                            "ended_conversion_formula": TRIALS_ENDED_CONVERSION_FORMULA,
                        },
                    },
                    "onboarding_dropoffs": {
                        "cohort_signups": 210,
                        "segments": [
                            {
                                "key": "no_action",
                                "label": "가입 후 무행동",
                                "description": "IG 연동·페이지 생성·DM 캠페인 어느 것도 없음",
                                "count": 61,
                                "available": True,
                                "samples": [
                                    {
                                        "user_id": 903,
                                        "email": "idle@user.com",
                                        "joined_at": "2026-07-09T10:12:00+09:00",
                                        "link": {"page": "/users/903", "params": {}},
                                    }
                                ],
                            },
                            {
                                "key": "page_created_not_published",
                                "label": "페이지 생성 후 미공개",
                                "description": "페이지를 만들었으나 공개(is_public)하지 않음",
                                "count": 18,
                                "available": True,
                                "samples": [],
                            },
                            {
                                "key": "paywall_no_payment",
                                "label": "유료 기능 시도 후 미결제",
                                "description": "유료 제한 모달을 봤으나(paywall_viewed) 결제하지 않음",
                                "count": 0,
                                "available": False,
                                "samples": [],
                            },
                        ],
                    },
                    "paid_conversion_analysis": {
                        "total": 18,
                        "by_plan": [
                            {"name": "pro", "display_name": "프로", "count": 11},
                            {"name": "basic", "display_name": "베이직", "count": 7},
                        ],
                        "paid_plan_no_payment": {
                            "count": 9,
                            "referral_trial": 4,
                            "card_trial": 5,
                            "definition": PAID_PLAN_NO_PAYMENT_DEFINITION,
                        },
                        "post_payment_usage": [
                            {"key": "dm_send", "label": "DM 발송", "users": 9},
                            {"key": "page_created", "label": "페이지 생성", "users": 6},
                            {"key": "spam_used", "label": "스팸 방어 사용", "users": 3},
                            {"key": "extra_ig", "label": "추가 IG 연동", "users": 2},
                        ],
                        "entry_paths": [
                            {"key": "dm_limit", "label": "DM 한도 초과", "count": 6},
                            {"key": "pricing_direct", "label": "가격표 직접 진입", "count": 4},
                            {"key": "badge_removal", "label": "배지 제거", "count": 2},
                        ],
                        "entry_paths_available": True,
                        "post_payment_window_days": 7,
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
            cache_ttl = MARKETING_DASHBOARD_CACHE_TTL
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
            cache_key = CACHE_KEY_TMPL.format(period=period)
            if days is None:  # R-1: 전체 기간 — 비교할 직전 기간이 없음 (prev=None)
                cur = (_service_start(now), now)
                prev = None
                cache_ttl = MARKETING_DASHBOARD_ALL_CACHE_TTL
            else:
                cur = (now - timedelta(days=days), now)
                prev = (now - timedelta(days=days * 2), now - timedelta(days=days))
                cache_ttl = MARKETING_DASHBOARD_CACHE_TTL

        # RBAC-3: 캐시에는 **원본**을 저장하고, 꺼낸 뒤 요청자 역할로 마스킹한다
        # (마스킹본을 캐시에 넣으면 full 역할이 마스킹된 값을 받는다).
        role = resolve_admin_role(request)
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(apply_pii_policy(cached, role=role))

        mrr_breakdown = _mrr_breakdown()
        cohort = _cohort_agg(*cur)
        _visits, unique_visitors_current = _visit_counts(*cur)
        # 코호트 flag_rows/attr_map/referral_users/visits_by_channel 1회 계산 →
        # funnel(채널 variant) 과 channels 양쪽에 재사용 (중복 쿼리 방지).
        cohort_flags = _cohort_flags(*cur)
        channel_variants = _funnel_channel_variants(cohort_flags)

        payload = {
            "period": period,
            "range": {
                "current_start": timezone.localtime(cur[0]).isoformat(),
                "current_end": timezone.localtime(cur[1]).isoformat(),
                # period=all 은 비교 대상이 없어 null (빈 구간을 previous 로 주면 오독됨)
                "previous_start": timezone.localtime(prev[0]).isoformat() if prev else None,
                "previous_end": timezone.localtime(prev[1]).isoformat() if prev else None,
            },
            "generated_at": timezone.localtime(now).isoformat(),
            "attribution_available": ATTRIBUTION_AVAILABLE,
            "snapshot": _snapshot_cached(now),
            "kpis": _kpis(cur, prev, mrr_breakdown["total"]),
            "funnel": _funnel(cohort, unique_visitors_current, channel_variants),
            "trends": _trends(*cur),
            "channels": _channels(*cur, flags=cohort_flags),
            "upsell_candidates": _upsell_candidates(now),
            "feature_stats": _feature_stats(cur, prev),
            "onboarding_dropoffs": _onboarding_dropoffs(*cur),
            "paid_conversion_analysis": _paid_conversion_analysis(cur),
            "subscription_retention": _subscription_retention(cur),
            "cohorts": _cohorts(now),
            "customer_actions": _customer_actions(now),
            "plan_distribution": _plan_distribution(),
            "mrr_breakdown": mrr_breakdown,
        }

        data = AdminMarketingDashboardSerializer(payload).data
        cache.set(cache_key, data, cache_ttl)

        logger.info(
            "[admin-dash-mkt] req=%s period=%s role=%s signups=%s mrr=%s attribution=%s",
            request_id,
            period,
            role,
            cohort["signups"],
            mrr_breakdown["total"],
            ATTRIBUTION_AVAILABLE,
        )
        return Response(apply_pii_policy(data, role=role))
