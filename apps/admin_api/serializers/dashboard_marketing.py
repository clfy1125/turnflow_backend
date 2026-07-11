"""apps/admin_api/serializers/dashboard_marketing.py — 마케팅 대시보드 응답 시리얼라이저.

라우팅: ``GET /api/v1/admin/dashboard/marketing/`` (``IsAdminUser``, is_staff=True).
이 모듈의 시리얼라이저는 **OpenAPI 응답 문서화 전용**이다 — 실제 집계 로직은
:mod:`apps.admin_api.views.dashboard_marketing` 가 담당한다.

집계 범위는 전 워크스페이스(GLOBAL). 임계값 상수는
:mod:`apps.admin_api.dashboard_constants` 참고 (프론트 계약).
"""

from __future__ import annotations

from rest_framework import serializers


class _DeltaMetricSerializer(serializers.Serializer):
    """기간 비교 지표 — current(현재 기간) vs previous(직전 동일 길이 기간)."""

    current = serializers.IntegerField(help_text="현재 기간 값")
    previous = serializers.IntegerField(allow_null=True, help_text="직전 기간 값")
    delta_pct = serializers.FloatField(
        allow_null=True,
        help_text="증감률(%) = round((current-previous)/previous*100, 1). previous==0 → null",
    )


class _MrrKpiSerializer(serializers.Serializer):
    """MRR KPI — point-in-time 라이브 계산이라 previous/delta 는 항상 null."""

    current = serializers.IntegerField(help_text="현재 MRR (원)")
    previous = serializers.IntegerField(
        allow_null=True, help_text="항상 null — 과거 시점 MRR 재구성 불가 (스냅샷 미도입)"
    )
    delta_pct = serializers.FloatField(allow_null=True, help_text="항상 null")
    currency = serializers.CharField(help_text='통화 — 항상 "KRW"')


class _KpisSerializer(serializers.Serializer):
    """핵심 KPI 묶음 — 전부 {current, previous, delta_pct}."""

    visits = _DeltaMetricSerializer(
        help_text="랜딩 방문 수 (LandingVisit 행 수). 어트리뷰션 미탑재 시 0"
    )
    unique_visitors = _DeltaMetricSerializer(
        help_text="고유 방문자 수 (distinct visitor_id). 어트리뷰션 미탑재 시 0"
    )
    signups = _DeltaMetricSerializer(help_text="가입 수 (User.date_joined ∈ 기간)")
    ig_connected = _DeltaMetricSerializer(
        help_text="첫 IG 연동이 기간 내인 오너 수 (owner 별 Min(created_at))"
    )
    first_page_published = _DeltaMetricSerializer(
        help_text="⚠ 근사 — 첫 '공개' 페이지의 created_at 기준 (공개 시각 미기록)"
    )
    first_dm_campaign = _DeltaMetricSerializer(
        help_text="첫 AutoDMCampaign 생성이 기간 내인 오너 수"
    )
    paid_conversions = _DeltaMetricSerializer(
        help_text="유저별 첫 PAID PaymentHistory.paid_at 이 기간 내인 수 — "
        "pro_activated_at 은 환불 시 null 처리되어 부적합"
    )
    mrr = _MrrKpiSerializer(help_text="MRR (point-in-time, previous=null)")


class _FunnelBranchesSerializer(serializers.Serializer):
    """activated 단계의 병렬 브랜치 — 합집합 == activated.count."""

    page_published = serializers.IntegerField(help_text="공개 페이지 보유 (현재 기준)")
    dm_campaign_created = serializers.IntegerField(help_text="DM 캠페인 생성 이력 보유")
    both = serializers.IntegerField(help_text="두 브랜치 모두 (교집합 — 합집합에서 1회만 카운트)")


class _FunnelStageSerializer(serializers.Serializer):
    """퍼널 단계 1개 — visit/signup/ig_connected/activated/paid."""

    key = serializers.CharField(help_text="visit / signup / ig_connected / activated / paid")
    count = serializers.IntegerField(
        help_text="단계 도달 수. visit 만 기간-이벤트, 나머지는 가입 코호트의 '현재까지' 도달"
    )
    rate_from_previous = serializers.FloatField(
        allow_null=True, help_text="직전 단계 대비 전환율 (0~1, 분모 0 → null)"
    )
    rate_from_signups = serializers.FloatField(
        allow_null=True, help_text="가입자 대비 도달율 (0~1) — 비선형 단계 비교용"
    )
    branches = _FunnelBranchesSerializer(
        required=False, help_text="activated 단계에만 존재 — 병렬 브랜치 분해"
    )


