"""
Referral API views — 레퍼럴 코드 입력 시 결제 없이 트라이얼 부여.

1. ValidateReferralCodeView   — 코드 사전 검증 (인증 불필요)
2. RedeemReferralCodeView     — 코드 사용 (트라이얼 시작)
3. MyReferralRedemptionView   — 내 레퍼럴 사용 이력 조회
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AiTokenBalance,
    ReferralCode,
    ReferralRedemption,
    SubscriptionStatus,
    UserSubscription,
)
from .serializers import (
    ReferralCodeRedeemRequestSerializer,
    ReferralCodeValidateResponseSerializer,
    ReferralRedemptionSerializer,
    SubscriptionPlanSerializer,
    UserSubscriptionSerializer,
)
from .subscription_utils import ensure_subscription

logger = logging.getLogger(__name__)


def _normalize_code(raw: str) -> str:
    return (raw or "").strip().upper()


# ──────────────────────────────────────────────
# 1) 레퍼럴 코드 사전 검증
# ──────────────────────────────────────────────


class ValidateReferralCodeView(APIView):
    """레퍼럴 코드가 사용 가능한지 사전 검증 (실제 사용 X)"""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["레퍼럴"],
        summary="레퍼럴 코드 검증",
        description="""
## 목적
사용자가 입력한 레퍼럴 코드가 **현재 시점에 사용 가능한지** 사전 검증합니다.
실제로 사용(트라이얼 시작)하지 않으며, 코드 입력 UI에서 즉시 피드백을 주기 위해 사용합니다.

## 인증
**불필요** — 회원가입/로그인 전 단계에서도 사용 가능

## 사용 시나리오
- 회원가입 화면의 "레퍼럴 코드 입력" 필드에서 blur 또는 onChange 검증
- 결제 페이지의 "프로모션 코드" 입력 시 즉시 표시
- "이 코드는 X일 무료 트라이얼이 적용됩니다" 같은 안내 문구 표시

## 검증 항목
| 항목 | 통과 조건 |
|------|----------|
| 코드 존재 | DB에 등록된 코드여야 함 |
| 활성 상태 | `is_active = true` |
| 시작 시각 | `valid_from`이 있다면 현재 ≥ valid_from |
| 종료 시각 | `valid_until`이 있다면 현재 ≤ valid_until |
| 사용 횟수 | `max_uses`가 있다면 `current_uses < max_uses` |

## 입력 정규화
- **대소문자 무시**: `welcome2026` 도 `WELCOME2026` 으로 처리
- **앞뒤 공백 제거**: 공백은 자동 trim

## 응답 필드 설명
| 필드 | 타입 | 설명 |
|------|------|------|
| `valid` | bool | 사용 가능 여부 |
| `reason` | string | 사용 불가 사유 (valid=false일 때만) |
| `trial_days` | int | 부여될 트라이얼 일수 (valid=true) |
| `plan` | object | 트라이얼로 부여될 플랜 정보 (valid=true) |

## 프론트엔드 통합
```typescript
const res = await fetch(
  `/api/v1/billing/referral/validate/?code=${encodeURIComponent(code)}`
);
const data = await res.json();

if (data.valid) {
  showHint(`${data.plan.display_name} ${data.trial_days}일 무료 체험!`);
} else {
  showError(data.reason);
}
```

## 에러 응답
| 코드 | 원인 |
|------|------|
| 400 | `code` 쿼리 파라미터 누락 또는 빈 값 |

