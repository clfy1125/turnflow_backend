"""카드 없는 프로 30일 자동 지급 API — ``POST /api/v1/billing/trial/auto-grant/``.

판정·실행은 :mod:`apps.billing.auto_trial` 단일 소스가 한다. 이 파일은 HTTP 껍데기와
커밋 후 부수효과(CAPI)만 담당한다.
"""

from __future__ import annotations

import logging

from django.db import transaction
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import auto_trial
from .models import UserSubscription
from .serializers import UserSubscriptionSerializer
from .subscription_utils import ensure_subscription

logger = logging.getLogger(__name__)


class AutoTrialGrantRequestSerializer(serializers.Serializer):
    """지급 요청 — 두 필드 모두 **로그 용도**이며 자격 판정에 쓰이지 않는다."""

    source = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=40,
        help_text="호출 지점 (예: signup, pro_trial_popup, top_bar). 자격 판정에 영향 없음",
    )
    provider = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=20,
        help_text="가입 수단 (email | google | kakao | instagram). 자격 판정에 영향 없음",
    )


class AutoTrialGrantView(APIView):
    """카드 없는 프로 30일 자동 지급 — **멱등**.

    프론트가 가입 직후 1회, 그리고 프로 체험 팝업의 CTA 에서 호출한다. 이미 받았거나
    자격이 없으면 ``granted=false`` + ``reason`` 으로 **200** 을 준다(에러가 아니다) —
    프론트가 조용히 넘어갈 수 있어야 하고, 409 로 내리면 호출부마다 예외 처리가 늘어난다.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["billing"],
        summary="카드 없는 프로 30일 자동 지급",
        description="""
## 개요
로그인한 사용자에게 **카드 등록 없이 프로 플랜 30일**을 지급합니다.
만료되면 **결제 없이 무료 플랜으로 자동 복귀**합니다(자동 유료 전환 없음).

2026-09-10 내부 회의에서 확정된 전환 개선 정책입니다 — "맨 처음부터 카드 등록을
요구하면 심리적 장벽이 크다. 먼저 쓰게 하고, 30일 안에 만든 캠페인을 옮기기 싫어서
남게 한다."

## 사용 시나리오
1. 가입 완료 직후 프론트가 1회 호출 (`source: "signup"`)
2. 프로 체험 팝업/상단 바의 "30일 무료로 시작하기" CTA (`source: "pro_trial_popup"`)
3. 이미 받은 사용자가 다시 눌러도 안전합니다 — `granted: false, reason: "trial_used"`

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
지급 조건(**전부 만족해야** 지급):
- 서버 킬스위치 `AUTO_PRO_TRIAL_ENABLED` 가 켜져 있음
- 체험을 쓴 적이 없음 (`trial_used_at is null` — 카드 체험·쿠폰 체험 포함 **1인 1회**)
- 결제 카드(빌링키)가 등록돼 있지 않음
- 현재 무료 플랜이며 상태가 `active` (유료·체험·정지·해지예약·미납이 아님)
- 탈퇴 유예 중이 아님

지급되면 구독이 이렇게 바뀝니다 — **카드 등록 체험과 같은 모양**입니다:
`status=trialing`, `plan=pro`, `has_billing_key=false`,
`current_period_end = 지금 + 30일`, `trial_used_at = 지금`, `trial_kind="auto"`.

체험 **중에** 카드를 등록하면(`POST /billing/toss/confirm/`) 시나리오는 `attach_only` 로
잡혀 **기간이 그대로 유지되고** 만료일에 첫 결제가 나갑니다.
체험이 **끝난 뒤** 카드를 등록하면 `charge_now`(즉시 결제)입니다 — 이미 30일을 썼기 때문입니다.

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `source` | 선택 | string(≤40) | 호출 지점. 로그·분석용, 판정에 영향 없음 |
| `provider` | 선택 | string(≤20) | 가입 수단(`email`/`google`/`kakao`/`instagram`). 로그용 |

## 응답
지급 성공/실패 모두 **200** 입니다.

| 필드 | 타입 | 설명 |
|------|------|------|
| `granted` | bool | 이번 호출로 체험이 켜졌는가 |
| `reason` | string\\|null | `granted=false` 일 때의 사유 |
| `subscription` | object | 현재 구독 전체 (`GET /billing/my-subscription/` 과 동일 스키마) |