class _FunnelSerializer(serializers.Serializer):
    """가입 코호트 퍼널."""

    semantics = serializers.CharField(
        help_text='항상 "signup_cohort" — date_joined ∈ 기간 코호트, 단계 도달은 현재까지 기준'
    )
    stages = _FunnelStageSerializer(many=True, help_text="고정 순서 5단계")


class _ChannelRowSerializer(serializers.Serializer):
    """채널 성과 1행 — SignupAttribution.channel 기준 (레퍼럴 오버레이 적용)."""

    channel = serializers.CharField(
        help_text='채널 키. 어트리뷰션 없는 가입자는 "unknown", '
        'ReferralRedemption 보유자는 "referral" (조회 시점 오버레이)'
    )
    visits = serializers.IntegerField(help_text="기간 내 해당 채널 LandingVisit 수")
    signups = serializers.IntegerField(help_text="코호트 가입자 수")
    signup_rate = serializers.FloatField(
        allow_null=True, help_text="signups / visits (visits 0 → null)"
    )
    activated = serializers.IntegerField(help_text="활성화 도달 수 (page ∪ campaign)")
    activation_rate = serializers.FloatField(
        allow_null=True, help_text="activated / signups (signups 0 → null)"
    )
    paid = serializers.IntegerField(help_text="유료 전환 수 (첫 PAID 결제 보유)")
    paid_rate = serializers.FloatField(allow_null=True, help_text="paid / signups")


class _ReferralCodeRowSerializer(serializers.Serializer):
    """레퍼럴 코드 성과 1행 (기간 내 trial_started_at 기준 코호트)."""

    code = serializers.CharField(help_text="레퍼럴 코드")
    redemptions = serializers.IntegerField(help_text="기간 내 사용(트라이얼 시작) 수")
    converted = serializers.IntegerField(help_text="그중 유료 전환(converted_to_paid) 수")
    conversion_rate = serializers.FloatField(allow_null=True, help_text="converted / redemptions")


class _ChannelsSerializer(serializers.Serializer):
    """채널 블록 — 어트리뷰션 미탑재 시 rows 는 빈 배열 (referral_codes 는 항상 제공)."""

    rows = _ChannelRowSerializer(many=True, help_text="채널별 성과 (signups desc)")
    referral_codes = _ReferralCodeRowSerializer(
        many=True, help_text="레퍼럴 코드별 성과 — billing 소스라 어트리뷰션과 무관"
    )


class _UpsellLinkSerializer(serializers.Serializer):
    """업셀 후보 드릴다운 링크."""

    page = serializers.CharField(allow_null=True, help_text="백오피스 라우트 (예: /users)")
    params = serializers.DictField(help_text="쿼리 파라미터 힌트 (예: {id: 812})")


class _UpsellMetricsSerializer(serializers.Serializer):
    """업셀 판정 근거 지표."""

    dm_used_month = serializers.IntegerField(
        help_text="이번 캘린더월 DM 사용량 — 실제 과금 정의: SENT_FOR_QUOTA_STATUSES 의 "
        "(캠페인 × 수신자) 고유쌍 (billing.dm_limits 와 동일)"
    )
    dm_limit = serializers.IntegerField(
        help_text="플랜 월 한도 (SubscriptionPlan.features.dm_monthly_limit, 기본 200)"
    )
    dm_usage_ratio = serializers.FloatField(
        allow_null=True, help_text="dm_used_month / dm_limit (한도 무제한/0 → null)"
    )
    page_clicks_30d = serializers.IntegerField(help_text="최근 30일 페이지 블록 클릭 수")
    spam_blocked_30d = serializers.IntegerField(
        help_text="최근 30일 스팸 차단 수 (detected+hidden)"
    )
    active_ig_connections = serializers.IntegerField(
        help_text="활성(status=active, is_active=True) IG 연동 수"
    )