> ⚠️ **유의**: 이 엔드포인트는 인증이 없습니다. 짧은 코드를 무차별 대입하는 공격이 가능하므로,
> 운영 단계에서는 IP 단위 throttle을 추가하는 것을 권장합니다.
        """,
        parameters=[
            OpenApiParameter(
                name="code",
                description="검증할 레퍼럴 코드. 대소문자 무시, 앞뒤 공백 자동 제거.",
                required=True,
                type=str,
                location=OpenApiParameter.QUERY,
                examples=[
                    OpenApiExample("예시", value="WELCOME2026"),
                ],
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=ReferralCodeValidateResponseSerializer,
                description="검증 결과 — valid 필드로 사용 가능 여부 확인",
                examples=[
                    OpenApiExample(
                        "사용 가능",
                        value={
                            "valid": True,
                            "trial_days": 30,
                            "plan": {
                                "id": "550e8400-e29b-41d4-a716-446655440002",
                                "name": "pro",
                                "display_name": "프로",
                                "monthly_price": 9900,
                                "features": {
                                    "max_pages": 5,
                                    "ai_generation": True,
                                    "remove_logo": True,
                                    "custom_css": True,
                                },
                                "sort_order": 1,
                            },
                        },
                    ),
                    OpenApiExample(
                        "코드 미존재",
                        value={"valid": False, "reason": "존재하지 않는 코드입니다."},
                    ),
                    OpenApiExample(
                        "비활성 코드",
                        value={"valid": False, "reason": "비활성화된 코드입니다."},
                    ),
                    OpenApiExample(
                        "기간 만료",
                        value={"valid": False, "reason": "유효 기간이 만료된 코드입니다."},
                    ),
                    OpenApiExample(
                        "사용 횟수 소진",
                        value={"valid": False, "reason": "사용 횟수가 모두 소진된 코드입니다."},
                    ),
                ],
            ),
            400: OpenApiResponse(
                description="code 파라미터 누락",
                examples=[
                    OpenApiExample(
                        "예시",
                        value={
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "code 쿼리 파라미터가 필요합니다.",
                                "details": {},
                            },
                        },
                    ),
                ],
            ),
        },
    )
    def get(self, request):
        code_str = _normalize_code(request.query_params.get("code", ""))
        if not code_str:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "code 쿼리 파라미터가 필요합니다.",
                        "details": {},
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            code = ReferralCode.objects.select_related("target_plan").get(code=code_str)
        except ReferralCode.DoesNotExist:
            return Response({"valid": False, "reason": "존재하지 않는 코드입니다."})

        ok, reason = code.is_redeemable()
        if not ok:
            return Response({"valid": False, "reason": reason})

        return Response(
            {
                "valid": True,
                "trial_days": code.trial_days,
                "plan": SubscriptionPlanSerializer(code.target_plan).data,
            }
        )


# ──────────────────────────────────────────────
# 2) 레퍼럴 코드 사용 (트라이얼 시작)
# ──────────────────────────────────────────────


class RedeemReferralCodeView(APIView):
    """레퍼럴 코드 사용 → 결제 없이 트라이얼 시작"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["레퍼럴"],
        summary="레퍼럴 코드 사용 (트라이얼 시작)",
        description="""
## 목적
입력한 레퍼럴 코드를 사용해 **결제 없이 N일 무료 트라이얼**을 시작합니다.
PayApp 결제는 호출되지 않으며, 카드 정보도 수집하지 않습니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 동작
성공 시 다음을 한 트랜잭션 안에서 수행합니다:
1. `UserSubscription`을 다음 값으로 갱신
   - `plan = referral_code.target_plan` (예: pro)
   - `status = trialing`
   - `current_period_start = now`
   - `current_period_end = now + trial_days`
   - `cancelled_at = null`
2. 사용자의 모든 페이지를 활성화 (유료 동등 권한)
3. `target_plan.features.monthly_ai_tokens` 만큼 AI 토큰 지급
4. `ReferralCode.current_uses += 1`
5. `ReferralRedemption` 생성

> 📌 `pro_activated_at`은 **설정하지 않습니다**. 이 필드는 7일 환불 심사용으로,
> 실제 결제가 발생한 시점(트라이얼 종료 후 정기결제)에서만 채워집니다.

## 트라이얼 만료 처리
- `current_period_end`가 지나면 매일 실행되는 `billing.handle_trial_expiry` 배치가 free 플랜으로 자동 다운그레이드합니다.
- 다운그레이드 시 페이지 비활성화/로고 복원/커스텀 CSS 초기화가 함께 진행됩니다.

## 트라이얼 → 유료 전환
- 트라이얼 중 `POST /billing/change-plan/` 또는 동일 플랜 결제 진행 시 정상 결제 흐름으로 진입합니다.
- PayApp 결제 완료 시점에 `ReferralRedemption.converted_to_paid`가 자동으로 `True`로 마킹됩니다.

## 사용 가능 조건 (모두 충족해야 함)
| 조건 | 위반 시 |
|------|---------|
| 사용자 본인이 아직 레퍼럴 미사용 (1유저 1회) | 400 `이미 레퍼럴 코드를 사용하셨습니다.` |
| 현재 무료 플랜 사용자 | 400 `이미 유료 플랜을 사용 중입니다.` |
| 현재 트라이얼 중 아님 | 400 `이미 트라이얼이 진행 중입니다.` |
| 코드 자체 사용 가능 (검증 API와 동일 검사) | 400 + 사유 메시지 |

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `code` | ✅ | string | 레퍼럴 코드. 대소문자 무시, 앞뒤 공백 자동 제거 |

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `detail` | string | 처리 결과 메시지 |
| `redemption` | object | 생성된 ReferralRedemption (trial 정보 포함) |
| `subscription` | object | 갱신된 UserSubscription |

## 프론트엔드 통합
```typescript
const res = await fetch('/api/v1/billing/referral/redeem/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${accessToken}`,
  },
  body: JSON.stringify({ code: 'WELCOME2026' }),
});

if (res.ok) {
  const data = await res.json();
  const endsAt = new Date(data.redemption.trial_ends_at);
  alert(`${endsAt.toLocaleDateString()}까지 무료 체험이 시작되었습니다!`);
  // 구독 상태 재조회 → UI 갱신
} else {
  const err = await res.json();
  showError(err.detail);
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 위 "사용 가능 조건" 위반 / 코드 검증 실패 / `code` 누락 |
| 401 | 인증 실패 |
        """,
        request=ReferralCodeRedeemRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="트라이얼 시작 완료",
                examples=[
                    OpenApiExample(
                        "성공",
                        value={
                            "detail": "트라이얼이 시작되었습니다.",
                            "redemption": {
                                "id": "f1e2d3c4-0000-0000-0000-000000000001",
                                "referral_code_value": "WELCOME2026",
                                "plan": {
                                    "id": "550e8400-e29b-41d4-a716-446655440002",
                                    "name": "pro",
                                    "display_name": "프로",
                                    "monthly_price": 9900,
                                    "features": {"max_pages": 5, "ai_generation": True},
                                    "sort_order": 1,
                                },
                                "trial_started_at": "2026-04-27T12:00:00Z",
                                "trial_ends_at": "2026-05-27T12:00:00Z",
                                "is_trial_active": True,
                                "converted_to_paid": False,
                                "converted_at": None,
                                "created_at": "2026-04-27T12:00:00Z",
                            },
                            "subscription": {
                                "id": "a1b2c3d4-0000-0000-0000-000000000002",
                                "plan": {"name": "pro", "display_name": "프로"},
                                "status": "trialing",
                                "current_period_start": "2026-04-27T12:00:00Z",
                                "current_period_end": "2026-05-27T12:00:00Z",
                            },
                        },
                    ),
                ],
            ),
            400: OpenApiResponse(
                description="사용 불가",
                examples=[
                    OpenApiExample(
                        "이미 사용함",
                        value={"detail": "이미 레퍼럴 코드를 사용하셨습니다."},
                    ),
                    OpenApiExample(
                        "유료 사용자",
                        value={"detail": "이미 유료 플랜을 사용 중입니다."},
                    ),
                    OpenApiExample(
                        "트라이얼 중",
                        value={"detail": "이미 트라이얼이 진행 중입니다."},
                    ),
                    OpenApiExample(
                        "코드 미존재",
                        value={"detail": "존재하지 않는 코드입니다."},
                    ),
                    OpenApiExample(
                        "기간 만료",
                        value={"detail": "유효 기간이 만료된 코드입니다."},
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
        },
    )
    def post(self, request):
        serializer = ReferralCodeRedeemRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code_str = _normalize_code(serializer.validated_data["code"])

        if not code_str:
            return Response(
                {"detail": "코드를 입력해주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 이미 레퍼럴을 사용했는지 (1유저 1회)
        if ReferralRedemption.objects.filter(user=request.user).exists():
            return Response(
                {"detail": "이미 레퍼럴 코드를 사용하셨습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 트랜잭션 + 코드 row 락
        try:
            with transaction.atomic():
                try:
                    code = (
                        ReferralCode.objects.select_for_update()
                        .select_related("target_plan")
                        .get(code=code_str)
                    )
                except ReferralCode.DoesNotExist:
                    return Response(
                        {"detail": "존재하지 않는 코드입니다."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                ok, reason = code.is_redeemable()
                if not ok:
                    return Response(
                        {"detail": reason},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                sub = (
                    UserSubscription.objects.select_for_update()
                    .select_related("plan")
                    .get(pk=ensure_subscription(request.user).pk)
                )

                # 이미 유료 플랜이거나 트라이얼 중이면 거부
                if sub.status == SubscriptionStatus.TRIALING:
                    return Response(
                        {"detail": "이미 트라이얼이 진행 중입니다."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if sub.is_paid_plan:
                    return Response(
                        {"detail": "이미 유료 플랜을 사용 중입니다."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                now = timezone.now()
                trial_ends = now + timedelta(days=code.trial_days)

                # 구독 갱신 — pro_activated_at은 결제 시점에만 설정하므로 건드리지 않음
                sub.plan = code.target_plan
                sub.status = SubscriptionStatus.TRIALING
                sub.current_period_start = now
                sub.current_period_end = trial_ends
                sub.cancelled_at = None
                sub.save(
                    update_fields=[
                        "plan",
                        "status",
                        "current_period_start",
                        "current_period_end",
                        "cancelled_at",
                        "updated_at",
                    ]
                )

                # 페이지 전체 활성화 (유료 동등)
                from apps.pages.models import Page

                Page.objects.filter(user=request.user, is_active=False).update(
                    is_active=True
                )

                # AI 토큰 지급
                monthly_tokens = code.target_plan.features.get("monthly_ai_tokens", 0)
                if monthly_tokens > 0:
                    balance = AiTokenBalance.get_or_create_for_user(request.user)
                    balance.grant(
                        monthly_tokens,
                        description=f"레퍼럴 트라이얼 토큰 지급 ({code.code})",
                    )

                # 사용 횟수 증가
                ReferralCode.objects.filter(pk=code.pk).update(
                    current_uses=F("current_uses") + 1,
                    updated_at=now,
                )

                redemption = ReferralRedemption.objects.create(
                    user=request.user,
                    referral_code=code,
                    trial_started_at=now,
                    trial_ends_at=trial_ends,
                )
        except Exception:
            logger.exception(
                "레퍼럴 코드 사용 처리 오류: user=%s code=%s",
                request.user.email,
                code_str,
            )
            raise

        logger.info(
            "레퍼럴 트라이얼 시작: user=%s code=%s plan=%s ends=%s",
            request.user.email,
            code.code,
            code.target_plan.name,
            trial_ends.isoformat(),
        )

        # 갱신된 인스턴스로 응답 (related 필드 보장)
        sub.refresh_from_db()
        return Response(
            {
                "detail": "트라이얼이 시작되었습니다.",
                "redemption": ReferralRedemptionSerializer(redemption).data,
                "subscription": UserSubscriptionSerializer(sub).data,
            }
        )


# ──────────────────────────────────────────────
# 3) 내 레퍼럴 사용 이력
# ──────────────────────────────────────────────


class MyReferralRedemptionView(APIView):
    """내 레퍼럴 사용 이력 조회"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["레퍼럴"],
        summary="내 레퍼럴 사용 이력 조회",
        description="""
## 목적
현재 사용자의 **레퍼럴 사용 여부**와 트라이얼 상태를 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 사용 시나리오
- 설정 페이지에서 "레퍼럴 코드 적용됨" 표시
- 트라이얼 잔여일 표시
- 레퍼럴 입력 UI 노출 여부 결정 (이미 사용했으면 숨김)
- 트라이얼 종료 임박 시 결제 유도 배너 표시

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `redeemed` | bool | 레퍼럴을 사용한 적이 있는지 |
| `redemption` | object | 사용 이력 (redeemed=true일 때) |
| `redemption.referral_code_value` | string | 사용한 코드 문자열 |
| `redemption.plan` | object | 트라이얼로 받은 플랜 |
| `redemption.trial_started_at` | datetime | 트라이얼 시작 시각 |
| `redemption.trial_ends_at` | datetime | 트라이얼 종료 시각 |
| `redemption.is_trial_active` | bool | 현재 트라이얼이 유효한지 (종료 전 + 미전환) |
| `redemption.converted_to_paid` | bool | 트라이얼 후 유료 결제로 전환했는지 |
| `redemption.converted_at` | datetime | 유료 전환 시각 |

## 프론트엔드 통합
```typescript
const res = await fetch('/api/v1/billing/referral/my-status/', {
  headers: { 'Authorization': `Bearer ${accessToken}` },
});
const data = await res.json();

if (!data.redeemed) {
  showReferralInputForm();
} else if (data.redemption.is_trial_active) {
  const endsAt = new Date(data.redemption.trial_ends_at);
  const daysLeft = Math.ceil((endsAt.getTime() - Date.now()) / 86_400_000);
  showTrialBanner(`무료 체험 ${daysLeft}일 남음`);
} else if (data.redemption.converted_to_paid) {
  // 정상 유료 사용자 — 별도 안내 불필요
} else {
  // 트라이얼 종료, 미전환 → free로 다운그레이드된 상태
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 인증 실패 |
        """,
        responses={
            200: OpenApiResponse(
                description="레퍼럴 사용 이력",
                examples=[
                    OpenApiExample(
                        "사용 안 함",
                        value={"redeemed": False},
                    ),
                    OpenApiExample(
                        "트라이얼 진행 중",
                        value={
                            "redeemed": True,
                            "redemption": {
                                "id": "f1e2d3c4-0000-0000-0000-000000000001",
                                "referral_code_value": "WELCOME2026",
                                "plan": {
                                    "id": "550e8400-...",
                                    "name": "pro",
                                    "display_name": "프로",
                                },
                                "trial_started_at": "2026-04-27T12:00:00Z",
                                "trial_ends_at": "2026-05-27T12:00:00Z",
                                "is_trial_active": True,
                                "converted_to_paid": False,
                                "converted_at": None,
                                "created_at": "2026-04-27T12:00:00Z",
                            },
                        },
                    ),
                    OpenApiExample(
                        "트라이얼 후 유료 전환",
                        value={
                            "redeemed": True,
                            "redemption": {
                                "id": "f1e2d3c4-0000-0000-0000-000000000001",
                                "referral_code_value": "WELCOME2026",
                                "plan": {"name": "pro", "display_name": "프로"},
                                "trial_started_at": "2026-04-27T12:00:00Z",
                                "trial_ends_at": "2026-05-27T12:00:00Z",
                                "is_trial_active": False,
                                "converted_to_paid": True,
                                "converted_at": "2026-05-20T09:30:00Z",
                                "created_at": "2026-04-27T12:00:00Z",
                            },
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        try:
            redemption = ReferralRedemption.objects.select_related(
                "referral_code", "referral_code__target_plan"
            ).get(user=request.user)
        except ReferralRedemption.DoesNotExist:
            return Response({"redeemed": False})

        return Response(
            {
                "redeemed": True,
                "redemption": ReferralRedemptionSerializer(redemption).data,
            }
        )
