"""apps/billing/consent_views.py — 결제 전 고지·동의 API.

1. ``GET  /api/v1/billing/subscription/preview/`` — 신규 구독 견적 (부작용 없는 조회)
2. ``POST /api/v1/billing/consents/``             — 동의 기록 저장 (증거 원장)

설계 결정 (프론트 질의 회신):
- 동의 저장은 **``toss/confirm`` 동봉이 아니라 별도 엔드포인트**다. 이유 두 가지 —
  ① 동의는 계약 체결(카드 등록) **전에** 성립해야 한다. confirm 에 실으면 기록 시각이
     카드 등록 이후가 되어 "체결 전 동의"의 순서가 증거상 뒤집힌다.
  ② confirm 이 실패(카드 거절·통신 오류)해도 "무엇을 보고 동의했는지"는 남아야 한다.
     confirm 에 묶으면 실패 시 동의 기록이 함께 사라진다.
- 저장 실패가 결제를 막지는 않는다(프론트가 201 을 기다릴 필요 없음). 다만 기록이 없으면
  30일 초과 체험은 첫 결제가 차단되므로(consent.blocks_first_charge) 조용히 무시하지 말 것.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.internal_auth import get_client_ip

from .consent import CONSENT_WINDOW_DAYS
from .models import ConsentKind, PaymentConsent, SubscriptionStatus, UserSubscription
from .serializers import (
    PaymentConsentCreateSerializer,
    PaymentConsentSerializer,
    SubscriptionPreviewSerializer,
)
from .toss_flows import BillingFlowError, preview_subscription

logger = logging.getLogger(__name__)

TAG = "사용자플랜"


def _error(message: str, *, code: int = 400, details: dict | None = None) -> Response:
    """프로젝트 표준 에러 포맷 (apps/core/exceptions.custom_exception_handler 와 동일)."""
    return Response(
        {"success": False, "error": {"code": code, "message": message, "details": details or {}}},
        status=code,
    )


class SubscriptionPreviewView(APIView):
    """신규 구독 견적 — 결제 전 고지 화면용 (읽기 전용)."""

    permission_classes = [IsAuthenticated]
    serializer_class = SubscriptionPreviewSerializer

    @extend_schema(
        tags=[TAG],
        summary="신규 구독 견적 (결제 전 고지용)",
        description="""
## 목적
**카드 등록 전에** 체험 종료일·첫 결제일·첫 결제 금액·이후 매월 금액을 확정해 내려줍니다.
전자상거래법 제13조 제2항(계약 체결 **전** 가격·결제 시기 고지)을 만족하는 고지 화면의
데이터 소스입니다.

프론트는 이 응답을 **그대로 표기**하고 자체 날짜 계산을 하지 않아야 합니다.
자체 계산(오늘+30일)과 서버 확정값이 하루라도 어긋나면 그 자체가 허위 고지입니다.

> 참고: 서버의 체험/주기 계산은 **고정 +30일**(`TRIAL_BASE_DAYS` / `PERIOD_DAYS`)입니다.
> 달력 1개월(`+1 month`)이 아니므로 28~31일 달의 오차가 없습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 부작용 없음
- 구독 행을 만들지 않습니다 (무료 구독 자동 생성도 하지 않음)
- 제휴 코드를 **소진하지 않습니다** (유효성만 확인)
- 토스 API 를 호출하지 않습니다

## 쿼리 파라미터
| 파라미터 | 필수 | 설명 |
|---|---|---|
| `plan_name` | ✅ | `basic` / `pro` |
| `extra_ig_accounts` | 선택 | 0~10. `pro` 에서만 허용 (기본 0) |
| `referral_code` | 선택 | 제휴/레퍼럴 코드. 프로 최초 구독(체험 시작)에서만 유효 |

## `scenario` 값
| 값 | 의미 | 오늘 청구 |
|---|---|---|
| `trial` | 프로 최초 구독 → 무료 체험 시작 | ❌ 없음 |
| `attach_only` | 이미 체험 중 + 카드 등록 (기간 불변) | ❌ 없음 |
| `charge_now` | 베이직 구독 / 체험 소진 후 재구독 | ✅ `first_charge_amount` |

## 날짜 필드 3개의 차이 (⚠️ 혼동 주의)
| 필드 | 의미 | 표기 예 |
|---|---|---|
| `trial_last_day` | 체험 **마지막 이용일** (KST 날짜) | "9월 3일까지 무료" |
| `trial_ends_at` | 체험 종료 **시각** = 첫 결제 시각 | — |
| `first_charge_at` | 첫 결제 시각 (= 유료 전환 시점) | "9월 4일 첫 결제" |