class _UpsellCandidateSerializer(serializers.Serializer):
    """업셀 후보 1명 — free/basic 오너, score desc 상위 UPSELL_CANDIDATES_LIMIT(10)."""

    user_id = serializers.IntegerField(help_text="User PK")
    email = serializers.CharField(help_text="유저 이메일")
    plan = serializers.CharField(help_text="현재 플랜 name (free/basic)")
    score = serializers.IntegerField(
        help_text="쿼터 >= UPSELL_DM_RATIO_HIGH(0.8) → +3 / >= UPSELL_DM_RATIO_MID(0.5) → +2 / "
        "클릭 >= UPSELL_CLICKS_HIGH(500) → +2 / >= UPSELL_CLICKS_MID(100) → +1 / "
        "스팸 >= UPSELL_SPAM_HEAVY(50) → +1 / 활성 IG >= UPSELL_MULTI_IG_MIN(2) → +2"
    )
    reasons = serializers.ListField(
        child=serializers.CharField(),
        help_text="enum: dm_quota_80pct | dm_quota_50pct | high_page_traffic | "
        "heavy_spam_filtering | multiple_ig_connections",
    )
    metrics = _UpsellMetricsSerializer(help_text="판정 근거 지표")
    link = _UpsellLinkSerializer(help_text="회원 상세 드릴다운")


class _TopPageSerializer(serializers.Serializer):
    """기간 내 조회수 상위 페이지 1건."""

    slug = serializers.CharField(help_text="페이지 slug")
    title = serializers.CharField(allow_blank=True, help_text="페이지 제목")
    views = serializers.IntegerField(help_text="기간 내 조회 수")
    clicks = serializers.IntegerField(help_text="기간 내 블록 클릭 수")


class _BiolinkStatsSerializer(serializers.Serializer):
    """바이오링크(페이지) 기능 통계."""

    public_pages_total = serializers.IntegerField(help_text="공개 페이지 총수 (현재 기준)")
    new_public_pages = _DeltaMetricSerializer(
        help_text="⚠ 근사 — 기간 내 created_at 인 공개 페이지 수 (공개 시각 미기록)"
    )
    views = _DeltaMetricSerializer(help_text="기간 내 PageView 수")
    clicks = _DeltaMetricSerializer(help_text="기간 내 BlockClick 수")
    ctr = serializers.FloatField(help_text="clicks.current / views.current (views 0 → 0.0)")
    top_pages = _TopPageSerializer(
        many=True, help_text="기간 내 조회수 상위 TOP_PAGES_LIMIT(5) 페이지"
    )


class _DmFeatureStatsSerializer(serializers.Serializer):
    """자동 DM 기능 통계."""

    campaigns_created = _DeltaMetricSerializer(help_text="기간 내 생성된 캠페인 수")
    requested = _DeltaMetricSerializer(help_text="기간 내 생성된 DM 로그 수 (전 상태)")
    delivered = _DeltaMetricSerializer(help_text="기간 내 delivered+read 수")
    delivery_rate = serializers.FloatField(
        help_text="(delivered+read) / (accepted+delivered+read+failed_no_trace) — 현재 기간"
    )


class _SpamFeatureStatsSerializer(serializers.Serializer):
    """스팸 필터 기능 통계."""

    detected = _DeltaMetricSerializer(help_text="기간 내 스팸 판정 수 (CLEAN 제외)")
    hidden = _DeltaMetricSerializer(help_text="기간 내 숨김 처리 수")


class _TrialsStatsSerializer(serializers.Serializer):
    """트라이얼 통계 — started 는 레퍼럴+카드등록, 전환은 레퍼럴 코호트만."""

    started = _DeltaMetricSerializer(
        help_text="기간 내 시작된 트라이얼 수 = ReferralRedemption.trial_started_at ∈ 기간 "
        "+ UserSubscription.trial_used_at ∈ 기간 (카드등록 트라이얼)"
    )
    converted = serializers.IntegerField(
        help_text="기간 내 시작한 '레퍼럴' 트라이얼 중 converted_to_paid=True (코호트)"
    )
    conversion_rate = serializers.FloatField(
        allow_null=True,
        help_text="converted / 기간 내 레퍼럴 트라이얼 시작 수 — 카드 트라이얼 전환은 "
        "전용 플래그 부재로 미포함 (레퍼럴 코호트 한정)",
    )


class _FeatureStatsSerializer(serializers.Serializer):
    """기능별 사용 통계."""

    biolink = _BiolinkStatsSerializer(help_text="바이오링크(페이지)")
    dm = _DmFeatureStatsSerializer(help_text="자동 DM")
    spam = _SpamFeatureStatsSerializer(help_text="스팸 필터")
    trials = _TrialsStatsSerializer(help_text="트라이얼")


