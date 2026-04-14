"""
Payment API views — 결제 관련 (토스페이먼츠 스켈레톤)
토스페이먼츠 승인 후 TODO 부분만 채우면 완성됨.
"""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse

from .models import PaymentHistory
from .serializers import PaymentHistorySerializer


class PaymentConfirmView(APIView):
    """
    토스페이먼츠 결제 승인
    프론트에서 토스 SDK 결제 완료 후 paymentKey, orderId, amount를 전달받아 승인 처리.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["PG사 연동"],
        summary="결제 승인 (토스페이먼츠)",
        description="""
## 목적
프론트엔드에서 **토스페이먼츠 결제 SDK** 결제 완료 후, 서버에서 최종 승인 처리를 수행합니다.

## ⚠️ 현재 상태
**토스페이먼츠 승인 대기 중** — 스켈레톤만 구현되어 있으며, 호출 시 `501 Not Implemented`를 반환합니다.
승인 완료 후 아래 흐름이 구현됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 예정 흐름 (토스 승인 후 구현)
```
프론트 토스 SDK → 결제 완료 → paymentKey, orderId, amount 수신
→ POST /api/v1/billing/payments/confirm/ 호출
→ 서버: 토스 API /v1/payments/confirm 호출 (서버-to-서버)
→ 성공 시: PaymentHistory 생성 + UserSubscription 플랜 변경
→ 프론트: 구독 상태 갱신
```

## 예정 요청 필드 (토스 승인 후)
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `payment_key` | ✅ | string | 토스에서 발급한 결제 키 |
| `order_id` | ✅ | string | 주문 ID (클라이언트에서 생성) |
| `amount` | ✅ | int | 결제 금액 (원). 서버 검증용 |

## 프론트엔드 통합 (토스 승인 후)
```typescript
// 토스 결제 SDK 성공 콜백에서 호출
async function onPaymentSuccess(paymentKey: string, orderId: string, amount: number) {
  const res = await fetch('/api/v1/billing/payments/confirm/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${accessToken}`
    },
    body: JSON.stringify({ payment_key: paymentKey, order_id: orderId, amount })
  });

  if (res.ok) {
    // 구독 상태 갱신
    const result = await res.json();
    console.log('결제 완료:', result);
  }
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 501 | 토스페이먼츠 미연동 (현재 상태) |
        """,
        responses={
            200: OpenApiResponse(description="결제 승인 성공 (토스 연동 후)"),
            401: OpenApiResponse(description="인증 실패"),
            501: OpenApiResponse(
                description="토스페이먼츠 미연동",
                examples=[
                    OpenApiExample(
                        "미연동 상태",
                        value={"detail": "토스페이먼츠 연동 대기 중입니다. 승인 후 사용 가능합니다."},
                    )
                ],
            ),
        },
    )
    def post(self, request):
        # TODO: 토스페이먼츠 승인 후 구현
        # payment_key = request.data.get("payment_key")
        # order_id = request.data.get("order_id")
        # amount = request.data.get("amount")
        #
        # 1) 토스 API /v1/payments/confirm 호출
        # 2) 응답 검증
        # 3) PaymentHistory 생성 (status=paid)
        # 4) UserSubscription.plan 변경, status=active
        # 5) toss_billing_key 등 저장 (자동결제 시)

        return Response(
            {"detail": "토스페이먼츠 연동 대기 중입니다. 승인 후 사용 가능합니다."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


class PaymentWebhookView(APIView):
    """
    토스페이먼츠 웹훅 수신
    결제 상태 변경 시 토스에서 호출하는 엔드포인트.
    """

    permission_classes = [AllowAny]  # 토스 서버에서 호출하므로 인증 불필요

    @extend_schema(
        tags=["PG사 연동"],
        summary="결제 웹훅 수신 (토스페이먼츠)",
        description="""
## 목적
토스페이먼츠에서 결제 상태 변경 시 **서버로 직접 호출**하는 웹훅 엔드포인트입니다.

## ⚠️ 현재 상태
**토스페이먼츠 승인 대기 중** — 스켈레톤만 구현되어 있습니다.

## 인증
**불필요** — 토스 서버에서 호출하므로 JWT 불필요. 대신 토스 웹훅 시크릿으로 검증 예정.

