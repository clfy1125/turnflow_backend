"""
Payment API views — PayApp 결제 연동.

1. PayAppFeedbackView  — PayApp feedbackurl 웹훅 (결제통보)
2. PayAppFailView      — PayApp failurl 웹훅 (정기결제 2회차 이후 실패)
3. PaymentHistoryView  — 결제 내역 조회
4. RefundPaymentView   — 결제 환불 요청
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse

from .models import (
    PayAppWebhookLog,
    PaymentHistory,
    PaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
    AiTokenBalance,
)
from .payapp_service import PayAppClient, PayAppError
from .serializers import PaymentHistorySerializer

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 헬퍼 함수
# ──────────────────────────────────────────────


def _pay_type_to_method(pay_type: str) -> str:
    """pay_type 정수 코드를 payment_method 문자열로 변환."""
    mapping = {"1": "card", "2": "phone", "6": "bank_transfer", "7": "virtual_account"}
    return mapping.get(str(pay_type), "other")


def _calculate_period_end() -> timezone.datetime:
    """다음 결제 주기 종료일을 계산 (월간 30일)."""
    return timezone.now() + timedelta(days=30)


# ──────────────────────────────────────────────
# 1) PayApp Feedback (결제통보)
# ──────────────────────────────────────────────


@method_decorator(csrf_exempt, name="dispatch")
class PayAppFeedbackView(APIView):
    """
    PayApp feedbackurl 웹훅 수신.
    결제 상태 변경 시 PayApp 서버가 POST로 호출합니다.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["PG사 연동"],
        summary="PayApp 결제 통보 웹훅 (feedbackurl)",
        description="""
## 목적
PayApp 서버가 결제 상태 변경 시 **직접 호출**하는 웹훅 엔드포인트입니다.

## ⚠️ 프론트엔드에서 호출하지 마세요
이 엔드포인트는 **PayApp 서버 → 백엔드** 전용입니다.

## 보안 검증
- `userid`, `linkkey`, `linkval` 값이 서버 설정과 일치하는지 검증
- 값 불일치 시에도 PayApp 재시도 방지를 위해 SUCCESS 반환

## 멱등 처리
- `(mul_no, pay_state)` 조합으로 DB unique 제약 → 중복 호출 시 안전하게 스킵
- feedbackurl은 여러 번 호출될 수 있으므로 반드시 멱등하게 처리합니다

## pay_state 처리 분기
| pay_state | 의미 | 처리 |
|-----------|------|------|
| 1 | 요청 (JS API 최초 노티) | 로그만 기록 |
| 4 | **결제완료** | PaymentHistory(paid) 생성 + 구독 활성화 + AI 토큰 부여 |
| 8, 32 | 요청취소 | 로그 기록 |
| 9, 64 | **승인취소(환불)** | PaymentHistory(refunded) + 구독 cancelled |
| 10 | 결제대기(가상계좌 입금전) | 로그 기록 |
| 70, 71 | **부분취소** | 부분 환불 기록 |

## 응답
PayApp은 HTTP 200 + body `SUCCESS`를 기대합니다.
`checkretry=y` 설정 시 SUCCESS가 아니면 최대 10회 재시도합니다.
        """,
        responses={
            200: OpenApiResponse(description="SUCCESS — PayApp이 기대하는 응답"),
        },
    )
    def post(self, request):
        data = request.POST.dict() if hasattr(request, "POST") else request.data

        # ── 보안 검증 ──
        if not self._verify_credentials(data):
            logger.warning("PayApp feedbackurl 인증 실패: %s", data.get("mul_no"))
            return HttpResponse("SUCCESS", status=200)

        mul_no = data.get("mul_no", "")
        pay_state = str(data.get("pay_state", ""))
        rebill_no = data.get("rebill_no", "")

        # ── 멱등 체크 ──
        try:
            log_entry, created = PayAppWebhookLog.objects.get_or_create(
                mul_no=mul_no,
                pay_state=pay_state,
                defaults={
                    "webhook_type": "feedback",
                    "rebill_no": rebill_no,
                    "raw_data": data,
                    "processed": False,
                },
            )
        except IntegrityError:
            return HttpResponse("SUCCESS", status=200)

        if not created and log_entry.processed:
            logger.info("PayApp feedbackurl 중복 호출 무시: mul_no=%s pay_state=%s", mul_no, pay_state)
            return HttpResponse("SUCCESS", status=200)

        # ── pay_state별 처리 ──
        try:
            with transaction.atomic():
                if pay_state == "4":
                    self._handle_payment_completed(data)
                elif pay_state in ("9", "64"):
                    self._handle_payment_cancelled(data)
                elif pay_state in ("70", "71"):
                    self._handle_partial_cancel(data)

                log_entry.processed = True
                log_entry.save(update_fields=["processed"])

        except Exception:
            logger.exception("PayApp feedbackurl 처리 오류: mul_no=%s", mul_no)

        return HttpResponse("SUCCESS", status=200)

    @staticmethod
    def _verify_credentials(data: dict) -> bool:
        """PayApp 인증 검증."""
        return (
            data.get("userid") == settings.PAYAPP_USERID
            and data.get("linkkey") == settings.PAYAPP_LINKKEY
            and data.get("linkval") == settings.PAYAPP_LINKVAL
        )

    @staticmethod
    def _handle_payment_completed(data: dict):
        """pay_state=4: 결제완료 처리."""
        mul_no = data.get("mul_no", "")
        rebill_no = data.get("rebill_no", "")
        price = int(data.get("price", 0))
        pay_type = str(data.get("pay_type", ""))
        var1 = data.get("var1", "")  # subscription_id
        var2 = data.get("var2", "")  # new plan name
        csturl = data.get("csturl", "")

        try:
            sub = UserSubscription.objects.select_for_update().get(id=var1)
        except (UserSubscription.DoesNotExist, ValueError):
            logger.error("PayApp 결제완료: subscription_id=%s 찾을 수 없음", var1)
            return

        # var2에 담긴 플랜으로 업데이트 (결제 완료 시점에 플랜 변경)
        new_plan = None
        if var2:
            try:
                new_plan = SubscriptionPlan.objects.get(name=var2, is_active=True)
            except SubscriptionPlan.DoesNotExist:
                logger.warning("PayApp 결제완료: plan_name=%s 찾을 수 없음", var2)

        plan_display = new_plan.display_name if new_plan else sub.plan.display_name

        # PaymentHistory 생성 (payapp_mul_no unique → 멱등)
        payment, created = PaymentHistory.objects.get_or_create(
            payapp_mul_no=mul_no,
            defaults={
                "user": sub.user,
                "subscription": sub,
                "amount": price,
                "status": PaymentStatus.PAID,
                "payment_method": _pay_type_to_method(pay_type),
                "description": f"턴플로우 {plan_display} 월간 구독",
                "payapp_rebill_no": rebill_no,
                "receipt_url": csturl,
                "pay_type_display": PayAppClient.get_pay_type_display(pay_type),
                "paid_at": timezone.now(),
            },
        )
        if not created:
            return

        # 구독 활성화 + 플랜 반영
        now = timezone.now()
        if new_plan:
            sub.plan = new_plan
        sub.status = SubscriptionStatus.ACTIVE
        sub.current_period_start = now
        sub.current_period_end = _calculate_period_end()
        sub.cancelled_at = None
        if rebill_no:
            sub.payapp_rebill_no = rebill_no
        # 유료 플랜 최초 활성화 시각 기록 (환불 7일 심사용)
        if sub.plan.name != "free" and not sub.pro_activated_at:
            sub.pro_activated_at = now
        sub.save(update_fields=[
            "plan", "status", "current_period_start", "current_period_end",
            "cancelled_at", "payapp_rebill_no", "pro_activated_at", "updated_at",
        ])

        # 페이지 전체 활성화 (유료 전환 시)
        if sub.plan.name != "free":
            from apps.pages.models import Page
            Page.objects.filter(user=sub.user, is_active=False).update(is_active=True)

        # AI 토큰 부여
        monthly_tokens = sub.plan.features.get("monthly_ai_tokens", 0)
        if monthly_tokens > 0:
            token_balance = AiTokenBalance.get_or_create_for_user(sub.user)
            token_balance.grant(
                monthly_tokens,
                description=f"{sub.plan.display_name} 구독 결제 토큰 지급",
            )

        # 레퍼럴 트라이얼 → 유료 전환 마킹 (있을 때만)
        from .models import ReferralRedemption

        try:
            redemption = ReferralRedemption.objects.select_for_update().get(user=sub.user)
            if not redemption.converted_to_paid:
                redemption.converted_to_paid = True
                redemption.converted_at = now
                redemption.save(update_fields=["converted_to_paid", "converted_at"])
        except ReferralRedemption.DoesNotExist:
            pass

        logger.info(
            "PayApp 결제완료 처리: user=%s plan=%s mul_no=%s",
            sub.user.email, sub.plan.name, mul_no,
        )

    @staticmethod
    def _handle_payment_cancelled(data: dict):
        """pay_state=9,64: 승인취소(환불) 처리 + 전체 다운그레이드 정리."""
        mul_no = data.get("mul_no", "")
        try:
            payment = PaymentHistory.objects.get(payapp_mul_no=mul_no)
        except PaymentHistory.DoesNotExist:
            logger.warning("PayApp 승인취소: mul_no=%s 결제내역 없음", mul_no)
            return

        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=["status"])

        if payment.subscription:
            sub = payment.subscription
            free_plan = SubscriptionPlan.objects.filter(name="free").first()
            if free_plan:
                from .tasks import _downgrade_to_free
                _downgrade_to_free(sub, free_plan, reason="payment_cancelled")

                # 환불 시 구독 결제로 부여된 AI 토큰 회수
                token_balance = AiTokenBalance.objects.filter(user=sub.user).first()
                if token_balance and payment.payapp_rebill_no:
                    # 해당 결제로 부여된 토큰을 추정하여 차감
                    from .models import AiTokenLedger
                    granted = AiTokenLedger.objects.filter(
                        user=sub.user,
                        amount__gt=0,
                        description__contains="구독 결제 토큰 지급",
                    ).order_by("-created_at").first()
                    if granted and token_balance.balance >= granted.amount:
                        token_balance.deduct(
                            granted.amount,
                            description=f"환불에 따른 토큰 회수 (mul_no={mul_no})",
                        )

        logger.info("PayApp 승인취소 처리: mul_no=%s", mul_no)

    @staticmethod
    def _handle_partial_cancel(data: dict):
        """pay_state=70,71: 부분취소 처리."""
        mul_no = data.get("mul_no", "")
        cancel_price = int(data.get("price", 0))

        try:
            payment = PaymentHistory.objects.get(
                payapp_mul_no=data.get("orig_mul_no", mul_no)
            )
        except PaymentHistory.DoesNotExist:
            logger.warning("PayApp 부분취소: mul_no=%s 원거래 없음", mul_no)
            return

        PaymentHistory.objects.create(
            user=payment.user,
            subscription=payment.subscription,
            amount=-cancel_price,
            status=PaymentStatus.REFUNDED,
            payment_method=payment.payment_method,
            description=f"부분 환불 (원거래: {payment.payapp_mul_no})",
            payapp_mul_no=mul_no,
            receipt_url=data.get("csturl", ""),
            paid_at=timezone.now(),
        )

        logger.info("PayApp 부분취소 처리: mul_no=%s 금액=%d", mul_no, cancel_price)


