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
    previous = serializers.IntegerField(
        allow_null=True,
        help_text="직전 기간 값. **period=all 이면 항상 null** — 비교할 직전 기간 자체가 "
        "없다는 뜻이며 '직전 0건'이 아님 (프론트는 증감 배지를 '—' 로)",
    )
    delta_pct = serializers.FloatField(
        allow_null=True,
        help_text="증감률(%) = round((current-previous)/previous*100, 1). "
        "previous==0 또는 previous==null(period=all) → null",
    )


class _MrrKpiSerializer(serializers.Serializer):
    """MRR KPI — point-in-time 라이브 계산이라 previous/delta 는 항상 null."""

    current = serializers.IntegerField(help_text="현재 MRR (원)")
    previous = serializers.IntegerField(
        allow_null=True, help_text="항상 null — 과거 시점 MRR 재구성 불가 (스냅샷 미도입)"
    )
    delta_pct = serializers.FloatField(allow_null=True, help_text="항상 null")
    currency = serializers.CharField(help_text='통화 — 항상 "KRW"')


class _PaidConversionsKpiSerializer(_DeltaMetricSerializer):
    """유료 전환 KPI — delta 지표 + 정의 문자열(M-2, 프론트 툴팁 정본)."""

    definition = serializers.CharField(
        help_text="지표 정의 (한국어) — '기간 내 실제 결제(Toss PAID)가 처음 발생한 회원 수 · "
        "체험·쿠폰 미결제 제외'"
    )


