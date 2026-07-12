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


class _FunnelNodeSerializer(serializers.Serializer):
    """퍼널 노드 1개 — visit/signup/ig_connected/dm_campaign/page_created/page_published/paid."""

    key = serializers.CharField(
        help_text=(
            "visit / signup / ig_connected / dm_campaign / page_created / page_published / paid"
        )
    )
    label = serializers.CharField(help_text="한국어 고정 라벨 (예: 방문/가입/IG 연동/…)")
    count = serializers.IntegerField(
        help_text="노드 도달 수. visit 만 기간-이벤트, 나머지는 가입 코호트의 '현재까지' 도달"
    )
    rate = serializers.FloatField(
        allow_null=True, help_text="rate_of 노드 대비 전환율 (0~1, 분모 0 → null)"
    )
    rate_of = serializers.CharField(
        allow_null=True, help_text="분모가 되는 노드 key (화살표 위 % 표기용) 또는 null"
    )
    formula = serializers.CharField(
        allow_null=True,
        help_text='공식 한국어 문자열 (i-아이콘 툴팁용, 예 "IG 연동 수 ÷ 가입 수 × 100")',
    )


class _FunnelBranchSerializer(serializers.Serializer):
    """가입 이후 분기 1개 — dm(DM 자동화) / biolink(바이오링크)."""

    key = serializers.CharField(help_text="dm / biolink")
    label = serializers.CharField(help_text='분기 라벨 ("DM 자동화" / "바이오링크")')
    steps = _FunnelNodeSerializer(
        many=True,
        help_text="분기 단계 노드. dm=[ig_connected, dm_campaign], biolink=[page_created, page_published]",
    )


class _FunnelChannelOptionSerializer(serializers.Serializer):
    """채널 드롭다운 옵션 1개 — variants 키와 1:1."""

    value = serializers.CharField(help_text='채널 키 ("all" 또는 채널명)')
    label = serializers.CharField(help_text="한국어 라벨 (CHANNEL_LABELS, 없는 키는 그대로)")


class _FunnelVariantSerializer(serializers.Serializer):
    """채널 1개 기준 분기 퍼널 — head → 분기 → conversion."""

    head = _FunnelNodeSerializer(many=True, help_text="공통 head [visit, signup]")
    branches = _FunnelBranchSerializer(many=True, help_text="병렬 분기 2개 (dm, biolink)")
    conversion = _FunnelNodeSerializer(help_text="수렴 노드 (paid, 가입 대비)")


class _FunnelSerializer(serializers.Serializer):
    """가입 코호트 분기 퍼널 — 채널별 variant 미리 계산 (드롭다운 전환 시 재요청 불필요)."""

    semantics = serializers.CharField(
        help_text='항상 "signup_cohort" — date_joined ∈ 기간 코호트, 도달은 현재까지 기준'
    )
    available_channels = _FunnelChannelOptionSerializer(
        many=True, help_text='드롭다운 옵션 — "all" 첫 항목 + signups>0 채널 (signups desc)'
    )
    variants = serializers.DictField(
        child=_FunnelVariantSerializer(),
        help_text='채널 키 → variant. "all" 항상 포함, 어트리뷰션 미탑재 시 all 만',
    )