`trial_ends_at` 을 날짜로 찍어 "체험 마지막 날"로 쓰면 하루 늘어나 보입니다.

## 프론트엔드 통합
```javascript
const qs = new URLSearchParams({ plan_name: 'pro', extra_ig_accounts: '0' });
if (couponCode) qs.set('referral_code', couponCode);
const res = await fetch(`/api/v1/billing/subscription/preview/?${qs}`, {
  headers: { Authorization: `Bearer ${accessToken}` },
});
if (!res.ok) return showRetryOnly();   // 금액을 모르는 상태의 동의는 동의가 아니다
const q = await res.json();
setNotice({
  freeUntil: q.trial_last_day,          // "9월 3일까지 무료"
  firstChargeAt: q.first_charge_at,     // "9월 4일 첫 결제"
  firstAmount: q.first_charge_amount,   // 14900 (부가세 포함)
  monthly: q.recurring_amount,
});
```

## 에러
| 코드 | 원인 |
|---|---|
| 400 | `plan_name` 누락/무료 플랜 / 추가 계정을 pro 외 플랜에 요청 / 이미 유료 구독 중 / 제휴 코드 무효 |
| 401 | 토큰 없음·만료 |
| 404 | 플랜을 찾을 수 없음 |
| 500 | 서버 오류 |
        """,
        parameters=[
            OpenApiParameter(
                name="plan_name",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="견적 대상 플랜 코드명 (basic/pro).",
            ),
            OpenApiParameter(
                name="extra_ig_accounts",
                type=int,
                location=OpenApiParameter.QUERY,
                required=False,
                description="추가 IG 계정 수 (0~10, pro 전용). 기본 0.",
            ),
            OpenApiParameter(
                name="referral_code",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="제휴/레퍼럴 코드. 프로 최초 구독(체험 시작)에서만 유효.",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=SubscriptionPreviewSerializer,
                description="견적",
                examples=[
                    OpenApiExample(
                        "프로 체험 (쿠폰 없음, 30일)",
                        value={
                            "scenario": "trial",
                            "is_trial": True,
                            "trial_days": 30,
                            "trial_ends_at": "2026-09-09T14:02:11+09:00",
                            "trial_last_day": "2026-09-08",
                            "first_charge_at": "2026-09-09T14:02:11+09:00",
                            "first_charge_amount": 14900,
                            "recurring_amount": 14900,
                            "next_renewal_at": "2026-10-09T14:02:11+09:00",
                            "plan": {"name": "pro", "display_name": "프로"},
                            "extra_ig_accounts": 0,
                            "extra_ig_account_price": 9900,
                            "referral_code": None,
                            "referral_bonus_days": 0,
                        },
                    ),
                    OpenApiExample(
                        "프로 체험 + 제휴코드 14일 (총 44일)",
                        value={
                            "scenario": "trial",
                            "is_trial": True,
                            "trial_days": 44,
                            "trial_ends_at": "2026-09-23T14:02:11+09:00",
                            "trial_last_day": "2026-09-22",
                            "first_charge_at": "2026-09-23T14:02:11+09:00",
                            "first_charge_amount": 14900,
                            "recurring_amount": 14900,
                            "next_renewal_at": "2026-10-23T14:02:11+09:00",
                            "plan": {"name": "pro", "display_name": "프로"},
                            "extra_ig_accounts": 0,
                            "extra_ig_account_price": 9900,
                            "referral_code": "HLEVEL26",
                            "referral_bonus_days": 14,
                        },
                    ),
                    OpenApiExample(
                        "베이직 즉시 결제 (체험 없음)",
                        value={
                            "scenario": "charge_now",
                            "is_trial": False,
                            "trial_days": 0,
                            "trial_ends_at": None,
                            "trial_last_day": None,
                            "first_charge_at": "2026-08-10T14:02:11+09:00",
                            "first_charge_amount": 9900,
                            "recurring_amount": 9900,
                            "next_renewal_at": "2026-09-09T14:02:11+09:00",
                            "plan": {"name": "basic", "display_name": "베이직"},
                            "extra_ig_accounts": 0,
                            "extra_ig_account_price": 9900,
                            "referral_code": None,
                            "referral_bonus_days": 0,
                        },
                    ),
                ],
            ),
            400: OpenApiResponse(description="파라미터/상태 오류 (표준 에러 포맷)"),
            401: OpenApiResponse(description="인증 실패 — 토큰이 없거나 만료됨"),
            404: OpenApiResponse(description="플랜을 찾을 수 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def get(self, request):
        plan_name = (request.query_params.get("plan_name") or "").strip()
        if not plan_name:
            return _error("plan_name 쿼리 파라미터가 필요합니다.", details={"field": "plan_name"})

        raw_extra = (request.query_params.get("extra_ig_accounts") or "0").strip()
        try:
            extra = int(raw_extra)
        except ValueError:
            return _error(
                "extra_ig_accounts 는 정수여야 합니다.", details={"field": "extra_ig_accounts"}
            )
        if not 0 <= extra <= 10:
            return _error(
                "extra_ig_accounts 는 0~10 사이여야 합니다.",
                details={"field": "extra_ig_accounts"},
            )

        referral_code = (request.query_params.get("referral_code") or "").strip() or None

        try:
            quote = preview_subscription(
                request.user,
                plan_name=plan_name,
                extra_ig_accounts=extra,
                referral_code=referral_code,
            )
        except BillingFlowError as e:
            return _error(e.detail, code=e.status_code, details=e.extra)

        return Response(SubscriptionPreviewSerializer(quote).data)


class PaymentConsentCreateView(APIView):
    """결제 전 고지·동의 기록 저장 (증거 원장)."""

    permission_classes = [IsAuthenticated]
    serializer_class = PaymentConsentCreateSerializer

    @extend_schema(
        tags=[TAG],
        summary="결제 동의 기록 저장",
        description=f"""