# ──────────────────────────────────────────────
# 2) PayApp Fail (정기결제 실패)
# ──────────────────────────────────────────────


@method_decorator(csrf_exempt, name="dispatch")
class PayAppFailView(APIView):
    """
    PayApp failurl 웹훅 수신.
    2회차 이후 정기결제 자동 승인 실패 시 호출됩니다.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["PG사 연동"],
        summary="PayApp 정기결제 실패 웹훅 (failurl)",
        description="""
## 목적
PayApp 정기결제 2회차 이후 자동 결제가 **실패**했을 때 호출되는 웹훅입니다.
1회차 승인 실패는 이 URL로 통보되지 않습니다.

## ⚠️ 프론트엔드에서 호출하지 마세요
이 엔드포인트는 **PayApp 서버 → 백엔드** 전용입니다.

## 처리 로직
1. `PayAppWebhookLog` 기록
2. 해당 구독의 `status`를 `past_due`로 변경 (유예 기간 시작)
3. 유예 기간(7일) 내 결제 성공하지 않으면 배치 태스크에서 free 다운그레이드

## 응답
HTTP 200 + body `SUCCESS`. SUCCESS가 아니면 재통보됩니다.
        """,
        responses={
            200: OpenApiResponse(description="SUCCESS"),
        },
    )
    def post(self, request):
        data = request.POST.dict() if hasattr(request, "POST") else request.data

        rebill_no = data.get("rebill_no", "")
        mul_no = data.get("mul_no", "")
        pay_state = str(data.get("pay_state", "99"))

        try:
            PayAppWebhookLog.objects.get_or_create(
                mul_no=mul_no,
                pay_state=pay_state,
                defaults={
                    "webhook_type": "fail",
                    "rebill_no": rebill_no,
                    "raw_data": data,
                    "processed": False,
                },
            )
        except IntegrityError:
            pass

        if rebill_no:
            subs = UserSubscription.objects.filter(
                payapp_rebill_no=rebill_no,
                status=SubscriptionStatus.ACTIVE,
            )
            updated = subs.update(status=SubscriptionStatus.PAST_DUE)
            if updated:
                logger.warning(
                    "PayApp 정기결제 실패: rebill_no=%s → past_due (%d건)",
                    rebill_no, updated,
                )

        return HttpResponse("SUCCESS", status=200)


# ──────────────────────────────────────────────
# 3) 결제 내역 조회
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
| `status` | string | `pending` / `paid` / `failed` / `refunded` |
| `payment_method` | string | `card` / `phone` / `bank_transfer` 등 |
| `description` | string | 결제 설명 (예: "프로 플랜 월간 구독") |
| `payapp_mul_no` | string | PayApp 결제요청번호 |
| `receipt_url` | string | 매출전표(영수증) URL. 카드 결제 시 제공 |
| `pay_type_display` | string | 결제수단 한글 표시명 (예: "신용카드") |
| `paid_at` | datetime | 결제 완료 시각 |
| `created_at` | datetime | 결제 요청 생성 시각 |

## 영수증 확인
`receipt_url`이 있는 결제건은 해당 URL을 브라우저에서 열면 PayApp 매출전표를 확인할 수 있습니다.

## 프론트엔드 통합
```typescript
const res = await fetch('/api/v1/billing/payments/history/', {
  headers: { 'Authorization': `Bearer ${accessToken}` }
});
const payments = await res.json();

payments.forEach(p => {
  console.log(`${p.description}: ${p.amount.toLocaleString()}원 (${p.status})`);
  if (p.receipt_url) {
    console.log(`영수증: ${p.receipt_url}`);
  }
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
                                "payapp_mul_no": "12345",
                                "receipt_url": "https://www.payapp.kr/CST/abc123",
                                "pay_type_display": "신용카드",
                                "paid_at": "2026-04-01T10:30:00Z",
                                "created_at": "2026-04-01T10:29:50Z",
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
# 4) 환불 적격성 확인 + 환불 요청
# ──────────────────────────────────────────────

REFUND_WINDOW_DAYS = 7  # 환불 가능 기간 (일)


def _check_pro_feature_usage(user, sub) -> dict:
    """
    프로 기능 사용 여부를 조사하여 환불 적격성을 판단한다.
    Returns: {"eligible": bool, "reasons": list[str], "details": dict}
    """
    from apps.pages.models import Page
    from .models import AiTokenLedger

    reasons = []
    details = {}

    # 1) 유료 활성화 후 7일 이내인지
    if sub.pro_activated_at:
        days_since = (timezone.now() - sub.pro_activated_at).days
        details["days_since_activation"] = days_since
        if days_since > REFUND_WINDOW_DAYS:
            reasons.append(f"유료 플랜 활성화 후 {REFUND_WINDOW_DAYS}일이 경과했습니다.")
    else:
        reasons.append("유료 플랜 활성화 기록이 없습니다.")

    # 2) 페이지 수 조사 (가입 시 1개 → 2개 이상이면 pro 기능 사용)
    page_count = Page.objects.filter(user=user).count()
    details["page_count"] = page_count
    if page_count > 1:
        reasons.append(f"프로 기능으로 페이지를 {page_count}개 보유 중입니다 (무료는 1개).")

    # 3) 구독 결제 후 부여된 AI 토큰 사용 확인
    if sub.pro_activated_at:
        tokens_used = AiTokenLedger.objects.filter(
            user=user,
            amount__lt=0,
            created_at__gte=sub.pro_activated_at,
        ).count()
        details["ai_tokens_used_since_pro"] = tokens_used
        if tokens_used > 0:
            reasons.append(f"프로 플랜 기간 중 AI 토큰 {tokens_used}회 사용했습니다.")

    # 4) 로고 제거 사용 여부 (logoStyle == "hidden")
    logo_removed_pages = Page.objects.filter(
        user=user,
        data__design_settings__logoStyle="hidden",
    ).count()
    details["logo_removed_pages"] = logo_removed_pages
    if logo_removed_pages > 0:
        reasons.append(f"로고 제거 기능을 {logo_removed_pages}개 페이지에서 사용했습니다.")

    # 5) 커스텀 CSS 사용 여부
    css_pages = Page.objects.filter(user=user).exclude(custom_css="").count()
    details["custom_css_pages"] = css_pages
    if css_pages > 0:
        reasons.append(f"커스텀 CSS를 {css_pages}개 페이지에서 사용했습니다.")

    eligible = len(reasons) == 0
    return {"eligible": eligible, "reasons": reasons, "details": details}


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

## 환불 가능 조건
모든 조건을 **동시에** 만족해야 환불 가능:
1. 유료 플랜 활성화 후 7일 이내
2. 페이지를 1개만 보유 (추가 생성 안 함)
3. AI 토큰 미사용 (프로 구독 이후)
4. 로고 제거 기능 미사용
5. 커스텀 CSS 미사용

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `eligible` | bool | 환불 가능 여부 |
| `reasons` | array[string] | 환불 불가 사유 목록. 빈 배열이면 환불 가능 |
| `details` | object | 상세 조사 결과 |
        """,
        responses={
            200: OpenApiResponse(description="환불 가능 여부"),
            400: OpenApiResponse(description="무료 플랜 사용자"),
        },
    )
    def get(self, request):
        from .subscription_utils import ensure_subscription
        sub = ensure_subscription(request.user)

        if sub.plan.name == "free":
            return Response(
                {"eligible": False, "reasons": ["무료 플랜 사용자는 환불 대상이 아닙니다."], "details": {}},
            )

        result = _check_pro_feature_usage(request.user, sub)
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
프로 기능 미사용 + 결제 후 7일 이내인 경우에만 환불 가능합니다.

