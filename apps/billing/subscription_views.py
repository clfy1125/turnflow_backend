"""
Subscription API views — 구독 관리
"""

import logging

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import SubscriptionPlan, SubscriptionStatus
from .serializers import (
    ChangeSubscriptionRequestSerializer,
    IGAccountActivationRequestSerializer,
    IGAccountActivationStateSerializer,
    PageActivationRequestSerializer,
    PageActivationStateSerializer,
    PaymentHistorySerializer,
    SubscriptionPlanSerializer,
    UserSubscriptionSerializer,
)
from .subscription_utils import ensure_subscription, get_ig_account_allowance
from .toss_flows import BillingFlowError, change_plan, preview_change_plan

logger = logging.getLogger(__name__)


class SubscriptionPlanListView(APIView):
    """활성 플랜 목록 조회 (인증 불필요)"""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["사용자플랜"],
        summary="구독 플랜 목록 조회",
        description="""
## 목적
서비스에서 제공하는 **활성화된 구독 플랜 목록**을 반환합니다.
요금제 선택 UI, 업그레이드 안내 페이지 등에서 사용합니다.

## 인증
**불필요** — 비로그인 상태에서도 플랜 목록 확인 가능

## 사용 시나리오
- 요금제 비교 페이지 렌더링
- 로그인 전 요금제 안내
- 업그레이드 모달에서 플랜 목록 표시

## 플랜 구조 (운영 DB 기준 · 가변)

| 플랜 | name | 판매가/월 | 정가 | 주요 기능 |
|------|------|----------|------|-----------|
| 무료 | `free` | 0원 | - | 페이지 1개, AI 생성 2회, DM 자동화 월 200건, 커스텀 CSS |
| 베이직 | `basic` | 3,900원 | 5,900원 | 페이지 5개, 배지 제거, AI 무제한, 기간별 분석·엑셀 |
| 프로 | `pro` | 9,900원 (프로모) | 15,900원 | DM 무제한, 스팸 댓글 필터, 다계정(+9,900원/추가 계정) |

> 플랜 집합·가격은 **DB-driven(가변)** 입니다 — 프론트는 이 목록을 fetch 해 렌더하고 값을 하드코딩하지 마세요.
> `monthly_price < list_price`이면 **할인 판매 중**입니다 (정가에 취소선 표시).
> 프로 플랜은 **최초 카드 등록 시 1개월 무료** (제휴 코드 입력 시 +1개월). 결제 시점 가격이
> 구독에 고정(그랜드파더링)되므로 프로모 종료 후에도 기존 구독자는 가입 가격을 유지합니다.
> 본 엔드포인트는 **활성(is_active=True) 플랜만** 반환합니다. 운영용 `admin`(관리자) 플랜은
> 비활성이라 여기 나오지 않습니다. 비활성 포함 전체 목록이 필요하면(어드민) `GET /api/v1/admin/subscription-plans/`.

## 응답 필드 설명
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | uuid | 플랜 고유 ID |
| `name` | string | 플랜 코드명 (`free` / `basic` / `pro`) |
| `display_name` | string | UI 표시용 이름 (`무료` / `베이직` / `프로`) |
| `monthly_price` | int | 현재 판매가 (원/월). 0이면 무료 |
| `list_price` | int | 정가 (원/월). monthly_price보다 크면 할인 중 |
| `features` | object | 기능 제한 설정 (아래 참조) |
| `sort_order` | int | UI 정렬 순서 (오름차순) |

## `features` 객체 구조
```json
{
  "max_pages": 1,            // 최대 링크페이지 수 (-1 = 무제한)
  "ai_generation": true,     // AI 페이지 생성 제공 여부 (표시용)
  "ai_unlimited": false,     // AI 생성 무제한 여부 (false면 가입 시 2회 제공)
  "remove_logo": false,      // 턴플로우 배지 제거 가능 여부
  "custom_css": true,        // 커스텀 CSS 사용 가능 여부
  "dm_monthly_limit": 200,   // DM 자동화 월 발송 한도 (-1 = 무제한)
  "analytics_export": false, // 기간별 분석·엑셀 다운로드 제공 여부
  "spam_filter": false,      // 스팸 댓글 필터링 제공 여부
  "dm_recovery": false,      // 실패 DM 복구(대댓글 안내→재전송) 제공 여부 (프로 전용)
  "max_ig_accounts": 1       // 연동 가능 IG 계정 수 (-1 = 무제한, 프로는 추가 구매 가능)
}
```

## 프론트엔드 통합
```typescript
// 요금제 페이지에서 플랜 목록 fetch
const res = await fetch('/api/v1/billing/plans/');
const plans = await res.json();

// 플랜별 가격 표시 (할인 중이면 정가 취소선)
plans.forEach(plan => {
  const onSale = plan.list_price > plan.monthly_price;
  console.log(
    `${plan.display_name}: ${plan.monthly_price.toLocaleString()}원/월` +
    (onSale ? ` (정가 ${plan.list_price.toLocaleString()}원)` : '')
  );
});
```
        """,
        responses={
            200: OpenApiResponse(
                response=SubscriptionPlanSerializer(many=True),
                description="활성 플랜 목록 (sort_order 오름차순)",
                examples=[
                    OpenApiExample(
                        "플랜 목록 (활성 플랜만)",
                        value=[
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440001",
                                "name": "free",
                                "display_name": "무료",
                                "monthly_price": 0,
                                "list_price": 0,
                                "features": {
                                    "max_pages": 1,
                                    "ai_generation": True,
                                    "ai_unlimited": False,
                                    "remove_logo": False,
                                    "custom_css": True,
                                    "dm_monthly_limit": 200,
                                    "analytics_export": False,
                                    "spam_filter": False,
                                    "dm_recovery": False,
                                    "max_ig_accounts": 1,
                                },
                                "sort_order": 0,
                            },
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440003",
                                "name": "basic",
                                "display_name": "베이직",
                                "monthly_price": 3900,
                                "list_price": 5900,
                                "features": {
                                    "max_pages": 5,
                                    "ai_generation": True,
                                    "ai_unlimited": True,
                                    "remove_logo": True,
                                    "custom_css": True,
                                    "dm_monthly_limit": 200,
                                    "analytics_export": True,
                                    "spam_filter": False,
                                    "dm_recovery": False,
                                    "max_ig_accounts": 1,
                                },
                                "sort_order": 1,
                            },
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440002",
                                "name": "pro",
                                "display_name": "프로",
                                "monthly_price": 9900,
                                "list_price": 15900,
                                "features": {
                                    "max_pages": 5,
                                    "ai_generation": True,
                                    "ai_unlimited": True,
                                    "remove_logo": True,
                                    "custom_css": True,
                                    "dm_monthly_limit": -1,
                                    "analytics_export": True,
                                    "spam_filter": True,
                                    "dm_recovery": True,
                                    "max_ig_accounts": 1,
                                },
                                "sort_order": 2,
                            },
                        ],
                    )
                ],
            ),
        },
    )
    def get(self, request):
        plans = SubscriptionPlan.objects.filter(is_active=True)
        serializer = SubscriptionPlanSerializer(plans, many=True)
        return Response(serializer.data)