## 목적
사용자가 고지 화면에서 **버튼을 눌러 동의한 사실**과 **그때 화면에 표시된 내용**을 저장합니다.
전자상거래법 제13조 제6항(무료→유료 정기결제 전환 시 소비자 동의) + 시행령 제20조의2
(전환 전 {CONSENT_WINDOW_DAYS}일 이내)의 증거입니다.

분쟁 시 재현해야 하는 것은 "동의했다"가 아니라 **그때 무엇을 보여줬는지**입니다. 그래서
고지한 금액·첫 결제일·문구 버전을 그 시점 값으로 스냅샷해 보관합니다(사후 정책 변경과 무관).

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 호출 시점 — `kind` 별
| `kind` | 언제 | 효과 |
|---|---|---|
| `initial` | 고지 시트에서 '동의하고 계속' → **토스 카드 등록 SDK 호출 직전** | 기록만 (게이트 없음) |
| `conversion` | `my-subscription.conversion_consent_required=true` 모달에서 동의 | **첫 결제 차단이 해제**된다 |

`initial` 은 `toss/confirm` **전에** 호출하세요 — 동의는 계약 체결 전에 성립해야 하고,
confirm 이 실패해도 "무엇을 보고 동의했는지"는 남아야 합니다. `toss/confirm` 에 동의 정보를
동봉하는 방식은 이 두 요건을 깨뜨리므로 채택하지 않았습니다.

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|---|---|---|---|
| `kind` | ✅ | `initial`/`conversion` | 동의 종류 |
| `plan_name` | ✅ | string | 화면에 표시한 플랜 코드명 (basic/pro) |
| `disclosed_first_charge_at` | 선택 | date | 화면에 표시한 첫 결제일 `YYYY-MM-DD` (ISO datetime 도 허용 — 날짜부만 저장) |
| `disclosed_amount` | ✅ | int | 화면에 표시한 금액(원, 부가세 포함) |
| `disclosed_recurring_cycle` | 선택 | string | 기본 `monthly` |
| `payment_method_type` | 선택 | string | 기본 `card` |
| `copy_version` | ✅ | string | 동의 문구 버전 (예: `billingConsent@2026-08-10`) |
| `agreed_terms` | ✅ | bool | 이용약관 |
| `agreed_privacy` | ✅ | bool | 개인정보 수집·이용 |
| `agreed_recurring` | ✅ | bool | 정기결제(자동 유료전환) |

**세 동의가 모두 `true` 여야 합니다.** 하나라도 `false` 면 400 — 공정위 지침상 명시적으로
동의하지 않은 항목은 '동의 없음'으로 처리해야 하고, 일부만 체크된 상태를 동의로 기록하면
그 기록 자체가 무효 증거가 됩니다.

## 함께 저장되는 것 (서버가 채움)
동의 시각 · 요청 IP · User-Agent · 세션/요청 식별자(`X-Request-ID`) · 동의 시점의 구독 행.

