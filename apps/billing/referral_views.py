"""
Referral API views — 쿠폰(제휴/레퍼럴 코드) 검증 및 사용 이력.

1. ValidateReferralCodeView   — 코드 사전 검증 + **결제 전 미리보기** (인증 불필요)
2. RedeemReferralCodeView     — **폐지**(항상 400). 쿠폰은 카드 등록 경로에서만 사용
3. MyReferralRedemptionView   — 내 레퍼럴 사용 이력 조회

⚠️ 쿠폰으로 트라이얼을 시작하는 경로는 **단 하나**다 —
``POST /billing/toss/confirm/`` 에 ``referral_code`` 동봉
(:func:`apps.billing.toss_flows.confirm_billing`, ``scenario="trial"``).
여기에 두 번째 경로를 만들지 말 것: 과거 이 파일의 redeem 이 기본 체험 30일을
빼먹어 "30일 + 14일" 쿠폰이 14일로 나갔다(2026-08-04 규명).
"""

import logging
from datetime import timedelta

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ReferralCode, ReferralRedemption
from .serializers import (
    ReferralCodeRedeemRequestSerializer,
    ReferralCodeValidateResponseSerializer,
    ReferralRedemptionSerializer,
    SubscriptionPlanSerializer,
)

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
| `trial_days` | int | 코드가 추가로 주는 **보너스** 일수 (valid=true) |
| `base_trial_days` | int | 카드 등록 시 기본 무료 일수 (코드 없이도 프로 최초 구독이 받는 값, 보통 30) |
| `total_trial_days` | int | **카드 등록 시 이 코드로 받는 총 무료 일수** = `base_trial_days + trial_days` |
| `plan` | object | 트라이얼로 부여될 플랜 정보 (valid=true) |

> 💡 **표기 주의**: 카드 등록(`POST /billing/toss/confirm/` 에 `referral_code` 동봉) 흐름에서는
> "원래 1개월 무료 → 코드 적용 시 **N개월 무료**" 를 보여줄 때 `total_trial_days` 를 사용하세요
> (예: base 30 + 보너스 30 = **60일 = 2개월 무료**). 반면 카드 없이 `POST /billing/referral/redeem/`
> 로 사용하면 base 없이 `trial_days` 만 적용됩니다.