class MySubscriptionView(APIView):
    """내 구독 조회"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="내 구독 정보 조회",
        description="""
## 목적
현재 로그인된 사용자의 **개인 구독 정보**를 반환합니다.
구독이 없는 사용자는 **자동으로 무료(Free) 플랜이 생성**됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 사용 시나리오
- 앱 초기화 시 현재 플랜 확인
- 설정 페이지에서 구독 상태 표시
- 기능 제한 체크 전 플랜 확인
- 결제 주기/만료일 안내

## 자동 생성 정책
구독 레코드가 없는 사용자가 이 API를 호출하면 **Free 플랜 구독이 자동 생성**됩니다.
별도의 구독 생성 API 호출은 불필요합니다.

## 응답 필드 설명
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | uuid | 구독 고유 ID |
| `plan` | object | 현재 플랜 상세 (SubscriptionPlan 객체, `list_price` 포함) |
| `plan_id` | uuid | 현재 플랜 ID |
| `status` | string | `active` / `cancelled` / `past_due` / `trialing` |
| `current_period_start` | datetime | 현재 결제 주기 시작일 (ISO 8601) |
| `current_period_end` | datetime | 현재 결제 주기 종료일 (ISO 8601). null이면 무기한 |
| `has_billing_key` | bool | 결제 카드 등록 여부 |
| `card_company` / `card_number_masked` | string | 등록된 카드 표시 정보 |
| `monthly_amount_snapshot` | int | 고정된 월 청구액 (가입 시점 가격 — 프로모 그랜드파더링) |
| `extra_ig_accounts` | int | 구매한 추가 IG 계정 수 (프로) |
| `pending_plan_name` | string | 예약된 플랜 변경 (다음 갱신 시 적용). null이면 없음 |
| `trial_used_at` | datetime | 프로 무료 체험 사용 시각 (1인 1회) |
| `cancelled_at` | datetime | 취소 시각. null이면 취소하지 않음 |
| `trial_ends_at` | datetime | 체험 종료(=첫 결제) 예정일. trialing일 때만 값 존재 |
| `next_billing` | object | `{date, amount}` — 다음 자동결제 예정. 무료/체험/해지예약이면 date null |
| `usage` | object | 사용량 현황 (아래 참조) |

## `usage` 객체
| 필드 | 설명 |
|------|------|
| `pages` | `{used, limit}` — 보유 페이지 수 / 플랜 한도 (-1=무제한) |
| `dm` | `{used, limit, period_start, period_end}` — 이번 달 DM 발송량 / 한도 (-1=무제한) |
| `ig_accounts` | `{used, limit}` — 활성 IG 연동 수 / 허용 수 (기본+추가구매, -1=무제한) |
| `ai_tokens` | `{balance, unlimited}` — 잔여 AI 토큰 (유료 플랜은 unlimited=true) |

## `status` 값 의미
| 상태 | 설명 | 기능 사용 |
|------|------|-----------|
| `active` | 정상 구독 중 | ✅ 가능 |
| `trialing` | 무료 체험 중 (종료 시 자동 첫 결제) | ✅ 가능 |
| `cancelled` | 해지 예약 (period_end까지 유지, 재개 가능) | ✅ period_end까지 가능 |
| `past_due` | 결제 실패 (재시도 중 — 카드 변경 시 즉시 재시도) | ⚠️ 유예 기간 (7일) |

