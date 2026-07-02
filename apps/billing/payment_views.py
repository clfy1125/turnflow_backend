"""
Payment API views — 결제 내역 / 환불.

1. PaymentHistoryView    — 결제 내역 조회
2. RefundEligibilityView — 환불 가능 여부 확인
3. RefundPaymentView     — 결제 환불 요청 (토스 취소 API)

토스 웹훅 수신은 toss_views.TossWebhookView, 처리 로직은 tasks.process_toss_webhook.
"""

import logging

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PaymentHistory, PaymentStatus
from .serializers import PaymentHistorySerializer
from .toss_service import TossBillingClient, TossError, TossNetworkError

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 1) 결제 내역 조회
# ──────────────────────────────────────────────


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
| `amount` | int | 결제 금액 (원). 환불은 음수값 |
| `status` | string | `pending`(확인 중) / `paid` / `failed` / `refunded` |
| `payment_method` | string | `card` |
| `description` | string | 결제 설명 (예: "턴플로우 프로 월간 구독") |
| `toss_order_id` | string | 주문 번호 |
| `receipt_url` | string | 토스 매출전표(영수증) URL |
| `card_company` | string | 결제 카드사 |
| `card_number_masked` | string | 마스킹된 카드번호 |
| `failure_code` / `failure_message` | string | 실패 건의 사유 |
| `paid_at` | datetime | 결제 완료 시각 |
| `created_at` | datetime | 결제 시도 생성 시각 |

## 상태 안내
- `pending`: 결제 결과 확인 중 (통신 지연) — 최대 30분 내 자동 확정됩니다.
- 환불 건은 `refunded` + 부분 환불은 음수 `amount` 행이 별도로 추가됩니다.

## 영수증 확인
`receipt_url`이 있는 결제건은 해당 URL에서 토스 매출전표를 확인할 수 있습니다.