## 환불 조건
| 조건 | 설명 |
|------|------|
| 본인 결제 | 로그인 사용자의 결제건만 |
| 결제완료(paid) | 상태가 paid인 건만 |
| 7일 이내 | 유료 플랜 활성화 후 7일 이내 |
| 프로 기능 미사용 | 페이지 추가 생성, AI 사용, 로고 제거, CSS 편집 없어야 함 |

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `reason` | 선택 | string | 환불 사유 |

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 환불 불가 (프로 기능 사용, 7일 초과), 이미 환불됨 |
| 404 | 결제건 없음 |
| 502 | PayApp API 오류 |
        """,
        responses={
            200: OpenApiResponse(response=PaymentHistorySerializer, description="환불 완료"),
            400: OpenApiResponse(description="환불 불가"),
            404: OpenApiResponse(description="결제건 없음"),
            502: OpenApiResponse(description="PayApp API 오류"),
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

        if not payment.payapp_mul_no:
            return Response(
                {"detail": "PayApp 결제 정보가 없어 환불할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 프로 기능 사용 여부 확인
        from .subscription_utils import ensure_subscription
        sub = ensure_subscription(request.user)
        usage_check = _check_pro_feature_usage(request.user, sub)

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
            PayAppClient.cancel_payment(
                mul_no=payment.payapp_mul_no,
                memo=reason[:100],
            )
        except PayAppError as e:
            logger.error("PayApp 환불 실패: mul_no=%s error=%s", payment.payapp_mul_no, e)
            return Response(
                {"detail": f"환불 처리 실패: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=["status"])

        return Response(PaymentHistorySerializer(payment).data)