## `kind=conversion` 의 부수 효과
구독의 `conversion_consent_at` 이 채워지고, 체험 종료 시 **정상 결제**됩니다.
동의가 없으면 결제하지 않고 무료 플랜으로 전환됩니다(데이터·설정·캠페인은 전부 보존).
`trialing` 상태가 아니면 기록은 남기지만 `conversion_consent_at` 은 갱신하지 않습니다
(응답 `applied_to_subscription=false`).

## 프론트엔드 통합
```javascript
await fetch('/api/v1/billing/consents/', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json', Authorization: `Bearer ${{accessToken}}` }},
  body: JSON.stringify({{
    kind: 'conversion',
    plan_name: 'pro',
    disclosed_first_charge_at: '2026-09-23',
    disclosed_amount: 14900,
    disclosed_recurring_cycle: 'monthly',
    payment_method_type: 'card',
    copy_version: 'billingConsent@2026-08-10',
    agreed_terms: true, agreed_privacy: true, agreed_recurring: true,
  }}),
}});
```

## 에러
| 코드 | 원인 |
|---|---|
| 400 | 필드 누락/타입 오류 / 세 동의 중 하나라도 false |
| 401 | 토큰 없음·만료 |
| 500 | 서버 오류 |
        """,
        request=PaymentConsentCreateSerializer,
        responses={
            201: OpenApiResponse(
                response=PaymentConsentSerializer,
                description="저장된 동의 기록",
                examples=[
                    OpenApiExample(
                        "2차 동의 저장 성공",
                        value={
                            "id": "9f1c0b7a-0000-0000-0000-0000000000aa",
                            "kind": "conversion",
                            "plan_name": "pro",
                            "disclosed_first_charge_at": "2026-09-23",
                            "disclosed_amount": 14900,
                            "disclosed_recurring_cycle": "monthly",
                            "payment_method_type": "card",
                            "copy_version": "billingConsent@2026-08-10",
                            "agreed_terms": True,
                            "agreed_privacy": True,
                            "agreed_recurring": True,
                            "consented_at": "2026-09-09T14:02:11+09:00",
                            "applied_to_subscription": True,
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="검증 실패 (표준 에러 포맷 · details 에 필드별 사유)",
                examples=[
                    OpenApiExample(
                        "정기결제 동의 미체크",
                        value={
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "입력값을 확인해주세요.",
                                "details": {
                                    "agreed_recurring": [
                                        "이 항목에 동의하지 않으면 동의 기록을 저장할 수 없습니다."
                                    ]
                                },
                            },
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰이 없거나 만료됨"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def post(self, request):
        serializer = PaymentConsentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _error("입력값을 확인해주세요.", details=serializer.errors)
        data = serializer.validated_data

        sub = UserSubscription.objects.filter(user=request.user).first()
        consent = PaymentConsent.objects.create(
            user=request.user,
            subscription=sub,
            kind=data["kind"],
            plan_name=data["plan_name"],
            disclosed_first_charge_at=data.get("disclosed_first_charge_at"),
            disclosed_amount=data["disclosed_amount"],
            disclosed_recurring_cycle=data.get("disclosed_recurring_cycle") or "monthly",
            payment_method_type=data.get("payment_method_type") or "card",
            copy_version=data["copy_version"],
            agreed_terms=True,
            agreed_privacy=True,
            agreed_recurring=True,
            ip_address=get_client_ip(request) or None,
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:300],
            session_key=(
                getattr(request, "id", "") or request.META.get("HTTP_X_REQUEST_ID", "") or ""
            )[:64],
        )

        # kind=conversion 은 **과금 게이트를 해제**한다 — 체험 중일 때만.
        applied = False
        if data["kind"] == ConsentKind.CONVERSION and sub is not None:
            if sub.status == SubscriptionStatus.TRIALING:
                # 최초 1회만 기록한다(가장 이른 동의 시각이 증거로서 정본).
                applied = bool(
                    UserSubscription.objects.filter(
                        pk=sub.pk, conversion_consent_at__isnull=True
                    ).update(conversion_consent_at=consent.consented_at)
                )
                if not applied:
                    applied = sub.conversion_consent_at is not None

        logger.info(
            "결제 동의 기록: user=%s kind=%s plan=%s amount=%s copy=%s applied=%s",
            request.user.email,
            consent.kind,
            consent.plan_name,
            consent.disclosed_amount,
            consent.copy_version,
            applied,
        )

        payload = PaymentConsentSerializer(consent).data
        payload["applied_to_subscription"] = applied
        return Response(payload, status=status.HTTP_201_CREATED)
