"""
Referral API views — 쿠폰(제휴/레퍼럴 코드) 검증 및 사용 이력.

1. ValidateReferralCodeView   — 코드 사전 검증 + **결제 전 미리보기** (인증 불필요)
2. RedeemReferralCodeView     — **체험 중 기간 연장 전용**. 그 외는 여전히 400
3. MyReferralRedemptionView   — 내 레퍼럴 사용 이력 조회

⚠️ 쿠폰으로 트라이얼을 **시작**하는 경로는 여전히 단 하나다 —
``POST /billing/toss/confirm/`` 에 ``referral_code`` 동봉
(:func:`apps.billing.toss_flows.confirm_billing`, ``scenario="trial"``).
여기에 "시작" 경로를 다시 만들지 말 것: 과거 이 파일의 redeem 이 기본 체험 30일을
빼먹어 "30일 + 14일" 쿠폰이 14일로 나갔다(2026-08-04 규명).

⭐ 2026-09-12 부분 부활 — **연장만**. '카드 없는 프로 30일'이 켜지면 가입 직후 이미
   ``TRIALING`` 이라 confirm 의 ``scenario="trial"`` 에 영영 도달하지 못한다. 그대로 두면
   제휴 코드가 항상 400 이 되어 44일 쿠폰이 통째로 죽는다. 그래서 이 뷰는 **이미 체험
   중인 사용자의 남은 기간에 보너스 일수를 더하는 일만** 한다
   (:func:`apps.billing.toss_flows.extend_trial_with_referral`).
   폐지 사유였던 결함이 여기서는 구조적으로 재발하지 않는다 — **base 30일은 이미 부여돼
   있고**, ``trial_used_at`` 도 이미 찍혀 있다(재체험 우회 구멍 없음).
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
    """제휴/레퍼럴 코드로 **진행 중인 무료 체험을 연장**한다 (카드 불필요).

    ⚠️ 이 뷰는 트라이얼을 **시작하지 않는다**. 2026-08-04 까지 그 일을 했었고,
    ``code.trial_days`` 만 부여하고 **기본 체험 30일(TRIAL_BASE_DAYS)을 가산하지 않아**
    14일 쿠폰 사용자가 44일 대신 14일만 받는 결함이 실서비스에서 발생했다
    (HLEVEL26 17건 중 3건 피해). 그래서 시작 경로는 영구 폐지다.

    2026-09-12 '카드 없는 프로 30일' 도입으로 **연장 경로만** 되살렸다. 자동 지급이
    가입 직후 ``TRIALING`` 을 만들기 때문에 ``toss/confirm`` 의 ``scenario="trial"`` 에
    도달할 수 없고, 그대로 두면 제휴 코드가 항상 400 이 된다.
    폐지 사유였던 두 결함이 여기서는 재발할 수 없다:
      - base 30일 누락 → **이미 부여된 기간에 더하기만** 한다
      - ``trial_used_at`` 미기록으로 인한 재체험 우회 → 자동 지급이 이미 찍어 뒀다
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["레퍼럴"],
        summary="제휴 코드로 무료 체험 연장",
        description="""
## 개요
**이미 무료 체험 중인 사용자**가 제휴/레퍼럴 코드를 입력해 남은 체험 기간에
보너스 일수를 더합니다. 카드 등록이 필요 없습니다.

예: 가입 시 자동 지급된 프로 30일을 쓰는 중에 14일짜리 코드를 입력 →
**남은 기간 끝에 14일이 이어 붙어 총 44일**이 됩니다.

## 사용 시나리오
- 가입 직후 카드 없는 프로 30일이 켜진 사용자가 제휴 코드를 뒤늦게 입력할 때
- 제휴 파트너 링크로 들어왔으나 코드 입력 화면을 가입 이후에 만나는 경우

체험을 **시작**할 때 코드를 함께 쓰려면 이 엔드포인트가 아니라
`POST /billing/toss/confirm/` 에 `referral_code` 를 동봉하세요(카드 등록 경로).

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
1. 현재 구독이 `trialing` 이 아니면 400 `REFERRAL_NOT_TRIALING`
2. 코드가 없거나 소진/비활성/만료면 400 (사유는 `detail`)
3. 이미 제휴 코드를 쓴 적이 있으면 400 (1인 1회)
4. 코드의 대상 플랜이 현재 체험 플랜과 다르면 400 `REFERRAL_PLAN_MISMATCH`
5. 통과 → `current_period_end += code.trial_days`, 코드 사용 횟수 +1, 사용 이력 기록

**기간은 "지금부터 N일"이 아니라 "남은 체험 끝 + N일"** 입니다 — 다시 잡으면 남은
기간을 빼앗게 됩니다.

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `code` | ✅ | string | 제휴/레퍼럴 코드 (대소문자 무시) |

## 주의사항
- 체험이 **끝난 뒤**에는 쓸 수 없습니다(400). 만료 전에 입력해야 합니다.
- 코드는 1인 1회입니다. 두 번째 코드는 400.
- 연장 후 표기는 `GET /billing/my-subscription/` 의 `trial_last_day` 를 쓰세요
  (`trial_ends_at` 을 날짜로 찍으면 하루 더 써도 되는 것처럼 보입니다).

## 사용 예시
```bash
curl -X POST https://api.turnflow.link/api/v1/billing/referral/redeem/ \
  -H "Authorization: Bearer $ACCESS" \
  -H "Content-Type: application/json" \
  -d '{"code": "HLEVEL26"}'
```
```json
{
  "success": true,
  "referral_code": "HLEVEL26",
  "bonus_days": 14,
  "trial_ends_at": "2026-10-26T05:12:00Z",
  "trial_last_day": "2026-10-25",
  "total_trial_days": 44,
  "detail": "제휴 코드가 적용되어 무료 체험이 14일 연장되었습니다."
}
```
        """,
        request=ReferralCodeRedeemRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="연장 완료",
                examples=[
                    OpenApiExample(
                        "연장 성공",
                        value={
                            "success": True,
                            "referral_code": "HLEVEL26",
                            "bonus_days": 14,
                            "trial_ends_at": "2026-10-26T05:12:00Z",
                            "trial_last_day": "2026-10-25",
                            "total_trial_days": 44,
                            "detail": "제휴 코드가 적용되어 무료 체험이 14일 연장되었습니다.",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="체험 중이 아니거나 코드가 유효하지 않음",
                examples=[
                    OpenApiExample(
                        "체험 중이 아님",
                        value={
                            "detail": "무료 체험 중에만 제휴 코드로 기간을 연장할 수 있습니다.",
                            "code": "REFERRAL_NOT_TRIALING",
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "무료 체험 중에만 제휴 코드로 기간을 연장할 수 있습니다.",
                                "details": {"code": "REFERRAL_NOT_TRIALING"},
                            },
                        },
                    ),
                    OpenApiExample(
                        "이미 사용함",
                        value={
                            "detail": "이미 제휴/레퍼럴 코드를 사용하셨습니다.",
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "이미 제휴/레퍼럴 코드를 사용하셨습니다.",
                                "details": {},
                            },
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음 — 없는 코드는 400 으로 응답합니다"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def post(self, request):
        from django.db import transaction as _tx

        from .toss_flows import BillingFlowError, extend_trial_with_referral

        serializer = ReferralCodeRedeemRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code_str = _normalize_code(serializer.validated_data["code"])

        try:
            with _tx.atomic():
                result = extend_trial_with_referral(request.user, code_str)
        except BillingFlowError as exc:
            payload = {"detail": exc.detail}
            machine_code = exc.extra.get("code")
            if machine_code:
                payload["code"] = machine_code
            # §6 통일 포맷을 **함께** 실어 보낸다 — 이 뷰는 DRF 예외 핸들러를 우회하므로
            # detail 만 내면 한 URL 이 두 포맷을 내게 된다(toss_views 와 같은 함정).
            payload["success"] = False
            payload["error"] = {
                "code": exc.status_code,
                "message": exc.detail,
                "details": {"code": machine_code} if machine_code else {},
            }
            return Response(payload, status=exc.status_code)

        sub = request.user.subscription
        sub.refresh_from_db()
        logger.info(
            "제휴 코드 체험 연장: user=%s code=%s +%s일",
            request.user.email,
            result["referral_code"],
            result["bonus_days"],
        )
        return Response(
            {
                "success": True,
                "referral_code": result["referral_code"],
                "bonus_days": result["bonus_days"],
                "trial_ends_at": result["trial_ends_at"],
                "trial_last_day": sub.trial_last_day,
                "total_trial_days": result["total_trial_days"],
                "detail": (
                    f"제휴 코드가 적용되어 무료 체험이 {result['bonus_days']}일 연장되었습니다."
                ),
            },
            status=status.HTTP_200_OK,
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