## 프론트엔드 통합
```typescript
// 앱 초기화 시 구독 정보 fetch
const res = await fetch('/api/v1/billing/my-subscription/', {
  headers: { 'Authorization': `Bearer ${accessToken}` }
});
const subscription = await res.json();

// 현재 플랜 확인
const isPaid = subscription.plan.name !== 'free';
const maxPages = subscription.plan.features.max_pages; // -1 = 무제한

// DM 사용량 게이지
const { used, limit } = subscription.usage.dm;
if (limit !== -1 && used / limit > 0.9) showUpgradeBanner();

// 체험 안내
if (subscription.status === 'trialing') {
  console.log(`무료 체험 중 — ${subscription.trial_ends_at}에 첫 결제`);
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
        """,
        responses={
            200: OpenApiResponse(
                response=UserSubscriptionSerializer,
                description="내 구독 정보",
                examples=[
                    OpenApiExample(
                        "무료 플랜 사용자",
                        value={
                            "id": "a1b2c3d4-0000-0000-0000-000000000001",
                            "plan": {
                                "id": "550e8400-e29b-41d4-a716-446655440001",
                                "name": "free",
                                "display_name": "무료",
                                "monthly_price": 0,
                                "list_price": 0,
                                "features": {"max_pages": 1, "dm_monthly_limit": 200},
                                "sort_order": 0,
                            },
                            "plan_id": "550e8400-e29b-41d4-a716-446655440001",
                            "status": "active",
                            "current_period_start": "2026-07-01T00:00:00Z",
                            "current_period_end": None,
                            "has_billing_key": False,
                            "card_company": "",
                            "card_number_masked": "",
                            "monthly_amount_snapshot": None,
                            "extra_ig_accounts": 0,
                            "pending_plan_name": None,
                            "trial_used_at": None,
                            "cancelled_at": None,
                            "usage": {
                                "pages": {"used": 1, "limit": 1},
                                "dm": {
                                    "used": 45,
                                    "limit": 200,
                                    "period_start": "2026-07-01T00:00:00+09:00",
                                    "period_end": "2026-08-01T00:00:00+09:00",
                                },
                                "ig_accounts": {"used": 1, "limit": 1},
                                "ai_tokens": {"balance": 2, "unlimited": False},
                            },
                            "trial_ends_at": None,
                            "next_billing": {"date": None, "amount": None},
                        },
                    ),
                    OpenApiExample(
                        "프로 무료 체험 중 (카드 등록됨)",
                        value={
                            "id": "a1b2c3d4-0000-0000-0000-000000000002",
                            "plan": {
                                "id": "550e8400-e29b-41d4-a716-446655440002",
                                "name": "pro",
                                "display_name": "프로",
                                "monthly_price": 9900,
                                "list_price": 15900,
                                "features": {"max_pages": 5, "dm_monthly_limit": -1},
                                "sort_order": 2,
                            },
                            "plan_id": "550e8400-e29b-41d4-a716-446655440002",
                            "status": "trialing",
                            "current_period_start": "2026-07-01T00:00:00Z",
                            "current_period_end": "2026-07-31T00:00:00Z",
                            "has_billing_key": True,
                            "card_company": "현대",
                            "card_number_masked": "433012******123*",
                            "monthly_amount_snapshot": 9900,
                            "extra_ig_accounts": 0,
                            "pending_plan_name": None,
                            "trial_used_at": "2026-07-01T00:00:00Z",
                            "cancelled_at": None,
                            "usage": {
                                "pages": {"used": 3, "limit": 5},
                                "dm": {
                                    "used": 0,
                                    "limit": -1,
                                    "period_start": "2026-07-01T00:00:00+09:00",
                                    "period_end": "2026-08-01T00:00:00+09:00",
                                },
                                "ig_accounts": {"used": 1, "limit": 1},
                                "ai_tokens": {"balance": 2, "unlimited": True},
                            },
                            "trial_ends_at": "2026-07-31T00:00:00Z",
                            "next_billing": {"date": "2026-07-31T00:00:00Z", "amount": 9900},
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰이 없거나 만료됨"),
        },
    )
    def get(self, request):
        from apps.pages.models import Page

        from .dm_limits import count_owner_dms_this_month, get_dm_monthly_limit
        from .models import AiTokenBalance
        from .models import SubscriptionStatus as Status
        from .subscription_utils import (
            count_active_ig_connections,
            get_ig_account_allowance,
            get_user_plan,
        )

        sub = ensure_subscription(request.user)
        plan = get_user_plan(request.user)  # cancelled+기간 내면 유료 플랜 유지
        data = UserSubscriptionSerializer(sub).data

        # ── 사용량 합성 ──
        dm_limit = get_dm_monthly_limit(request.user)
        dm_used = 0 if dm_limit == -1 else count_owner_dms_this_month(request.user)
        from apps.integrations.campaign_stats import _month_bounds

        period_start, period_end = _month_bounds()
        token_balance = AiTokenBalance.objects.filter(user=request.user).first()

        data["usage"] = {
            "pages": {
                "used": Page.objects.filter(user=request.user).count(),
                "limit": plan.features.get("max_pages", 1),
            },
            "dm": {
                "used": dm_used,
                "limit": dm_limit,
                "period_start": period_start,
                "period_end": period_end,
            },
            "ig_accounts": {
                "used": count_active_ig_connections(request.user),
                "limit": get_ig_account_allowance(request.user),
            },
            "ai_tokens": {
                "balance": token_balance.balance if token_balance else 0,
                "unlimited": plan.name != "free",
            },
        }

        # ── 트라이얼/다음 결제 ──
        data["trial_ends_at"] = sub.current_period_end if sub.status == Status.TRIALING else None
        next_billing_date = None
        if (
            sub.has_billing_key
            and sub.current_period_end
            and sub.status
            in (
                Status.ACTIVE,
                Status.TRIALING,
            )
        ):
            next_billing_date = sub.current_period_end
        elif sub.status == Status.PAST_DUE:
            next_billing_date = sub.next_billing_retry_at
        data["next_billing"] = {
            "date": next_billing_date,
            "amount": sub.renewal_amount if next_billing_date else None,
        }

        return Response(data)


class ChangeSubscriptionView(APIView):
    """플랜 변경 요청"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="구독 플랜 변경 (유료 ↔ 유료)",
        description="""
## 목적
**빌링키(카드)가 등록된 유료 구독자**의 플랜을 변경합니다.
무료 사용자의 구독 시작은 `POST /billing/toss/confirm/`, 무료 전환(해지)은
`POST /billing/cancel/`을 사용하세요.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `plan_name` | ✅ | `basic`/`pro` | 변경할 플랜 코드명 |
| `extra_ig_accounts` | 선택 | int (0~10) | pro 업그레이드 시 함께 설정할 추가 IG 계정 수 |

## 변경 규칙
| 방향 | 동작 |
|------|------|
| **업그레이드** (basic→pro) | **남은 기간분 차액만 즉시 비례 결제**(신규 플랜 잔여분 − 기존 플랜 잔여 크레딧) + **결제 주기(갱신일) 유지**. 다음 갱신부터 신규 플랜 전액. 잔여 차액이 0이면 `payment`가 null이며 무과금으로 즉시 적용 |
| **다운그레이드** (pro→basic) | **예약** — 현재 주기 종료(다음 갱신)에 적용. 응답 `effective_at` 참고 |
| 같은 플랜 + 예약 있음 | 예약 취소 |
| 같은 플랜 + 예약 없음 | 400 |

## 변경 불가 조건
| 상황 | 응답 |
|------|------|
| 무료 플랜 사용자 | 400 — confirm API 안내 |
| 카드 미등록 | 400 |
| 무료 체험 중 | 400 — 체험 종료 후 변경 |
| 미납(past_due) | 400 — 카드 갱신으로 미납 해소 먼저 |
| 해지 예약(cancelled) | 400 — 재개 후 변경 |

## 프론트엔드 통합
```typescript
const res = await fetch('/api/v1/billing/change-plan/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${accessToken}`
  },
  body: JSON.stringify({ plan_name: 'pro' })
});
const data = await res.json();
if (data.effective_at) {
  // 다운그레이드 예약됨 — effective_at에 적용
} else if (data.payment) {
  // 업그레이드 즉시 결제 완료
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 변경 불가 조건 (위 표 참고) |
| 401 | 토큰 없음/만료 |
| 402 | 업그레이드 결제 카드 거절 |
| 404 | 존재하지 않거나 비활성 플랜 |
| 202 | 결제 결과 확인 중 (네트워크 모호) |
| 502 | 토스 API 통신 오류 |
        """,
        request=ChangeSubscriptionRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="변경 완료 (업그레이드=즉시, 다운그레이드=예약)",
                examples=[
                    OpenApiExample(
                        "업그레이드 즉시 결제",
                        value={
                            "detail": "프로 플랜으로 업그레이드되었습니다.",
                            "subscription": {
                                "plan": {"name": "pro", "display_name": "프로"},
                                "status": "active",
                                "monthly_amount_snapshot": 9900,
                            },
                            "payment": {"amount": 9900, "status": "paid"},
                            "effective_at": None,
                        },
                    ),
                    OpenApiExample(
                        "다운그레이드 예약",
                        value={
                            "detail": "베이직 플랜으로 변경이 예약되었습니다. 현재 결제 주기가 끝나는 시점에 적용됩니다.",
                            "subscription": {
                                "plan": {"name": "pro"},
                                "pending_plan_name": "basic",
                            },
                            "payment": None,
                            "effective_at": "2026-08-01T00:00:00Z",
                        },
                    ),
                ],
            ),
            202: OpenApiResponse(description="결제 결과 확인 중 (네트워크 모호)"),
            400: OpenApiResponse(description="변경 불가 조건"),
            401: OpenApiResponse(description="인증 실패"),
            402: OpenApiResponse(description="업그레이드 결제 거절"),
            404: OpenApiResponse(description="플랜을 찾을 수 없음"),
            502: OpenApiResponse(description="토스 API 통신 오류"),
        },
    )
    def post(self, request):
        serializer = ChangeSubscriptionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = change_plan(
                request.user,
                plan_name=serializer.validated_data["plan_name"],
                extra_ig_accounts=serializer.validated_data.get("extra_ig_accounts") or 0,
            )
        except BillingFlowError as e:
            body = {"detail": e.detail, **e.extra}
            return Response(body, status=e.status_code)

        return Response(
            {
                "detail": result["detail"],
                "subscription": UserSubscriptionSerializer(result["subscription"]).data,
                "payment": (
                    PaymentHistorySerializer(result["payment"]).data if result["payment"] else None
                ),
                "effective_at": result["effective_at"],
            }
        )


class ChangePlanPreviewView(APIView):
    """플랜 변경 견적 (미리보기) — 부작용 없음"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="플랜 변경 견적 (결제 전 미리보기)",
        description="""
## 목적
`POST /billing/change-plan/`을 **실제로 실행하기 전에**, 지금 변경하면 **즉시 얼마가
청구되는지**를 계산해 돌려줍니다. **부작용이 전혀 없습니다**(토스 호출·DB 변경·결제 없음).
실행 API와 **동일한 계산 로직**을 공유하므로 견적 금액 = 실제 청구 금액입니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `plan_name` | ✅ | `basic`/`pro` | 변경하려는 플랜 코드명 |
| `extra_ig_accounts` | 선택 | int (0~10) | pro 업그레이드 시 함께 설정할 추가 IG 계정 수 |

## 응답 필드
| 필드 | 설명 |
|------|------|
| `direction` | `upgrade`(즉시 비례 청구) / `downgrade`(예약, 무과금) / `noop`(동일 플랜) |
| `immediate_charge.amount` | **지금 즉시 청구될 금액(원, 세포함)**. 0이면 무과금 즉시 적용 |
| `immediate_charge.proration` | 업그레이드 시 계산 내역: `remaining_days`(잔여일), `new_plan_prorated`(신규 잔여분), `current_plan_credit`(기존 크레딧), `net`(순청구=amount) |
| `effective_at` | 다운그레이드면 적용 시각(=현재 주기 종료일), 그 외 null |
| `next_renewal_amount` | 변경 후 **다음 정기 갱신 예정 총액(전액)** — 즉시 청구액과 별개 |
| `next_renewal_at` | 다음 갱신 예정 시각(현재 주기 종료일) |

> ⚠️ `immediate_charge.amount`(지금 청구)와 `next_renewal_amount`(다음 갱신 전액)는
> 다른 값입니다. 업그레이드는 남은 기간분 **차액만** 지금 청구하고, 다음 갱신부터 전액입니다.

## 프론트엔드 통합 (2단계: 견적 → 확정)
```typescript
// 1) 견적
const q = await (await fetch('/api/v1/billing/change-plan/preview/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
  body: JSON.stringify({ plan_name: 'pro' }),
})).json();
// "지금 6,000원이 결제됩니다" 확인 UI 노출: q.immediate_charge.amount

// 2) 사용자가 확정하면 실행 API 호출
if (confirm) {
  await fetch('/api/v1/billing/change-plan/', { method: 'POST', /* 동일 body */ });
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 변경 불가 조건(무료 사용자·카드 미등록·체험/미납/해지 상태·동일 플랜) |
| 401 | 토큰 없음/만료 |
| 404 | 존재하지 않거나 비활성 플랜 |
        """,
        request=ChangeSubscriptionRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="견적 (부작용 없음)",
                examples=[
                    OpenApiExample(
                        "업그레이드 견적 (basic→pro, 잔여 12일)",
                        value={
                            "direction": "upgrade",
                            "immediate_charge": {
                                "amount": 2400,
                                "currency": "KRW",
                                "description": "프로 잔여 12일분 비례 청구 (기존 플랜 크레딧 -1560원 차감)",
                                "proration": {
                                    "period_days": 30,
                                    "remaining_days": 12,
                                    "new_plan_prorated": 3960,
                                    "current_plan_credit": 1560,
                                    "net": 2400,
                                },
                            },
                            "effective_at": None,
                            "next_renewal_amount": 9900,
                            "next_renewal_at": "2026-08-08T00:00:00+09:00",
                        },
                    ),
                    OpenApiExample(
                        "다운그레이드 견적 (pro→basic, 무과금·예약)",
                        value={
                            "direction": "downgrade",
                            "immediate_charge": {
                                "amount": 0,
                                "currency": "KRW",
                                "description": "베이직 다운그레이드는 다음 갱신에 적용됩니다(무과금).",
                                "proration": None,
                            },
                            "effective_at": "2026-08-08T00:00:00+09:00",
                            "next_renewal_amount": 5900,
                            "next_renewal_at": "2026-08-08T00:00:00+09:00",
                        },
                    ),
                ],
            ),
            400: OpenApiResponse(description="변경 불가 조건"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="플랜을 찾을 수 없음"),
        },
    )
    def post(self, request):
        serializer = ChangeSubscriptionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            quote = preview_change_plan(
                request.user,
                plan_name=serializer.validated_data["plan_name"],
                extra_ig_accounts=serializer.validated_data.get("extra_ig_accounts") or 0,
            )
        except BillingFlowError as e:
            return Response({"detail": e.detail, **e.extra}, status=e.status_code)

        return Response(quote)