class _KpisSerializer(serializers.Serializer):
    """핵심 KPI 묶음 — 전부 {current, previous, delta_pct}."""

    visits = _DeltaMetricSerializer(
        help_text="랜딩 방문 수 (LandingVisit 행 수 = 세션 단위). 퍼널/채널의 방문자 지표는 "
        "이게 아니라 고유 방문자 기준. 어트리뷰션 미탑재 시 0"
    )
    unique_visitors = _DeltaMetricSerializer(
        help_text="고유 방문자 수 (distinct visitor_id) — 퍼널 head·채널 visits 와 동일 단위. "
        "어트리뷰션 미탑재 시 0"
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
    paid_conversions = _PaidConversionsKpiSerializer(
        help_text="유저별 첫 PAID PaymentHistory.paid_at 이 기간 내인 수 (실결제만 — "
        "체험·쿠폰 미결제 제외, definition 필드 참고). "
        "pro_activated_at 은 환불 시 null 처리되어 부적합"
    )
    mrr = _MrrKpiSerializer(help_text="MRR (point-in-time, previous=null)")


class _FunnelNodeSerializer(serializers.Serializer):
    """퍼널 노드 1개 — visit/signup/활성화/분기 4노드/paid."""

    key = serializers.CharField(
        help_text=(
            "visit / signup / activated / ig_connected / dm_campaign / "
            "page_created / page_published / paid"
        )
    )
    label = serializers.CharField(help_text="한국어 고정 라벨 (예: 방문자/가입/IG 연동/…)")
    count = serializers.IntegerField(
        help_text="노드 도달 수. visit 만 기간-이벤트이며 **고유 방문자**(distinct visitor_id, "
        "세션 수 아님 — 세션은 kpis.visits), 나머지는 가입 코호트의 '현재까지' 도달"
    )
    rate = serializers.FloatField(
        allow_null=True, help_text="rate_of 노드 대비 전환율 (0~1, 분모 0 → null)"
    )
    rate_of = serializers.CharField(
        allow_null=True, help_text="분모가 되는 노드 key (화살표 위 % 표기용) 또는 null"
    )
    formula = serializers.CharField(
        allow_null=True,
        help_text="공식/정의 한국어 문자열 (i-아이콘 툴팁용) — M-6 이후 모든 노드에서 "
        "non-null 로 채워짐 (백엔드 값이 정본, null 폴백은 방어용)",
    )
    previous = serializers.IntegerField(
        allow_null=True,
        help_text="MKT-1(R-8) — **직전 동일 기간의 같은 집계**. 노드가 자기 증감을 들고 있으므로 "
        "배지와 바로 위 숫자가 항상 같은 모집단이다 (kpis 로 대체하지 말 것: "
        "kpis.paid_conversions 는 실결제만이라 conversion 노드와 모집단이 다르다). "
        "`period=all` 은 비교할 직전 기간이 없어 **null** (R-1 규칙).",
    )
    delta_pct = serializers.FloatField(
        allow_null=True,
        help_text="(current − previous) / previous × 100, 소수 1자리. "
        "previous 가 null 이거나 0 이면 **null** (÷0·오독 방지).",
    )


class _FunnelConversionBreakdownSerializer(serializers.Serializer):
    """유료플랜 전환 분해 (R-4) — 모든 값의 합 == conversion.count (프론트 검증용).

    무료체험은 **카드 등록 완료(billing_key_issued_at) 건만** — 어드민 수동 부여
    무카드 계정은 breakdown 뿐 아니라 conversion.count 에서도 빠진다
    (제외 인원은 conversion.excluded_no_card).
    """

    pro_trial = serializers.IntegerField(
        help_text="프로 무료체험 — 현재 TRIALING · 플랜 pro · 카드 등록 완료 · 실결제 이력 없음"
    )
    basic_trial = serializers.IntegerField(
        help_text="베이직 무료체험 (동일 정의, 플랜 basic). 현재 제품 정책상 체험은 프로 전용 "
        "→ 사실상 항상 0. 키는 합계식 안정성을 위해 **항상 포함**되므로 0 이면 프론트에서 "
        "행을 생략하면 된다"
    )
    pro_paid = serializers.IntegerField(
        help_text="프로 실결제 — PAID 이력 보유 + 현재 구독 플랜 pro"
    )
    basic_paid = serializers.IntegerField(help_text="베이직 실결제 — PAID 이력 보유 + 현재 basic")
    other = serializers.IntegerField(
        help_text="잔여 보정 — 해지 후 free 강등 등으로 위 4분류에 안 맞는 인원 (보통 0). "
        "pro_trial + basic_trial + pro_paid + basic_paid + other == count 를 보장"
    )


class _FunnelConversionNodeSerializer(_FunnelNodeSerializer):
    """수렴 노드(paid) — '유료플랜 전환'(카드 등록 체험 + 실결제) + 3분할 breakdown.

    R-4: rate_of 가 signup → **activated** 로 변경 (방문→가입→활성화→유료 직렬).
    """

    breakdown = _FunnelConversionBreakdownSerializer(
        help_text="플랜×결제여부 분해 — 값들의 합 == count (basic_trial 포함)"
    )
    excluded_no_card = serializers.IntegerField(
        help_text="카드 미등록(어드민 수동 부여 등)이라 전환에서 제외된 체험 인원 수 — "
        "화면 비노출, 검증/로그용 참고값 (count 에 포함되지 않음)"
    )


class _FunnelActivationOverlapSerializer(serializers.Serializer):
    """활성화 노드의 중복 제거 구성 재료 (R-3)."""

    both = serializers.IntegerField(
        help_text="공개 페이지 AND DM 캠페인 둘 다 보유한 코호트 회원 수. 프론트는 "
        "dm_only = dm_campaign - both, page_published_only = page_published - both 로 계산"
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
    """채널 드롭다운 옵션 1개 — variants 키와 1:1 (MKT-4)."""

    value = serializers.CharField(
        help_text='"all" 또는 **channels.rows[].key** (other / 링크 pk / 제휴코드). '
        "표와 같은 키라 프론트가 rows 에 조인해 같은 순서로 정렬할 수 있다"
    )
    label = serializers.CharField(
        help_text="표시명 — 링크 이름·제휴코드는 사람이 붙인 값이라 **서버만이 정본**이다"
        "(프론트 사전에는 없다). rows[].label 과 같은 값"
    )


class _FunnelVariantSerializer(serializers.Serializer):
    """채널 1개 기준 퍼널 — head → 활성화 → conversion (+ 분기 상세는 branches)."""

    head = _FunnelNodeSerializer(many=True, help_text="공통 head [visit, signup]")
    branches = _FunnelBranchSerializer(
        many=True,
        help_text="병렬 분기 2개 (dm, biolink) — R-3 이후 메인 퍼널에서는 숨기고 "
        "'자세히 보기' 팝업에서 재사용한다 (계약 유지, 제거하지 않음)",
    )
    activation = _FunnelNodeSerializer(
        help_text="R-3 — 분기 4노드를 대체하는 단일 '활성화 유저' 노드 "
        "(key=activated, rate_of=signup). count = 공개 페이지 ∪ DM 캠페인 보유 코호트 회원 "
        "(중복 제거) = branches 의 dm_campaign ∪ page_published"
    )
    activation_overlap = _FunnelActivationOverlapSerializer(
        help_text="R-3 — 활성화 구성 분해용 교집합 (both)"
    )
    conversion = _FunnelConversionNodeSerializer(
        help_text="수렴 노드 (paid=유료플랜 전환) — count=카드 등록 체험+실결제, "
        "**rate 분모는 activated**(R-4), breakdown 으로 3분할"
    )


class _FunnelSerializer(serializers.Serializer):
    """가입 코호트 분기 퍼널 — 채널별 variant 미리 계산 (드롭다운 전환 시 재요청 불필요)."""

    semantics = serializers.CharField(
        help_text='항상 "signup_cohort" — date_joined ∈ 기간 코호트, 도달은 현재까지 기준'
    )
    available_channels = _FunnelChannelOptionSerializer(
        many=True,
        help_text='드롭다운 옵션 — "all" + **가입 1명 이상인 행만**(고르면 전부 0인 빈 퍼널이 '
        "되는 항목은 싣지 않는다. 표에는 0 방문 링크도 남아 있다). "
        "배열 순서는 계약이 아니다 — 키가 rows[].key 와 같으니 프론트가 표와 같은 순서로 "
        "정렬해 쓰면 된다",
    )
    available_channels_truncated = serializers.BooleanField(
        help_text="저장 링크 variant 가 상한(10)에서 잘렸는지 (MKT-4). true 면 드롭다운에 "
        "'상위 10개만' 안내를 붙일 것. other/제휴코드는 캡 대상이 아니다"
    )
    variants = serializers.DictField(
        child=_FunnelVariantSerializer(),
        help_text='행 키 → variant. "all" 항상 포함, available_channels[].value 와 1:1',
    )


class _ChannelPerfMixin(serializers.Serializer):
    """채널 행·소스가 공유하는 성과 축 (퍼널 분기와 같은 단계 컬럼)."""

    visits = serializers.IntegerField(
        allow_null=True,
        help_text="기간 내 고유 방문자 수 (distinct visitor_id — 세션 수 아님). "
        "**제휴코드 행은 항상 null** — 코드는 URL 에 실려 오는 값이 아니라 결제 화면 "
        "입력값이라 코드에 귀속되는 '방문'이 존재하지 않는다(0 과 구분 필요)",
    )
    signups = serializers.IntegerField(help_text="코호트 가입자 수")
    signup_rate = serializers.FloatField(
        allow_null=True,
        help_text="signups / visits(고유 방문자). visits 가 0 이거나 null 이면 null",
    )
    ig_connected = serializers.IntegerField(help_text="IG 연동 도달 수 (DM 갈래 1단계)")
    dm_campaign = serializers.IntegerField(help_text="DM 캠페인 생성 수 (DM 갈래 2단계)")
    page_created = serializers.IntegerField(help_text="페이지 생성 수 (바이오링크 갈래 1단계)")
    page_published = serializers.IntegerField(help_text="페이지 공개 수 (바이오링크 갈래 2단계)")
    paid = serializers.IntegerField(
        help_text="**실결제**(첫 PAID 이력 보유) 전환 수 — 무료체험 미포함 (N-4 확정)"
    )
    free_trial = serializers.IntegerField(
        help_text="현재 무료체험 진행 중(TRIALING 유료플랜)이며 미결제인 코호트 회원 수 (N-4)"
    )
    paid_rate = serializers.FloatField(allow_null=True, help_text="paid / signups (실결제 기준)")


class _UnsavedUtmComboSerializer(serializers.Serializer):
    """저장 안 된 UTM 조합 1개 (MKT-5) — 이 줄에서 바로 '링크로 저장'할 수 있게 하는 재료."""

    utm = serializers.DictField(
        help_text="{source, medium, campaign, content} — **정규화 전 원문**. "
        "저장 화면에 그대로 실어 보내면 된다 (매칭은 대소문자·공백 무시라 원문 그대로 저장해도 "
        "다음 집계부터 이 유입이 그 링크 행으로 올라온다)"
    )
    visits = serializers.IntegerField(help_text="이 조합의 고유 방문자 수")
    signups = serializers.IntegerField(help_text="이 조합으로 귀속된 코호트 가입자 수")
    paid = serializers.IntegerField(help_text="그중 실결제 인원")
    first_seen = serializers.DateTimeField(
        allow_null=True, help_text="이 조합의 첫 방문 시각. 방문 없이 가입만 있으면 null"
    )
    last_seen = serializers.DateTimeField(
        allow_null=True,
        help_text="마지막 방문 시각 — 오래된 잔재와 지금 집행 중인 캠페인을 구분하는 데 쓴다",
    )


class _ChannelSourceRowSerializer(_ChannelPerfMixin):
    """``kind=other`` 행을 펼쳤을 때 나오는 소스 1줄 (리퍼러 추정 유입의 내역)."""

    key = serializers.CharField(
        help_text="소스 키. **리퍼러 파생 채널 9종**(instagram_organic / facebook_organic / "
        "youtube_organic / tiktok_organic / threads_organic / blog_organic / search_organic / "
        "other_referral / direct) + **특수 4종**: biolink(고객 바이오링크 배지 경유) / "
        "unsaved_utm(UTM 은 있는데 저장된 채널 링크와 매칭 안 됨) / "
        "**excluded_link**(저장은 됐지만 집계에서 뺀 링크 — MKT-12) / "
        "**excluded_code**(집계에서 뺀 제휴 코드의 가입자 — MKT-15). "
        "UTM 이 붙은 유입은 링크 행 아니면 unsaved_utm/excluded_link 으로 가므로 **유료 채널 키"
        "(meta_ads 등)는 여기 나타나지 않는다**. "
        "excluded_code 는 방문 개념이 없어 `visits=0` 이다(코드는 URL 이 아니라 결제 화면 입력). "
        'direct 는 "가입 귀속 기록 없음"(구 unknown)까지 포함한다'
    )
    label = serializers.CharField(help_text="한국어 표시명 (서버 제공 — 프론트 하드코딩 불필요)")
    combos = _UnsavedUtmComboSerializer(
        many=True,
        required=False,
        help_text="`key=unsaved_utm` 전용 (MKT-5) — 어떤 UTM 조합이 들어오는지. "
        "방문 desc 상위 10개, 다른 소스 줄에는 없다",
    )
    combos_truncated = serializers.BooleanField(
        required=False, help_text="`key=unsaved_utm` 전용 — 조합이 상위 10개에서 잘렸는지"
    )


class _ChannelRowSerializer(_ChannelPerfMixin):
    """채널 성과 1행 (MKT-2) — ``kind`` 로 3종을 구분하는 **이종 배열**.

    | kind | 무엇 | 전용 필드 |
    |---|---|---|
    | `other` | 리퍼러로 '추정'한 유입 전부를 접은 1행 (항상 첫 행, 1개) | `sources` |
    | `link` | 저장한 채널 링크 1개 (방문 0이어도 행이 나온다. **집계 제외 링크는 행이 없다** — MKT-12) | `channel`,`url`,`utm`,`created_by_email` |
    | `referral_code` | 제휴 코드 1개 (사용 0건이어도 행이 나온다. **집계 제외 코드는 행이 없다** — MKT-15) | `description`,`redemptions`,`converted`,`conversion_rate`,`code_id`,`can_exclude` |

    배열 **순서를 그대로 렌더**하면 된다 (정렬 정책은 서버 소유): other → link(가입 desc)
    → referral_code(가입 desc). 모르는 `kind` 는 건너뛰어도 되며, 추가 시 사전 공지한다.
    """

    kind = serializers.ChoiceField(
        choices=["other", "link", "referral_code"], help_text="행 종류 판별자"
    )
    key = serializers.CharField(
        help_text='행 고유 키 — other="other", link=MarketingChannelLink.pk(문자열), '
        "referral_code=코드 문자열. **trends.by_channel 의 키와 동일**(프론트 조인 키)"
    )
    label = serializers.CharField(help_text="화면 표시명 (링크 이름 / 코드 / '기타 …')")
    referral_overlap = serializers.IntegerField(
        help_text="P-3 — 원래 이 행으로 집계됐어야 하나 제휴코드 사용으로 코드 행으로 "
        "이동한 코호트 인원 수. 오버레이는 배타적(중복 집계 없음)이라 이 행이 그만큼 "
        "과소 집계됨을 보정 표기하는 용도. 코드 행은 이동의 도착지라 항상 0"
    )
    # ── kind 별 전용 필드 (해당 kind 에서만 존재) ──
    sources = _ChannelSourceRowSerializer(
        many=True,
        required=False,
        help_text="`kind=other` 전용 — 접힌 유입의 내역. 정렬 visits desc → signups desc → "
        "key asc (방문 0인 줄은 아래로 가되 가입 순으로 뜬다). 줄이 8개를 넘으면 하위를 "
        "other_referral('기타 외부')로 서버가 접는다 — direct/biolink/unsaved_utm 은 예외. "
        "⚠️ **겹치는 지표는 visits 하나뿐**: 한 방문자가 두 소스로 들어올 수 있어 "
        "Σsources.visits ≥ other.visits (둘 다 각자 정확한 고유 방문자 수). "
        "signups·ig_connected·dm_campaign·page_created·page_published·paid·free_trial 은 "
        "**사람 1명이 한 소스에만 귀속**되므로 Σsources == other 등식이 성립한다",
    )
    channel = serializers.CharField(
        required=False, help_text="`kind=link` 전용 — 저장 시 서버가 파생한 채널 키(배지 표기용)"
    )
    url = serializers.CharField(required=False, help_text="`kind=link` 전용 — 완성 UTM URL")
    utm = serializers.DictField(
        required=False,
        help_text="`kind=link` 전용 — {source, medium, campaign, content} 원문(정규화 전)",
    )
    created_by_email = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='`kind=link` 전용 — 링크를 만든 관리자. marketing_viewer 에는 "" (RBAC-4)',
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='`kind=referral_code` 전용 — 제휴 내부 메모. marketing_viewer 에는 ""',
    )
    # ⚠️ MKT-7 — 코드 행은 축이 둘이고 **값이 다를 수 있다**. 화면에 둘 다 넣을 것.
    #   성과 축(signups/paid): 기간 내 **가입한** 회원 중 이 코드를 쓴 사람 (가입일 기준)
    #   코드 축(redemptions/converted): 기간 내 **코드를 쓴** 건 (체험 시작일 기준)
    # 1월에 가입해 7월에 코드를 쓴 사람은 7월 redemptions 에는 들어가지만 7월 signups 에는
    # 없다. 제휴사 보고 숫자는 코드 축을 쓰는 것이 맞다.
    redemptions = serializers.IntegerField(
        required=False,
        help_text="`kind=referral_code` 전용 — 기간 내 코드 사용(체험 시작) 건수. "
        "**signups 와 다를 수 있다**(기준 시각: 체험 시작 vs 가입). 1유저 1회라 건수==인원",
    )
    converted = serializers.IntegerField(
        required=False,
        help_text="`kind=referral_code` 전용 — 그중 유료 전환(ReferralRedemption."
        "converted_to_paid) 수. **paid(첫 PAID 이력 보유)와 정의가 달라 값이 다를 수 있다**",
    )
    # ⚠️ allow_null=True 를 주면 안 된다 — DRF get_attribute 가 키 부재 시 SkipField 보다
    # allow_null 을 먼저 보고 None 을 채워, other/link 행에도 conversion_rate=null 이 붙는다.
    # 값이 None 인 경우(사용 0건)는 allow_null 없이도 그대로 null 로 직렬화된다.
    conversion_rate = serializers.FloatField(
        required=False, help_text="`kind=referral_code` 전용 — converted/redemptions"
    )
    code_id = serializers.CharField(
        required=False,
        help_text="`kind=referral_code` 전용 (MKT-15) — `PATCH /admin/referral-codes/{code_id}/` "
        "대상 uuid. `key` 는 코드 문자열이라 이것 없이는 목록 API 조인이 필요한데, 그 경로는 "
        "marketing_viewer 가 읽을 수 없어 프론트가 권한을 이중 판정하게 된다",
    )
    can_exclude = serializers.BooleanField(
        required=False,
        help_text="`kind=referral_code` 전용 (MKT-15) — 이 요청자가 이 코드를 집계에서 뺄 수 "
        "있는지. **full 만 true**. 채널 링크의 can_exclude 와 같은 판정 함수이며, "
        "키가 없으면 **false 로 읽으세요**(게이트는 닫히는 쪽으로 실패해야 한다)",
    )


class _AttributionGapSerializer(serializers.Serializer):
    """MKT-10 — 귀속 기록이 없어 어느 채널에도 집계되지 않은 가입 인원 (데이터 품질).

    사용자 행동이 아니라 **우리 계측 공백**이라 채널 행이 아니다. 채널 표의 신뢰도
    정보로 표 밖에 두며, 비율이 크면 그 자체가 고쳐야 할 버그 신호다.
    """

    signups_unattributed = serializers.IntegerField(
        help_text="이 기간 가입자 중 SignupAttribution 행이 없는 인원. "
        "**Σrows[].signups + 이 값 == 이 기간 가입자 수**(funnel 의 signup 노드 count)"
    )
    share = serializers.FloatField(
        allow_null=True, help_text="전체 가입 대비 비율 (0~1). 가입 0이면 null"
    )
    since = serializers.DateTimeField(
        allow_null=True,
        help_text="계측 최초 기록 시각(전 기간 SignupAttribution 최솟값). 이 시각 이전 가입은 "
        "애초에 기록이 없으므로 화면에 '계측 도입 이전 가입 포함'을 덧붙일 수 있다. "
        "어트리뷰션 미탑재면 null",
    )


class _ChannelsSerializer(serializers.Serializer):
    """채널 블록 (MKT-2) — kind 판별자를 가진 이종 행 배열 + 데이터 품질 필드.

    CLN-1: 기존 ``referral_codes`` 블록은 제거됐다 — rows 의 referral_code 행이
    상위집합(사용 0건 코드 포함)이라 같은 데이터를 두 곳에 둘 이유가 없다.
    """

    rows = _ChannelRowSerializer(
        many=True,
        help_text="채널별 성과 3종 (other → link → referral_code, 각 그룹 내부는 가입 desc). "
        "어트리뷰션 미탑재여도 link/referral_code 행은 나온다(각각 admin/billing 소스). "
        "**개수 상한 없음** — 1(other) + 저장 링크 전체 + 제휴 코드 전체가 전부 실린다"
        "(잘리지 않으므로 `_truncated` 플래그도 없다). 표가 길어지면 링크를 "
        "`excluded_from_stats` 로 빼는 것이 정리 수단이다(MKT-12). 상한이 있는 것은 "
        "`sources`(8) · `funnel.available_channels`(저장 링크 10) · `combos`(10) 뿐이다",
    )
    attribution_gap = _AttributionGapSerializer(
        help_text="MKT-10 — 어느 채널에도 집계되지 않은 가입 인원 (채널 행이 아니라 표의 "
        "신뢰도 정보)"
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

    user_id = serializers.IntegerField(
        allow_null=True,
        help_text="User PK. **pii_masked=true(마케팅 조회 전용 역할)면 null** — 대신 ref 사용",
    )
    ref = serializers.CharField(
        required=False,
        help_text="회원 참조용 비가역 안정 식별자 `u_<hmac6>` (RBAC-3). 역할과 무관하게 항상 "
        "제공되며, 같은 회원은 어느 리스트에서도 같은 값 → 리스트 key·중복 인지용",
    )
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


class _CreatedBreakdownSerializer(serializers.Serializer):
    """기간 내 생성된 공개 페이지의 생성 방식 분해 (M-1).

    모집단 = new_public_pages.current (기간 내 created_at 공개 페이지) →
    ai + imported + manual == new_public_pages.current 항상 성립.
    우선순위: imported > ai > manual (임포트 후 AI 리메이크는 imported 로 1회만).
    """

    ai = serializers.IntegerField(
        help_text="AI 생성 — 성공(SUCCEEDED)한 AiJob(bio_remake/theme_generation/"
        "copy_generation) 보유 페이지 (imported 제외 후)"
    )
    imported = serializers.IntegerField(
        help_text="외부 이전 — Page.import_source ∈ {inpock, litly, linktree}"
    )
    manual = serializers.IntegerField(help_text="직접 생성 — 그 외 전부")


class _BiolinkStatsSerializer(serializers.Serializer):
    """바이오링크(페이지) 기능 통계."""

    public_pages_total = serializers.IntegerField(help_text="공개 페이지 총수 (현재 기준)")
    new_public_pages = _DeltaMetricSerializer(
        help_text="⚠ 근사 — 기간 내 created_at 인 공개 페이지 수 (공개 시각 미기록)"
    )
    created_breakdown = _CreatedBreakdownSerializer(
        help_text="생성 방식 분해 (현재 기간) — 합 == new_public_pages.current"
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


class _SnapshotPlanCountSerializer(serializers.Serializer):
    """플랜 분해 1행 — Σ count == 상위 total 보장.

    고정 패널(R-2)에서 시작해 T-1 의 체험 지표까지 함께 쓴다. 플랜 축이 '현재 구독 플랜'인지
    '체험 시작 플랜(trial_plan)'인지는 **쓰는 쪽 help_text** 에 적는다 — 두 축이 섞이면
    만료로 free 가 된 회원이 free 체험자로 보인다.
    """

    name = serializers.CharField(help_text="SubscriptionPlan.name (pro/basic)")
    display_name = serializers.CharField(help_text="플랜 표시명 (프로/베이직)")
    count = serializers.IntegerField(help_text="해당 플랜 회원 수")


class _TrialsStatsSerializer(serializers.Serializer):
    """트라이얼 통계 — started 는 레퍼럴+카드등록, 전환은 레퍼럴 코호트만.

    N-3: active(현재 체험 중) + paid_conversion_rate(전체 체험 실결제 전환율) +
    비율 2종의 한국어 계산식 문자열(conversion_formula/paid_conversion_formula) 추가.
    """

    started = _DeltaMetricSerializer(
        help_text="기간 내 시작된 트라이얼 수 = ReferralRedemption.trial_started_at ∈ 기간 "
        "+ UserSubscription.trial_used_at ∈ 기간 (카드등록 트라이얼)"
    )
    active = serializers.IntegerField(
        help_text="조회 시점 무료체험 진행 중인 회원 수 — TRIALING 상태 유료플랜 구독 "
        "(free/admin 제외, 기간과 무관한 point-in-time)"
    )
    converted = serializers.IntegerField(
        help_text="기간 내 시작한 '레퍼럴' 트라이얼 중 converted_to_paid=True (코호트)"
    )
    conversion_rate = serializers.FloatField(
        allow_null=True,
        help_text="converted / 기간 내 레퍼럴 트라이얼 시작 수 — 실결제(PAID) 기준 "
        "(converted_to_paid 는 결제 성공 시점 마킹), 카드 트라이얼은 전용 플래그 부재로 "
        "분모·분자 제외 (레퍼럴 코호트 한정)",
    )
    conversion_formula = serializers.CharField(
        help_text="conversion_rate 의 한국어 계산식 (i-아이콘 툴팁 정본)"
    )
    paid_conversion_rate = serializers.FloatField(
        allow_null=True,
        help_text="기간 내 시작한 전체 체험(레퍼럴+카드, 회원 dedupe) 중 현재까지 "
        "실제 결제(PAID)가 발생한 회원 비율 (시작자 0 → null)",
    )
    paid_conversion_formula = serializers.CharField(
        help_text="paid_conversion_rate 의 한국어 계산식 (i-아이콘 툴팁 정본)"
    )
    ended = serializers.IntegerField(
        help_text="P-1 — 이 기간에 무료체험이 '끝난' 고객 수 (유저 dedupe, 쿠폰+카드 전체). "
        "종료 = 만료·중도 해지(예정일 도래)·체험 중 결제 전환. 진행 중 체험은 제외 — "
        "체험 길이가 가변이라 시작이 아닌 종료 시점 기준의 대표 분모"
    )
    ended_converted = serializers.IntegerField(
        help_text="ended 중 실제 결제(Toss PAID)로 이어져 유지된 고객 수 (전환 판정은 조회 시점)"
    )
    ended_conversion_rate = serializers.FloatField(
        allow_null=True, help_text="ended_converted / ended (0~1, 분모 0 → null) — 대표 전환율"
    )
    ended_conversion_formula = serializers.CharField(
        help_text="ended_conversion_rate 의 한국어 계산식 (i-아이콘 툴팁 정본)"
    )
    # ── T-1: 체험 중 취소 ──
    # ⚠️ `ended - ended_converted`(종료 후 미결제)와 **다른 값**이다. 그쪽에는 취소를 누르지
    #    않고 그냥 만료된 회원과 카드 승인 실패 회원이 섞인다. 여기는 취소 시점에
    #    status==TRIALING 이었던 것만 센다.
    cancelled_during_trial = serializers.IntegerField(
        help_text="T-1 — 이 기간에 **무료체험 중 구독을 취소**한 고유 회원 수 "
        "(쿠폰·카드 체험 공통, 회원 dedupe). 취소 시점 상태로 판정하므로 그냥 만료된 회원과 "
        "결제 실패 회원은 포함되지 않는다. "
        "⚠️ **billing 0024 배포 이후의 취소만 정확** — 그 이전 취소는 cancelled_at 이 만료 "
        "다운그레이드로 덮여 복원이 불가능하고, 마이그레이션이 되살릴 수 있었던 행"
        "(아직 cancelled 로 남아 있던 것)만 들어 있다. 과거 기간일수록 과소 집계"
    )
    cancelled_during_trial_by_plan = _SnapshotPlanCountSerializer(
        many=True,
        help_text="플랜 분해 (Σ count == cancelled_during_trial). 플랜 축은 **체험을 시작한 "
        "플랜**(trial_plan) — 현재 plan 을 쓰면 만료로 free 가 된 회원이 free 로 잡힌다. "
        "플랜 레코드가 지워진 경우만 name='unknown'",
    )
    trial_cancel_rate = serializers.FloatField(
        allow_null=True,
        help_text="cancelled_during_trial ÷ 이 기간 체험 시작 **회원 수**(레퍼럴+카드 dedupe, "
        "= paid_conversion_rate 의 분모와 동일). 분모 0 → null. "
        "⚠️ `started.count` 는 이벤트 합산이라 두 종류를 다 쓴 회원이 2로 세어져 분모가 다르다",
    )
    trial_cancel_formula = serializers.CharField(
        help_text="trial_cancel_rate 의 한국어 계산식 (i-아이콘 툴팁 정본)"
    )
    cancel_accurate_since = serializers.DateTimeField(
        allow_null=True,
        help_text="T-2 — 이 시각 **이후**의 체험 취소는 정확하다는 기준(해당 환경에 "
        "billing 0024 가 적용된 시각). 이전 취소는 cancelled_at 이 만료 다운그레이드로 덮여 "
        "복원 불가라 마이그레이션이 되살릴 수 있었던 행만 들어 있다 → 조회 구간 시작이 이 "
        "시각보다 앞서면 과소 집계임을 화면에서 구분하세요. "
        "**null 이면 표시하지 마세요**(마이그레이션 기록을 못 찾은 경우 — 추측 금지)",
    )


class _FeatureStatsSerializer(serializers.Serializer):
    """기능별 사용 통계."""

    biolink = _BiolinkStatsSerializer(help_text="바이오링크(페이지)")
    dm = _DmFeatureStatsSerializer(help_text="자동 DM")
    spam = _SpamFeatureStatsSerializer(help_text="스팸 필터")
    trials = _TrialsStatsSerializer(help_text="트라이얼")


class _DropoffSampleSerializer(serializers.Serializer):
    """이탈 세그먼트 샘플 회원 1명 (CS 드릴다운용)."""

    user_id = serializers.IntegerField(
        allow_null=True,
        help_text="User PK. **pii_masked=true(마케팅 조회 전용 역할)면 null** — 대신 ref 사용",
    )
    ref = serializers.CharField(
        required=False,
        help_text="회원 참조용 비가역 안정 식별자 `u_<hmac6>` (RBAC-3). 역할과 무관하게 항상 "
        "제공되며, 같은 회원은 어느 리스트에서도 같은 값 → 리스트 key·중복 인지용",
    )
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


class _PaidPlanNoPaymentSerializer(serializers.Serializer):
    """체험·쿠폰 유료플랜 미결제 회원 (M-2 동반 지표).

    기간 내 체험(카드등록 trial_used_at)·쿠폰(레퍼럴 trial_started_at)으로 유료 플랜을
    시작했고 조회 시점까지 PAID 결제 이력이 전혀 없는 회원 수 (합집합, 유저 dedupe).
    """

    count = serializers.IntegerField(help_text="미결제 체험·쿠폰 회원 수 (경로 합집합 dedupe)")
    referral_trial = serializers.IntegerField(
        help_text="그중 쿠폰(레퍼럴) 경로 — ReferralRedemption.trial_started_at ∈ 기간"
    )
    card_trial = serializers.IntegerField(
        help_text="그중 체험(카드등록) 경로 — UserSubscription.trial_used_at ∈ 기간. "
        "두 경로 모두 탄 회원은 양쪽에 잡혀 합이 count 를 넘을 수 있음"
    )
    definition = serializers.CharField(help_text="지표 정의 (한국어, 프론트 툴팁 정본)")


class _PaidConversionAnalysisSerializer(serializers.Serializer):
    """유료 전환 분석 — 선택 플랜 / 결제 진입 경로 / 결제 후 사용 (3축 분리).

    '무엇 때문에 결제했나'를 단정하지 않는다 — 진입 경로는 CheckoutEvent 텔레메트리로
    귀속하며, 프론트 이벤트 미전송 시 entry_paths_available=false 로 강등된다.
    """

    total = serializers.IntegerField(help_text="기간 내 유료 전환자 수 (유저별 첫 PAID)")
    by_plan = _ConversionByPlanRowSerializer(
        many=True, help_text="선택 플랜별 전환자 수 (현재 구독 플랜 기준, admin/free 제외)"
    )
    paid_plan_no_payment = _PaidPlanNoPaymentSerializer(
        help_text="M-2 동반 지표 — 기간 내 체험·쿠폰으로 유료 플랜을 시작했으나 "
        "현재까지 미결제인 회원 수 ('실결제 N명 / 무료 유료플랜 M명' 병기용)"
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

    user_id = serializers.IntegerField(
        allow_null=True,
        help_text="User PK. **pii_masked=true(마케팅 조회 전용 역할)면 null** — 대신 ref 사용",
    )
    ref = serializers.CharField(
        required=False,
        help_text="회원 참조용 비가역 안정 식별자 `u_<hmac6>` (RBAC-3). 역할과 무관하게 항상 "
        "제공되며, 같은 회원은 어느 리스트에서도 같은 값 → 리스트 key·중복 인지용",
    )
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

    basis = serializers.CharField(
        help_text='현재 "approx_no_snapshot" — 일별 스냅샷 이력이 충분히 쌓여 정확 계산으로 '
        '전환되면 "snapshot" 으로 바뀜 (프론트는 basis !== "snapshot" 이면 근사 배지)'
    )
    snapshot_since = serializers.CharField(
        allow_null=True,
        help_text="P-4 — 일별 구독 스냅샷(billing.snapshot_daily_metrics) 적재 시작일 "
        "(YYYY-MM-DD). 미적재면 null. 이 날짜 이후 기간부터 정확 계산 전환 대상",
    )
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


class _RevenueByPlanRowSerializer(serializers.Serializer):
    """기간 매출의 플랜별 1행 (추가 IG 계정 과금은 제외 — 별도 블록)."""

    name = serializers.CharField(help_text='플랜 머신값. 구독이 지워진 결제는 "unknown"')
    display_name = serializers.CharField(help_text="플랜 표시명")
    net = serializers.IntegerField(help_text="이 플랜의 gross − refunded (원)")
    payments = serializers.IntegerField(help_text="결제 성공 건수")


class _RevenueExtraIgSerializer(serializers.Serializer):
    """추가 IG 계정 과금분 (주문 ID 의 -extra-/-ex- 조각으로 판별)."""

    net = serializers.IntegerField(help_text="추가 계정 과금 net (원)")
    payments = serializers.IntegerField(help_text="추가 계정 결제 건수")


class _PeriodRevenueSerializer(serializers.Serializer):
    """MKT-3 — **선택한 기간에 실제 발생한 매출**. MRR 카드를 대체한다.

    MRR 은 월 환산 반복 매출이라 기간 필터에 반응하지 않았다(7일을 골라도 30일을 골라도
    같은 값) — 옆 카드들과 시간축이 어긋나 같은 화면에서 두 기준이 섞였다.

    **귀속 규칙**
    - `gross` = 결제 시점(paid_at) 귀속. 나중에 환불돼도 **과거 gross 는 변하지 않는다**.
    - `refunded` = 환불 시점(refunded_at) 귀속. 6월 결제를 7월에 환불하면 7월에 잡힌다.
    - 같은 기간 안에서 결제 후 환불되면 양쪽에 잡혀 net 에서 상쇄된다.

    `mrr_breakdown` / `kpis.mrr` 는 그대로 유지된다 (CSV·계약 하위호환).
    """

    gross = serializers.IntegerField(help_text="기간 내 결제 성공 금액 합계 (원, 부분취소 행 제외)")
    refunded = serializers.IntegerField(help_text="기간 내 환불된 금액 (원, 양수)")
    net = serializers.IntegerField(help_text="gross − refunded — **화면 헤드라인**")
    payments = serializers.IntegerField(help_text="결제 성공 건수 (실패·재시도 실패 건 제외)")
    paying_users = serializers.IntegerField(help_text="결제한 고유 회원 수")
    previous = serializers.IntegerField(
        allow_null=True, help_text="직전 동일 기간의 net. **period=all 이면 null**"
    )
    delta_pct = serializers.FloatField(
        allow_null=True, help_text="net 기준 증감률(%). previous 가 null/0 이면 null"
    )
    by_plan = _RevenueByPlanRowSerializer(many=True, help_text="플랜별 분해 (net desc)")
    extra_ig_accounts = _RevenueExtraIgSerializer(
        help_text="추가 IG 계정 과금 — by_plan 과 배타적이라 "
        "**Σby_plan.net + extra_ig_accounts.net == net**"
    )
    vat_included = serializers.BooleanField(
        help_text="net 이 부가세 포함 금액인지. 우리는 토스에 승인 요청한 **총 결제금액**을 "
        "그대로 저장하므로 항상 true (별도 세액 필드 없음)"
    )


class _PeriodRangeSerializer(serializers.Serializer):
    """집계 기간 경계 (Asia/Seoul ISO 8601). current=[start,end), previous=직전 동일 길이."""

    current_start = serializers.DateTimeField(
        help_text="현재 기간 시작. period=all 이면 **서비스 최초 가입 시각**(회원 0명이면 now)"
    )
    current_end = serializers.DateTimeField(help_text="현재 기간 끝 (미포함) — 프리셋은 now")
    previous_start = serializers.DateTimeField(
        allow_null=True, help_text="직전 기간 시작. **period=all 이면 null** (비교 대상 없음)"
    )
    previous_end = serializers.DateTimeField(
        allow_null=True, help_text="직전 기간 끝 (미포함). **period=all 이면 null**"
    )


class _TrendChannelSliceSerializer(serializers.Serializer):
    """일별 추이의 채널 1개 분해 슬라이스 (Q-1 — 스택 막대그래프 층 재료).

    채널 귀속은 채널별 성과 표와 동일(가입 시 저장 채널 + 제휴코드 사용자 referral
    오버라이드) — visits 만 방문 자체의 저장 채널.

    ⚠️ 합계 규칙 (MKT-10 / Q-B): 사람 단위 3지표는 귀속 기록 없는 인원이 빠져 있어
    ``Σ(채널) + bucket.unattributed[m] == 버킷[m]``. visits 만 ``Σ(채널) == 버킷 visits``.
    """

    visits = serializers.IntegerField(
        help_text="이 채널 방문 수 (세션 단위 — 버킷 visits 와 동단위)"
    )
    signups = serializers.IntegerField(help_text="이 채널 귀속 가입 수")
    activated = serializers.IntegerField(
        help_text="이 채널 귀속 활성 유저 수 (버킷 activated 분해)"
    )
    paid = serializers.IntegerField(help_text="이 채널 귀속 유료 전환 수 (첫 PAID 발생일 기준)")


class _TrendUnattributedSerializer(serializers.Serializer):
    """추이 버킷의 '유입 경로 기록 없음' 인원 (MKT-10 의 추이 판 — Q-B).

    채널별 성과 표의 ``channels.attribution_gap`` 과 **같은 판정**(귀속 행 없음 &
    제휴코드 미사용)을 버킷 단위로 쪼갠 값. 채널 층으로 그리면 다시 채널처럼 읽히므로
    ``by_channel`` **밖**에 둔다 — 표에서 경고 줄로 뺀 것과 같은 이유.

    ``visits`` 는 없다 — 방문은 행 자체(UTM·리퍼러)로 판정하므로 공백이 성립하지 않는다.
    """

    signups = serializers.IntegerField(help_text="이 버킷 가입 중 귀속 기록이 없는 인원")
    activated = serializers.IntegerField(help_text="이 버킷 활성 중 귀속 기록이 없는 인원")
    paid = serializers.IntegerField(help_text="이 버킷 첫 결제 중 귀속 기록이 없는 인원")


class _TrendBucketSerializer(serializers.Serializer):
    """추이 버킷 1개 (로컬 날짜, 제로필 — 빈 버킷도 0 으로 포함)."""

    date = serializers.CharField(
        help_text="버킷 **시작일** (로컬 Asia/Seoul, YYYY-MM-DD) — "
        "granularity=week 면 그 주 월요일, month 면 그 달 1일"
    )
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
        help_text="LandingVisit.created_at TruncDate — **행 수(세션 단위)**, 퍼널/채널의 "
        "고유 방문자와 단위 다름. 어트리뷰션 미탑재 시 0"
    )
    activated = serializers.IntegerField(
        help_text="Q-1 — 그 버킷에 DM 캠페인 생성 or 페이지 공개(공개 페이지 created_at 근사)한 "
        "고유 회원 수 (**버킷 단위** user dedupe — 주별이면 같은 주 중복 활동은 1명, "
        "가입 시기 무관 이벤트 기준)"
    )
    # MKT-2: 키가 파생 채널이 아니라 channels.rows[].key 다 (표=링크 단위인데 그래프만
    # 채널 단위면 한 화면에 두 분류가 공존한다). 라벨은 rows 에서 찾아 쓸 것(단일 소스).
    by_channel = serializers.DictField(
        child=_TrendChannelSliceSerializer(),
        help_text="Q-1 — 채널 키 → {visits, signups, activated, paid} 분해. "
        "값이 전부 0인 채널은 생략 (프론트 0 처리). 채널 키 셋은 channels.rows 와 동일. "
        "**사람 단위 3지표는 귀속 공백 인원 제외** — unattributed 참고",
    )
    unattributed = _TrendUnattributedSerializer(
        help_text="MKT-10 / Q-B — 귀속 기록이 없어 by_channel 에서 제외된 인원. "
        "항등: Σby_channel[m] + unattributed[m] == 이 버킷의 [m]. 항상 존재(0 포함)"
    )


class _TrendsSerializer(serializers.Serializer):
    """추이 블록 — current 기간 전체를 로컬 날짜 기준 zero-fill (항상 포함)."""

    granularity = serializers.CharField(
        help_text='"day" | "week" | "month" — R-5: 구간이 길면 자동 상향 '
        "(<=120일 day / <=400일 week(월요일 시작) / 그 이상 month(1일 시작)). "
        "프론트는 이 값을 읽어 그대로 렌더 (day 가 아니면 일별 토글 비활성)"
    )
    buckets = _TrendBucketSerializer(
        many=True,
        help_text="버킷 시작일 오름차순 제로필. 마지막 버킷은 진행 중(미완결)일 수 있음",
    )


class _CohortRowSerializer(serializers.Serializer):
    """코호트 매트릭스 1행 (Q-2)."""

    cohort = serializers.CharField(
        help_text='코호트 키 — subscription: "YYYY-MM"(첫 결제 월), usage: "YYYY-MM-DD"(가입 주 월요일)'
    )
    size = serializers.IntegerField(help_text="코호트 크기 (해당 월 첫 결제 / 해당 주 가입 수)")
    values = serializers.ListField(
        child=serializers.FloatField(),
        help_text="values[i] = 기준 +(i+1)기간 시점의 유지/사용 비율 (0~1). "
        "아직 도래하지 않은(또는 진행 중인) 기간은 생략되어 배열이 짧아짐. size 0 → []",
    )


class _SubscriptionCohortsSerializer(serializers.Serializer):
    """구독 유지 코호트 (첫 결제 월 × M+1..M+5) — 기간 필터 무관, 최근 6개월."""

    unit = serializers.CharField(help_text='항상 "month"')
    max_periods = serializers.IntegerField(help_text="관찰 기간 수 (5 고정)")
    basis = serializers.CharField(
        help_text='"snapshot"(전 값이 일별 스냅샷 소스) | "approx"(하나라도 현재 상태 역산 — '
        "현재 유지 중이거나 다운그레이드 시각이 시점 이후면 유지로 간주). "
        '프론트는 approx 일 때 "근사" 표기'
    )
    rows = _CohortRowSerializer(many=True, help_text="최근 6개월 코호트 (오래된 월부터)")


class _UsageCohortsSerializer(serializers.Serializer):
    """제품 사용 코호트 (가입 주 × W+1..W+5) — 이벤트 로그 소급이라 항상 정확."""

    unit = serializers.CharField(help_text='항상 "week"')
    max_periods = serializers.IntegerField(help_text="관찰 기간 수 (5 고정)")
    rows = _CohortRowSerializer(
        many=True,
        help_text="최근 6주 가입 코호트 (오래된 주부터). '사용' = 그 주에 DM 캠페인 생성 · "
        "DM 발송 발생 · 페이지 생성/공개 중 1개 이상. 진행 중인 주는 값에서 생략",
    )


class _CohortsSerializer(serializers.Serializer):
    """코호트 분석 매트릭스 2종 (Q-2) — 기간 필터와 무관 (항상 최근 6개월/6주)."""

    subscription = _SubscriptionCohortsSerializer(help_text="탭 1 — 구독 유지 코호트")
    usage = _UsageCohortsSerializer(help_text="탭 2 — 제품 사용 코호트")


class _PaymentFailedRowSerializer(serializers.Serializer):
    """결제 실패 고객 1명 (Q-3 ①) — PAST_DUE 유료 구독 (dunning 중/소진)."""

    user_id = serializers.IntegerField(
        allow_null=True,
        help_text="User PK. **pii_masked=true(마케팅 조회 전용 역할)면 null** — 대신 ref 사용",
    )
    ref = serializers.CharField(
        required=False,
        help_text="회원 참조용 비가역 안정 식별자 `u_<hmac6>` (RBAC-3). 역할과 무관하게 항상 "
        "제공되며, 같은 회원은 어느 리스트에서도 같은 값 → 리스트 key·중복 인지용",
    )
    email = serializers.CharField(allow_blank=True, help_text="회원 이메일")
    plan = serializers.CharField(help_text="플랜 name")
    plan_display = serializers.CharField(help_text="플랜 표시명")
    amount = serializers.IntegerField(help_text="월 청구액 (원, 추가 IG 포함)")
    failed_at = serializers.CharField(
        allow_null=True, help_text="마지막 FAILED 결제 시각 (Asia/Seoul ISO, 없으면 null)"
    )
    reason = serializers.CharField(
        allow_blank=True, help_text="PG 실패 사유 (UserSubscription.last_billing_error, 없으면 '')"
    )
    retry_status = serializers.CharField(
        help_text="scheduled(재시도 예약됨) | exhausted(3회 소진 — 유예 만료 대기) | none"
    )
    next_retry_at = serializers.CharField(
        allow_null=True, help_text="다음 자동 재시도 예정 시각 (D+1/D+3/D+5, 없으면 null)"
    )
    retry_count = serializers.IntegerField(help_text="현재 주기 과금 시도 횟수")
    retry_max = serializers.IntegerField(help_text="최대 재시도 횟수 (3)")
    link = _UpsellLinkSerializer(help_text="회원 상세 드릴다운")


class _DormantRowSerializer(serializers.Serializer):
    """장기 미사용 고객 1명 (Q-3 ②) — 유료 ACTIVE 인데 30일+ 기능 미사용 (해지 위험)."""

    user_id = serializers.IntegerField(
        allow_null=True,
        help_text="User PK. **pii_masked=true(마케팅 조회 전용 역할)면 null** — 대신 ref 사용",
    )
    ref = serializers.CharField(
        required=False,
        help_text="회원 참조용 비가역 안정 식별자 `u_<hmac6>` (RBAC-3). 역할과 무관하게 항상 "
        "제공되며, 같은 회원은 어느 리스트에서도 같은 값 → 리스트 key·중복 인지용",
    )
    email = serializers.CharField(allow_blank=True, help_text="회원 이메일")
    plan = serializers.CharField(help_text="플랜 name")
    plan_display = serializers.CharField(help_text="플랜 표시명")
    last_active_at = serializers.CharField(
        allow_null=True,
        help_text="마지막 기능 사용 시각 (DM 발송·캠페인 생성·페이지 공개/수정·페이지 클릭 중 "
        "최신, Asia/Seoul ISO). 활동 이력이 전혀 없으면 null",
    )
    idle_days = serializers.IntegerField(
        help_text="미사용 경과일. 활동 이력 없으면 첫 결제(없으면 구독 생성) 후 경과일"
    )
    dm_30d = serializers.IntegerField(help_text="최근 30일 DM 발송 로그 수 (정의상 대개 0)")
    page_clicks_30d = serializers.IntegerField(help_text="최근 30일 페이지 클릭 수 (정의상 대개 0)")
    link = _UpsellLinkSerializer(help_text="회원 상세 드릴다운")


class _RecentChurnRowSerializer(serializers.Serializer):
    """최근 해지 고객 1명 (Q-3 ③) — 해지 '완료'(free 다운그레이드) + 실결제 이력 (윈백 대상)."""

    user_id = serializers.IntegerField(
        allow_null=True,
        help_text="User PK. **pii_masked=true(마케팅 조회 전용 역할)면 null** — 대신 ref 사용",
    )
    ref = serializers.CharField(
        required=False,
        help_text="회원 참조용 비가역 안정 식별자 `u_<hmac6>` (RBAC-3). 역할과 무관하게 항상 "
        "제공되며, 같은 회원은 어느 리스트에서도 같은 값 → 리스트 key·중복 인지용",
    )
    email = serializers.CharField(allow_blank=True, help_text="회원 이메일")
    plan = serializers.CharField(
        allow_blank=True,
        help_text="해지 전 플랜 name — 다운그레이드가 이전 플랜을 소거하므로 "
        "CancellationEvent.from_plan best-effort (미수집 시 '')",
    )
    plan_display = serializers.CharField(
        allow_blank=True, help_text="해지 전 플랜 표시명 (없으면 '')"
    )
    churned_at = serializers.CharField(help_text="해지 완료(다운그레이드) 시각 (Asia/Seoul ISO)")
    reason = serializers.CharField(allow_blank=True, help_text="해지 사유 키 (미수집 시 '')")
    reason_label = serializers.CharField(
        allow_blank=True, help_text="해지 사유 라벨 (미수집 시 '')"
    )
    tenure_months = serializers.IntegerField(help_text="첫 결제 ~ 해지까지 개월 (30일 단위 근사)")
    monthly_amount = serializers.IntegerField(help_text="마지막 PAID 결제 금액 (원)")
    link = _UpsellLinkSerializer(help_text="회원 상세 드릴다운")


class _CustomerActionsSerializer(serializers.Serializer):
    """고객 액션 리스트 3종 (Q-3) — 기간 필터와 무관한 현재 스냅샷, 각 최대 20건."""

    payment_failed = _PaymentFailedRowSerializer(
        many=True, help_text="① 결제 실패 (PAST_DUE 유료 구독, failed_at desc)"
    )
    dormant = _DormantRowSerializer(
        many=True, help_text="② 장기 미사용 (유료 ACTIVE + 30일+ 미사용, 미사용 오래된 순)"
    )
    recent_churn = _RecentChurnRowSerializer(
        many=True,
        help_text="③ 최근 해지 완료 (30일 내 free 다운그레이드 + 실결제 이력, churned_at desc). "
        "recent_cancellations(취소 예약, 아직 유료)와 상호 배타",
    )


class _SnapshotPayingSerializer(serializers.Serializer):
    """실제 결제 인원 (R-2 ①) — PAID 이력 보유 + 현재 유료 ACTIVE 구독."""

    total = serializers.IntegerField(
        help_text="실제 결제(Toss PAID) 이력이 있고 현재 유료 구독이 ACTIVE 인 고유 회원 수. "
        "**PAST_DUE(결제 실패 dunning 중)는 제외** — customer_actions.payment_failed 에 별도 "
        "집계됨. free/admin 플랜 제외"
    )
    by_plan = _SnapshotPlanCountSerializer(
        many=True, help_text="현재 구독 플랜 기준 분해 (Σ count == total)"
    )


class _SnapshotTrialingSerializer(serializers.Serializer):
    """체험 인원 (R-2 ②) — 조회 시점 TRIALING + 카드 등록 완료."""

    total = serializers.IntegerField(
        help_text="조회 시점 status=TRIALING · 유료플랜(free/admin 제외) · **카드 등록 완료**"
        "(billing_key_issued_at) 고유 회원 수. 카드 필터가 없는 "
        "feature_stats.trials.active 보다 작거나 같은 것이 정상"
    )
    by_plan = _SnapshotPlanCountSerializer(
        many=True, help_text="플랜 분해 (Σ count == total). 체험이 프로 전용이면 pro 1행"
    )


class _SnapshotTrialStartedSerializer(serializers.Serializer):
    """누적 체험 시작 인원 (T-1) — ``trialing`` 과 다르다.

    ``trialing`` 은 **지금 체험 중**인 사람, 이쪽은 **한 번이라도 체험을 시작한** 사람이다
    (전환·만료·취소 후에도 남는다). 화면 상단에서 `프로 체험 인원 / 그중 취소` 를 한 줄로
    만들 때 분모로 쓰라고 만든 값이다.
    """

    total = serializers.IntegerField(
        help_text="전체 기간 누적, 체험을 시작한 적 있는 고유 회원 수 "
        "(카드등록 체험 + 쿠폰 체험, admin 플랜 제외). feature_stats.trials.started 는 "
        "**기간 델타 + 이벤트 합산**이라 이 값과 단위가 다르다"
    )
    by_plan = _SnapshotPlanCountSerializer(
        many=True,
        help_text="체험을 시작한 플랜 기준 분해 (Σ count == total). 현재 plan 이 아니라 "
        "**trial_plan**(시작 시점 기록)이라 만료로 free 가 된 회원도 pro 로 남는다",
    )


class _SnapshotTrialNowSerializer(serializers.Serializer):
    """S-2 — **지금 체험 기간 중인** 회원 + 과금 여부 3분해 (상단 타일 전용).

    ``trialing`` / ``trial_started`` 와 무엇이 다른가:

    | 필드 | 모집단 |
    |---|---|
    | `trialing` | status==TRIALING **+ 카드 보유** (취소자·무카드 제외) |
    | `trial_started` | 전체 기간 **누적** 시작자 (전환·만료·취소 포함) |
    | **`trial_now`** | **지금 체험 기간 중**인 회원 전부 (취소자·무카드 **포함**) |

    누적값으로 타일을 만들면 "6명인데 진행 중 0 · 취소 0" 처럼 합이 맞지 않는다 —
    누적에는 이미 결제로 넘어간 회원과 만료된 회원이 섞여 있기 때문이다.
    """

    total = serializers.IntegerField(
        help_text="지금 체험 기간 중(`current_period_end` 미도래)인 유료플랜 회원 수. "
        "**취소자도 포함**한다 — 취소하면 status 는 CANCELLED 로 바뀌지만 그 회원은 "
        "기간말까지 여전히 프로를 쓰는 체험자다. **카드 미등록 체험자도 포함**한다"
    )
    by_plan = _SnapshotPlanCountSerializer(
        many=True,
        help_text="플랜 분해 (Σ count == total). 축은 **현재 plan** — 누적 지표의 trial_plan 과 "
        "다르다('지금 쓰는 플랜'을 묻는 값이고 취소자도 아직 다운그레이드 전이다)",
    )
    will_charge = serializers.IntegerField(
        help_text="체험 중 + **카드 있음** + 미취소 → 체험이 끝나면 실제로 과금된다"
    )
    cancelled = serializers.IntegerField(
        help_text="체험 중 취소를 예약한 회원(기간 남음) → 과금 없이 free 로 내려간다. "
        "판정: `cancelled_during_trial_at` 이 있고 **현재 기간 안**에서 일어난 취소 "
        "(재체험 시 과거 기록을 지우지 않으므로 기간 포함까지 봐야 유료 해지와 안 섞인다)"
    )
    no_card = serializers.IntegerField(
        help_text="체험 중 + **카드 없음** + 미취소 → 과금 대상이 아니다(쿠폰 체험). "
        "요청은 2분해였지만 이 인원이 실재하므로(prod 실측 9명) 3번째 버킷이 필요하다 — "
        "will_charge 에 넣으면 '결제 예약'이 거짓이 되고, total 에서 빼면 체험 인원이 축소된다. "
        "**계약: will_charge + cancelled + no_card == total**"
    )


class _SnapshotTrialCancelledSerializer(serializers.Serializer):
    """누적 체험 취소 인원 (S-1) — ``trial_started`` 의 부분집합.

    ``feature_stats.trials.cancelled_during_trial`` 은 **기간 종속**(``[start, end)``)이라
    이 패널(기간 무관)에 얹으면 한 타일 안에서 시간축이 섞인다. 그래서 누적판을 따로 둔다.
    """

    total = serializers.IntegerField(
        help_text="전체 기간 누적, 체험 중 구독을 취소한 적 있는 고유 회원 수 "
        "(admin 플랜 제외). trial_started 와 **같은 함수·같은 축**이라 "
        "`total <= trial_started.total` 이 항상 성립한다. "
        "⚠️ 정확도 경계는 feature_stats.trials.cancel_accurate_since 참고"
    )
    by_plan = _SnapshotPlanCountSerializer(
        many=True,
        help_text="체험을 시작한 플랜 기준 분해 (Σ count == total) — trial_started 와 동일 축",
    )


class _SnapshotSerializer(serializers.Serializer):
    """상단 고정 패널 (R-2) — **전체 기간 누적, period/커스텀 범위와 무관**.

    period=7d 응답에도 period=all 응답에도 같은 값이 들어간다 (별도 캐시 키 공유).
    """

    as_of = serializers.DateTimeField(help_text="스냅샷 산출 시각 (Asia/Seoul ISO)")
    paying = _SnapshotPayingSerializer(help_text="① 실제 결제 인원")
    trialing = _SnapshotTrialingSerializer(help_text="② 체험 인원 (카드 등록 완료 기준)")
    trial_now = _SnapshotTrialNowSerializer(
        help_text="② S-2 — **지금 체험 기간 중**인 회원 + 과금 여부 3분해. "
        "상단 체험 타일은 이 값으로 만드세요(`will_charge + cancelled + no_card == total`). "
        "`trialing` 은 취소자·무카드가 빠져 합이 맞지 않습니다"
    )
    trial_started = _SnapshotTrialStartedSerializer(
        help_text="②' T-1 — 전체 기간 누적 **체험을 시작한 적 있는** 회원 (현재 진행 여부 무관). "
        "누적이라 상단 타일에는 부적합 — CSV 추이용"
    )
    trial_cancelled = _SnapshotTrialCancelledSerializer(
        help_text="②'' S-1 — 전체 기간 누적 **체험 중 취소한** 회원. trial_started 와 같은 "
        "축이라 `trial_cancelled.total <= trial_started.total` 이 항상 성립한다"
    )
    visitors = serializers.IntegerField(
        help_text="③ 전체 기간 고유 방문자 수 (distinct visitor_id). "
        "attribution_available=false 면 0"
    )
    signups = serializers.IntegerField(help_text="④ 누적 가입 회원 수")
    activated = serializers.IntegerField(
        help_text="⑤ **가입 시기 무관** 공개 페이지 보유 ∪ DM 캠페인 보유 고유 회원 수 "
        "(중복 제거). funnel.activation.count 는 '이 기간 가입 코호트' 기준이라 정의가 다름 "
        "— period=all 에서만 두 값이 일치"
    )


class AdminMarketingDashboardSerializer(serializers.Serializer):
    """마케팅 대시보드 단일 집계 응답 (전 워크스페이스 GLOBAL, Redis 5분 캐시)."""

    period = serializers.CharField(
        help_text="적용된 기간 (7d/30d/90d/all) 또는 커스텀 범위면 'custom'"
    )
    range = _PeriodRangeSerializer(
        help_text="현재/직전 기간 경계 (커스텀은 previous=직전 동일 길이 구간, "
        "period=all 은 previous_*=null)"
    )
    generated_at = serializers.DateTimeField(
        help_text="집계 생성 시각 — 캐시 신선도 표시용 (TTL: 프리셋/커스텀 300s, all 900s)"
    )
    attribution_available = serializers.BooleanField(
        help_text="어트리뷰션 서브시스템(apps.analytics) 탑재 여부 — false 면 "
        "visits/unique_visitors=0, channels.rows=[] 로 강등"
    )
    pii_masked = serializers.BooleanField(
        required=False,
        help_text="RBAC-3 — 이 응답의 고객 개인정보가 **서버에서** 마스킹됐는지. "
        "marketing_viewer 역할이면 true (email 부분 마스킹 · user_id=null · link 비움 · "
        "referral_codes[].description 제거), full 역할이면 false. "
        "true 면 프론트는 안내 배너 + 이메일 복사/검색 UI 숨김",
    )
    snapshot = _SnapshotSerializer(
        help_text="R-2 — 상단 고정 패널 (전체 기간 누적, 기간 파라미터와 무관)"
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
    cohorts = _CohortsSerializer(
        help_text="코호트 분석 매트릭스 2종 (Q-2) — 기간 필터 무관 (최근 6개월/6주 고정)"
    )
    customer_actions = _CustomerActionsSerializer(
        help_text="고객 액션 리스트 3종 (Q-3) — 기간 필터 무관 (현재 스냅샷, 각 20건)"
    )
    plan_distribution = _PlanDistributionRowSerializer(
        many=True, help_text="플랜별 구독 상태 분포 (전 플랜, sort_order 순)"
    )
    period_revenue = _PeriodRevenueSerializer(
        help_text="MKT-3 — 선택 기간에 실제 발생한 매출 (MRR 카드 대체)"
    )
    mrr_breakdown = _MrrBreakdownSerializer(
        help_text="MRR 브레이크다운 (point-in-time). 화면에서는 period_revenue 로 대체됐지만 "
        "CSV·계약 하위호환을 위해 유지"
    )
