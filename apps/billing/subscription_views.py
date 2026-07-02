"""
Subscription API views — 구독 관리
"""

import logging

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import SubscriptionPlan, SubscriptionStatus
from .serializers import (
    ChangeSubscriptionRequestSerializer,
    PaymentHistorySerializer,
    SubscriptionPlanSerializer,
    UserSubscriptionSerializer,
)
from .subscription_utils import ensure_subscription
from .toss_flows import BillingFlowError, change_plan

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
| **업그레이드** (basic→pro) | **즉시 새 플랜 전액 결제** + 결제 주기 리셋(오늘부터 30일). 비례배분 없음 |
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
    """페이지 활성화 조정 (다운그레이드 후 플랜 한도에 맞춰 활성 페이지 선택)"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="활성 페이지 조정 상태 확인",
        description="""
## 목적
현재 사용자의 **페이지 활성화 조정이 필요한지** 확인합니다.
플랜 최대 페이지 수보다 보유 페이지가 많으면 조정이 필요합니다.

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `needs_activation_adjustment` | bool | 조정이 필요한지 여부 |
| `max_pages` | int | 현재 플랜의 최대 페이지 수 |
| `total_pages` | int | 보유한 전체 페이지 수 |
| `active_pages` | int | 현재 활성 페이지 수 |
| `can_change_today` | bool | 오늘 활성화 변경 가능 여부 (하루 1회) |
| `pages` | array | 전체 페이지 목록 (id, slug, title, is_active) |
        """,
        responses={200: OpenApiResponse(description="활성화 조정 상태")},
    )
    def get(self, request):
        from apps.pages.models import Page

        from .subscription_utils import get_user_plan

        plan = get_user_plan(request.user)
        max_pages_raw = plan.features.get("max_pages", 1)
        is_unlimited = max_pages_raw == -1
        max_pages = 999999 if is_unlimited else max_pages_raw

        sub = ensure_subscription(request.user)
        pages = Page.objects.filter(user=request.user).order_by("created_at")
        total = pages.count()
        active = pages.filter(is_active=True).count()

        # 무제한 플랜(프로 플러스)은 하루 1회 제한 없음
        can_change = True
        if not is_unlimited and sub.page_activation_changed_at:
            can_change = (timezone.now() - sub.page_activation_changed_at).days >= 1

        return Response(
            {
                "needs_activation_adjustment": total > max_pages,
                "max_pages": max_pages,
                "total_pages": total,
                "active_pages": active,
                "can_change_today": can_change,
                "pages": [
                    {"id": p.id, "slug": p.slug, "title": p.title, "is_active": p.is_active}
                    for p in pages
                ],
            }
        )

    @extend_schema(
        tags=["사용자플랜"],
        summary="활성 페이지 선택",
        description="""
## 목적
다운그레이드 후 플랜 한도에 맞춰 **활성화할 페이지를 선택**합니다.
하루 1회만 변경 가능합니다.

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `active_page_ids` | ✅ | array[int] | 활성화할 페이지 ID 목록. 플랜 최대 페이지 수 이하 |

## 에러
| 코드 | 메시지 |
|------|--------|
| 400 | 활성 페이지 수가 플랜 한도 초과, 존재하지 않는 페이지 ID, 오늘 이미 변경함 |
| 403 | 조정이 필요하지 않은 상태 |
        """,
        responses={
            200: OpenApiResponse(description="활성화 변경 완료"),
            400: OpenApiResponse(description="유효성 검증 실패"),
        },
    )
    def post(self, request):
        from apps.pages.models import Page

        from .subscription_utils import get_user_plan

        plan = get_user_plan(request.user)
        max_pages = plan.features.get("max_pages", 1)
        is_unlimited = max_pages == -1

        sub = ensure_subscription(request.user)

        # 하루 1회 제한 (무제한 플랜은 제외)
        if not is_unlimited and sub.page_activation_changed_at:
            elapsed = (timezone.now() - sub.page_activation_changed_at).total_seconds()
            if elapsed < 86400:
                return Response(
                    {"detail": "페이지 활성화 변경은 하루에 1번만 가능합니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        active_page_ids = request.data.get("active_page_ids", [])
        if not isinstance(active_page_ids, list):
            return Response(
                {"detail": "active_page_ids는 배열이어야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not is_unlimited and len(active_page_ids) > max_pages:
            return Response(
                {"detail": f"현재 플랜에서는 최대 {max_pages}개 페이지만 활성화할 수 있습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 사용자의 페이지인지 확인
        user_pages = Page.objects.filter(user=request.user)
        valid_ids = set(user_pages.values_list("id", flat=True))
        invalid = set(active_page_ids) - valid_ids
        if invalid:
            return Response(
                {"detail": f"존재하지 않는 페이지 ID: {list(invalid)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(active_page_ids) == 0:
            return Response(
                {"detail": "최소 1개의 페이지를 활성화해야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 활성화 적용
        user_pages.filter(id__in=active_page_ids).update(is_active=True)
        user_pages.exclude(id__in=active_page_ids).update(is_active=False)

        sub.page_activation_changed_at = timezone.now()
        sub.save(update_fields=["page_activation_changed_at", "updated_at"])

        return Response(
            {
                "detail": f"{len(active_page_ids)}개 페이지가 활성화되었습니다.",
                "active_page_ids": active_page_ids,
            }
        )
