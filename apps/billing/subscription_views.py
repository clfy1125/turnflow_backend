"""
Subscription API views — 구독 관리
"""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse

from .models import SubscriptionPlan, SubscriptionStatus
from .serializers import (
    SubscriptionPlanSerializer,
    UserSubscriptionSerializer,
    ChangeSubscriptionRequestSerializer,
)
from .subscription_utils import ensure_subscription


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

## 플랜 구조

| 플랜 | name | 월 요금 | 연 요금 | 주요 기능 |
|------|------|---------|---------|-----------|
| 무료 | `free` | 0원 | 0원 | 페이지 3개 제한 |
| 프로 | `pro` | 9,900원 | 99,000원 | 무제한 페이지, AI 생성, 로고 삭제 |
| 프로 플러스 | `pro_plus` | 19,900원 | 199,000원 | 프로 + 커스텀 CSS |

## 응답 필드 설명
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | uuid | 플랜 고유 ID. 플랜 변경 API에서 사용 |
| `name` | string | 플랜 코드명 (`free` / `pro` / `pro_plus`) |
| `display_name` | string | UI 표시용 이름 (`무료` / `프로` / `프로 플러스`) |
| `monthly_price` | int | 월간 요금 (원). 0이면 무료 |
| `yearly_price` | int | 연간 요금 (원). 0이면 무료 |
| `features` | object | 기능 제한 설정 (아래 참조) |
| `sort_order` | int | UI 정렬 순서 (오름차순) |

## `features` 객체 구조
```json
{
  "max_pages": 3,           // 최대 페이지 수 (-1 = 무제한)
  "ai_generation": false,   // AI 페이지 생성 사용 가능 여부
  "remove_logo": false,     // 하단 로고 삭제 가능 여부
  "custom_css": false       // 커스텀 CSS 사용 가능 여부
}
```