## 프론트엔드 통합
```typescript
const res = await fetch('/api/v1/billing/payments/history/', {
  headers: { 'Authorization': `Bearer ${accessToken}` }
});
const payments = await res.json();

payments.forEach(p => {
  console.log(`${p.description}: ${p.amount.toLocaleString()}원 (${p.status})`);
  if (p.receipt_url) console.log(`영수증: ${p.receipt_url}`);
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
                                "description": "턴플로우 프로 월간 구독",
                                "toss_order_id": "tfsub-a1b2c3d4e5-20260801-a0",
                                "receipt_url": "https://dashboard.tosspayments.com/sales-slip?...",
                                "card_company": "현대",
                                "card_number_masked": "433012******123*",
                                "failure_code": "",
                                "failure_message": "",
                                "paid_at": "2026-08-01T10:30:00Z",
                                "created_at": "2026-08-01T10:29:50Z",
                            },
                        ],
                    ),
                    OpenApiExample("결제 내역 없음 (무료 사용자)", value=[]),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        payments = PaymentHistory.objects.filter(user=request.user)
        serializer = PaymentHistorySerializer(payments, many=True)
        return Response(serializer.data)


# ──────────────────────────────────────────────
# 2) 환불 적격성 확인 + 환불 요청
# ──────────────────────────────────────────────

REFUND_WINDOW_DAYS = 7  # 환불 가능 기간 (결제일 기준, 일)


def check_refund_eligibility(user, sub, paid_at) -> dict:
    """환불 적격성 심사 — 해당 결제(paid_at) 이후의 유료 기능 사용 여부를 조사.

    트라이얼 도입으로 기준이 '유료 활성화'가 아닌 **'해당 결제 시점'**이다:
    트라이얼 기간의 사용은 무료 제공분이므로 심사 대상이 아니다.
    커스텀 CSS는 무료 플랜도 허용되는 기능이라 심사하지 않는다.
    """
    from apps.pages.models import Page

    from .models import AiTokenLedger

    reasons = []
    details = {}

    # 1) 결제 후 7일 이내인지
    if paid_at:
        days_since = (timezone.now() - paid_at).days
        details["days_since_payment"] = days_since
        if days_since > REFUND_WINDOW_DAYS:
            reasons.append(f"결제 후 {REFUND_WINDOW_DAYS}일이 경과했습니다.")
    else:
        reasons.append("결제 기록이 없습니다.")
        return {"eligible": False, "reasons": reasons, "details": details}

    # 2) 결제 이후 생성한 페이지 (유료 한도 활용)
    pages_created = Page.objects.filter(user=user, created_at__gte=paid_at).count()
    details["pages_created_since_payment"] = pages_created
    if pages_created > 0:
        reasons.append(f"결제 이후 페이지를 {pages_created}개 생성했습니다.")

    # 3) 결제 이후 AI 사용
    ai_used = AiTokenLedger.objects.filter(user=user, amount__lt=0, created_at__gte=paid_at).count()
    details["ai_used_since_payment"] = ai_used
    if ai_used > 0:
        reasons.append(f"결제 이후 AI 생성을 {ai_used}회 사용했습니다.")

    # 4) 배지(로고) 제거 사용 여부 — basic+ 전용 기능
    logo_removed_pages = Page.objects.filter(
        user=user,
        data__design_settings__logoStyle="hidden",
    ).count()
    details["logo_removed_pages"] = logo_removed_pages
    if logo_removed_pages > 0:
        reasons.append(f"배지 제거 기능을 {logo_removed_pages}개 페이지에서 사용했습니다.")

    # 5) 프로 전용 기능 사용 (스팸필터 활성 / IG 다계정)
    if sub.plan.name == "pro":
        from apps.integrations.models import IGAccountConnection, SpamFilterConfig

        active_igs = IGAccountConnection.objects.filter(
            workspace__owner=user, status=IGAccountConnection.Status.ACTIVE
        ).count()
        details["active_ig_accounts"] = active_igs
        if active_igs > 1:
            reasons.append(f"프로 기능으로 IG 계정을 {active_igs}개 연동 중입니다.")

        spam_active = SpamFilterConfig.objects.filter(
            ig_connection__workspace__owner=user, status=SpamFilterConfig.Status.ACTIVE
        ).count()
        details["spam_filter_active"] = spam_active
        if spam_active > 0:
            reasons.append("스팸 댓글 필터링을 사용 중입니다.")

    eligible = len(reasons) == 0
    return {"eligible": eligible, "reasons": reasons, "details": details}


def _latest_paid_payment(user):
    return (
        PaymentHistory.objects.filter(user=user, status=PaymentStatus.PAID, amount__gt=0)
        .order_by("-paid_at")
        .first()
    )


class RefundEligibilityView(APIView):
    """환불 가능 여부 확인"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="환불 가능 여부 확인",
        description="""
## 목적
현재 사용자의 **환불 가능 여부**를 확인합니다.
프론트엔드에서 환불 버튼 표시 여부를 결정하는 데 사용합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 환불 가능 조건 (모두 동시 만족)
1. 마지막 결제 후 **7일 이내**
2. 결제 이후 페이지 추가 생성 없음
3. 결제 이후 AI 생성 사용 없음
4. 배지(로고) 제거 기능 미사용
5. (프로) IG 다계정 연동 없음 + 스팸 필터 미사용

> 무료 체험 기간의 사용은 심사 대상이 아닙니다 — 체험은 무료 제공분이며,
> 환불 기준은 **실제 결제 시점 이후**의 유료 기능 사용 여부입니다.

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `eligible` | bool | 환불 가능 여부 |
| `reasons` | array[string] | 환불 불가 사유 목록. 빈 배열이면 환불 가능 |
| `details` | object | 상세 조사 결과 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
        """,
        responses={
            200: OpenApiResponse(
                description="환불 가능 여부",
                examples=[
                    OpenApiExample(
                        "환불 가능",
                        value={
                            "eligible": True,
                            "reasons": [],
                            "details": {"days_since_payment": 2},
                        },
                    ),
                    OpenApiExample(
                        "환불 불가 (기간 경과)",
                        value={
                            "eligible": False,
                            "reasons": ["결제 후 7일이 경과했습니다."],
                            "details": {"days_since_payment": 12},
                        },
                    ),
                    OpenApiExample(
                        "무료 플랜 사용자",
                        value={
                            "eligible": False,
                            "reasons": ["환불 대상 결제가 없습니다."],
                            "details": {},
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        from .subscription_utils import ensure_subscription

        sub = ensure_subscription(request.user)
        payment = _latest_paid_payment(request.user)
        if payment is None:
            return Response(
                {"eligible": False, "reasons": ["환불 대상 결제가 없습니다."], "details": {}},
            )

        result = check_refund_eligibility(request.user, sub, payment.paid_at)
        return Response(result)


class RefundPaymentView(APIView):
    """결제 환불 요청"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["사용자플랜"],
        summary="결제 환불 요청",
        description="""
## 목적
특정 결제건에 대해 **전체 환불**을 요청합니다.
환불 성공 시 구독은 즉시 무료 플랜으로 전환됩니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 환불 조건
| 조건 | 설명 |
|------|------|
| 본인 결제 | 로그인 사용자의 결제건만 |
| 결제완료(paid) | 상태가 paid인 건만 |
| 7일 이내 | 해당 결제 후 7일 이내 |
| 유료 기능 미사용 | 결제 이후 페이지 생성/AI 사용/배지 제거/프로 기능 사용 없어야 함 |

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `reason` | 선택 | string | 환불 사유 |

## 처리
토스 결제 취소 API 호출 → 성공 시 결제 상태 `refunded` + 구독 무료 전환 +
결제로 부여된 AI 토큰 회수.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 환불 불가 (유료 기능 사용, 7일 초과), 이미 환불됨 |
| 401 | 토큰 없음/만료 |
| 404 | 결제건 없음 |
| 502 | 토스 API 오류 |
        """,
        responses={
            200: OpenApiResponse(response=PaymentHistorySerializer, description="환불 완료"),
            400: OpenApiResponse(description="환불 불가"),
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="결제건 없음"),
            502: OpenApiResponse(description="토스 API 오류"),
        },
    )
    def post(self, request, payment_id):
        try:
            payment = PaymentHistory.objects.get(id=payment_id, user=request.user)
        except PaymentHistory.DoesNotExist:
            return Response(
                {"detail": "결제 내역을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if payment.status != PaymentStatus.PAID:
            return Response(
                {"detail": f"현재 상태({payment.get_status_display()})에서는 환불할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not payment.toss_payment_key:
            return Response(
                {"detail": "토스 결제 정보가 없어 환불할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 유료 기능 사용 여부 확인 (해당 결제 시점 기준)
        from .subscription_utils import ensure_subscription

        sub = ensure_subscription(request.user)
        usage_check = check_refund_eligibility(request.user, sub, payment.paid_at)

        if not usage_check["eligible"]:
            return Response(
                {
                    "detail": "환불 조건을 충족하지 않습니다.",
                    "reasons": usage_check["reasons"],
                    "details": usage_check["details"],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = request.data.get("reason", "고객 요청 환불")

        try:
            TossBillingClient.cancel_payment(
                payment_key=payment.toss_payment_key,
                cancel_reason=reason,
            )
        except TossNetworkError:
            return Response(
                {"detail": "환불 처리 중 통신 오류가 발생했습니다. 잠시 후 다시 시도해주세요."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except TossError as e:
            if e.code == "ALREADY_CANCELED_PAYMENT":
                # 토스에선 이미 취소됨 (대시보드 취소 등) — 우리 DB만 반영
                pass
            else:
                logger.error("토스 환불 실패: order=%s code=%s", payment.toss_order_id, e.code)
                return Response(
                    {"detail": f"환불 처리 실패: {e.message}", "toss_code": e.code},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

        from .toss_flows import apply_refund

        apply_refund(payment, downgrade=True, reason="user_refund")
        payment.refresh_from_db()

        return Response(PaymentHistorySerializer(payment).data)