class _PlanDistributionRowSerializer(serializers.Serializer):
    """플랜 분포 1행 — 전 플랜(비활성 포함), sort_order 순."""

    name = serializers.CharField(help_text="SubscriptionPlan.name")
    display_name = serializers.CharField(help_text="SubscriptionPlan.display_name")
    total = serializers.IntegerField(help_text="해당 플랜 UserSubscription 총수 (전 상태)")
    active = serializers.IntegerField(help_text="status=active")
    trialing = serializers.IntegerField(help_text="status=trialing")
    past_due = serializers.IntegerField(help_text="status=past_due")
    cancelled = serializers.IntegerField(help_text="status=cancelled")


class _MrrByPlanRowSerializer(serializers.Serializer):
    """플랜별 MRR 1행 (기본료만 — 추가 IG 계정 매출은 extra_ig_accounts 블록)."""

    name = serializers.CharField(help_text="SubscriptionPlan.name")
    display_name = serializers.CharField(help_text="SubscriptionPlan.display_name")
    subscribers = serializers.IntegerField(help_text="ACTIVE 구독자 수")
    mrr = serializers.IntegerField(
        help_text="기본료 합 (원) — Coalesce(monthly_amount_snapshot, plan.monthly_price)"
    )


class _ExtraIgAccountsMrrSerializer(serializers.Serializer):
    """추가 IG 계정 매출 (프로 전용 애드온)."""

    count = serializers.IntegerField(help_text="ACTIVE pro 구독의 extra_ig_accounts 합")
    unit_price = serializers.IntegerField(help_text="계정당 단가 — EXTRA_IG_ACCOUNT_PRICE(9900원)")
    mrr = serializers.IntegerField(help_text="count × unit_price (원)")


class _MrrBreakdownSerializer(serializers.Serializer):
    """MRR 브레이크다운 — point-in-time, ACTIVE 유료 구독만 (TRIALING/free 제외)."""

    total = serializers.IntegerField(help_text="총 MRR (원) = by_plan 합 + 추가 IG 계정 매출")
    by_plan = _MrrByPlanRowSerializer(many=True, help_text="플랜별 기본료 MRR (sort_order 순)")
    extra_ig_accounts = _ExtraIgAccountsMrrSerializer(help_text="추가 IG 계정 매출")


class _PeriodRangeSerializer(serializers.Serializer):
    """집계 기간 경계 (Asia/Seoul ISO 8601). current=[start,end), previous=직전 동일 길이."""

    current_start = serializers.DateTimeField(help_text="현재 기간 시작")
    current_end = serializers.DateTimeField(help_text="현재 기간 끝 (미포함)")
    previous_start = serializers.DateTimeField(help_text="직전 기간 시작")
    previous_end = serializers.DateTimeField(help_text="직전 기간 끝 (미포함)")


class AdminMarketingDashboardSerializer(serializers.Serializer):
    """마케팅 대시보드 단일 집계 응답 (전 워크스페이스 GLOBAL, Redis 5분 캐시)."""

    period = serializers.CharField(help_text="적용된 기간 (7d/30d/90d)")
    range = _PeriodRangeSerializer(help_text="현재/직전 기간 경계")
    generated_at = serializers.DateTimeField(
        help_text="집계 생성 시각 — 캐시(MARKETING_DASHBOARD_CACHE_TTL=300s) 신선도 표시용"
    )
    attribution_available = serializers.BooleanField(
        help_text="어트리뷰션 서브시스템(apps.analytics) 탑재 여부 — false 면 "
        "visits/unique_visitors=0, channels.rows=[] 로 강등"
    )
    kpis = _KpisSerializer(help_text="핵심 KPI (전부 기간 비교)")
    funnel = _FunnelSerializer(help_text="가입 코호트 퍼널")
    channels = _ChannelsSerializer(help_text="채널별 성과 + 레퍼럴 코드")
    upsell_candidates = _UpsellCandidateSerializer(
        many=True, help_text="업셀 후보 상위 UPSELL_CANDIDATES_LIMIT(10), score desc"
    )
    feature_stats = _FeatureStatsSerializer(help_text="기능별 사용 통계")
    plan_distribution = _PlanDistributionRowSerializer(
        many=True, help_text="플랜별 구독 상태 분포 (전 플랜, sort_order 순)"
    )
    mrr_breakdown = _MrrBreakdownSerializer(help_text="MRR 브레이크다운")