`reason` 값: `disabled`(서버 스위치 OFF) · `trial_used`(1인 1회 소진) ·
`has_billing_key`(카드 이미 등록) · `already_pro`(무료·active 가 아님) ·
`not_new_user`(가입 후 허용 창 초과 — 창이 설정된 경우만) ·
`not_ad_attributed`(광고 귀속 아님 — 그 판정을 켠 경우만) ·
`account_pending_deletion` · `plan_unavailable`(운영 사고).

## 주의사항
- **표기는 `subscription.trial_last_day`(KST 날짜)를 쓰세요.** `current_period_end` 를
  날짜로 찍으면 하루 더 써도 되는 것처럼 보입니다.
- 이 엔드포인트는 결제를 일으키지 않으므로 결제 전 고지·동의(`POST /billing/consents/`)
  대상이 아닙니다. 동의는 나중에 **카드를 등록하는 시점**에 받습니다.
- 멱등합니다. 네트워크 재시도로 두 번 호출돼도 기간이 60일이 되지 않습니다.

## 사용 예시
```javascript
const res = await fetch('/api/v1/billing/trial/auto-grant/', {
  method: 'POST',
  headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ source: 'signup', provider: 'kakao' }),
});
const data = await res.json();
if (data.granted) {
  // 홈에 "프로 30일이 켜져 있어요 · {trial_last_day}까지" 상태 카드 표시
  showTrialCard(data.subscription.trial_last_day);
}
```
        """,
        request=AutoTrialGrantRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=UserSubscriptionSerializer,
                description="지급됨 또는 지급 불가(둘 다 200)",
                examples=[
                    OpenApiExample(
                        "지급됨",
                        value={
                            "granted": True,
                            "reason": None,
                            "subscription": {
                                "status": "trialing",
                                "plan": {"name": "pro", "display_name": "프로"},
                                "has_billing_key": False,
                                "current_period_end": "2026-10-12T07:30:00Z",
                                "trial_last_day": "2026-10-11",
                                "trial_kind": "auto",
                                "trial_used_at": "2026-09-12T07:30:00Z",
                                "trial_total_days": 30,
                            },
                        },
                    ),
                    OpenApiExample(
                        "이미 체험을 썼음",
                        value={
                            "granted": False,
                            "reason": "trial_used",
                            "subscription": {
                                "status": "active",
                                "plan": {"name": "free", "display_name": "무료"},
                                "has_billing_key": False,
                                "trial_last_day": None,
                                "trial_kind": "auto",
                            },
                        },
                    ),
                    OpenApiExample(
                        "서버 스위치 OFF",
                        value={"granted": False, "reason": "disabled", "subscription": {}},
                    ),
                ],
            ),
            400: OpenApiResponse(description="요청 바디 검증 실패 (source/provider 길이 초과 등)"),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def post(self, request):
        serializer = AutoTrialGrantRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = serializer.validated_data.get("source", "")
        provider = serializer.validated_data.get("provider", "")

        sub, reason = auto_trial.grant(request.user, source=source, provider=provider)

        if sub is None:
            # ⚠️ `ensure_subscription(request.user)` 이 돌려주는 것은 요청 시작 시점에
            #    **캐시된** 관련 객체일 수 있다(`user.subscription`). 그 값을 그대로 응답에
            #    담으면 방금 지급된 체험이 "무료 플랜 · 종료일 없음" 으로 보인다 —
            #    프론트가 이 응답으로 구독 상태를 갱신하므로 화면이 통째로 틀어진다.
            #    DB 에서 다시 읽는다.
            current = UserSubscription.objects.select_related("plan").get(
                pk=ensure_subscription(request.user).pk
            )
            return Response(
                {
                    "granted": False,
                    "reason": reason,
                    "subscription": UserSubscriptionSerializer(current).data,
                },
                status=status.HTTP_200_OK,
            )

        # Meta 전환 API — StartTrial(trial_kind="auto"). 커밋 후에 던진다(워커가 옛 행을
        # 읽지 않도록). 실패해도 체험은 이미 켜졌다 — 예외를 밖으로 던지지 않는다.
        def _fire():
            from apps.analytics.conversions import track_trial_started

            track_trial_started(sub, request=request)

        try:
            transaction.on_commit(_fire)
        except Exception:  # noqa: BLE001
            logger.exception("auto_trial: CAPI 예약 실패 user=%s", request.user.id)

        return Response(
            {
                "granted": True,
                "reason": None,
                "subscription": UserSubscriptionSerializer(sub).data,
            },
            status=status.HTTP_200_OK,
        )