## 프론트엔드 통합
```typescript
const res = await fetch(
  `/api/v1/billing/referral/validate/?code=${encodeURIComponent(code)}`
);
const data = await res.json();

if (data.valid) {
  // 카드 등록(체험 시작) 화면: 총 무료 기간을 개월로 환산해 노출
  const months = Math.round(data.total_trial_days / 30);
  showHint(`${data.plan.display_name} ${months}개월 무료 체험! (${data.total_trial_days}일)`);
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
                        "사용 가능 (카드 등록 시 2개월 무료)",
                        value={
                            "valid": True,
                            "trial_days": 30,
                            "base_trial_days": 30,
                            "total_trial_days": 60,
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

        # 총 무료 일수 = 기본 체험 + 코드 보너스. 쿠폰은 카드 등록 경로에서만 쓰이므로
        # 이 값이 유일한 정답이다 (카드 없는 redeem 경로는 폐지 — 그 경로가 base 를
        # 빼먹어 "30일 + 14일" 이 14일로 나가던 결함의 원인이었다).
        from .models import EXTRA_IG_ACCOUNT_PRICE
        from .toss_flows import TRIAL_BASE_DAYS, get_current_selling_price

        total_days = TRIAL_BASE_DAYS + code.trial_days
        # 미리보기 추정치 — 실제 확정은 confirm 시점의 now 기준
        first_charge_at = timezone.now() + timedelta(days=total_days)

        return Response(
            {
                "valid": True,
                "trial_days": code.trial_days,
                "base_trial_days": TRIAL_BASE_DAYS,
                "total_trial_days": total_days,
                "plan": SubscriptionPlanSerializer(code.target_plan).data,
                # 결제 전 미리보기 — 프론트가 "쿠폰 적용하고 결제하면 이렇게 됩니다" 를
                # 카드 입력 **전에** 보여줄 수 있도록 서버가 계산해서 내려준다.
                "requires_card": True,
                "trial_ends_at": first_charge_at,
                "first_charge_at": first_charge_at,
                "first_charge_amount": get_current_selling_price(code.target_plan),
                "extra_ig_account_price": EXTRA_IG_ACCOUNT_PRICE,
            }
        )


# ──────────────────────────────────────────────
# 2) 레퍼럴 코드 사용 (트라이얼 시작)
# ──────────────────────────────────────────────


class RedeemReferralCodeView(APIView):
    """폐지됨 — 쿠폰은 카드 등록(toss confirm) 경로에서만 사용한다.

    ⚠️ 이 경로는 ``code.trial_days`` 만 부여하고 **기본 체험 30일(TRIAL_BASE_DAYS)을
    가산하지 않았다**. 그래서 14일 쿠폰 사용자가 "30일 + 14일 = 44일" 대신 **14일만**
    받는 결함이 실서비스에서 발생했다(2026-08-04 규명, HLEVEL26 17건 중 3건 피해).

    같은 쿠폰을 카드 등록에 동봉한 :func:`apps.billing.toss_flows.confirm_billing`
    (``scenario="trial"``) 은 ``TRIAL_BASE_DAYS + bonus_days`` 로 44일을 정확히 줬다.
    두 경로가 서로 다른 값을 주는 게 근본 원인이었으므로, 경로를 하나로 없앤다.

    부수적으로 막히는 것: 이 경로는 ``trial_used_at`` 을 채우지 않아, 체험이 만료돼
    free 로 강등된 뒤 카드를 등록하면 ``scenario="trial"`` 로 재판정돼 **30일 무료
    체험이 한 번 더** 나갔다(1인 1회 원칙 우회). 경로 폐지로 이 구멍도 닫힌다.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["레퍼럴"],
        summary="[폐지] 카드 없이 쿠폰 사용",
        deprecated=True,
        description="""
## ⛔ 폐지된 엔드포인트 — 항상 400 을 반환합니다

이 경로는 **더 이상 트라이얼을 시작하지 않습니다.** 쿠폰(제휴/레퍼럴 코드)은
**카드 등록과 함께** 사용하세요 → `POST /billing/toss/confirm/` 에 `referral_code` 동봉.

## 왜 폐지했는가

이 경로는 `code.trial_days` 만 부여하고 **기본 무료 체험 30일을 가산하지 않았습니다.**
그래서 14일 쿠폰 사용자가 `30일 + 14일 = 44일` 이 아니라 **14일만** 받았습니다.
같은 쿠폰을 카드 등록에 동봉하면 정상적으로 44일이 부여됩니다. 두 경로가 서로 다른
값을 주는 것이 결함의 근본 원인이었으므로, 경로를 하나로 통일했습니다.

## 프론트엔드가 해야 할 일

1. 쿠폰 입력 → `GET /billing/referral/validate/?code=XXX` 로 검증 **및 미리보기 정보 획득**
   - `total_trial_days` — 총 무료 일수 (예: 44). **`trial_days`(=14, 보너스분)를 그대로
     노출하면 안 됩니다.**
   - `first_charge_at` — 첫 결제 예정 시각 (= 무료 체험 종료 시각)
   - `first_charge_amount` — 첫 결제 예정 금액(원)
2. "쿠폰 적용 시 44일 무료, 2026-09-17에 14,900원 첫 결제" 를 **카드 입력 전에** 안내
3. 카드 등록 시 `POST /billing/toss/confirm/` 에 `referral_code` 를 **함께** 전송

## 응답

항상 `400` + `code: "REFERRAL_REQUIRES_CARD"`.
`detail` 은 사용자에게 그대로 보여줄 수 있는 한국어 문장입니다.
        """,
        request=ReferralCodeRedeemRequestSerializer,
        responses={
            400: OpenApiResponse(
                description="폐지됨 — 카드 등록 경로를 사용해야 함",
                examples=[
                    OpenApiExample(
                        "폐지 안내",
                        value={
                            "detail": (
                                "쿠폰은 카드 등록과 함께 사용해야 합니다. "
                                "결제 수단을 등록하면 무료 체험이 시작됩니다."
                            ),
                            "code": "REFERRAL_REQUIRES_CARD",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
        },
    )
    def post(self, request):
        """항상 400 — 쿠폰은 카드 등록 경로(toss/confirm)로만 사용한다.

        구독 상태를 **전혀 건드리지 않는다**(읽기조차 하지 않는다). 이 뷰가 하던
        트라이얼 시작 로직은 통째로 제거됐다 — 살려두면 base 30일을 빼먹는 두 번째
        경로가 다시 생긴다.
        """
        logger.info(
            "폐지된 카드없는 쿠폰 경로 호출 차단: user=%s",
            request.user.email,
        )
        return Response(
            {
                "detail": (
                    "쿠폰은 카드 등록과 함께 사용해야 합니다. "
                    "결제 수단을 등록하면 무료 체험이 시작됩니다."
                ),
                "code": "REFERRAL_REQUIRES_CARD",
            },
            status=status.HTTP_400_BAD_REQUEST,
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