## ⚠️ 프론트엔드에서 호출하지 마세요
이 엔드포인트는 **토스 서버 → 백엔드** 전용입니다.
프론트엔드에서 직접 호출할 필요가 없습니다.

## 예정 처리 이벤트 (토스 승인 후)
| 이벤트 | 동작 |
|--------|------|
| `PAYMENT_STATUS_CHANGED` | PaymentHistory 상태 업데이트 |
| `BILLING_KEY_STATUS_CHANGED` | 빌링키 상태 처리 |
| 자동결제 실패 | UserSubscription.status → `past_due` |

## 에러
| 코드 | 원인 |
|------|------|
| 200 | 항상 200 반환 (웹훅 재시도 방지) |
        """,
        responses={
            200: OpenApiResponse(
                description="웹훅 수신 확인",
                examples=[OpenApiExample("성공", value={"status": "ok"})],
            ),
        },
    )
    def post(self, request):
        # TODO: 토스페이먼츠 승인 후 구현
        # 1) 토스 웹훅 시크릿 검증
        # 2) event type에 따라 처리:
        #    - PAYMENT_STATUS_CHANGED: PaymentHistory 상태 업데이트
        #    - BILLING_KEY_STATUS_CHANGED: 빌링키 상태 처리
        # 3) 자동결제 실패 시 UserSubscription.status = past_due

        return Response({"status": "ok"})


class PaymentHistoryView(APIView):
    """결제 내역 조회"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="결제 내역 조회",
        description="""
## 목적
현재 사용자의 **전체 결제 내역**을 최신순으로 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 사용 시나리오
- 설정 > 결제 내역 페이지
- 영수증/인보이스 확인

## 응답 필드 설명
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | uuid | 결제 고유 ID |
| `amount` | int | 결제 금액 (원) |
| `status` | string | `pending` / `paid` / `failed` / `refunded` |
| `payment_method` | string | 결제 수단 (`card` 등). null이면 미확정 |
| `description` | string | 결제 설명 (예: "프로 플랜 월간 구독") |
| `toss_order_id` | string | 토스 주문 ID. null이면 토스 미연동 |
| `paid_at` | datetime | 실제 결제 완료 시각. null이면 미완료 |
| `created_at` | datetime | 결제 요청 생성 시각 |

## `status` 값 의미
| 상태 | 설명 |
|------|------|
| `pending` | 결제 진행 중 |
| `paid` | 결제 완료 |
| `failed` | 결제 실패 |
| `refunded` | 환불 완료 |

## 프론트엔드 통합
```typescript
// 결제 내역 조회
const res = await fetch('/api/v1/billing/payments/history/', {
  headers: { 'Authorization': `Bearer ${accessToken}` }
});
const payments = await res.json();

// 결제 내역 표시
payments.forEach(p => {
  console.log(`${p.description}: ${p.amount.toLocaleString()}원 (${p.status})`);
});
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
        """,
        responses={
            200: OpenApiResponse(
                response=PaymentHistorySerializer(many=True),
                description="결제 내역 목록 (최신순)",
                examples=[
                    OpenApiExample(
                        "결제 내역",
                        value=[
                            {
                                "id": "b1c2d3e4-0000-0000-0000-000000000001",
                                "amount": 9900,
                                "status": "paid",
                                "payment_method": "card",
                                "description": "프로 플랜 월간 구독",
                                "toss_order_id": "ORDER-20260401-001",
                                "paid_at": "2026-04-01T10:30:00Z",
                                "created_at": "2026-04-01T10:29:50Z",
                            },
                            {
                                "id": "b1c2d3e4-0000-0000-0000-000000000002",
                                "amount": 9900,
                                "status": "paid",
                                "payment_method": "card",
                                "description": "프로 플랜 월간 구독",
                                "toss_order_id": "ORDER-20260301-001",
                                "paid_at": "2026-03-01T10:30:00Z",
                                "created_at": "2026-03-01T10:29:50Z",
                            },
                        ],
                    ),
                    OpenApiExample(
                        "결제 내역 없음 (무료 사용자)",
                        value=[],
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        payments = PaymentHistory.objects.filter(user=request.user)
        serializer = PaymentHistorySerializer(payments, many=True)
        return Response(serializer.data)