class _ChannelRowSerializer(serializers.Serializer):
    """채널 성과 1행 — SignupAttribution.channel 기준 (레퍼럴 오버레이 적용).

    비순차 제품 특성 반영 — 단일 '활성화' 대신 분기 단계별 컬럼(IG 연동/DM 캠페인 ·
    페이지 생성/페이지 공개) 을 제공한다 (퍼널 분기와 동일한 축).
    """

    channel = serializers.CharField(
        help_text='채널 키. 어트리뷰션 없는 가입자는 "unknown", '
        'ReferralRedemption 보유자는 "referral" (조회 시점 오버레이)'
    )
    visits = serializers.IntegerField(help_text="기간 내 해당 채널 LandingVisit 수")
    signups = serializers.IntegerField(help_text="코호트 가입자 수")
    signup_rate = serializers.FloatField(
        allow_null=True, help_text="signups / visits (visits 0 → null)"
    )
    ig_connected = serializers.IntegerField(help_text="IG 연동 도달 수 (DM 갈래 1단계)")
    dm_campaign = serializers.IntegerField(help_text="DM 캠페인 생성 수 (DM 갈래 2단계)")
    page_created = serializers.IntegerField(help_text="페이지 생성 수 (바이오링크 갈래 1단계)")
    page_published = serializers.IntegerField(help_text="페이지 공개 수 (바이오링크 갈래 2단계)")
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
    active_users = _DeltaMetricSerializer(
        help_text="기간 내 공개 페이지를 만든 고유 회원 수 (페이지 공개 사용자)"
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
    active_users = _DeltaMetricSerializer(
        help_text="기간 내 DM 캠페인을 만든 고유 오너 수 (DM 캠페인 생성 사용자)"
    )
    requested = _DeltaMetricSerializer(help_text="기간 내 생성된 DM 로그 수 (전 상태)")
    delivered = _DeltaMetricSerializer(help_text="기간 내 delivered+read 수")
    delivery_rate = serializers.FloatField(
        help_text="(delivered+read) / (accepted+delivered+read+failed_no_trace) — 현재 기간"
    )


class _SpamFeatureStatsSerializer(serializers.Serializer):
    """스팸 필터 기능 통계."""

    active_users = _DeltaMetricSerializer(
        help_text="기간 내 스팸 방어가 동작한 고유 오너 수 (스팸 방어 사용 사용자)"
    )
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


class _DropoffSampleSerializer(serializers.Serializer):
    """이탈 세그먼트 샘플 회원 1명 (CS 드릴다운용)."""

    user_id = serializers.IntegerField(help_text="User PK")
    email = serializers.CharField(allow_blank=True, help_text="회원 이메일")
    joined_at = serializers.DateTimeField(help_text="가입 일시 (Asia/Seoul ISO)")
    link = _UpsellLinkSerializer(help_text="회원 상세 드릴다운 (/users/{id})")


class _OnboardingSegmentSerializer(serializers.Serializer):
    """온보딩 이탈 세그먼트 1개."""

    key = serializers.CharField(
        help_text="no_action / ig_no_campaign / page_created_not_published / "
        "campaign_no_send / paywall_no_payment"
    )
    label = serializers.CharField(help_text="한국어 라벨")
    description = serializers.CharField(help_text="세그먼트 정의 설명")
    count = serializers.IntegerField(help_text="해당 세그먼트 회원 수 (가입 코호트 기준)")
    available = serializers.BooleanField(
        help_text="측정 가능 여부. paywall_no_payment 는 CheckoutEvent 미탑재 시 false"
    )
    samples = _DropoffSampleSerializer(
        many=True, help_text="최근 가입 순 샘플 회원 (ONBOARDING_SAMPLE_LIMIT=5)"
    )


class _OnboardingDropoffsSerializer(serializers.Serializer):
    """온보딩 이탈자 — 가입 코호트의 단계별 이탈 세그먼트 (고정 순서)."""

    cohort_signups = serializers.IntegerField(help_text="기간 내 가입 코호트 총수 (분모)")
    segments = _OnboardingSegmentSerializer(
        many=True, help_text="이탈 세그먼트 (측정 4 + paywall_no_payment)"
    )


class _ConversionByPlanRowSerializer(serializers.Serializer):
    """유료 전환 플랜 분해 1행 (admin/free 제외)."""

    name = serializers.CharField(help_text="SubscriptionPlan.name (basic/pro)")
    display_name = serializers.CharField(help_text="플랜 표시명")
    count = serializers.IntegerField(help_text="현재 플랜이 이것인 전환자 수")


class _PostPaymentUsageRowSerializer(serializers.Serializer):
    """결제 후 사용 기능 1행 — 결제 후 창(기본 7일) 내 실제 사용 유저 수."""

    key = serializers.CharField(help_text="dm_send / page_created / spam_used / extra_ig")
    label = serializers.CharField(help_text="한국어 라벨")
    users = serializers.IntegerField(help_text="결제 후 창 내 해당 기능을 쓴 전환자 수")


class _EntryPathRowSerializer(serializers.Serializer):
    """결제 진입 경로 1행 — trigger_feature 기준 (CheckoutEvent 귀속)."""

    key = serializers.CharField(help_text="trigger_feature 키 (예: dm_limit, pricing_direct)")
    label = serializers.CharField(help_text="한국어 라벨 (미지정 키는 원문)")
    count = serializers.IntegerField(help_text="이 경로로 귀속된 전환자 수")


class _PaidConversionAnalysisSerializer(serializers.Serializer):
    """유료 전환 분석 — 선택 플랜 / 결제 진입 경로 / 결제 후 사용 (3축 분리).

    '무엇 때문에 결제했나'를 단정하지 않는다 — 진입 경로는 CheckoutEvent 텔레메트리로
    귀속하며, 프론트 이벤트 미전송 시 entry_paths_available=false 로 강등된다.
    """

    total = serializers.IntegerField(help_text="기간 내 유료 전환자 수 (유저별 첫 PAID)")
    by_plan = _ConversionByPlanRowSerializer(
        many=True, help_text="선택 플랜별 전환자 수 (현재 구독 플랜 기준, admin/free 제외)"
    )
    post_payment_usage = _PostPaymentUsageRowSerializer(
        many=True, help_text="결제 후 창 내 실제 사용 기능별 유저 수"
    )
    entry_paths = _EntryPathRowSerializer(
        many=True, help_text="결제 진입 경로(업그레이드 트리거)별 전환자 수 (count desc)"
    )
    entry_paths_available = serializers.BooleanField(
        help_text="CheckoutEvent 텔레메트리 탑재/수집 여부 — false 면 entry_paths=[]"
    )
    post_payment_window_days = serializers.IntegerField(
        help_text="결제 후 사용 관찰 창 (일, 기본 7)"
    )


class _CancelReasonRowSerializer(serializers.Serializer):
    """해지 사유 1행 (CancellationEvent.reason 집계)."""

    key = serializers.CharField(help_text="사유 키 (price/low_usage/no_effect/...)")
    label = serializers.CharField(help_text="한국어 라벨 (미지정 키는 원문)")
    count = serializers.IntegerField(help_text="해당 사유 제출 수")


class _CancelDefenseSerializer(serializers.Serializer):
    """취소 방어 성과 (CancellationEvent 기반). 이벤트 미탑재/0 시 전체가 null."""

    tries = serializers.IntegerField(help_text="취소 버튼 클릭 고유 유저 수")
    retained = serializers.IntegerField(help_text="중단/철회로 유지 선택한 고유 유저 수")
    defense_rate = serializers.FloatField(allow_null=True, help_text="retained / tries")


class _MrrMovementSerializer(serializers.Serializer):
    """MRR 변동 (간이 워터폴 — 스냅샷 부재로 부분)."""

    new_mrr = serializers.IntegerField(help_text="기간 내 첫 결제 + 현재 유료 유지 고객 월 금액 합")
    at_risk_mrr = serializers.IntegerField(help_text="취소 예약 + past_due 월 금액 합 (예상 이탈)")
    current_mrr = serializers.IntegerField(help_text="현재 유료 ACTIVE 월 금액 합")
    note = serializers.CharField(help_text="완전 워터폴(업/다운그레이드·실현 해지)은 스냅샷 후")


class _RecentCancellationSerializer(serializers.Serializer):
    """최근 취소 예약(해지 위험) 고객 1명 — CS 액션용."""

    user_id = serializers.IntegerField(help_text="User PK")
    email = serializers.CharField(allow_blank=True, help_text="회원 이메일")
    plan = serializers.CharField(help_text="현재 플랜 표시명")
    monthly_amount = serializers.IntegerField(help_text="월 청구액 (원, 추가 IG 포함)")
    days_remaining = serializers.IntegerField(
        allow_null=True, help_text="현재 주기 종료까지 남은 일수 (되살릴 수 있는 기간)"
    )
    cancelled_at = serializers.DateTimeField(allow_null=True, help_text="취소 예약 시각")
    reason = serializers.CharField(allow_blank=True, help_text="해지 사유 키 (이벤트 있을 때)")
    reason_label = serializers.CharField(allow_blank=True, help_text="해지 사유 라벨")
    recent_dm_7d = serializers.IntegerField(help_text="최근 7일 DM 발송 로그 수")
    recent_clicks_30d = serializers.IntegerField(help_text="최근 30일 페이지 클릭 수")
    link = _UpsellLinkSerializer(help_text="회원 상세 드릴다운")


class _SubscriptionRetentionSerializer(serializers.Serializer):
    """구독 유지·해지 분석 — 유료 전환 이후 생존 지표 ('왜 계속 남고, 왜 떠나는가').

    ⚠ basis=approx_no_snapshot — 유지/해지율은 스냅샷 부재로 근사(코호트 대비 현재 생존).
    현재-상태 카운트(취소 예약/past_due/at-risk MRR)는 정확.
    """

    basis = serializers.CharField(help_text='항상 "approx_no_snapshot" — 근사 근거 플래그')
    window_days = serializers.IntegerField(help_text="유지율 산출 기준 기간 일수")
    retention_rate = serializers.FloatField(
        allow_null=True, help_text="기간 시작 전 첫 결제 고객 중 현재 유료 유지 비율 (0~1, 근사)"
    )
    churn_rate = serializers.FloatField(allow_null=True, help_text="1 - retention_rate")
    paying_now = serializers.IntegerField(help_text="현재 유료 ACTIVE 고객 수 (free/admin 제외)")
    cancel_scheduled = serializers.IntegerField(
        help_text="취소 예약 수 — CANCELLED + 유료 + 주기 남음 (아직 살아있음, 재개 가능)"
    )
    payment_failed = serializers.IntegerField(help_text="결제 실패(past_due) 수 — dunning 중")
    realized_churn = serializers.IntegerField(
        help_text="기간 내 실제 해지 수 — free 다운그레이드 중 결제 이력 보유(트라이얼 만료 제외)"
    )
    at_risk_mrr = serializers.IntegerField(
        help_text="예상 이탈 MRR (원) — 취소 예약 + past_due 월 금액 합"
    )
    mrr_movement = _MrrMovementSerializer(help_text="MRR 변동 (간이)")
    cancel_reasons = _CancelReasonRowSerializer(
        many=True, help_text="해지 사유 TOP N (CancellationEvent, 미탑재 시 [])"
    )
    cancel_reasons_available = serializers.BooleanField(
        help_text="CancellationEvent 텔레메트리 수집 여부 — false 면 cancel_reasons=[]"
    )
    cancel_defense = _CancelDefenseSerializer(
        allow_null=True, help_text="취소 방어 성과 (이벤트 미탑재/0 시 null)"
    )
    recent_cancellations = _RecentCancellationSerializer(
        many=True, help_text="최근 취소 예약 고객 (RECENT_CANCELLATIONS_LIMIT, cancelled_at desc)"
    )


class _PlanDistributionRowSerializer(serializers.Serializer):
    """플랜 분포 1행 — 전 플랜(비활성 포함, admin 제외), sort_order 순.

    admin 은 운영용 내부 계정이라 마케팅 무관 → 제외 (MRR 과 동일 정책).
    """

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
    """MRR 브레이크다운 — point-in-time, ACTIVE 유료 구독만 (TRIALING/free/admin 제외)."""

    total = serializers.IntegerField(help_text="총 MRR (원) = by_plan 합 + 추가 IG 계정 매출")
    by_plan = _MrrByPlanRowSerializer(many=True, help_text="플랜별 기본료 MRR (sort_order 순)")
    extra_ig_accounts = _ExtraIgAccountsMrrSerializer(help_text="추가 IG 계정 매출")


class _PeriodRangeSerializer(serializers.Serializer):
    """집계 기간 경계 (Asia/Seoul ISO 8601). current=[start,end), previous=직전 동일 길이."""

    current_start = serializers.DateTimeField(help_text="현재 기간 시작")
    current_end = serializers.DateTimeField(help_text="현재 기간 끝 (미포함)")
    previous_start = serializers.DateTimeField(help_text="직전 기간 시작")
    previous_end = serializers.DateTimeField(help_text="직전 기간 끝 (미포함)")


class _TrendBucketSerializer(serializers.Serializer):
    """일별 추이 버킷 1개 (로컬 날짜, 제로필 — 빈 날도 0 으로 포함)."""

    date = serializers.CharField(help_text="로컬(Asia/Seoul) 날짜 YYYY-MM-DD")
    signups = serializers.IntegerField(help_text="가입 수 (User.date_joined, TruncDate)")
    paid = serializers.IntegerField(
        help_text="유저별 첫 PAID paid_at 이 이 날인 수 (KPI first-paid 재사용)"
    )
    dm_delivered = serializers.IntegerField(
        help_text="SentDMLog status in (delivered, read), created_at TruncDate"
    )
    page_views = serializers.IntegerField(help_text="PageView.viewed_at TruncDate")
    page_clicks = serializers.IntegerField(help_text="BlockClick.clicked_at TruncDate")
    visits = serializers.IntegerField(
        help_text="LandingVisit.created_at TruncDate — 어트리뷰션 미탑재 시 0"
    )


class _TrendsSerializer(serializers.Serializer):
    """일별 추이 블록 — current 기간 전체를 로컬 날짜 단위 zero-fill (항상 포함)."""

    granularity = serializers.CharField(help_text='항상 "day"')
    buckets = _TrendBucketSerializer(
        many=True, help_text="로컬 날짜별 제로필 버킷 (date 오름차순, 길이 = 기간 일수)"
    )


class AdminMarketingDashboardSerializer(serializers.Serializer):
    """마케팅 대시보드 단일 집계 응답 (전 워크스페이스 GLOBAL, Redis 5분 캐시)."""

    period = serializers.CharField(help_text="적용된 기간 (7d/30d/90d) 또는 커스텀 범위면 'custom'")
    range = _PeriodRangeSerializer(
        help_text="현재/직전 기간 경계 (커스텀은 previous=직전 동일 길이 구간)"
    )
    generated_at = serializers.DateTimeField(
        help_text="집계 생성 시각 — 캐시(MARKETING_DASHBOARD_CACHE_TTL=300s) 신선도 표시용"
    )
    attribution_available = serializers.BooleanField(
        help_text="어트리뷰션 서브시스템(apps.analytics) 탑재 여부 — false 면 "
        "visits/unique_visitors=0, channels.rows=[] 로 강등"
    )
    kpis = _KpisSerializer(help_text="핵심 KPI (전부 기간 비교)")
    funnel = _FunnelSerializer(help_text="가입 코호트 분기 퍼널 (채널별 variant 포함)")
    trends = _TrendsSerializer(help_text="일별 추이 (로컬 날짜 zero-fill, 항상 포함)")
    channels = _ChannelsSerializer(help_text="채널별 성과 + 레퍼럴 코드")
    upsell_candidates = _UpsellCandidateSerializer(
        many=True, help_text="업셀 후보 상위 UPSELL_CANDIDATES_LIMIT(10), score desc"
    )
    feature_stats = _FeatureStatsSerializer(help_text="기능별 사용 통계")
    onboarding_dropoffs = _OnboardingDropoffsSerializer(
        help_text="온보딩 이탈자 (단계별 이탈 세그먼트 + 샘플 회원)"
    )
    paid_conversion_analysis = _PaidConversionAnalysisSerializer(
        help_text="유료 전환 분석 (선택 플랜/진입 경로/결제 후 사용)"
    )
    subscription_retention = _SubscriptionRetentionSerializer(
        help_text="구독 유지·해지 분석 (유지율/취소 예약/이탈 MRR/해지 사유/최근 취소)"
    )
    plan_distribution = _PlanDistributionRowSerializer(
        many=True, help_text="플랜별 구독 상태 분포 (전 플랜, sort_order 순)"
    )
    mrr_breakdown = _MrrBreakdownSerializer(help_text="MRR 브레이크다운")