## 프론트엔드 통합
```typescript
// 요금제 페이지에서 플랜 목록 fetch
const res = await fetch('/api/v1/billing/plans/');
const plans = await res.json();

// 플랜별 가격 표시
plans.forEach(plan => {
  console.log(`${plan.display_name}: ${plan.monthly_price.toLocaleString()}원/월`);
});
```
        """,
        responses={
            200: OpenApiResponse(
                response=SubscriptionPlanSerializer(many=True),
                description="활성 플랜 목록 (sort_order 오름차순)",
                examples=[
                    OpenApiExample(
                        "플랜 목록",
                        value=[
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440001",
                                "name": "free",
                                "display_name": "무료",
                                "monthly_price": 0,
                                "yearly_price": 0,
                                "features": {"max_pages": 3, "ai_generation": False, "remove_logo": False, "custom_css": False},
                                "sort_order": 0,
                            },
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440002",
                                "name": "pro",
                                "display_name": "프로",
                                "monthly_price": 9900,
                                "yearly_price": 99000,
                                "features": {"max_pages": -1, "ai_generation": True, "remove_logo": True, "custom_css": False},
                                "sort_order": 1,
                            },
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440003",
                                "name": "pro_plus",
                                "display_name": "프로 플러스",
                                "monthly_price": 19900,
                                "yearly_price": 199000,
                                "features": {"max_pages": -1, "ai_generation": True, "remove_logo": True, "custom_css": True},
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
| `plan` | object | 현재 플랜 상세 (SubscriptionPlan 객체) |
| `plan_id` | uuid | 현재 플랜 ID |
| `status` | string | `active` / `cancelled` / `past_due` / `trialing` |
| `billing_cycle` | string | `monthly` / `yearly` |
| `current_period_start` | datetime | 현재 결제 주기 시작일 (ISO 8601) |
| `current_period_end` | datetime | 현재 결제 주기 종료일 (ISO 8601). null이면 무기한 |
| `cancelled_at` | datetime | 취소 시각. null이면 취소하지 않음 |
| `created_at` | datetime | 구독 생성일 |
| `updated_at` | datetime | 마지막 수정일 |

## `status` 값 의미
| 상태 | 설명 | 기능 사용 |
|------|------|-----------|
| `active` | 정상 구독 중 | ✅ 가능 |
| `trialing` | 체험 기간 | ✅ 가능 |
| `cancelled` | 취소됨 (period_end까지 유지) | ✅ period_end까지 가능 |
| `past_due` | 결제 실패 (자동결제 재시도 중) | ⚠️ 유예 기간 |

## 프론트엔드 통합
```typescript
// 앱 초기화 시 구독 정보 fetch
const res = await fetch('/api/v1/billing/my-subscription/', {
  headers: { 'Authorization': `Bearer ${accessToken}` }
});
const subscription = await res.json();

// 현재 플랜 확인
const isPro = subscription.plan.name !== 'free';
const canUseAI = subscription.plan.features.ai_generation;
const maxPages = subscription.plan.features.max_pages; // -1 = 무제한

// 취소 상태 확인
if (subscription.status === 'cancelled') {
  const endDate = new Date(subscription.current_period_end);
  console.log(`${endDate.toLocaleDateString()}까지 기존 기능 사용 가능`);
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
                                "yearly_price": 0,
                                "features": {"max_pages": 3, "ai_generation": False, "remove_logo": False, "custom_css": False},
                                "sort_order": 0,
                            },
                            "plan_id": "550e8400-e29b-41d4-a716-446655440001",
                            "status": "active",
                            "billing_cycle": "monthly",
                            "current_period_start": "2026-04-01T00:00:00Z",
                            "current_period_end": None,
                            "cancelled_at": None,
                            "created_at": "2026-04-01T00:00:00Z",
                            "updated_at": "2026-04-01T00:00:00Z",
                        },
                    ),
                    OpenApiExample(
                        "프로 플랜 (월간 구독)",
                        value={
                            "id": "a1b2c3d4-0000-0000-0000-000000000002",
                            "plan": {
                                "id": "550e8400-e29b-41d4-a716-446655440002",
                                "name": "pro",
                                "display_name": "프로",
                                "monthly_price": 9900,
                                "yearly_price": 99000,
                                "features": {"max_pages": -1, "ai_generation": True, "remove_logo": True, "custom_css": False},
                                "sort_order": 1,
                            },
                            "plan_id": "550e8400-e29b-41d4-a716-446655440002",
                            "status": "active",
                            "billing_cycle": "monthly",
                            "current_period_start": "2026-04-01T00:00:00Z",
                            "current_period_end": "2026-05-01T00:00:00Z",
                            "cancelled_at": None,
                            "created_at": "2026-03-15T00:00:00Z",
                            "updated_at": "2026-04-01T00:00:00Z",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰이 없거나 만료됨"),
        },
    )
    def get(self, request):
        sub = ensure_subscription(request.user)
        serializer = UserSubscriptionSerializer(sub)
        return Response(serializer.data)


class ChangeSubscriptionView(APIView):
    """플랜 변경 요청"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="구독 플랜 변경",
        description="""
## 목적
현재 구독 플랜을 다른 플랜으로 변경합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 사용 시나리오
- 무료 → 프로 업그레이드
- 프로 → 프로 플러스 업그레이드
- 유료 → 무료 다운그레이드

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `plan_id` | ✅ | uuid | 변경할 플랜의 ID. `GET /api/v1/billing/plans/`에서 확인 |
| `billing_cycle` | 선택 | string | `monthly`(기본) 또는 `yearly` |

## 플랜 변경 시나리오별 동작

### 유료 → 무료 (다운그레이드)
- **즉시 처리**: `status`가 `cancelled`로 변경됨
- `current_period_end`까지 기존 유료 기능 **계속 사용 가능**
- 프론트에서 "○월 ○일까지 프로 기능을 사용할 수 있습니다" 안내 권장

### 무료 → 유료 (업그레이드) ⚠️ 현재 미구현
- **토스페이먼츠 연동 전**: `402 Payment Required` 응답
- 응답에 `payment_required: true`와 선택한 플랜 정보가 포함됨
- 토스 연동 완료 후 결제 → 플랜 변경 자동 처리 예정

### 유료 → 유료 (플랜 변경) ⚠️ 현재 미구현
- 동일하게 `402 Payment Required` 응답

## 프론트엔드 통합
```typescript
// 1) 플랜 목록에서 원하는 플랜 ID 확인
const plans = await fetch('/api/v1/billing/plans/').then(r => r.json());
const proPlan = plans.find(p => p.name === 'pro');

// 2) 플랜 변경 요청
const res = await fetch('/api/v1/billing/change-plan/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${accessToken}`
  },
  body: JSON.stringify({
    plan_id: proPlan.id,
    billing_cycle: 'monthly'
  })
});

if (res.status === 402) {
  // 결제가 필요한 경우 → 결제 페이지로 이동
  const data = await res.json();
  console.log('결제 필요:', data.plan.display_name, data.billing_cycle);
  // → 토스 결제 위젯 호출
}

if (res.status === 200) {
  // 다운그레이드 완료
  const subscription = await res.json();
  console.log('변경 완료:', subscription.status);
}
```

## 에러
| 코드 | 원인 | 응답 예시 |
|------|------|-----------|
| 400 | 동일 플랜으로 변경 시도 | `{"detail": "이미 동일한 플랜을 사용 중입니다."}` |
| 400 | 유효성 검증 실패 | `{"plan_id": ["This field is required."]}` |
| 401 | 토큰 없음/만료 | — |
| 402 | 결제 필요 (유료 전환) | `{"payment_required": true, "plan": {...}, "billing_cycle": "monthly"}` |
| 404 | 존재하지 않거나 비활성 플랜 | `{"detail": "플랜을 찾을 수 없습니다."}` |
        """,
        request=ChangeSubscriptionRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=UserSubscriptionSerializer,
                description="다운그레이드 성공 (유료 → 무료)",
                examples=[
                    OpenApiExample(
                        "다운그레이드 성공",
                        value={
                            "id": "a1b2c3d4-0000-0000-0000-000000000002",
                            "plan": {"id": "550e8400-...", "name": "pro", "display_name": "프로"},
                            "status": "cancelled",
                            "billing_cycle": "monthly",
                            "current_period_end": "2026-05-01T00:00:00Z",
                            "cancelled_at": "2026-04-13T12:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="동일 플랜 변경 시도 / 유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            402: OpenApiResponse(
                description="결제 필요 — 토스페이먼츠 연동 후 사용 가능",
                examples=[
                    OpenApiExample(
                        "결제 필요 응답",
                        value={
                            "detail": "결제가 필요합니다. 토스페이먼츠 연동 후 사용 가능합니다.",
                            "payment_required": True,
                            "plan": {
                                "id": "550e8400-e29b-41d4-a716-446655440002",
                                "name": "pro",
                                "display_name": "프로",
                                "monthly_price": 9900,
                                "yearly_price": 99000,
                                "features": {"max_pages": -1, "ai_generation": True, "remove_logo": True, "custom_css": False},
                            },
                            "billing_cycle": "monthly",
                        },
                    )
                ],
            ),
            404: OpenApiResponse(description="플랜을 찾을 수 없음"),
        },
    )
    def post(self, request):
        serializer = ChangeSubscriptionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        plan_id = serializer.validated_data["plan_id"]
        billing_cycle = serializer.validated_data.get("billing_cycle", "monthly")

        try:
            new_plan = SubscriptionPlan.objects.get(id=plan_id, is_active=True)
        except SubscriptionPlan.DoesNotExist:
            return Response(
                {"detail": "플랜을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        sub = ensure_subscription(request.user)

        # 같은 플랜이면 무시
        if sub.plan_id == new_plan.id:
            return Response(
                {"detail": "이미 동일한 플랜을 사용 중입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 유료 → Free 다운그레이드
        if new_plan.name == "free":
            sub.status = SubscriptionStatus.CANCELLED
            from django.utils import timezone

            sub.cancelled_at = timezone.now()
            # current_period_end까지 기존 기능 유지 (프론트에서 안내)
            sub.save(update_fields=["status", "cancelled_at", "updated_at"])
            return Response(UserSubscriptionSerializer(sub).data)

        # Free → 유료 or 유료 → 유료 (토스 연동 전)
        # 토스페이먼츠 승인 후 이 부분에 결제 로직 추가
        return Response(
            {
                "detail": "결제가 필요합니다. 토스페이먼츠 연동 후 사용 가능합니다.",
                "payment_required": True,
                "plan": SubscriptionPlanSerializer(new_plan).data,
                "billing_cycle": billing_cycle,
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
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
                            "billing_cycle": "monthly",
                            "current_period_end": "2026-05-01T00:00:00Z",
                            "cancelled_at": "2026-04-13T12:00:00Z",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="취소 불가",
                examples=[
                    OpenApiExample("무료 플랜", value={"detail": "무료 플랜은 취소할 수 없습니다."}),
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

        from django.utils import timezone

        sub.status = SubscriptionStatus.CANCELLED
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=["status", "cancelled_at", "updated_at"])

        return Response(UserSubscriptionSerializer(sub).data)
