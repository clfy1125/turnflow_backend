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
  제휴코드 쪽으로 배타 분류 (코드 사용이 가입 이후라 가입 시점 저장 불가).
  퍼널 드롭다운에서는 channel="referral", 채널별 성과 표/추이에서는 **코드별 행**.
- **채널별 성과·추이의 행 단위(MKT-2)**: 파생 채널이 아니라 ``other``(리퍼러 추정 전부를
  접은 1행) / ``link``(저장한 채널 링크 1개) / ``referral_code``(제휴코드 1개) 3종이다.
  UTM 으로 확인된 유입과 리퍼러로 추정한 유입을 같은 층에 두면 추정값(다이렉트)이 확실한
  값을 압도하기 때문. ``trends.by_channel`` 의 키도 같은 ``rows[].key`` 를 쓴다.
  ⚠️ **퍼널 드롭다운(available_channels)만 파생 채널 키를 유지**한다 — 계약이 다르다.
- **기간 매출(MKT-3)**: ``period_revenue`` = gross(결제 시점 귀속) − refunded(환불 시점
  귀속). MRR 은 point-in-time 이라 기간 필터에 반응하지 않아 화면에서 빠졌지만
  ``mrr_breakdown``/``kpis.mrr`` 응답 필드는 하위호환으로 유지한다.
- 업셀 후보의 DM 사용량은 **실제 과금 정의**(billing.dm_limits) 재사용 —
  SENT_FOR_QUOTA_STATUSES + (캠페인 × 수신자) 고유쌍, 캘린더월(_month_bounds).
- ``period=all``(R-1): current = [서비스 최초 가입 시각, now), **직전 기간 없음** —
  ``range.previous_* = null`` + 모든 delta 지표 ``previous/delta_pct = null``
  (빈 구간을 previous 로 넘기면 "직전 0명"으로 오독됨). 캐시 TTL 은 15분.
- ``snapshot``(R-2): 상단 고정 패널 — **기간 파라미터와 무관한 전체 기간 누적**.
  별도 캐시 키(``admin:dash:mkt:snapshot``)라 모든 period 응답이 같은 값을 공유한다.
- 무료체험 집계(R-4)는 **카드 등록 완료**(billing_key_issued_at) 체험만 —
  어드민이 수동 부여한 무카드 계정을 전환 실적에서 제외한다 (퍼널/채널 공통).
- 모든 카운트는 전사(GLOBAL). 응답은 Redis 캐시 (키 ``admin:dash:mkt:{period}``, 60초 —
  ``period=all``/``snapshot`` 은 900초). **방문/가입 적재에는 캐시 무효화 훅이 없다**
  (공개 비콘·가입 경로가 어드민 캐시를 알아선 안 된다) → 신규 유입은 구조적으로 TTL 만큼
  늦게 보인다. 즉시 확인이 필요하면 ``?refresh=1``(full 역할 전용, dashboard_cache
  ``wants_cache_bypass``) — 화면의 '새로고침' 버튼이 실어 보낸다.