class CancelSubscriptionView(APIView):
    """구독 취소"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="구독 취소",
        description="""
## 목적
현재 유료 구독을 취소합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 사용 시나리오
- 설정 > 구독 관리 > 구독 취소 버튼

## 취소 동작
- `status`가 `cancelled`로 변경됨
- `cancelled_at`에 취소 시각이 기록됨
- **`current_period_end`까지 기존 유료 기능 계속 사용 가능**
- 기간 만료 후 자동으로 무료 플랜으로 전환 예정

## 취소 불가 조건
| 상황 | 응답 코드 | 메시지 |
|------|-----------|--------|
| 무료 플랜 사용자 | 400 | `"무료 플랜은 취소할 수 없습니다."` |
| 이미 취소된 구독 | 400 | `"이미 취소된 구독입니다."` |

## 프론트엔드 통합
```typescript
// 구독 취소
const res = await fetch('/api/v1/billing/cancel/', {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${accessToken}` }
});

if (res.ok) {
  const sub = await res.json();
  const endDate = new Date(sub.current_period_end);
  alert(`구독이 취소되었습니다. ${endDate.toLocaleDateString()}까지 기존 기능을 사용할 수 있습니다.`);
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 무료 플랜 취소 시도 / 이미 취소된 구독 |
| 401 | 토큰 없음/만료 |
        """,
        responses={
            200: OpenApiResponse(
                response=UserSubscriptionSerializer,
                description="취소된 구독 정보",
                examples=[
                    OpenApiExample(
                        "취소 성공",
                        value={
                            "id": "a1b2c3d4-0000-0000-0000-000000000002",
                            "plan": {"id": "550e8400-...", "name": "pro", "display_name": "프로"},
                            "status": "cancelled",
                            "current_period_end": "2026-05-01T00:00:00Z",
                            "cancelled_at": "2026-04-13T12:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="취소 불가",
                examples=[
                    OpenApiExample(
                        "무료 플랜", value={"detail": "무료 플랜은 취소할 수 없습니다."}
                    ),
                    OpenApiExample("이미 취소됨", value={"detail": "이미 취소된 구독입니다."}),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        sub = ensure_subscription(request.user)

        if sub.plan.name == "free":
            return Response(
                {"detail": "무료 플랜은 취소할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if sub.status == SubscriptionStatus.CANCELLED:
            return Response(
                {"detail": "이미 취소된 구독입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 토스 호출 불필요 — 갱신 스케줄러가 CANCELLED 구독을 과금하지 않는 것이 곧 해지.
        # 빌링키는 유지해 기간 내 재개(resume)를 즉시 가능하게 한다.
        # 기간 만료 시 handle_cancelled_expiry 가 다운그레이드 + 빌링키 삭제를 수행.
        sub.status = SubscriptionStatus.CANCELLED
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=["status", "cancelled_at", "updated_at"])

        return Response(UserSubscriptionSerializer(sub).data)


class ResumeSubscriptionView(APIView):
    """취소(일시정지)된 구독 재개"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="구독 재개",
        description="""
## 목적
취소(해지 예약)한 유료 구독을 **재개**합니다.
`current_period_end` 이전, 결제 카드가 등록되어 있을 때만 재개 가능합니다.

## 동작
- `status`가 `active`로 복원, `cancelled_at`이 초기화됩니다
- 다음 갱신 시점부터 등록된 카드로 자동결제가 재개됩니다 (외부 호출 없음 — 즉시 완료)

## 재개 불가 조건
| 상황 | 응답 코드 | 메시지 |
|------|-----------|--------|
| 취소 상태가 아닌 구독 | 400 | "취소된 구독만 재개할 수 있습니다." |
| 구독 기간 만료 | 400 | "구독 기간이 만료되어 재개할 수 없습니다. 새로 결제해주세요." |
| 결제 카드 미등록 | 400 | "결제 카드가 등록되어 있지 않습니다. 카드 등록 후 재개해주세요." |
        """,
        responses={
            200: OpenApiResponse(
                response=UserSubscriptionSerializer, description="재개된 구독 정보"
            ),
            400: OpenApiResponse(description="재개 불가"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        sub = ensure_subscription(request.user)

        if sub.status != SubscriptionStatus.CANCELLED:
            return Response(
                {"detail": "취소된 구독만 재개할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 기간 만료 체크
        if sub.current_period_end and sub.current_period_end <= timezone.now():
            return Response(
                {"detail": "구독 기간이 만료되어 재개할 수 없습니다. 새로 결제해주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 갱신 과금이 가능해야 재개 의미가 있음 (무카드 레퍼럴 트라이얼 해지 등 방어)
        if not sub.has_billing_key:
            return Response(
                {"detail": "결제 카드가 등록되어 있지 않습니다. 카드 등록 후 재개해주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sub.status = SubscriptionStatus.ACTIVE
        sub.cancelled_at = None
        sub.save(update_fields=["status", "cancelled_at", "updated_at"])

        return Response(UserSubscriptionSerializer(sub).data)


class PageActivationView(APIView):
    """페이지 활성화 조정 (다운그레이드 후 플랜 한도에 맞춰 활성 페이지 선택)

    ig-account-activation 의 페이지 판. 페이지에는 두 개의 플래그가 있다:
    - `is_active`: **요금제 활성 슬롯**(다운그레이드 축소 대상). billing 소유. **마스터**.
    - `is_public`: **사용자 공개 토글**(게시 여부). 페이지 에디터 소유. **is_active 에 종속**.
    불변식: `is_public ⟹ is_active` — is_active=False 인 페이지는 is_public 도 반드시 False.
    실제 외부 노출은 둘 다 True 여야 한다(`is_live = is_active AND is_public`,
    apps/pages/views.py 참조). 선택한 페이지는 슬롯 확보 + 게시(is_active=True, is_public=True),
    미선택은 슬롯 반납 + 비공개(is_active=False, is_public=False). 재업그레이드 후엔
    사용자가 직접 재공개한다(구 is_public 자동 복원 계약 폐기).
    """

    permission_classes = [IsAuthenticated]

    # ── 공용: 현재 상태 dict 구성 (GET/POST 응답 공유) ──
    def _state(self, user) -> dict:
        from apps.pages.models import Page

        from .subscription_utils import get_user_plan

        plan = get_user_plan(user)
        max_pages_raw = plan.features.get("max_pages", 1)
        is_unlimited = max_pages_raw == -1
        max_pages = 999999 if is_unlimited else max_pages_raw

        sub = ensure_subscription(user)
        pages = list(Page.objects.filter(user=user).order_by("created_at"))
        total = len(pages)
        active = sum(1 for p in pages if p.is_active)
        live = sum(1 for p in pages if p.is_active and p.is_public)

        # 활성 수 기준 (IG 판과 동일 계약) — 보유수(total) 기준이면 초과 보유 유저에게
        # 영원히 true 로 남아 다이얼로그가 반복되고 하루 1회 제한도 영구 우회된다.
        needs = active > max_pages

        # 하루 1회 제한 — 무제한 플랜/강제 조정 상황은 항상 허용
        can_change = True
        if not is_unlimited and not needs and sub.page_activation_changed_at:
            can_change = (timezone.now() - sub.page_activation_changed_at).days >= 1

        return {
            "needs_activation_adjustment": needs,
            "max_pages": max_pages,
            "total_pages": total,
            "active_pages": active,
            "live_pages": live,
            "can_change_today": can_change,
            "pages": [
                {
                    "id": p.id,
                    "slug": p.slug,
                    "title": p.title,
                    "is_active": p.is_active,
                    "is_public": p.is_public,
                    "is_live": p.is_active and p.is_public,
                }
                for p in pages
            ],
        }

    @extend_schema(
        tags=["사용자플랜"],
        summary="활성 페이지 조정 상태 확인",
        description="""
## 목적
현재 사용자의 **페이지 활성화 조정이 필요한지** 확인하고, 보유 페이지 전체와
허용량·활성/공개 상태를 반환합니다. 플랜 최대 페이지 수보다 **활성(is_active) 페이지**가
많으면 `needs_activation_adjustment=true` 이며 프론트는 활성 페이지 선택 다이얼로그를
엽니다 (선택을 마치면 false 로 해소 — 초과분을 계속 보유해도 반복되지 않음).

## 두 개의 플래그 (중요) — is_active 가 마스터, is_public 은 종속
페이지에는 두 boolean 이 있고 **불변식 `is_public ⟹ is_active`** 를 지킵니다
(is_active=False 인 페이지는 is_public 도 항상 False):
- **`is_active`** — 요금제 활성 슬롯(**마스터**). 다운그레이드 축소 대상이며 billing 이 관리합니다.
- **`is_public`** — 사용자 공개 토글(게시 여부, is_active 에 **종속**). 페이지 에디터(PATCH /pages/multipages/{id}/)가 관리합니다.

실제 외부 노출은 **둘 다 True** 여야 합니다 → `is_live = is_active AND is_public`.
**프론트가 "이 페이지가 지금 켜져 있나"를 판단할 때는 `is_active` 단독이 아니라 `is_live` 를 쓰세요.**
(과거 `is_active` 단독을 신뢰해 `is_public=false` 인 페이지를 "켜짐"으로 오표시하던 버그를 이 값으로 해소.)

## 다운그레이드 시 페이지 처리 정책
- **무료(free)로 다운그레이드**: 가장 먼저 생성된 `max_pages` 개만 `is_active=True` 로 남기고
  초과분은 `is_active=False` **+ `is_public=False`** 로 자동 비활성화합니다
  (슬롯 없는데 공개로 남는 상태를 불변식으로 차단).
- **유료 → 하위 유료(예: unlimited→pro)로 다운그레이드**: 페이지 `is_active`/`is_public` 을
  자동으로 변경하지 않습니다. 보유수가 허용량을 초과하면 `needs_activation_adjustment=true` 로
  내려오므로, 사용자가 이 API 의 POST 로 유지할 페이지를 직접 선택해야 합니다.

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `needs_activation_adjustment` | bool | 조정 필요 여부 (**다이얼로그 트리거**) |
| `max_pages` | int | 현재 플랜의 최대 페이지 수. 무제한은 999999 |
| `total_pages` | int | 보유한 전체 페이지 수 |
| `active_pages` | int | 활성 슬롯(is_active) 페이지 수 |
| `live_pages` | int | 실제 노출 중(is_active AND is_public) 페이지 수 |
| `can_change_today` | bool | 오늘 변경 가능 여부 (하루 1회, 강제 조정 상황은 항상 true) |
| `pages` | array | 전체 페이지 (id, slug, title, is_active, is_public, is_live) |
        """,
        responses={
            200: OpenApiResponse(
                response=PageActivationStateSerializer, description="활성화 조정 상태"
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
        },
    )
    def get(self, request):
        return Response(self._state(request.user))

    @extend_schema(
        tags=["사용자플랜"],
        summary="활성 페이지 선택",
        description="""
## 목적
다운그레이드 후 플랜 한도에 맞춰 **활성화할 페이지를 선택**합니다.
선택한 페이지는 활성 슬롯 확보 + **게시(is_active=True, is_public=True)**, 선택되지 않은
나머지 페이지는 **슬롯 반납 + 비공개(is_active=False, is_public=False)** 로 전환됩니다
(트랜잭션으로 원자 적용). 불변식 `is_public ⟹ is_active` 를 지켜 "슬롯 없는데 공개(서빙)되는"
페이지가 생기지 않습니다.

> ⚠️ **계약 변경**: 미선택 페이지의 `is_public` 은 더 이상 보존되지 않습니다. 이후 업그레이드해도
> `is_active` 만 복원되고 `is_public` 은 `false` 로 남으므로, **재공개는 사용자가 직접**
> (PATCH /pages/multipages/{id}/ `{is_public:true}`) 해야 합니다.

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `active_page_ids` | ✅ | array[int] | 활성화할 페이지 ID 목록. 플랜 최대 페이지 수 이하, 전부 본인 소유(최소 1개) |

## 규칙
- 개수는 `max_pages` 이하여야 합니다.
- 모든 id 는 본인 소유여야 합니다.
- **하루 1회** 변경 제한이 있으나, 재조정이 필요한 상황(`needs_activation_adjustment=true`,
  즉 활성수>허용량)에서는 **항상 허용**됩니다(강제 조정 우회 — 선택 완료 시 해소되는
  일시 조건). 무제한 플랜은 제한 없음.
- 선택한 페이지가 초안(is_public=false)이었어도 이 호출로 **게시**됩니다 — 강제 조정 다이얼로그에서
  "유지할 페이지 선택 = 노출" 의도를 따릅니다.

## 응답
GET 과 동일 스키마의 최신 상태(`PageActivationStateSerializer`)를 반환합니다.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 허용량 초과, 존재하지 않는/타인 페이지 ID, 빈 목록, 오늘 이미 변경(강제 조정 아닐 때) |
| 401 | 토큰 없음/만료 |
        """,
        request=PageActivationRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=PageActivationStateSerializer,
                description="활성화 변경 완료 (GET 과 동일 스키마)",
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def post(self, request):
        from apps.pages.models import Page

        from .subscription_utils import get_user_plan

        plan = get_user_plan(request.user)
        max_pages = plan.features.get("max_pages", 1)
        is_unlimited = max_pages == -1

        sub = ensure_subscription(request.user)

        req = PageActivationRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        active_page_ids = req.validated_data["active_page_ids"]

        # 사용자의 페이지인지 확인
        user_pages = Page.objects.filter(user=request.user)
        valid_ids = set(user_pages.values_list("id", flat=True))
        invalid = set(active_page_ids) - valid_ids
        if invalid:
            return Response(
                {"detail": f"존재하지 않는 페이지 ID: {sorted(invalid)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not is_unlimited and len(active_page_ids) > max_pages:
            return Response(
                {"detail": f"현재 플랜에서는 최대 {max_pages}개 페이지만 활성화할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 강제 조정(활성수>허용량) 상황이면 하루 1회 제한 우회 — IG 판과 동일 계약.
        # 보유수(total) 기준이면 POST 로 절대 해소되지 않아 제한이 영구 무력화된다
        # (초과 페이지를 삭제하지 않고 보유만 하면 슬롯 무한 로테이션 가능).
        active_now = user_pages.filter(is_active=True).count()
        needs = not is_unlimited and active_now > max_pages
        if not is_unlimited and not needs and sub.page_activation_changed_at:
            elapsed = (timezone.now() - sub.page_activation_changed_at).total_seconds()
            if elapsed < 86400:
                return Response(
                    {"detail": "페이지 활성화 변경은 하루에 1번만 가능합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 적용: 선택 페이지는 활성 슬롯 + 게시(is_active=True, is_public=True),
        # 미선택은 슬롯 반납 + 비공개(is_active=False, is_public=False).
        # 불변식(is_public ⟹ is_active): 슬롯을 반납한 페이지가 공개로 남으면 "슬롯 없는데
        # 서빙되는" 페이지가 되므로 is_public 도 함께 내린다. (구 "is_public 보존 →
        # 업그레이드 시 자동 복원" 계약은 폐기 — 재업그레이드 후엔 사용자가 직접 재공개한다.)
        with transaction.atomic():
            user_pages.filter(id__in=active_page_ids).update(is_active=True, is_public=True)
            user_pages.exclude(id__in=active_page_ids).update(is_active=False, is_public=False)

            sub.page_activation_changed_at = timezone.now()
            sub.save(update_fields=["page_activation_changed_at", "updated_at"])

        return Response(self._state(request.user))


class IGAccountActivationView(APIView):
    """활성 IG 계정 선택 (허용량 축소 후 어떤 계정을 활성으로 둘지 조정)

    page-activation 을 IG 계정에 옮긴 형태. 비활성(is_active=False) 계정은 연결/토큰은
    보존되지만 DM·인사이트·스팸필터 등 기능에서 제외된다(하드 연결해제 아님).
    허용량은 '활성 계정 수'를 제한한다(비활성은 슬롯을 비움).
    """

    permission_classes = [IsAuthenticated]

    # ── 공용: 현재 상태 dict 구성 (GET/POST 응답 공유) ──
    def _state(self, user) -> dict:
        from apps.integrations.models import IGAccountConnection

        allowance = get_ig_account_allowance(user)
        is_unlimited = allowance < 0
        max_ig = 999999 if is_unlimited else allowance
        sub = ensure_subscription(user)

        owned = list(
            IGAccountConnection.objects.filter(workspace__owner=user)
            .exclude(status=IGAccountConnection.Status.REVOKED)
            .select_related("workspace")
            .order_by("created_at")
        )
        total = len(owned)
        active = sum(1 for c in owned if c.is_active)

        needs = (not is_unlimited and active > max_ig) or bool(sub.ig_activation_review_needed)

        # 하루 1회 제한 — 무제한 플랜/강제 조정 상황은 항상 허용
        can_change = True
        if not is_unlimited and not needs and sub.ig_account_activation_changed_at:
            can_change = (timezone.now() - sub.ig_account_activation_changed_at).days >= 1

        return {
            "needs_activation_adjustment": needs,
            "max_ig_accounts": max_ig,
            "total_accounts": total,
            "active_accounts": active,
            "can_change_today": can_change,
            "accounts": [
                {
                    "id": str(c.id),
                    "username": c.username or "",
                    "name": c.name or "",
                    "profile_picture_url": c.profile_picture_url or "",
                    "is_active": c.is_active,
                    "status": c.status,
                    "workspace_name": c.workspace.name,
                }
                for c in owned
            ],
        }

    @extend_schema(
        tags=["사용자플랜"],
        summary="활성 IG 계정 조정 상태 확인",
        description="""
## 목적
계정(사용자) 단위로 **활성 IG 계정 재선택이 필요한지** 확인하고, 연동된(비-REVOKED) 계정
전체와 허용량·활성 상태를 반환합니다. 프론트는 로그인/구독 변화 시 이 API 를 호출해
`needs_activation_adjustment=true` 이면 활성 계정 선택 다이얼로그를 엽니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수. 워크스페이스 owner 스코프로 집계합니다.

## 언제 조정이 필요한가
- 활성 계정 수가 허용량(1 + 추가 계정)을 초과할 때
- 갱신/다운그레이드 시 허용량이 줄어 초과분이 **자동 비활성**되고 재선택 유도 플래그가 설정됐을 때
  (`ig_activation_review_needed`)

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `needs_activation_adjustment` | bool | 활성 계정 재선택 필요 여부 (**다이얼로그 트리거로 사용**) |
| `max_ig_accounts` | int | 허용량 = 1 + 추가 계정. 무제한(관리자/무제한 플랜)은 999999 |
| `total_accounts` | int | 연동된(비-REVOKED) 계정 수 |
| `active_accounts` | int | 현재 활성 계정 수 |
| `can_change_today` | bool | 오늘 변경 가능 여부 (하루 1회, 강제 조정 상황은 항상 true) |
| `accounts` | array | 계정 목록 (id, username, name, profile_picture_url, is_active, status, workspace_name) |

## 프론트엔드 통합
```javascript
const res = await fetch('/api/v1/billing/ig-account-activation/', {
  headers: { Authorization: `Bearer ${accessToken}` },
});
const state = await res.json();
if (state.needs_activation_adjustment) openActivationDialog(state);
```
        """,
        responses={
            200: OpenApiResponse(
                response=IGAccountActivationStateSerializer,
                description="활성화 조정 상태",
                examples=[
                    OpenApiExample(
                        "재선택 필요 (허용량 1, 연동 3)",
                        value={
                            "needs_activation_adjustment": True,
                            "max_ig_accounts": 1,
                            "total_accounts": 3,
                            "active_accounts": 1,
                            "can_change_today": True,
                            "accounts": [
                                {
                                    "id": "0a1b2c3d-....",
                                    "username": "turnflow_official",
                                    "name": "Turnflow",
                                    "profile_picture_url": "https://media.turnflow.link/...",
                                    "is_active": True,
                                    "status": "active",
                                    "workspace_name": "내 워크스페이스",
                                }
                            ],
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="(GET 에서는 미발생) 잘못된 요청"),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="(GET 에서는 미발생) 리소스 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def get(self, request):
        return Response(self._state(request.user))

    @extend_schema(
        tags=["사용자플랜"],
        summary="활성 IG 계정 선택",
        description="""
## 목적
허용량에 맞춰 **활성으로 둘 IG 계정을 선택**합니다. 선택되지 않은 나머지 소유 계정은
**소프트 비활성**(is_active=False)됩니다 — 연결/토큰은 보존되지만 기능에서 제외되고,
해당 계정의 활성 캠페인은 일시중지, 발송 대기 중이던 DM 은 취소(SKIPPED)됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수.

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `active_account_ids` | ✅ | array[string] | 활성으로 둘 IG 계정 id 목록. 허용량 이하, 전부 본인 소유(최소 1개) |

## 규칙
- 개수는 허용량(`max_ig_accounts`) 이하여야 합니다.
- 모든 id 는 본인 소유(비-REVOKED)여야 합니다.
- **하루 1회** 변경 제한이 있으나, 재조정이 필요한 상황(`needs_activation_adjustment=true`)에서는
  항상 허용됩니다. 무제한 플랜은 제한 없음.
- 재활성화된 계정의 캠페인은 자동 재개되지 않습니다(사용자가 수동으로 재개).

## 프론트엔드 통합
```javascript
await fetch('/api/v1/billing/ig-account-activation/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
  body: JSON.stringify({ active_account_ids: ['0a1b2c3d-....'] }),
});
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 허용량 초과, 존재하지 않는/타인 계정 ID, 빈 목록, 오늘 이미 변경(강제 조정 아닐 때) |
| 401 | 토큰 없음/만료 |
| 500 | 서버 오류 |
        """,
        request=IGAccountActivationRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=IGAccountActivationStateSerializer,
                description="활성화 변경 완료 (GET 과 동일 스키마)",
            ),
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음"),
            404: OpenApiResponse(description="리소스 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def post(self, request):
        from apps.integrations.models import IGAccountConnection

        req = IGAccountActivationRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        active_ids = {str(x) for x in req.validated_data["active_account_ids"]}

        user = request.user
        allowance = get_ig_account_allowance(user)
        is_unlimited = allowance < 0
        max_ig = 999999 if is_unlimited else allowance
        sub = ensure_subscription(user)

        # 강제 조정(초과/자동비활성) 상황이면 하루 1회 제한 우회
        active_now = IGAccountConnection.objects.filter(
            workspace__owner=user,
            status=IGAccountConnection.Status.ACTIVE,
            is_active=True,
        ).count()
        needs = (not is_unlimited and active_now > max_ig) or bool(sub.ig_activation_review_needed)
        if not is_unlimited and not needs and sub.ig_account_activation_changed_at:
            elapsed = (timezone.now() - sub.ig_account_activation_changed_at).total_seconds()
            if elapsed < 86400:
                return Response(
                    {"detail": "IG 계정 활성화 변경은 하루에 1번만 가능합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 허용량 초과 검사
        if not is_unlimited and len(active_ids) > max_ig:
            return Response(
                {"detail": f"현재 허용량은 {max_ig}개입니다. 그 이하로만 활성화할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 소유(비-REVOKED) 계정 확인
        owned = list(
            IGAccountConnection.objects.filter(workspace__owner=user).exclude(
                status=IGAccountConnection.Status.REVOKED
            )
        )
        valid_ids = {str(c.id) for c in owned}
        invalid = active_ids - valid_ids
        if invalid:
            return Response(
                {"detail": f"존재하지 않거나 접근할 수 없는 계정 ID: {sorted(invalid)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 적용: 선택 계정 활성, 나머지 소유 계정 소프트 비활성(캠페인 PAUSE + in-flight DM SKIP)
        for conn in owned:
            if str(conn.id) in active_ids:
                conn.activate()
            else:
                conn.deactivate(reason="user_activation_choice")

        sub.ig_account_activation_changed_at = timezone.now()
        sub.ig_activation_review_needed = False
        sub.save(
            update_fields=[
                "ig_account_activation_changed_at",
                "ig_activation_review_needed",
                "updated_at",
            ]
        )

        return Response(self._state(user))