"""

from __future__ import annotations

import logging
import re
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

from apps.admin_api.dashboard_cache import (
    CACHE_BYPASS,
    CACHE_HEADER,
    CACHE_HIT,
    CACHE_MISS,
    MKT_CACHE_KEY_CUSTOM_TMPL,
    MKT_CACHE_KEY_SNAPSHOT,
    MKT_CACHE_KEY_TMPL,
    wants_cache_bypass,
)
from apps.admin_api.dashboard_constants import (
    CANCEL_REASONS_TOP,
    CHECKOUT_ATTRIBUTION_WINDOW_DAYS,
    COHORT_MAX_PERIODS,
    COHORT_SUBSCRIPTION_MONTHS,
    COHORT_USAGE_WEEKS,
    CUSTOMER_ACTIONS_LIMIT,
    DORMANT_IDLE_DAYS,
    FUNNEL_LINK_VARIANTS_MAX,
    MARKETING_DASHBOARD_ALL_CACHE_TTL,
    MARKETING_DASHBOARD_CACHE_TTL,
    MARKETING_DASHBOARD_SNAPSHOT_CACHE_TTL,
    ONBOARDING_SAMPLE_LIMIT,
    POST_PAYMENT_WINDOW_DAYS,
    RECENT_CANCELLATIONS_LIMIT,
    RECENT_CHURN_WINDOW_DAYS,
    SNAPSHOT_ROSTER_ID_CACHE_MAX,
    SOURCE_ROWS_MAX,
    TOP_PAGES_LIMIT,
    TRENDS_DAY_MAX_SPAN_DAYS,
    TRENDS_WEEK_MAX_SPAN_DAYS,
    UNSAVED_UTM_COMBOS_LIMIT,
    UPSELL_CANDIDATES_LIMIT,
    UPSELL_CLICKS_HIGH,
    UPSELL_CLICKS_MID,
    UPSELL_DM_RATIO_HIGH,
    UPSELL_DM_RATIO_MID,
    UPSELL_MULTI_IG_MIN,
    UPSELL_SPAM_HEAVY,
)
from apps.admin_api.models import MarketingChannelLink
from apps.admin_api.pii import apply_pii_policy
from apps.admin_api.roles import resolve_admin_role
from apps.admin_api.serializers.dashboard_marketing import AdminMarketingDashboardSerializer
from apps.admin_api.snapshot_rosters import (
    BUCKET_CANCELLED,
    BUCKET_WILL_CHARGE,
    paying_subscriptions_qs,
    trial_cancelled_qs,
    trial_no_card_qs,
    trial_will_charge_qs,
)
from apps.ai_jobs.models import AiJob
from apps.billing.dm_limits import DEFAULT_DM_MONTHLY_LIMIT
from apps.billing.models import (
    EXTRA_IG_ACCOUNT_PRICE,
    DailyPaidCohortSnapshot,
    DailySubscriptionSnapshot,
    PaymentHistory,
    PaymentStatus,
    ReferralCode,
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

# UTM 표준화(NFC·공백) — 모델 의존이 없는 순수 모듈이라 위 guard 와 별개로 임포트한다.
# 앱 자체가 없을 때만 폴백(그 경우 매칭할 방문 데이터도 없다).
try:
    from apps.analytics.utm import normalize_utm
except ImportError:  # pragma: no cover — analytics 앱 미배치

    def normalize_utm(value):
        return (value or "").strip()


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
# T-1: '체험 중 취소'는 종료 후 미결제(ended - ended_converted)와 다르다 — 그냥 만료된
# 회원과 카드 승인 실패 회원이 섞이지 않도록 취소 시점 상태로만 판정한다.
TRIALS_CANCEL_FORMULA = (
    "이 기간에 무료체험을 쓰던 중 구독을 취소한 회원 수 ÷ 이 기간 체험을 시작한 회원 수 × 100\n"
    "같은 회원은 한 번만 계산하며, 취소 시점에 체험 중이었던 경우만 포함합니다. "
    "체험이 그냥 끝난 경우와 결제가 실패한 경우는 포함하지 않습니다."
)

# period → 일수. "all"(R-1) = 서비스 오픈부터 now 까지라 고정 일수가 없어 None,
# 이 경우에만 previous 구간을 만들지 않는다(prev=None → delta 전부 null).
ALLOWED_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "all": None}
PERIOD_ALL = "all"
# 캐시 키는 apps/admin_api/dashboard_cache.py 가 정본 — 채널 링크 CRUD 가 같은 키를 지워야
# 하므로(MKT-11) 뷰 안에 두면 무효화하는 쪽이 알 수 없다. 여기서는 별칭만 유지한다.
CACHE_KEY_TMPL = MKT_CACHE_KEY_TMPL
CACHE_KEY_CUSTOM_TMPL = MKT_CACHE_KEY_CUSTOM_TMPL
# R-2: 기간 무관 고정 패널 — period 별 응답이 공유하는 단일 키 (계산 1회)
CACHE_KEY_SNAPSHOT = MKT_CACHE_KEY_SNAPSHOT
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

# ── MKT-2: 채널 행 분류 (링크/제휴코드 = 우리가 집행한 것, 그 외는 전부 '기타') ──────
# UTM 으로 들어온 것(확실)과 리퍼러로 추정한 것(추정)을 같은 층에 놓으면 추정값이
# 확실한 값을 압도한다(다이렉트가 항상 1위). 그래서 행을 3종으로 나눈다:
#   other(=리퍼러 추정 전부를 접은 1행) / link(저장한 채널 링크 1개) / referral_code(제휴코드 1개)
ROW_KIND_OTHER = "other"
ROW_KIND_LINK = "link"
ROW_KIND_REFERRAL_CODE = "referral_code"
OTHER_ROW_KEY = "other"
OTHER_ROW_LABEL = "기타 (기본 링크로 들어옴)"

# other 행 안의 소스 키 (리퍼러 파생 채널 + 아래 특수 소스들)
SOURCE_BIOLINK = "biolink"
SOURCE_UNSAVED_UTM = "unsaved_utm"
SOURCE_DIRECT = "direct"
# MKT-12: 저장은 됐지만 집계에서 뺀 링크로 들어온 유입. **unsaved_utm 으로 합치지 않는다** —
# ① 라벨이 "저장 안 된 링크"인데 실제로는 저장돼 있어 거짓말이 되고, ② unsaved_utm 의
# combos 에 실려 "이 조합으로 링크 저장" 버튼이 뜨는데 누르면 중복 400 이 난다(링크가 이미
# 있으니까). 자기 줄로 두면 "뺀 인원이 얼마인지"도 펼침에서 보인다.
SOURCE_EXCLUDED_LINK = "excluded_link"
# MKT-15: 집계에서 뺀 **제휴 코드**의 가입자. excluded_link 와 합치지 않는다 — 합치면
# 뺀 것이 링크인지 코드인지 화면에서 구분할 수 없다(둘의 정리 주체·경로가 다르다).
SOURCE_EXCLUDED_CODE = "excluded_code"
# 초과분을 접는 버킷 (SOURCE_ROWS_MAX). 파생 채널 키를 그대로 재사용한다.
SOURCE_OTHER_REFERRAL = "other_referral"
# 접기·판별상 특별 취급하는 소스 — 신호가 크거나(direct) 조치 가능해서(biolink/unsaved_utm/
# excluded_link/excluded_code) 순위와 무관하게 항상 자기 줄을 유지한다.
_SOURCE_NEVER_FOLD = (
    SOURCE_DIRECT,
    SOURCE_BIOLINK,
    SOURCE_UNSAVED_UTM,
    SOURCE_EXCLUDED_LINK,
    SOURCE_EXCLUDED_CODE,
)

# 소스 라벨 — CHANNEL_LABELS 보다 우선. direct 는 채널 표기("다이렉트")로 되돌리지 말 것:
# 실제 의미는 "주소를 직접 입력했다"가 아니라 **리퍼러가 아예 없었다**(앱 내 이동·메신저
# 경유 포함)다. MKT-9 로 문구를 "어디서 왔는지 모름" → **"출처 미상"** 으로 줄였다
# (표에서 다른 줄과 길이 균형이 안 맞았다). 라벨 정본은 서버다.
_SOURCE_LABEL_OVERRIDES = {
    SOURCE_BIOLINK: "고객 바이오링크 페이지",
    SOURCE_UNSAVED_UTM: "저장 안 된 링크(UTM)",
    SOURCE_DIRECT: "출처 미상",
    SOURCE_EXCLUDED_LINK: "집계에서 뺀 링크",
    SOURCE_EXCLUDED_CODE: "집계에서 뺀 제휴 코드",
}

# 고객 바이오링크 페이지(turnflow.link/@slug)의 'Powered by' 배지가 붙이는 UTM.
#
# ⚠️⚠️ **이 문자열의 정본은 고객용 프론트(TurnflowLink) 렌더러다** — 백엔드에도, 어드민
#      프론트에도 없다. 아래 값은 prod 실측(2026-07-28)으로 역추적한 것이고, **판별 주체는
#      여기(백엔드)** 다: utm_source=turnflow_badge & utm_medium=biolink & utm_content=<slug>.
#      렌더러가 UTM 규칙을 바꾸면 **에러 없이** 바이오링크 유입이 '저장 안 된 링크(UTM)'
#      줄로 흘러든다(조용한 오분류). 세 저장소 어디에도 정본이 없는 결합이라, 어드민 프론트
#      `src/lib/channels.ts` 에도 같은 경고가 있다 — 한쪽만 고치지 말 것.
#      같은 이유로 이 조합을 쓰는 채널 링크는 저장해도 영구히 0 이다(바이오링크 판정이
#      링크 매칭보다 우선하므로) — 어드민 프론트의 CHANNEL_PRESETS 추가 금지와 짝이다.
_BIOLINK_UTM_SOURCES = {"turnflow_badge"}
_BIOLINK_UTM_MEDIUMS = {"biolink"}
# 배지가 UTM 을 잃어도 잡히도록 하는 2차 신호 — 자기 도메인 + /@slug 경로 리퍼러.
# (derive_channel 은 자기 도메인 리퍼러를 버려 direct 로 만들기 때문에 여기서 따로 본다.)
_BIOLINK_REFERRER_RE = re.compile(r"^https?://[^/]*turnflow\.link/@", re.I)

# 방문 원본 행을 파이썬으로 버킷팅한다(정확한 고유 방문자 계산). 이 수를 넘으면 경고 —
# 집계를 SQL 그룹바이로 옮길 시점 (prod 2026-07-28 기준 139행).
VISIT_ROWS_WARN = 300_000


def _norm(value: str) -> str:
    """UTM 값 매칭 정규화 — NFC + 공백류 축약(normalize_utm) + 소문자. None/빈 문자열은 "".

    ⚠️ 반드시 ``analytics.channels.normalize_utm`` 을 통과시킬 것 — 한글 UTM 은 저장 경로에
    따라 NFC/NFD 가 섞일 수 있고(macOS·iOS 복붙), 그러면 **화면상 완전히 같은 두 값**이
    다른 문자열로 취급돼 저장 링크 매칭이 조용히 실패한다. 여기서 정규화하면 정규화 이전에
    적재된 과거 행도 함께 구제된다(읽기 시점 정규화).
    """
    return normalize_utm(value).lower()


def _utm_key(source: str, medium: str, campaign: str, content: str) -> tuple:
    """저장 링크 매칭 키 — (source, medium, campaign, content) 4개 완전일치.

    저장 시점(MarketingChannelLink)과 방문 시점(LandingVisit/SignupAttribution)이
    **같은 정규화**를 거쳐야 하므로 반드시 이 함수를 쓸 것. null 과 "" 는 같게 본다.
    """
    return (_norm(source), _norm(medium), _norm(campaign), _norm(content))


def _is_biolink(source: str, medium: str, referrer: str) -> bool:
    """고객 바이오링크 페이지를 거쳐 들어온 유입인가 (우리가 집행한 채널이 아님).

    UTM 이 있어도 바이오링크 경유면 '기타'로 접는다 — 배지 링크가 UTM 을 달고 오기
    때문에 UTM 유무로는 구분되지 않는다.
    """
    return (
        _norm(source) in _BIOLINK_UTM_SOURCES
        or _norm(medium) in _BIOLINK_UTM_MEDIUMS
        or bool(referrer and _BIOLINK_REFERRER_RE.match(referrer.strip()))
    )


def _resolve_row_key(source, medium, campaign, content, referrer, channel, link_index) -> tuple:
    """유입 1건 → (row_key, source_key). source_key 는 other 행일 때만 non-null.

    판정 순서 (앞이 이김):
      1. 바이오링크 경유          → other / biolink
      2. UTM 있음 + 저장 링크 일치 → 그 링크 행
      2b. 그 링크가 집계 제외      → other / excluded_link  (MKT-12 — 행은 없애고 인원은 흡수)
      3. UTM 있음 + 미매칭        → other / unsaved_utm  ("링크를 저장 안 하고 쓰는 중" 신호)
      4. UTM 없음                → other / 저장된 파생 채널(리퍼러 추정)
    제휴코드 오버레이는 **가입자에게만** 적용되며 호출부에서 먼저 처리한다.

    ⚠️ 저장된 channel 이 ``unknown``(파생 실패)인 경우는 ``direct`` 로 접는다 — 둘 다
    "출처 미상"이라 라벨이 같은 줄이 둘 생기는 화면 결함이 된다 (MKT-6 ②).
    **귀속 행 자체가 없는 회원은 여기 오지 않는다** — 채널 분해에서 아예 빼고
    ``attribution_gap`` 으로 따로 센다 (MKT-10).
    """
    if _is_biolink(source, medium, referrer):
        return OTHER_ROW_KEY, SOURCE_BIOLINK
    key = _utm_key(source, medium, campaign, content)
    if any(key):
        link_pk = link_index.get(key)
        if link_pk is not None:
            return str(link_pk), None
        # MKT-12: 값이 None 이면서 키가 있는 경우 = **저장은 됐지만 집계 제외된 링크**.
        # 없는 키(get→None)와 구분해야 하므로 `in` 으로 다시 본다.
        if key in link_index:
            return OTHER_ROW_KEY, SOURCE_EXCLUDED_LINK
        return OTHER_ROW_KEY, SOURCE_UNSAVED_UTM
    if not channel or channel == "unknown":
        return OTHER_ROW_KEY, SOURCE_DIRECT
    return OTHER_ROW_KEY, channel


def _link_index(links) -> dict:
    """[MarketingChannelLink] → {4-튜플: pk | None}. ``None`` = **집계 제외된 링크**(MKT-12).

    키를 지우지 않고 None 을 넣는 이유: 지우면 그 유입이 '저장 안 된 링크(UTM)'로 흘러가
    라벨이 거짓이 되고 combos 의 "이 조합으로 링크 저장"이 중복 400 을 낸다. 키를 남겨
    ``_resolve_row_key`` 가 전용 소스로 보낸다.

    같은 조합이 둘이면 **집계에 포함된 링크가 먼저**, 그 다음 **먼저 만든 링크**가 이긴다.
    저장 시 중복 조합은 시리얼라이저가 400 으로 막지만(serializers.marketing) 그 검증
    이전 데이터가 있을 수 있고, 활성 링크를 우선하지 않으면 **동일 조합의 다른 링크를
    제외했을 때 살아있는 링크의 행이 0 이 되는** 놀라운 동작이 된다.
    """
    index: dict = {}
    for link in sorted(links, key=lambda x: (x.excluded_from_stats, x.created_at, x.pk)):
        index.setdefault(
            _utm_key(link.utm_source, link.utm_medium, link.utm_campaign, link.utm_content),
            None if link.excluded_from_stats else link.pk,
        )
    return index


def _source_label(key: str) -> str:
    return _SOURCE_LABEL_OVERRIDES.get(key) or CHANNEL_LABELS.get(key, key)


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


def _empty_trend_gap() -> dict:
    """추이 버킷의 '귀속 기록 없음' 슬롯 (MKT-10 의 추이 판 — Q-B).

    visits 는 없다 — 방문은 행 자체(UTM·리퍼러)로 판정하므로 '귀속 기록 없음'이라는
    개념이 성립하지 않는다. 사람 단위 3지표만 공백을 가진다.
    """
    return {"signups": 0, "activated": 0, "paid": 0}


def _trend_row_key_of(uid, attr_row: dict, referral_users: dict) -> str | None:
    """trends 막대의 채널 층 키 — **채널별 성과 표의 rows[].key 와 동일**(MKT-2).

    표는 링크 단위인데 그래프만 파생 채널 단위면 한 화면에 두 분류가 공존한다.
    제휴코드 사용자는 코드 행으로(표와 같은 배타적 오버레이), 나머지는 attr_row 가
    이미 계산해 둔 (row_key, source) 의 row_key 를 쓴다.

    **None = 귀속 기록 없음**(MKT-10 / Q-B). 예전엔 other 로 접었지만, 그러면 같은
    ``other`` 키가 표·퍼널(제외)과 추이(포함)에서 다른 인원을 뜻한다 → 버킷의
    ``unattributed`` 로 빼고 by_channel 에서 제외한다. 코드 사용자는 귀속 행이 없어도
    공백이 아니다(코드 자체가 유입 경로) — 판정 순서를 바꾸지 말 것.

    MKT-15: 코드가 집계 제외면 ``other`` 다(공백이 아니다). 표에서 그 사람들을
    other/excluded_code 로 흡수하므로 추이도 같은 행으로 가야 한다.
    """
    code = referral_users.get(uid)
    if code:
        return code
    if uid in referral_users:  # 집계 제외 코드 — 표와 같이 other 로
        return OTHER_ROW_KEY
    bucket = attr_row.get(uid)
    return bucket[0] if bucket is not None else None


def _trends(start, end, visit_rows: list[tuple] | None = None) -> dict:
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
    - by_channel(Q-1 → MKT-2): visits/signups/activated/paid 4지표의 분해. **키는
      channels.rows[].key 와 동일**(other / 링크 pk / 제휴코드) — 라벨은 프론트가 rows 에서
      찾는다(단일 소스). 전부 0인 키는 생략.
    - unattributed(MKT-10 / Q-B): 귀속 기록이 없어 by_channel 에서 **제외된** 인원.
      표·퍼널과 같은 모집단을 쓰려면 추이도 빼야 한다 — 안 빼면 같은 ``other`` 키가
      카드마다 다른 인원을 뜻한다. 버킷 총량에는 **그대로 남는다**:
      ``Σby_channel[m] + unattributed[m] == 버킷[m]`` (m = signups/activated/paid),
      ``Σby_channel.visits == 버킷 visits``(방문은 공백 개념 없음 — 등식 그대로).
    """
    tz = timezone.get_current_timezone()
    granularity = _trends_granularity(start, end)
    if visit_rows is None:
        visit_rows = _visit_rows(start, end)

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

    # visits — 총량(세션) + 행 키 분해 {(버킷, row_key): n}. 채널별 성과 표와 같은
    # _resolve_row_key 를 쓰므로 그래프의 층과 표의 행이 정확히 대응한다 (MKT-2).
    visits: Counter = Counter()
    visits_by_bucket_row: Counter = Counter()
    link_index = _link_index(MarketingChannelLink.objects.all())
    for _vid, src, med, camp, content, referrer, channel, created in visit_rows:
        if created is None:
            continue
        bucket = _bk(timezone.localtime(created, tz).date())
        row_key, _source = _resolve_row_key(src, med, camp, content, referrer, channel, link_index)
        visits[bucket] += 1
        visits_by_bucket_row[(bucket, row_key)] += 1

    # 유저 행 귀속 맵 — 관련 유저만 조회 (signups + activated + paid)
    involved = {uid for uid, _b in signup_rows} | set(first_paid_map)
    for users in activated_by_bucket.values():
        involved |= users
    attr_row: dict = {}
    referral_users: dict = {}
    if ATTRIBUTION_AVAILABLE and involved:
        for uid, channel, src, med, camp, content, referrer in SignupAttribution.objects.filter(
            user_id__in=involved
        ).values_list(
            "user_id",
            "channel",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "referrer",
        ):
            attr_row[uid] = _resolve_row_key(src, med, camp, content, referrer, channel, link_index)
        referral_users = _referral_user_map(involved)

    # (버킷, row_key) 슬라이스 집계. 귀속 기록 없는 회원(row_key=None)은 by_channel 에서
    # 빼고 버킷의 unattributed 로 모은다 (MKT-10 을 추이에도 적용 — Q-B).
    # 항등: Σby_channel[m] + unattributed[m] == 버킷 총량[m] (m = signups/activated/paid),
    #       Σby_channel.visits == 버킷 visits (방문은 행 자체로 판정 — 공백 개념 없음).
    slice_key = defaultdict(lambda: {"visits": 0, "signups": 0, "activated": 0, "paid": 0})
    unattributed: dict = defaultdict(_empty_trend_gap)
    for (bucket, row_key), n in visits_by_bucket_row.items():
        slice_key[(bucket, row_key)]["visits"] += n

    def _put(bucket, uid, metric: str) -> None:
        row_key = _trend_row_key_of(uid, attr_row, referral_users)
        if row_key is None:
            unattributed[bucket][metric] += 1
            return
        slice_key[(bucket, row_key)][metric] += 1

    for uid, bucket in signup_rows:
        _put(bucket, uid, "signups")
    for bucket, users in activated_by_bucket.items():
        for uid in users:
            _put(bucket, uid, "activated")
    for uid, bucket in first_paid_map.items():
        _put(bucket, uid, "paid")

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
                "unattributed": unattributed.get(cur) or _empty_trend_gap(),
            }
        )
        cur = _bucket_next(cur, granularity)
    return {"granularity": granularity, "buckets": buckets}


def _visit_rows(start, end) -> list[tuple]:
    """기간 내 방문 원본 행 — (visitor_id, source, medium, campaign, content, referrer,
    channel, created_at).

    MKT-2 이후 방문의 행 귀속(link/other-source)은 UTM 4-튜플 + 리퍼러까지 봐야 정해지고,
    ``visits`` 는 **고유 방문자** 수라 버킷별로 집합을 만들어야 정확하다. 그래서 SQL
    group-by 대신 원본 행을 한 번 읽어 채널 표(_channels)와 추이(_trends)가 함께 쓴다.
    행 수가 VISIT_ROWS_WARN 을 넘으면 경고 — SQL 집계로 옮길 시점이다.
    """
    if not ATTRIBUTION_AVAILABLE:
        return []
    rows = list(
        LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end).values_list(
            "visitor_id",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "referrer",
            "channel",
            "created_at",
        )
    )
    if len(rows) > VISIT_ROWS_WARN:
        logger.warning(
            "[admin-dash-mkt] LandingVisit %s rows > %s — 채널 집계 SQL 전환 검토 필요",
            len(rows),
            VISIT_ROWS_WARN,
        )
    return rows


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

# 채널별 퍼널 counts 의 0 초기값 — _cohort_agg 와 같은 축(키 셋)이어야 한다.
# 직전 기간에 존재하지 않던 채널의 previous 로도 쓴다 (MKT-1: 그 채널로 0명이었던 게 맞음).
_EMPTY_FUNNEL_COUNTS: dict = {
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


def _funnel_node(
    key: str, count: int, numer: int, denom: int, rate_of, formula, previous: int | None = None
) -> dict:
    """퍼널 노드 1개 — {key, label, count, rate, rate_of, formula, previous, delta_pct}.

    rate = numer/denom (denom 0 → null). rate_of = 분모 노드 key(또는 null),
    formula = 한국어 정의 문자열 (M-6 — 모든 노드에서 non-null, 프론트 툴팁 정본).

    MKT-1(R-8): 노드가 **자기 증감**을 들고 있다 — previous 는 직전 동일 기간의 같은
    집계라 배지와 바로 위 숫자가 어긋날 수 없다. kpis 로 대신 채우면 모집단이 달라진다
    (kpis.paid_conversions=실결제만 vs conversion 노드=체험+실결제).
    previous is None(= period=all, 비교 기간 없음) → previous/delta_pct 모두 null (R-1).
    previous == 0 → delta_pct null (÷0).
    """
    return {
        "key": key,
        "label": _FUNNEL_NODE_LABELS[key],
        "count": count,
        "rate": _rate(numer, denom),
        "rate_of": rate_of,
        "formula": formula,
        "previous": previous,
        "delta_pct": (round((count - previous) / previous * 100, 1) if previous else None),
    }


def _prev_plan_total(prev_counts: dict | None) -> int | None:
    """직전 기간의 '유료플랜 전환' 인원 = paid + trial_only (conversion.count 와 동일 정의)."""
    if prev_counts is None:
        return None
    return prev_counts.get("paid", 0) + prev_counts.get("trial_only", 0)


def _build_funnel_variant(
    counts: dict,
    visitors: int,
    prev_counts: dict | None = None,
    prev_visitors: int | None = None,
) -> dict:
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

    MKT-1(R-8): prev_counts/prev_visitors 를 주면 4개 노드(visit/signup/activation/
    conversion) 모두 previous·delta_pct 가 채워진다. prev 가 없으면(period=all) 전부 null.
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
    #
    # FMT-1 작성 기준 — 이 문구는 **외주 마케팅 파트너가 툴팁에서 그대로 읽는다**:
    #   · 완전한 문장 + 마침표. `÷` `×` 외의 기호나 `·` 나열 금지.
    #   · 내부 용어 금지 — "가입 코호트"→"이 기간에 가입한 회원", "고유 방문자"→
    #     "사이트를 방문한 사람 수", "중복 제거"→"같은 회원은 한 번만 계산합니다",
    #     "도달 여부는 현재까지 기준"→"가입 이후 현재까지의 활동을 기준으로 합니다".
    #   · 사람 단위 명칭은 **회원**으로 통일(유저·고객·사람 혼용 금지).
    #   · **마크다운 강조 금지** — 툴팁은 평문 렌더라 `**` 가 그대로 보인다.
    # 두 번째 문장은 줄바꿈(\n)으로 잇는다.
    head = [
        _funnel_node(
            "visit",
            visitors,
            0,
            0,
            None,
            "이 기간에 사이트를 방문한 사람 수입니다.\n"
            "같은 사람이 여러 번 방문해도 한 명으로 계산하며, 브라우저 단위로 구분합니다.",
            prev_visitors,
        ),
        _funnel_node(
            "signup",
            signups,
            signups,
            visitors,
            "visit",
            "이 기간에 가입한 회원 수 ÷ 사이트를 방문한 사람 수 × 100\n"
            "같은 사람이 여러 번 방문해도 한 명으로 계산합니다.",
            prev_counts["signups"] if prev_counts is not None else None,
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
                    "이 기간에 가입한 회원 중 인스타그램 계정을 연동한 회원의 비율입니다.\n"
                    "같은 회원은 한 번만 계산하며, 가입 이후 현재까지의 활동을 기준으로 합니다.",
                ),
                _funnel_node(
                    "dm_campaign",
                    dm,
                    dm,
                    ig,
                    "ig_connected",
                    "인스타그램 계정을 연동한 회원 중 DM 캠페인을 하나 이상 만든 회원의 "
                    "비율입니다.\n"
                    "같은 회원은 한 번만 계산하며, 가입 이후 현재까지의 활동을 기준으로 합니다.",
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
                    "이 기간에 가입한 회원 중 페이지를 만든 회원의 비율입니다. 공개하지 "
                    "않은 페이지도 포함합니다.\n"
                    "같은 회원은 한 번만 계산하며, 가입 이후 현재까지의 활동을 기준으로 합니다.",
                ),
                _funnel_node(
                    "page_published",
                    page,
                    page,
                    page_created,
                    "page_created",
                    "페이지를 만든 회원 중 페이지를 공개한 회원의 비율입니다.\n"
                    "같은 회원은 한 번만 계산하며, 가입 이후 현재까지의 활동을 기준으로 합니다.",
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
        "이 기간에 가입한 회원 중 DM 캠페인을 하나 이상 만들었거나, 페이지를 공개한 "
        "회원의 비율입니다.\n"
        "같은 회원은 한 번만 계산하며, 가입 이후 현재까지의 활동을 기준으로 합니다.",
        prev_counts.get("activated", 0) if prev_counts is not None else None,
    )
    conversion = _funnel_node(
        "paid",
        plan_total,
        plan_total,
        activated,
        "activated",
        "활성화된 회원 중 무료체험을 시작했거나 실제 결제를 완료한 회원의 비율입니다.\n"
        "무료체험은 카드 등록을 완료한 경우만 포함하며, 관리자가 직접 부여한 무료체험은 "
        "제외합니다. 실제 결제는 Toss에서 결제 완료된 건만 포함합니다.",
        _prev_plan_total(prev_counts),
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


def _funnel(
    all_counts: dict,
    visitors_all: int,
    channel_variants: list[tuple],
    prev_all_counts: dict | None = None,
    prev_visitors_all: int | None = None,
    prev_channel_map: dict | None = None,
    codes: frozenset = frozenset(),
) -> dict:
    """가입 코호트 분기 퍼널 — variants.all + 채널별 variant (드롭다운용, 미리 계산).

    - all_counts: _cohort_agg(*cur) (signups/ig_connected/page_published/dm_campaign/paid 사용)
    - visitors_all: 전체 현재 기간 고유 방문자 수 (distinct visitor_id)
    - channel_variants: [(row_key, label, counts, visitors), ...] signups desc 정렬됨.
      비어 있으면(어트리뷰션 미탑재) available_channels 는 all 만.
    - prev_*: MKT-1(R-8) 직전 기간 값. prev_channel_map = {row_key: (counts, visitors)}.
      직전 기간에 없던 행은 previous=0(그 경로로 0명이었던 게 맞음), period=all 이면
      셋 다 None → 전 노드 previous/delta_pct = null.

    **MKT-4**: value 는 ``channels.rows[].key``, ``label`` 은 서버가 함께 준다(링크 이름·
    제휴코드는 프론트 사전에 없다). variant 1개가 노드·분기·breakdown 을 통째로 들고 있어
    **저장 링크는 가입 상위 FUNNEL_LINK_VARIANTS_MAX 개만** 싣는다 — 잘리면
    ``available_channels_truncated=true``. other/제휴코드는 캡 대상이 아니다.
    배열 순서는 계약이 아니다(프론트가 rows 에 조인해 재정렬한다).
    """
    signups = all_counts["signups"]
    if signups > COHORT_SNAPSHOT_WARN_ROWS:
        logger.warning(
            "[admin-dash-mkt] cohort %s rows > %s — 스냅샷 테이블 전환 검토 필요",
            signups,
            COHORT_SNAPSHOT_WARN_ROWS,
        )

    has_prev = prev_all_counts is not None
    available_channels = [{"value": "all", "label": "전체 채널"}]
    variants = {
        "all": _build_funnel_variant(all_counts, visitors_all, prev_all_counts, prev_visitors_all)
    }
    # 저장 링크만 캡 대상 (other/제휴코드는 개수가 유계이고 운영상 항상 필요)
    link_seen = 0
    truncated = False
    for key, label, counts, visitors in channel_variants:
        if key != OTHER_ROW_KEY and key not in codes:
            link_seen += 1
            if link_seen > FUNNEL_LINK_VARIANTS_MAX:
                truncated = True
                continue
        available_channels.append({"value": key, "label": label})
        prev_counts, prev_visitors = (None, None)
        if has_prev:
            prev_counts, prev_visitors = (prev_channel_map or {}).get(
                key, (_EMPTY_FUNNEL_COUNTS, 0)
            )
        variants[key] = _build_funnel_variant(counts, visitors, prev_counts, prev_visitors)

    return {
        "semantics": "signup_cohort",
        "available_channels": available_channels,
        "available_channels_truncated": truncated,
        "variants": variants,
    }


# ── 채널 ─────────────────────────────────────────────────────────────


def _referral_user_map(user_ids) -> dict:
    """{user_id: 코드 문자열 | None}. ``None`` = **집계에서 뺀 코드**(MKT-15).

    키를 지우지 않고 None 을 넣는 이유는 ``_link_index`` 와 같다 — 세 가지 상태를
    구분해야 하기 때문이다:

    ==========================  ==================  ==========================
    상태                        판정                 귀속
    ==========================  ==================  ==========================
    코드 없음                   ``uid not in map``   저장 채널(attr_row)
    코드 사용                   ``map[uid]`` truthy  그 코드 행
    코드 사용 + 집계 제외       값이 None            other / excluded_code
    ==========================  ==================  ==========================

    ⚠️ ``uid in referral_users`` 멤버십 판정이 **제외된 코드에도 True 로 남아야** 한다 —
    ``_attribution_gap`` 이 이 멤버십으로 "코드 사용자는 계측 공백이 아니다"를 판정하므로,
    키를 지우면 코드를 집계에서 뺀 순간 그 사람들이 갑자기 '귀속 기록 없음'으로 넘어가
    ``signups_unattributed`` 가 부풀고 화면의 경고 줄이 잘못 뜬다.
    """
    return {
        uid: (None if excluded else code)
        for uid, code, excluded in ReferralRedemption.objects.filter(
            user_id__in=user_ids
        ).values_list("user_id", "referral_code__code", "referral_code__excluded_from_stats")
    }


def _cohort_flags(start, end) -> tuple:
    """가입 코호트 flag_rows + 어트리뷰션 맵 (채널/퍼널 공용, 중복 쿼리 방지).

    반환: (flag_rows, attr_map, attr_utm, referral_users, visits_by_channel, attr_row)
    - flag_rows: [(id, ig, pgc, pg, cp, pd, cur_plan, cur_status, cur_card), ...]
      (_cohort_qs values_list) pgc=페이지 생성(공개 무관), pg=페이지 공개, pd=실결제,
      cur_* = 현재 구독의 플랜명/상태/빌링키 발급 시각 (체험 판정은 _trial_flags).
    - attr_map: {user_id: channel} (SignupAttribution)
    - attr_utm: {user_id: (utm_source, utm_medium, utm_campaign, utm_content)} —
      MKT-5 저장 안 된 UTM 조합 분해용 (원문, 정규화 전)
    - referral_users: {user_id: 제휴코드 문자열 | None} (ReferralRedemption 보유 — 조회 시점
      오버레이. `uid in referral_users` 멤버십 판정 + Q-4 referral 채널 코드 단위 분해 겸용).
      **None = 집계에서 뺀 코드**(MKT-15) — 자세한 규약은 :func:`_referral_user_map`
    - visits_by_channel: {channel: 고유 방문자 수(distinct visitor_id)} — 세션 수 아님
    - attr_row: {user_id: (row_key, source_key)} — MKT-2 채널 **행** 귀속
      (link / other+소스). 제휴코드 오버레이는 미적용 — 호출부가 먼저 본다.
    어트리뷰션 미탑재 시 전부 빈 값.
    """
    if not ATTRIBUTION_AVAILABLE:
        return [], {}, {}, {}, {}, {}
    flag_rows = list(
        _cohort_qs(start, end).values_list(
            "id", "ig", "pgc", "pg", "cp", "pd", "cur_plan", "cur_status", "cur_card"
        )
    )
    user_ids = [r[0] for r in flag_rows]
    link_index = _link_index(MarketingChannelLink.objects.all())
    attr_map: dict = {}
    attr_utm: dict = {}
    attr_row: dict = {}
    for uid, channel, src, med, camp, content, referrer in SignupAttribution.objects.filter(
        user_id__in=user_ids
    ).values_list(
        "user_id",
        "channel",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "referrer",
    ):
        attr_map[uid] = channel
        attr_utm[uid] = (src or "", med or "", camp or "", content or "")
        attr_row[uid] = _resolve_row_key(src, med, camp, content, referrer, channel, link_index)
    referral_users = _referral_user_map(user_ids)
    visits_by_channel = {
        r["channel"]: r["v"]
        for r in LandingVisit.objects.filter(created_at__gte=start, created_at__lt=end)
        .values("channel")
        .annotate(v=Count("visitor_id", distinct=True))
    }
    return flag_rows, attr_map, attr_utm, referral_users, visits_by_channel, attr_row


def _channel_of(uid, attr_map: dict, referral_users) -> str:
    """유저 채널 판정 — ReferralRedemption 보유는 저장 채널과 무관하게 'referral' 오버레이.

    referral_users 는 {user_id: 코드} dict (멤버십 판정만 사용 — set 처럼 동작).
    """
    return "referral" if uid in referral_users else attr_map.get(uid, "unknown")


def _row_labels(links, codes) -> dict:
    """row_key → 화면 표시명. 퍼널 드롭다운과 채널 표가 **같은 이름**을 쓰게 하는 단일 소스.

    키가 링크 pk·제휴코드가 되면서 이름은 사람이 붙인 값이 됐다 — 프론트 사전에는 없고
    서버만이 정본이라 ``available_channels[].label`` 을 반드시 함께 내려야 한다 (MKT-4).
    """
    labels = {OTHER_ROW_KEY: OTHER_ROW_LABEL}
    labels.update({str(link.pk): link.name for link in links})
    labels.update({code: code for code in codes})
    return labels


def _funnel_channel_variants(flags: tuple, visit_rows: list[tuple]) -> list[tuple]:
    """행별 퍼널 counts 집계 → [(row_key, label, counts, visitors), ...] (signups desc).

    counts keys 는 _cohort_agg 와 동일 축 (activated/both/플랜별 분해 포함) —
    _build_funnel_variant 가 all variant 와 같은 구조를 만들 수 있어야 한다 (R-3/R-4).

    **MKT-4: 키는 channels.rows[].key 와 같다** (other / 링크 pk / 제휴코드).
    예전엔 파생 채널 키였는데, 같은 화면 위아래에 두 분류가 공존해 드롭다운에서 고른
    이름이 아래 표에 없는 상태였다. 채널 표와 같은 ``_resolve_row_key`` 를 쓴다.
    가입 0인 행은 제외 — 고르면 전부 0인 빈 퍼널이라 고를 이유가 없다.
    """
    flag_rows, _attr_map, _attr_utm, referral_users, _visits_by_channel, attr_row = flags
    links = list(MarketingChannelLink.objects.all())
    link_index = _link_index(links)
    visitors_by_row, _by_source = _visitors_by_bucket(visit_rows, link_index)
    labels = _row_labels(links, ReferralCode.objects.values_list("code", flat=True))

    per_channel: dict = defaultdict(lambda: dict(_EMPTY_FUNNEL_COUNTS))
    for uid, ig, pgc, pg, cp, pd, cur_plan, cur_status, cur_card in flag_rows:
        tr, tr_no_card = _trial_flags(cur_plan, cur_status, cur_card)
        code = referral_users.get(uid)
        bucket = attr_row.get(uid)
        has_code = uid in referral_users
        if not has_code and bucket is None:
            continue  # MKT-10: 귀속 기록 없는 회원은 채널 variant 에서도 제외 (표와 동일 모집단)
        # MKT-15: 집계 제외 코드(has_code 지만 code is None)는 other — 표와 같은 귀속.
        channel = code or (OTHER_ROW_KEY if has_code else bucket[0])
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
        (key, labels.get(key, key), counts, len(visitors_by_row.get(key, ())))
        for key, counts in per_channel.items()
        if counts["signups"] > 0
    ]
    variants.sort(key=lambda t: (-t[2]["signups"], t[0]))
    return variants


# 성과 축의 '가입자 수 계열' 필드 (signups 제외) — 전부 **사람 1명 = 한 곳** 배타 집계라
# Σsources == other 가 성립한다. 겹치는 것은 visits 뿐 (MKT-8).
_PERF_COUNT_FIELDS = (
    "ig_connected",
    "dm_campaign",
    "page_created",
    "page_published",
    "paid",
    "free_trial",
)


def _empty_perf_slot() -> dict:
    """채널 행/소스의 성과 카운터 0 초기값 (행·소스·링크 전부 같은 축)."""
    return {"signups": 0, **{f: 0 for f in _PERF_COUNT_FIELDS}}


def _accumulate_perf(slot: dict, ig, pgc, pg, cp, pd, trialing) -> None:
    """가입자 1명의 단계 도달을 슬롯에 누적 (행/소스/링크 공용 — 정의 복제 방지)."""
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
    if trialing and not pd:
        slot["free_trial"] += 1


def _perf_payload(slot: dict, visits: int | None) -> dict:
    """슬롯 + 방문 수 → 행/소스 공통 지표 dict.

    visits=None(제휴코드 행)이면 signup_rate 도 null — 코드는 URL 에 실려 오는 값이
    아니라 결제 화면 입력값이라 '방문'이 존재하지 않는다. 0 으로 주면 화면에
    "방문 0명인데 가입 12명"이라는 불가능한 행이 된다.
    """
    return {
        "visits": visits,
        "signups": slot["signups"],
        "signup_rate": (_rate(slot["signups"], visits) if visits is not None else None),
        "ig_connected": slot["ig_connected"],
        "dm_campaign": slot["dm_campaign"],
        "page_created": slot["page_created"],
        "page_published": slot["page_published"],
        "paid": slot["paid"],
        "free_trial": slot["free_trial"],
        "paid_rate": _rate(slot["paid"], slot["signups"]),
    }


def _visitors_by_bucket(visit_rows: list[tuple], link_index: dict) -> tuple[dict, dict]:
    """방문 원본 행 → ({row_key: 방문자 집합}, {source_key: 방문자 집합}).

    합산이 아니라 **집합**이라 각 버킷의 고유 방문자 수가 정확하다. 대신 한 방문자가
    두 소스로 들어오면 양쪽에 들어가므로 Σsources.visits ≥ other.visits 다 (MKT-8).
    채널 표(_channel_rows)와 퍼널 variant(_funnel_channel_variants)가 같은 결과를 공유한다.
    """
    by_row: dict = defaultdict(set)
    by_source: dict = defaultdict(set)
    for vid, src, med, camp, content, referrer, channel, _created in visit_rows:
        row_key, source_key = _resolve_row_key(
            src, med, camp, content, referrer, channel, link_index
        )
        by_row[row_key].add(vid)
        if source_key is not None:
            by_source[source_key].add(vid)
    return by_row, by_source


def _unsaved_utm_combos(visit_rows: list[tuple], link_index: dict, signups_by_combo: dict) -> tuple:
    """'저장 안 된 링크(UTM)' 조합 목록 (MKT-5) — (combos, truncated).

    합계만 있으면 "저장 안 된 링크로 40명 들어왔다"에서 끝나고 할 수 있는 일이 없다.
    조합이 보이면 그 자리에서 링크로 저장할 수 있다 → 다음 집계부터 자기 행으로 올라온다.

    - 묶음 키는 **정규화된** 4-튜플(대소문자·공백 변형이 한 줄로 합쳐진다).
    - 응답의 ``utm`` 은 **정규화 전 원문** — 저장 화면에 그대로 실어 보내야 하므로
      처음 본 방문의 원문을 유지한다.
    - visits = 고유 방문자, first/last_seen = 그 조합의 방문 시각 범위.
    """
    buckets: dict = {}
    for vid, src, med, camp, content, referrer, channel, created in visit_rows:
        row_key, source_key = _resolve_row_key(
            src, med, camp, content, referrer, channel, link_index
        )
        if source_key != SOURCE_UNSAVED_UTM:
            continue
        norm = _utm_key(src, med, camp, content)
        slot = buckets.get(norm)
        if slot is None:
            slot = buckets[norm] = {
                "utm": {
                    "source": src or "",
                    "medium": med or "",
                    "campaign": camp or "",
                    "content": content or "",
                },
                "visitors": set(),
                "first_seen": created,
                "last_seen": created,
            }
        slot["visitors"].add(vid)
        if created is not None:
            if slot["first_seen"] is None or created < slot["first_seen"]:
                slot["first_seen"] = created
            if slot["last_seen"] is None or created > slot["last_seen"]:
                slot["last_seen"] = created
    # 방문이 없고 가입만 있는 조합(랜딩 미경유 가입)도 줄이 나와야 저장할 수 있다
    for norm, agg in signups_by_combo.items():
        slot = buckets.get(norm)
        if slot is None:
            buckets[norm] = {
                "utm": agg["utm"],
                "visitors": set(),
                "first_seen": None,
                "last_seen": None,
            }

    combos = [
        {
            "utm": slot["utm"],
            "visits": len(slot["visitors"]),
            "signups": signups_by_combo.get(norm, {}).get("signups", 0),
            "paid": signups_by_combo.get(norm, {}).get("paid", 0),
            "first_seen": slot["first_seen"],
            "last_seen": slot["last_seen"],
        }
        for norm, slot in buckets.items()
    ]
    combos.sort(key=lambda c: (-c["visits"], -c["signups"], c["utm"]["source"]))
    return combos[:UNSAVED_UTM_COMBOS_LIMIT], len(combos) > UNSAVED_UTM_COMBOS_LIMIT


def _fold_sources(sources: list[dict]) -> list[dict]:
    """소스 줄이 너무 많으면 하위를 ``other_referral`` 로 접는다 (MKT-6 ①).

    방문 desc 로 정렬한 뒤 상한을 넘는 꼬리만 합친다. direct/biolink/unsaved_utm 은
    신호가 크거나 조치 가능한 줄이라 순위와 무관하게 접지 않는다.
    """
    if len(sources) <= SOURCE_ROWS_MAX:
        return sources
    keep, fold = [], []
    for src in sources:
        if len(keep) < SOURCE_ROWS_MAX or src["key"] in _SOURCE_NEVER_FOLD:
            keep.append(src)
        else:
            fold.append(src)
    if not fold:
        return keep
    merged = next((s for s in keep if s["key"] == SOURCE_OTHER_REFERRAL), None)
    if merged is None:
        merged = {"key": SOURCE_OTHER_REFERRAL, "label": _source_label(SOURCE_OTHER_REFERRAL)}
        merged.update({k: 0 for k in ("visits", "signups", *_PERF_COUNT_FIELDS)})
        merged.update({"signup_rate": None, "paid_rate": None})
        keep.append(merged)
    for src in fold:
        merged["visits"] = (merged["visits"] or 0) + (src["visits"] or 0)
        for field in ("signups", *_PERF_COUNT_FIELDS):
            merged[field] += src[field]
    merged["signup_rate"] = _rate(merged["signups"], merged["visits"])
    merged["paid_rate"] = _rate(merged["paid"], merged["signups"])
    keep.sort(key=lambda s: (-(s["visits"] or 0), -s["signups"], s["key"]))
    return keep


def _channel_rows(start, end, flags: tuple, visit_rows: list[tuple]) -> list[dict]:
    """MKT-2 — 채널별 성과 행 3종 (other / link / referral_code) 을 한 배열로.

    프론트는 **배열 순서를 그대로 렌더**하므로 정렬 정책은 서버가 갖는다:
    other 가 항상 첫 행, 그 뒤로 link → referral_code 를 각각 (가입 desc, 이름 asc).

    - other: 리퍼러로 '추정'한 유입 전부를 접은 1행. ``sources`` 로 펼친다
      (biolink / unsaved_utm / 리퍼러 파생 채널). visits 는 소스 합이 아니라
      **other 전체의 고유 방문자**라, 한 방문자가 두 소스로 들어오면
      Σsources.visits > other.visits 가 될 수 있다(둘 다 정확한 값).
    - link: 저장한 채널 링크 1개 = 1행. **방문 0이어도 행이 나온다** — "만들었는데
      아무도 안 온 링크"를 보는 것이 이 화면의 용도다.
      단 ``excluded_from_stats=True`` 인 링크는 **행이 나오지 않고**(MKT-12) 그 유입은
      other 행의 ``excluded_link`` 소스로 흡수된다 — 인원을 총합에서 빼지는 않는다.
    - referral_code: 제휴코드 1개 = 1행, 최상위. 코드는 채널과 같은 층의 유입 경로다.
      사용 0건이어도 행이 나오고, visits/signup_rate 는 항상 null.

    제휴코드 사용자는 저장 채널과 무관하게 코드 행으로 이동한다(기존 referral 오버레이와
    같은 규칙, 배타적). 이동으로 원 행이 과소 집계되는 양은 각 행의 ``referral_overlap``.
    """
    flag_rows, _attr_map, attr_utm, referral_users, _visits_by_channel, attr_row = flags
    links = list(MarketingChannelLink.objects.select_related("created_by"))
    link_index = _link_index(links)
    visitors_by_row, visitors_by_source = _visitors_by_bucket(visit_rows, link_index)

    # ── 가입/단계 도달: 행·소스별 누적 ──
    per_row: dict = defaultdict(_empty_perf_slot)
    per_source: dict = defaultdict(_empty_perf_slot)
    referral_overlap: dict = defaultdict(int)
    # MKT-5: 저장 안 된 UTM 조합별 가입/실결제 (방문만으로는 조합의 가치를 알 수 없다)
    signups_by_combo: dict = {}
    for uid, ig, pgc, pg, cp, pd, cur_plan, cur_status, cur_card in flag_rows:
        trialing, _no_card = _trial_flags(cur_plan, cur_status, cur_card)
        code = referral_users.get(uid)
        bucket = attr_row.get(uid)
        if code or uid in referral_users:
            # 제휴코드 행으로 이동 — 원래 있었을 행에 과소 집계량을 표기.
            # ⚠️ 코드 사용자는 귀속 행이 없어도 **채널을 안다**(코드 자체가 유입 경로) →
            #    attribution_gap 이 아니다. 원 행이 없으면 보정할 대상도 없다.
            #
            # MKT-15: 코드가 집계 제외면(code is None) 도착지가 코드 행이 아니라
            # other/excluded_code 다. 그러면 원 행이 **other 였던 사람은 제자리로 돌아온
            # 것**이라 과소 집계가 없다 → referral_overlap 을 올리면 거짓이 된다.
            # 그래서 "도착지가 원 행과 다를 때만" 올린다(제외 없을 땐 기존과 동일).
            dest_row = code or OTHER_ROW_KEY
            if bucket is not None and dest_row != bucket[0]:
                referral_overlap[bucket[0]] += 1
            if code:
                _accumulate_perf(per_row[code], ig, pgc, pg, cp, pd, trialing)
            else:
                _accumulate_perf(per_row[OTHER_ROW_KEY], ig, pgc, pg, cp, pd, trialing)
                _accumulate_perf(per_source[SOURCE_EXCLUDED_CODE], ig, pgc, pg, cp, pd, trialing)
            continue
        if bucket is None:
            # MKT-10: 귀속 기록이 아예 없는 회원 — 사용자 행동이 아니라 **우리 계측 공백**이다.
            # direct 에 섞으면 마케터가 유입 채널 성과로 읽는다(prod 51명 = 그 줄의 절반 이상)
            # → 채널 분해에서 제외하고 attribution_gap 으로만 노출한다.
            continue
        origin_row, origin_source = bucket
        _accumulate_perf(per_row[origin_row], ig, pgc, pg, cp, pd, trialing)
        if origin_source is not None:
            _accumulate_perf(per_source[origin_source], ig, pgc, pg, cp, pd, trialing)
        if origin_source == SOURCE_UNSAVED_UTM:
            src, med, camp, content = attr_utm.get(uid, ("", "", "", ""))
            slot = signups_by_combo.setdefault(
                _utm_key(src, med, camp, content),
                {
                    "utm": {
                        "source": src,
                        "medium": med,
                        "campaign": camp,
                        "content": content,
                    },
                    "signups": 0,
                    "paid": 0,
                },
            )
            slot["signups"] += 1
            if pd:
                slot["paid"] += 1

    # ── other 행 (항상 첫 행, 항상 존재) ──
    sources = [
        {
            "key": key,
            "label": _source_label(key),
            **_perf_payload(
                per_source.get(key) or _empty_perf_slot(), len(visitors_by_source.get(key, ()))
            ),
        }
        for key in set(per_source) | set(visitors_by_source)
    ]
    sources.sort(key=lambda s: (-(s["visits"] or 0), -s["signups"], s["key"]))
    sources = _fold_sources(sources)
    for src_row in sources:
        if src_row["key"] == SOURCE_UNSAVED_UTM:
            combos, truncated = _unsaved_utm_combos(visit_rows, link_index, signups_by_combo)
            src_row["combos"] = combos
            src_row["combos_truncated"] = truncated
    rows: list[dict] = [
        {
            "kind": ROW_KIND_OTHER,
            "key": OTHER_ROW_KEY,
            "label": OTHER_ROW_LABEL,
            **_perf_payload(
                per_row.get(OTHER_ROW_KEY) or _empty_perf_slot(),
                len(visitors_by_row.get(OTHER_ROW_KEY, ())),
            ),
            "referral_overlap": referral_overlap.get(OTHER_ROW_KEY, 0),
            "sources": sources,
        }
    ]

    # ── link 행 (0 방문 포함) ──
    # created_by_email 은 **원문 그대로** 담는다 — 제한 역할 마스킹은 캐시 이후
    # (apply_pii_policy). 여기서 역할별로 만들면 full 이 채운 캐시가 그대로 새어 나간다.
    link_rows = [
        {
            "kind": ROW_KIND_LINK,
            "key": str(link.pk),
            "label": link.name,
            "channel": link.channel,
            "url": link.url,
            "utm": {
                "source": link.utm_source,
                "medium": link.utm_medium,
                "campaign": link.utm_campaign,
                "content": link.utm_content,
            },
            "created_by_email": (link.created_by.email if link.created_by else ""),
            **_perf_payload(
                per_row.get(str(link.pk)) or _empty_perf_slot(),
                len(visitors_by_row.get(str(link.pk), ())),
            ),
            "referral_overlap": referral_overlap.get(str(link.pk), 0),
        }
        for link in links
        # MKT-12 의 '행 제거' 지점. 인원은 사라지지 않는다 — _link_index 가 이 링크의
        # 4-튜플을 None 으로 표시해 유입이 other/excluded_link 로 흡수되므로
        # `Σrows.signups + attribution_gap == 기간 가입자 수` 항등이 유지된다.
        if not link.excluded_from_stats
    ]
    link_rows.sort(key=lambda r: (-r["signups"], -(r["visits"] or 0), r["label"]))
    rows.extend(link_rows)

    # ── referral_code 행 (사용 0건 포함) ──
    redemption_agg = {
        r["referral_code__code"]: r
        for r in (
            ReferralRedemption.objects.filter(trial_started_at__gte=start, trial_started_at__lt=end)
            .values("referral_code__code")
            .annotate(
                redemptions=Count("id"),
                converted=Count("id", filter=Q(converted_to_paid=True)),
            )
        )
    }
    code_rows = []
    # MKT-15: 집계 제외 코드는 행이 없다. 가입자는 위 루프에서 other/excluded_code 로
    # 흡수되므로 `Σrows.signups + attribution_gap == 기간 가입자 수` 항등은 유지된다.
    for code_obj in ReferralCode.objects.filter(excluded_from_stats=False):
        code = code_obj.code
        agg = redemption_agg.get(code) or {"redemptions": 0, "converted": 0}
        code_rows.append(
            {
                "kind": ROW_KIND_REFERRAL_CODE,
                "key": code,
                # MKT-15: PATCH 대상 uuid. key 는 코드 문자열이라 이것 없이는 프론트가
                # /admin/referral-codes/ 를 조인해야 하는데, 그 경로는 marketing_viewer 가
                # 읽을 수 없어 권한을 이중 판정하게 된다.
                "code_id": str(code_obj.pk),
                # ⚠️ 역할 의존 값이지만 여기서는 **full 기준(True)** 으로 넣는다 —
                #    제한 역할용 False 는 캐시 이후 apply_pii_policy 가 덮는다.
                #    여기서 역할별로 만들면 full 이 채운 캐시를 제한 역할이 그대로 받는다.
                "can_exclude": True,
                "label": code,
                "description": code_obj.description or "",
                **_perf_payload(per_row.get(code) or _empty_perf_slot(), None),
                "referral_overlap": 0,  # 코드 행은 이동의 도착지라 항상 0
                "redemptions": agg["redemptions"],
                "converted": agg["converted"],
                "conversion_rate": _rate(agg["converted"], agg["redemptions"]),
            }
        )
    code_rows.sort(key=lambda r: (-r["signups"], -r["redemptions"], r["key"]))
    rows.extend(code_rows)

    keys = [r["key"] for r in rows]
    if len(keys) != len(set(keys)):
        # link pk 와 같은 문자열의 제휴코드(예: 코드 "41") — trends.by_channel 에서 병합된다
        logger.warning(
            "[admin-dash-mkt] 채널 행 key 충돌: %s", [k for k in keys if keys.count(k) > 1]
        )
    return rows


def _attribution_gap(flags: tuple) -> dict:
    """MKT-10 — 귀속 기록이 없어 **어느 채널에도 집계되지 않은** 가입 인원 (데이터 품질).

    ``sources`` 안의 한 줄로 주면 다시 채널처럼 읽히므로 **채널 배열 밖**에 둔다.
    비율이 크면 그 자체가 고쳐야 할 계측 버그 신호라, 숫자를 보이게 두는 것이 목적이다.

    항등: ``Σrows[].signups + signups_unattributed == 이 기간 가입자 수``
    (= funnel variants.all 의 signup 노드 count).

    ``since`` = SignupAttribution 최초 기록 시각(전 기간) — 그 이전 가입은 애초에 기록이
    없으므로 "계측 도입 이전 가입 포함"을 화면에서 덧붙일 수 있다.

    ⚠️ **제휴코드 사용자는 공백이 아니다** — 귀속 행이 없어도 코드 자체가 유입 경로라
    코드 행에 집계된다. 여기서 세면 rows 합과 이 값이 겹쳐 항등이 깨진다.
    코드를 집계에서 뺐어도(MKT-15) 마찬가지다 — 그 사람들은 other/excluded_code 로
    흡수되므로 여전히 공백이 아니다. ``referral_users`` 는 제외된 코드도 **키를 유지**하니
    아래 멤버십 판정이 그대로 성립한다(:func:`_referral_user_map` 참고).
    """
    flag_rows, _attr_map, _attr_utm, referral_users, _visits_by_channel, attr_row = flags
    total = len(flag_rows)
    unattributed = sum(
        1 for row in flag_rows if row[0] not in attr_row and row[0] not in referral_users
    )
    since = None
    if ATTRIBUTION_AVAILABLE:
        since = SignupAttribution.objects.aggregate(m=Min("created_at"))["m"]
    return {
        "signups_unattributed": unattributed,
        "share": _rate(unattributed, total),
        "since": since,
    }


def _channels(
    start,
    end,
    flags: tuple | None = None,
    visit_rows: list[tuple] | None = None,
) -> dict:
    """채널별 성과 (MKT-2) + 레퍼럴 코드 목록.

    ``rows`` 는 ``kind`` 판별자를 가진 **3종 행**이다 (:func:`_channel_rows` 참고):
    other(리퍼러 추정 전부를 접은 1행) / link(저장 링크 1개) / referral_code(제휴코드 1개).
    파생 채널(meta_ads, search_organic …)은 더 이상 최상위 행이 아니라 other.sources 안에
    있다 — UTM 으로 확인된 유입과 리퍼러로 추정한 유입을 같은 층에 두면 추정값이 확실한
    값을 압도하기 때문(MKT-2 의 동기).

    - flags: 뷰에서 미리 계산한 _cohort_flags(start,end) (funnel 과 중복 쿼리 방지).
    - visit_rows: 뷰에서 미리 읽은 _visit_rows(start,end) (trends 와 공유).
    - 링크 행의 created_by_email · 코드 행의 description 은 원문으로 담고, 제한 역할
      마스킹은 **캐시 이후** apply_pii_policy 가 한다 (여기서 하면 캐시가 오염된다).
    - paid 는 **실결제(첫 PAID 이력)** 만 — 체험 미포함 (N-4). free_trial(현재 체험 중 &
      미결제)은 별도 컬럼 — R-4 이후 **카드 등록 완료 체험만** (퍼널 conversion 과 동일 정의).
    - CLN-1: 기존 ``referral_codes`` 블록은 **제거**됐다 — rows 의 referral_code 행이
      상위집합(사용 0건 코드까지 포함)이라 같은 데이터가 두 곳에 있을 이유가 없다.
    - MKT-10: 귀속 기록이 없는 회원은 rows 에서 빠지고 ``attribution_gap`` 에만 잡힌다
      (계측 공백을 채널 성과로 읽지 않게).
    """
    if flags is None:
        flags = _cohort_flags(start, end)
    if visit_rows is None:
        visit_rows = _visit_rows(start, end)
    return {
        "rows": _channel_rows(start, end, flags, visit_rows),
        "attribution_gap": _attribution_gap(flags),
    }


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


# T-2: '체험 중 취소'가 정확해지는 경계 = 이 마이그레이션이 **그 환경에** 적용된 시각.
# 상수로 박지 않는 이유: dev/prod 적용 시각이 다르고, 상수는 재배포 없이 못 고친다.
_TRIAL_CANCEL_MIGRATION = ("billing", "0024_subscription_trial_history")


def _cancel_accurate_since():
    """``cancelled_during_trial_at`` 기록이 시작된 시각 (없으면 None).

    이 시각 **이후**의 취소만 정확하다. 이전 것은 cancelled_at 이 만료 다운그레이드로
    덮여 복원이 불가능하고, 마이그레이션 백필이 되살릴 수 있었던 행(당시 아직 cancelled
    상태로 남아 있던 것)만 들어 있다 — 그래서 과거 기간은 과소 집계된다.

    django_migrations 를 읽는다: 환경별로 자동으로 맞고, 스쿼시/이름 변경으로 못 찾으면
    None 을 돌려 프론트가 '표시하지 않음'으로 안전하게 떨어진다.
    """
    try:
        from django.db.migrations.recorder import MigrationRecorder

        app, name = _TRIAL_CANCEL_MIGRATION
        row = MigrationRecorder.Migration.objects.filter(app=app, name=name).first()
        return row.applied if row else None
    except Exception:  # noqa: BLE001 — 관측용 부가 정보라 실패해도 대시보드는 떠야 한다
        logger.warning("[admin-dash-mkt] cancel_accurate_since 조회 실패", exc_info=True)
        return None


def _trials_cancelled(start, end, started_users: int) -> dict:
    """T-1: 기간 내 **체험 중 취소**한 고유 회원 수 + 플랜 분해 + 취소율.

    ``ended - ended_converted``(체험 종료 후 미결제)와 **다르다** — 그 값에는 취소를 누르지
    않고 그냥 만료된 회원과 카드 승인 실패 회원이 섞인다. 여기서는 취소 시점에
    ``status == TRIALING`` 이었던 것만 센다(:attr:`UserSubscription.cancelled_during_trial_at`).

    플랜은 ``trial_plan``(체험 시작 시 기록한 내구 값) 기준이다 — ``plan`` 은 만료 시 free 로
    바뀌므로 그걸 쓰면 취소한 프로 체험이 전부 free 로 보인다.

    ⚠️ 이 지표는 필드 도입(billing 0024) **이후의 취소만 정확**하다. 그 이전 취소는
    cancelled_at 이 만료 다운그레이드로 덮여 복원 불가라, 마이그레이션이 복원할 수 있었던
    행(아직 CANCELLED 로 남아 있던 것)만 들어 있다. 과거 기간 조회 시 과소 집계된다.
    """
    rows = (
        UserSubscription.objects.filter(
            cancelled_during_trial_at__gte=start, cancelled_during_trial_at__lt=end
        )
        .order_by()
        .values("trial_plan__name", "trial_plan__display_name")
        .annotate(c=Count("user_id", distinct=True))
    )
    by_plan = [
        {
            "name": r["trial_plan__name"] or "unknown",
            "display_name": r["trial_plan__display_name"] or "알 수 없음",
            "count": r["c"],
        }
        for r in rows
    ]
    by_plan.sort(key=lambda x: (-x["count"], x["name"]))
    total = sum(x["count"] for x in by_plan)
    return {
        "cancelled_during_trial": total,
        "cancelled_during_trial_by_plan": by_plan,
        "trial_cancel_rate": _rate(total, started_users),
        "trial_cancel_formula": TRIALS_CANCEL_FORMULA,
        "cancel_accurate_since": _cancel_accurate_since(),
    }


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
            # T-1: 체험 중 취소 (분모는 회원 단위 체험 시작자 — started 는 이벤트 합산이라
            # 레퍼럴+카드를 둘 다 쓴 회원이 2로 세어져 분모로는 부적합)
            **_trials_cancelled(*cur, started_users=len(starter_uids)),
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


# ── 기간 매출 (MKT-3) ────────────────────────────────────────────────

# 추가 IG 계정 과금의 주문 ID 조각 — one_off_order_id(tag="extra") 의 "-extra-" 와
# proration_extra_order_id 의 "-ex-" 두 가지 (billing.toss_flows). 플랜 기본료와 분리해
# 보여주기 위한 판별이며, description(한국어 문구)보다 안정적이다.
_EXTRA_ACCOUNT_ORDER_RE = r"-(extra|ex)-"
# 결제가 '성공한' 상태 집합. 나중에 환불됐어도 그 시점엔 돈이 들어왔으므로 gross 에 남는다
# (빼면 과거 기간 매출이 소급 변경된다 — refunded 는 환불 시점 기간에서 뺀다).
_REVENUE_SUCCESS_STATUSES = (PaymentStatus.PAID, PaymentStatus.REFUNDED)


def _revenue_slice(start, end) -> dict:
    """[start, end) 구간의 매출 원장 집계 — gross/refunded/net + 건수·인원.

    귀속 규칙 (MKT-3 ①):
      - gross    = **결제 시점**(paid_at) 귀속. 이후 환불돼도 과거 gross 는 안 변한다.
      - refunded = **환불 시점**(refunded_at) 귀속. 6월 결제를 7월에 환불하면 7월에 잡힌다.
      - net      = gross − refunded (같은 기간에 결제+환불이면 서로 상쇄돼 0).

    부분취소는 별도 행(amount < 0, status=refunded — tasks._record_partial_cancels)이라
    gross 는 **amount > 0 만** 세고, refunded 는 부호와 무관하게 절대값을 더한다.
    """
    # paid_at 이 비어 있는 성공 건이 존재한다 — 웹훅 CANCELED 경로가 PENDING 을 PAID 로
    # 중간 확정할 때 승인 시각을 몰라 비워두던 흔적(지금은 채운다). 그대로 두면 gross 에서만
    # 빠지고 refunded 에는 잡혀 net 이 음수가 되므로 created_at 으로 대체한다.
    success = Q(status__in=_REVENUE_SUCCESS_STATUSES, amount__gt=0)
    charged = (
        PaymentHistory.objects.filter(success)
        .annotate(_charged_at=Coalesce("paid_at", "created_at"))
        .filter(_charged_at__gte=start, _charged_at__lt=end)
    )
    gross = charged.aggregate(s=Sum("amount"))["s"] or 0
    payments = charged.count()
    paying_users = charged.order_by().values("user_id").distinct().count()

    refunds = PaymentHistory.objects.filter(
        status=PaymentStatus.REFUNDED, refunded_at__gte=start, refunded_at__lt=end
    )
    refunded = sum(abs(a) for a in refunds.values_list("amount", flat=True))
    return {
        "gross": gross,
        "refunded": refunded,
        "net": gross - refunded,
        "payments": payments,
        "paying_users": paying_users,
        "_charged": charged,
        "_refunds": refunds,
    }


def _period_revenue(cur: tuple, prev: tuple | None) -> dict:
    """MKT-3 — 선택한 기간에 **실제 발생한 매출**. MRR(월 환산 반복 매출) 대체.

    MRR 은 point-in-time 이라 기간 필터에 반응하지 않아, 같은 화면에서 옆 카드들과
    시간축이 어긋났다. 이 블록은 range.current 를 그대로 따른다.

    - by_plan: 구독의 **현재 플랜** 기준 분해 (결제 시점 플랜은 저장돼 있지 않다 —
      플랜을 바꾼 회원의 과거 결제는 현재 플랜에 잡히는 근사). 추가 IG 계정 과금은
      여기서 빠지고 extra_ig_accounts 로 분리돼, Σby_plan.net + extra.net == net 이 성립.
    - vat_included: 우리가 저장하는 amount 는 토스에 승인 요청한 **총 결제금액**이라
      부가세 포함이다 (별도 세액 필드 없음).
    """
    slice_cur = _revenue_slice(*cur)
    prev_net = _revenue_slice(*prev)["net"] if prev else None

    charged, refunds = slice_cur.pop("_charged"), slice_cur.pop("_refunds")
    extra_q = Q(toss_order_id__regex=_EXTRA_ACCOUNT_ORDER_RE)

    # 플랜별: 기본료 결제만 (추가 계정 과금 제외) — gross − refunded 를 같은 축에서 뺀다
    plan_gross: dict = {}
    for row in (
        charged.exclude(extra_q)
        .values("subscription__plan__name", "subscription__plan__display_name")
        .annotate(gross=Sum("amount"), payments=Count("id"))
    ):
        name = row["subscription__plan__name"] or "unknown"
        plan_gross[name] = {
            "name": name,
            "display_name": row["subscription__plan__display_name"] or "(구독 정보 없음)",
            "net": row["gross"] or 0,
            "payments": row["payments"],
        }
    for row in (
        refunds.exclude(extra_q).values("subscription__plan__name").annotate(refunded=Sum("amount"))
    ):
        name = row["subscription__plan__name"] or "unknown"
        slot = plan_gross.setdefault(
            name, {"name": name, "display_name": "(구독 정보 없음)", "net": 0, "payments": 0}
        )
        slot["net"] -= abs(row["refunded"] or 0)
    by_plan = sorted(plan_gross.values(), key=lambda p: (-p["net"], p["name"]))

    extra_gross = charged.filter(extra_q).aggregate(s=Sum("amount"))["s"] or 0
    extra_refunded = sum(abs(a) for a in refunds.filter(extra_q).values_list("amount", flat=True))

    net = slice_cur["net"]
    return {
        **slice_cur,
        "previous": prev_net,
        "delta_pct": (round((net - prev_net) / prev_net * 100, 1) if prev_net else None),
        "by_plan": by_plan,
        "extra_ig_accounts": {
            "net": extra_gross - extra_refunded,
            "payments": charged.filter(extra_q).count(),
        },
        "vat_included": True,
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


def _trial_now(now) -> dict:
    """S-2: **지금 체험 기간 중인** 회원 + 결제 여부 3분해 (기간 무관 스냅샷).

    ``trialing`` 과 다른 두 가지:
    ① **취소자를 포함한다.** 취소 시점에 status 가 CANCELLED 로 바뀌지만(subscription_views)
       그 회원은 **기간말까지 여전히 프로를 쓰는 체험자**다. 빼면 타일의 합이 안 맞는다.
    ② **카드 미등록 체험자도 포함한다.** ``trialing`` 은 카드 보유만 세는데, 쿠폰 체험자는
       카드 없이 체험 중일 수 있다(prod 실측 9명) — 빼면 "체험 인원"이 실제보다 작게 나온다.

    분해 (``will_charge + cancelled + no_card == total``):
    - will_charge — 체험 중 + 카드 있음 + 미취소 → 기간말에 **과금된다**
    - cancelled   — 체험 중 취소(기간 남음) → 과금 없이 free 로 내려간다
    - no_card     — 체험 중 + **카드 없음** + 미취소 → 과금 대상이 아니다(쿠폰 체험).
      프론트 요청은 2분해였지만 이 인원이 실재해 3번째 버킷이 필요하다 — will_charge 에
      넣으면 "결제 예약"이 거짓이 되고, total 에서 빼면 체험 인원이 축소된다.

    플랜 축은 **현재 ``plan``** 이다(누적 지표의 ``trial_plan`` 과 다르다) — '지금 쓰는 플랜'을
    묻는 값이고, 취소자도 아직 다운그레이드 전이라 plan 이 유효하다.
    """
    base = UserSubscription.objects.exclude(plan__name__in=_PAID_EXCLUDE).filter(
        current_period_end__gt=now
    )
    # SNAP-1/2: 모수 쿼리는 apps/admin_api/snapshot_rosters.py 가 정본이다 — 명단
    # 엔드포인트(/admin/snapshot/trial/)가 **같은 쿼리**를 재사용해야 타일과 명단이
    # 어긋나지 않는다(요청서 §공통 ①).
    will_charge_qs = trial_will_charge_qs(now)
    cancelled_qs = trial_cancelled_qs(now)

    will_charge = will_charge_qs.count()
    no_card = trial_no_card_qs(now).count()
    cancelled = cancelled_qs.count()
    # by_plan 은 세 버킷을 합친 모집단 — Σ count == total 이 성립해야 한다
    trialing = base.filter(status=SubscriptionStatus.TRIALING)
    rows, total = _plan_count_rows(base.filter(Q(pk__in=trialing) | Q(pk__in=cancelled_qs)))
    return {
        "total": total,
        "by_plan": rows,
        "will_charge": will_charge,
        "cancelled": cancelled,
        "no_card": no_card,
    }


def _trial_plan_rows(*, cancelled_only: bool = False) -> tuple[list[dict], int]:
    """누적 체험 인원의 플랜 분해 + 총계 — **시작(T-1)과 취소(S-1)의 공용 축**.

    판정은 ``trial_plan`` 보유 여부 하나다 — 카드 체험(toss_flows)·쿠폰 체험
    (referral_views)이 시작 시점에 모두 이 필드를 쓰므로 두 종류가 한 축에 모인다.
    admin 플랜은 마케팅 무관이라 제외 (paying/trialing 과 동일 정책).

    ⚠️ 두 지표가 **반드시 같은 함수**를 써야 한다: 취소 집합은 시작 집합의 부분집합이라
    `trial_cancelled.total <= trial_started.total` 이 화면 계약인데, 축이 갈라지면
    (한쪽만 admin 제외, 한쪽만 unknown 버킷 …) 취소가 시작보다 큰 줄이 생긴다.
    ``trial_plan`` 이 비어 있는 행(플랜 레코드 삭제 → SET_NULL)은 **양쪽에서 함께 빠져**
    부분집합 관계가 유지된다.
    """
    qs = UserSubscription.objects.filter(trial_plan__isnull=False)
    if cancelled_only:
        qs = qs.filter(cancelled_during_trial_at__isnull=False)
    rows = (
        qs.exclude(trial_plan__name__in=_PAID_EXCLUDE)
        .order_by()
        .values("trial_plan__name", "trial_plan__display_name")
        .annotate(c=Count("user_id", distinct=True))
    )
    by_plan = [
        {
            "name": r["trial_plan__name"],
            "display_name": r["trial_plan__display_name"] or r["trial_plan__name"],
            "count": r["c"],
        }
        for r in rows
    ]
    by_plan.sort(key=lambda x: (-x["count"], x["name"]))
    return by_plan, sum(x["count"] for x in by_plan)


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
    # SNAP-1: 모수는 snapshot_rosters.paying_subscriptions_qs 정본 (명단 재사용).
    paying_rows, paying_total = _plan_count_rows(paying_subscriptions_qs())
    trial_rows, trial_total = _plan_count_rows(
        UserSubscription.objects.filter(
            status=SubscriptionStatus.TRIALING, billing_key_issued_at__isnull=False
        ).exclude(plan__name__in=_PAID_EXCLUDE)
    )
    # T-1/S-1: 전체 기간 **누적** 체험 시작·취소 인원 + 플랜 분해. feature_stats 쪽 값은
    # [start, end) 로 자른 기간 종속 값이라 이 패널(기간 무관)에 얹으면 한 타일 안에서
    # 시간축이 섞인다. 플랜 축은 trial_plan(내구 기록) — plan 을 쓰면 만료해 free 로
    # 내려간 사람이 free 체험자로 잡힌다. 같은 함수라 cancelled <= started 가 보장된다.
    started_rows, started_total = _trial_plan_rows()
    cancelled_rows, cancelled_total = _trial_plan_rows(cancelled_only=True)

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
        "trial_now": _trial_now(now),
        "trial_started": {"total": started_total, "by_plan": started_rows},
        "trial_cancelled": {"total": cancelled_total, "by_plan": cancelled_rows},
        "visitors": visitors,
        "signups": User.objects.count(),
        "activated": len(page_owners | campaign_owners),
        # SNAP-1/2: 타일을 만든 **그 행들**의 id 를 함께 캐시에 담는다. 명단 엔드포인트가
        # 이 집합 위에서 페이지네이션하므로 `타일 숫자 == 명단 count` 가 캐시 지연과
        # 무관하게 성립한다(요청서 §공통 ② 1번안). 값(plan_name/bucket)까지 함께 얼려
        # `?plan=`·`?bucket=` 의 부분합도 by_plan/버킷 카운트와 정확히 일치시킨다.
        # 응답에는 나가지 않는다 — _SnapshotSerializer 에 선언된 필드만 직렬화된다.
        "_roster_ids": _roster_id_maps(now),
    }


def _roster_id_maps(now) -> dict:
    """명단 엔드포인트용 id→축 매핑. 상한 초과 시 None (뷰가 라이브로 폴백).

    Redis 를 다른 기능과 공유하므로 캐시 항목이 무한히 커지면 안 된다. 현재 규모(수십~수백)
    에서는 수 KB 라 문제없고, 상한을 넘으면 명단은 라이브로 계산되고 응답의 ``as_of`` 가
    지금 시각이 되어 화면이 "타일과 다를 수 있음"을 시각 차이로 드러낸다.
    """
    paying = {
        str(pk): plan
        for pk, plan in paying_subscriptions_qs().values_list("pk", "plan__name")[
            : SNAPSHOT_ROSTER_ID_CACHE_MAX + 1
        ]
    }
    trial = {
        str(pk): BUCKET_WILL_CHARGE
        for pk in trial_will_charge_qs(now).values_list("pk", flat=True)[
            : SNAPSHOT_ROSTER_ID_CACHE_MAX + 1
        ]
    }
    trial.update(
        {
            str(pk): BUCKET_CANCELLED
            for pk in trial_cancelled_qs(now).values_list("pk", flat=True)[
                : SNAPSHOT_ROSTER_ID_CACHE_MAX + 1
            ]
        }
    )
    return {
        "paying": None if len(paying) > SNAPSHOT_ROSTER_ID_CACHE_MAX else paying,
        "trial": None if len(trial) > SNAPSHOT_ROSTER_ID_CACHE_MAX else trial,
    }


def _snapshot_cached(now, *, bypass: bool = False) -> dict:
    """R-6 ②: 기간과 무관하므로 별도 캐시 키 — 모든 period 응답이 계산 1회를 공유.

    ``bypass=True``(?refresh=1)면 이 키까지 재계산한다 — 안 그러면 새로고침 후에도
    상단 고정 패널만 최대 15분 스테일로 남아 "일부 숫자만 안 바뀐다"가 된다.
    """
    if not bypass:
        cached = cache.get(CACHE_KEY_SNAPSHOT)
        if cached is not None:
            return cached
    data = _snapshot(now)
    cache.set(CACHE_KEY_SNAPSHOT, data, MARKETING_DASHBOARD_SNAPSHOT_CACHE_TTL)
    return data


class AdminMarketingDashboardView(APIView):
    """어드민 마케팅 대시보드 집계 (단일 GET, Redis 60초 캐시 · ?refresh=1 로 우회)."""

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
  `date` 는 버킷 **시작일**, 필드 구성·합계 규칙(`by_channel` + `unattributed`)은 일별과 동일하며
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
  각 노드 = `{key, label, count, rate, rate_of, formula, previous, delta_pct}`.
  rate: signup=가입/고유 방문자, ig_connected=ig/가입, dm_campaign=dm/ig,
  page_created=생성/가입, page_published=공개/생성, paid=유료플랜 전환/가입(수렴이라 가입 대비).
- **퍼널 노드 증감 `previous`/`delta_pct` (MKT-1 = R-8)**: `head[visit, signup]` ·
  `activation` · `conversion` **4개 노드 모두**에 붙습니다 (채널 variant 포함).
  `previous` = **직전 동일 기간의 같은 집계**, `delta_pct` = `(current−previous)/previous×100`
  (소수 1자리). 노드가 자기 증감을 들고 있으므로 배지와 바로 위 숫자가 같은 모집단이며,
  `kpis` 로 대체하면 어긋납니다 — `kpis.paid_conversions` 는 **실결제만**이라
  conversion 노드(체험+실결제)와 모집단이 다르고, '활성화 유저'에 대응하는 KPI 는 아예 없습니다.
  `previous` 가 **null**(= `period=all`, R-1 규칙) 이거나 **0** 이면 `delta_pct` 는 null.
  직전 기간에 없던 채널의 variant 는 `previous: 0`(그 채널로 0명이었던 것이 사실).
  `branches[*].steps[*]` 는 스키마 일관성을 위해 두 필드를 갖지만 **항상 null** 입니다
  (팝업 전용 상세라 증감 배지를 쓰지 않음).
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
  전부 0인 채널은 생략. 주별 합산은 프론트에서.
- **`trends.buckets[].unattributed`(MKT-10 / Q-B)**: 귀속 기록이 없어 `by_channel` 에서
  **제외된** 인원 `{signups, activated, paid}`(항상 존재, 0 포함). 채널별 성과 표
  (`channels.attribution_gap`)·퍼널 채널 variant 와 **같은 모집단**을 쓰기 위한 것 —
  안 빼면 같은 `other` 키가 표·퍼널 vs 추이에서 다른 인원을 뜻합니다.
  항등: `Σby_channel[m] + unattributed[m] == 버킷[m]`(m = signups/activated/paid),
  `Σby_channel.visits == 버킷 visits`(방문은 행 자체로 판정 — 공백 개념 없음).
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
- 응답은 Redis 에 **60초 캐시** (프리셋 키 `admin:dash:mkt:{period}`,
  커스텀 키 `admin:dash:mkt:custom:{start}:{end}`). 단 `period=all` 은 **900초(15분)**,
  `snapshot` 은 별도 키 `admin:dash:mkt:snapshot` 에 **900초** (모든 period 공유).
- **신선도**: 채널 링크 저장/수정/삭제는 캐시를 즉시 무효화하지만, **방문·가입 적재는
  하지 않습니다** → 방금 들어온 유입은 최대 TTL 만큼 늦게 보입니다. `?refresh=1`
  (full 역할 전용)로 캐시를 우회해 즉시 재계산할 수 있고, 실제 적용 여부는 응답 헤더
  `X-Cache`(HIT/MISS/BYPASS)로 확인합니다. 화면에는 바디의 `generated_at`(집계 시각)을
  "N분 전 기준"으로 표시하는 것을 권장합니다 — 캐시 히트 시 이 값이 과거 시각입니다.

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
            OpenApiParameter(
                name="refresh",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="`1`/`true` 면 **캐시를 건너뛰고 즉시 재계산**한 뒤 다시 캐시에 "
                "적재합니다(고정 패널 snapshot 키까지 함께 갱신). 화면의 '새로고침' 버튼에 "
                "실어 보내세요 — 신규 방문/가입은 적재 시 캐시 무효화가 없어 기본적으로 "
                "TTL(60초, period=all 은 900초)만큼 늦게 보입니다. "
                "**`full` 역할만 유효** — `marketing_viewer` 가 보내면 403 이 아니라 "
                "조용히 무시되고 캐시된 응답이 옵니다(비싼 재계산 반복 방지). "
                "응답 헤더 `X-Cache: HIT | MISS | BYPASS` 로 실제 적용 여부를 확인하세요. "
                "신선도 표시는 응답 바디의 `generated_at`(집계 수행 시각)을 쓰면 됩니다.",
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
                        # MKT-4: value = channels.rows[].key, label 은 서버 정본
                        "available_channels": [
                            {"value": "all", "label": "전체 채널"},
                            {"value": "other", "label": "기타 (기본 링크로 들어옴)"},
                            {"value": "41", "label": "7월 메타 리타겟팅"},
                            {"value": "SUMMER10", "label": "SUMMER10"},
                        ],
                        "available_channels_truncated": False,
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
                                        "previous": 3000,
                                        "delta_pct": 30.0,
                                    },
                                    {
                                        "key": "signup",
                                        "label": "가입",
                                        "count": 210,
                                        "rate": 0.0538,
                                        "rate_of": "visit",
                                        "formula": "기간 내 가입한 회원 수(가입 코호트) ÷ "
                                        "고유 방문자 수 × 100",
                                        "previous": 180,
                                        "delta_pct": 16.7,
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
                                    "formula": "이 기간에 가입한 회원 중 DM 캠페인을 하나 "
                                    "이상 만들었거나, 페이지를 공개한 회원의 비율입니다.\n"
                                    "같은 회원은 한 번만 계산하며, 가입 이후 현재까지의 "
                                    "활동을 기준으로 합니다.",
                                    "previous": 78,
                                    "delta_pct": 23.1,
                                },
                                "activation_overlap": {"both": 21},
                                "conversion": {
                                    "key": "paid",
                                    "label": "유료플랜 전환",
                                    "count": 18,
                                    "rate": 0.1875,
                                    "rate_of": "activated",
                                    "formula": "활성화된 회원 중 무료체험을 시작했거나 실제 "
                                    "결제를 완료한 회원의 비율입니다.\n무료체험은 카드 등록을 "
                                    "완료한 경우만 포함하며, 관리자가 직접 부여한 무료체험은 "
                                    "제외합니다. 실제 결제는 Toss에서 결제 완료된 건만 포함합니다.",
                                    "previous": 13,
                                    "delta_pct": 38.5,
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
                                # 13 = by_channel 합 12 + unattributed 1 (MKT-10/Q-B 항등)
                                "signups": 13,
                                "paid": 2,
                                "dm_delivered": 340,
                                "page_views": 210,
                                "page_clicks": 45,
                                "visits": 180,
                                "activated": 6,
                                # MKT-2: 키 = channels.rows[].key (other / 링크 pk / 제휴코드)
                                "by_channel": {
                                    "other": {
                                        "visits": 140,
                                        "signups": 7,
                                        "activated": 3,
                                        "paid": 1,
                                    },
                                    "41": {
                                        "visits": 40,
                                        "signups": 3,
                                        "activated": 2,
                                        "paid": 1,
                                    },
                                    "SUMMER10": {
                                        "visits": 0,
                                        "signups": 2,
                                        "activated": 1,
                                        "paid": 0,
                                    },
                                },
                                # 귀속 기록 없음 — by_channel 에서 빠지고 여기로만 잡힌다
                                "unattributed": {
                                    "signups": 1,
                                    "activated": 0,
                                    "paid": 0,
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
                                    "other": {
                                        "visits": 205,
                                        "signups": 8,
                                        "activated": 4,
                                        "paid": 0,
                                    }
                                },
                                "unattributed": {
                                    "signups": 0,
                                    "activated": 0,
                                    "paid": 0,
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
                    # MKT-2: kind 판별자를 가진 3종 행. 배열 순서 그대로 렌더하면 된다.
                    "channels": {
                        "rows": [
                            {
                                "kind": "other",
                                "key": "other",
                                "label": "기타 (기본 링크로 들어옴)",
                                "visits": 2080,
                                "signups": 141,
                                "signup_rate": 0.0678,
                                "ig_connected": 90,
                                "dm_campaign": 61,
                                "page_created": 77,
                                "page_published": 52,
                                "paid": 8,
                                "free_trial": 12,
                                "paid_rate": 0.0567,
                                "referral_overlap": 5,
                                "sources": [
                                    {
                                        "key": "instagram_organic",
                                        "label": "인스타그램 유입",
                                        "visits": 700,
                                        "signups": 50,
                                        "signup_rate": 0.0714,
                                        "ig_connected": 33,
                                        "dm_campaign": 22,
                                        "page_created": 28,
                                        "page_published": 19,
                                        "paid": 3,
                                        "free_trial": 4,
                                        "paid_rate": 0.06,
                                    },
                                    {
                                        "key": "biolink",
                                        "label": "고객 바이오링크 페이지",
                                        "visits": 120,
                                        "signups": 6,
                                        "signup_rate": 0.05,
                                        "ig_connected": 3,
                                        "dm_campaign": 1,
                                        "page_created": 2,
                                        "page_published": 1,
                                        "paid": 0,
                                        "free_trial": 1,
                                        "paid_rate": 0.0,
                                    },
                                    {
                                        "key": "unsaved_utm",
                                        "label": "저장 안 된 링크(UTM)",
                                        "visits": 40,
                                        "signups": 2,
                                        "signup_rate": 0.05,
                                        "ig_connected": 1,
                                        "dm_campaign": 0,
                                        "page_created": 1,
                                        "page_published": 0,
                                        "paid": 0,
                                        "free_trial": 0,
                                        "paid_rate": 0.0,
                                        # MKT-5: 이 줄에서 바로 '링크로 저장'할 수 있게 하는 재료
                                        "combos": [
                                            {
                                                "utm": {
                                                    "source": "kakao",
                                                    "medium": "cpc",
                                                    "campaign": "0728_open",
                                                    "content": "",
                                                },
                                                "visits": 31,
                                                "signups": 2,
                                                "paid": 0,
                                                "first_seen": "2026-07-19T13:12:00+09:00",
                                                "last_seen": "2026-07-28T10:30:00+09:00",
                                            }
                                        ],
                                        "combos_truncated": False,
                                    },
                                    {
                                        "key": "direct",
                                        "label": "어디서 왔는지 모름",
                                        "visits": 880,
                                        "signups": 48,
                                        "signup_rate": 0.0545,
                                        "ig_connected": 30,
                                        "dm_campaign": 20,
                                        "page_created": 25,
                                        "page_published": 17,
                                        "paid": 3,
                                        "free_trial": 4,
                                        "paid_rate": 0.0625,
                                    },
                                ],
                            },
                            {
                                "kind": "link",
                                "key": "41",
                                "label": "7월 메타 리타겟팅",
                                "channel": "meta_ads",
                                "url": (
                                    "https://turnflow.link/?utm_source=meta&utm_medium=cpc"
                                    "&utm_campaign=jul_retarget"
                                ),
                                "utm": {
                                    "source": "meta",
                                    "medium": "cpc",
                                    "campaign": "jul_retarget",
                                    "content": "",
                                },
                                "created_by_email": "me@turnflow.link",
                                "visits": 412,
                                "signups": 38,
                                "signup_rate": 0.0922,
                                "ig_connected": 25,
                                "dm_campaign": 17,
                                "page_created": 20,
                                "page_published": 14,
                                "paid": 4,
                                "free_trial": 6,
                                "paid_rate": 0.1053,
                                "referral_overlap": 0,
                            },
                            {
                                "kind": "referral_code",
                                "key": "SUMMER10",
                                "label": "SUMMER10",
                                "description": "여름 인플루언서 제휴",
                                "visits": None,
                                "signups": 12,
                                "signup_rate": None,
                                "ig_connected": 9,
                                "dm_campaign": 7,
                                "page_created": 8,
                                "page_published": 5,
                                "paid": 5,
                                "free_trial": 4,
                                "paid_rate": 0.4167,
                                "referral_overlap": 0,
                                "redemptions": 12,
                                "converted": 5,
                                "conversion_rate": 0.4167,
                            },
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
                    # MKT-3: 선택 기간에 실제 발생한 매출 (화면 헤드라인 = net)
                    "period_revenue": {
                        "gross": 4820000,
                        "refunded": 120000,
                        "net": 4700000,
                        "payments": 63,
                        "paying_users": 58,
                        "previous": 4290000,
                        "delta_pct": 9.6,
                        "by_plan": [
                            {
                                "name": "pro",
                                "display_name": "프로",
                                "net": 3900000,
                                "payments": 41,
                            },
                            {
                                "name": "basic",
                                "display_name": "베이직",
                                "net": 620000,
                                "payments": 19,
                            },
                        ],
                        "extra_ig_accounts": {"net": 180000, "payments": 3},
                        "vat_included": True,
                    },
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
        # ?refresh=1 (full 역할만) — 캐시를 건너뛰고 재계산 후 다시 적재한다.
        bypass = wants_cache_bypass(request)
        if not bypass:
            cached = cache.get(cache_key)
            if cached is not None:
                return Response(
                    apply_pii_policy(cached, role=role), headers={CACHE_HEADER: CACHE_HIT}
                )

        mrr_breakdown = _mrr_breakdown()
        cohort = _cohort_agg(*cur)
        _visits, unique_visitors_current = _visit_counts(*cur)
        # 코호트 flag_rows/attr_map/referral_users/visits_by_channel 1회 계산 →
        # funnel(채널 variant) 과 channels 양쪽에 재사용 (중복 쿼리 방지).
        cohort_flags = _cohort_flags(*cur)
        # MKT-2: 방문 원본 행 1회 조회 → 채널 표·추이·퍼널 variant 가 **같은 행 분류**를 공유
        visit_rows = _visit_rows(*cur)
        channel_variants = _funnel_channel_variants(cohort_flags, visit_rows)
        referral_code_keys = frozenset(ReferralCode.objects.values_list("code", flat=True))

        # MKT-1(R-8): 퍼널 노드가 자기 증감을 들도록 직전 기간 코호트를 한 번 더 집계한다.
        # period=all 은 prev 자체가 없어(R-1) 계산도 하지 않는다 → 전 노드 previous=null.
        prev_cohort = prev_visitors = prev_channel_map = None
        if prev:
            prev_cohort = _cohort_agg(*prev)
            _prev_visits, prev_visitors = _visit_counts(*prev)
            prev_channel_map = {
                key: (counts, visitors)
                for key, _label, counts, visitors in _funnel_channel_variants(
                    _cohort_flags(*prev), _visit_rows(*prev)
                )
            }

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
            "snapshot": _snapshot_cached(now, bypass=bypass),
            "kpis": _kpis(cur, prev, mrr_breakdown["total"]),
            "funnel": _funnel(
                cohort,
                unique_visitors_current,
                channel_variants,
                prev_cohort,
                prev_visitors,
                prev_channel_map,
                referral_code_keys,
            ),
            "trends": _trends(*cur, visit_rows=visit_rows),
            "channels": _channels(*cur, flags=cohort_flags, visit_rows=visit_rows),
            "period_revenue": _period_revenue(cur, prev),
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
            "[admin-dash-mkt] req=%s period=%s role=%s signups=%s mrr=%s attribution=%s cache=%s",
            request_id,
            period,
            role,
            cohort["signups"],
            mrr_breakdown["total"],
            ATTRIBUTION_AVAILABLE,
            CACHE_BYPASS if bypass else CACHE_MISS,
        )
        return Response(
            apply_pii_policy(data, role=role),
            headers={CACHE_HEADER: CACHE_BYPASS if bypass else CACHE_MISS},
        )
