"""플랜별 요율을 적용하는 스로틀 (감사 H-8 대응).

DRF 의 ``ScopedRateThrottle`` 은 scope 하나에 요율 하나라 두 가지를 못 한다:
"무료는 조이고 유료는 넉넉하게" 와 "시간당 + 하루" 두 창을 동시에 거는 것.
이 모듈이 그 둘을 더한다.

1. **플랜 티어별 요율** — 요청 시점에 ``get_effective_plan`` 으로 free/paid 를 판정해
   ``<base>_<tier>_<window>`` scope 를 고른다. 어뷰즈는 무료 가입으로 오므로 무료만
   조이면 되고, 돈을 내는 고객에게는 사실상 만날 일 없는 상한을 준다.
   (2026-08-12 실측: 가장 헤비한 고객이 하루 2.3건. 유료 상한 400/일 = 약 170배 여유)
2. **두 개의 창** — 시간당·하루를 별도 클래스로 걸어 버스트와 총량을 함께 막는다.

## 왜 NUM_PROXIES 와 무관한가

DRF 는 **인증된 요청을 IP 가 아니라 ``user.pk`` 로 키잉**한다. 여기 클래스들이 붙는
AI 엔드포인트는 전부 ``IsAuthenticated`` 이므로, ``X-Forwarded-For`` 가 프록시 뒤에서
희석되는 문제(``NUM_PROXIES`` 미설정)의 영향을 받지 않는다. 익명 요청은 IP 로 떨어지지만
그건 이 스로틀의 대상이 아니다.

## ⚠️ 전역 스로틀로 쓰지 말 것

IG/토스 웹훅은 ``AllowAny`` 라 전역 스로틀이 걸리면 Meta 버스트가 429 를 받고,
**Meta 는 반복 실패 시 웹훅 구독을 auto-disable** 한다(과거 실제 발생 → 댓글 무음).
반드시 대상 뷰에만 명시적으로 붙인다.

## fail-open 이다

settings 에 해당 scope 의 요율이 없으면 **제한 없이 통과**시킨다. 설정 누락이 기능 정지로
이어지지 않게 하려는 의도이지만, 뒤집어 말하면 **요율을 지우면 스로틀이 조용히 꺼진다**.
요율 키 이름을 바꿀 때는 ``config/settings/base.py`` 의 ``DEFAULT_THROTTLE_RATES`` 와
여기 ``scope_base``/``window`` 조합이 맞는지 확인할 것.
"""

from __future__ import annotations

import logging

from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle

logger = logging.getLogger(__name__)

#: 유료로 간주하는 SubscriptionPlan.name (admin = 운영용 무제한 플랜)
PAID_PLAN_NAMES = frozenset({"basic", "pro", "admin"})


def resolve_plan_tier(user) -> str:
    """요청자의 요율 티어를 ``"paid"`` / ``"free"`` 로 판정한다.

    판정 실패(구독 조회 오류 등)는 **보수적으로 ``"free"``**. 유료 고객이 잠깐 무료 요율로
    떨어져도 무료 상한(40/일)이 실사용(2.3/일)의 17배라 실제로 막힐 일이 없어서,
    "실패 시 무제한 통과"보다 이쪽이 안전하다.
    """
    try:
        from apps.billing.subscription_utils import get_effective_plan

        plan = get_effective_plan(user)
        name = (getattr(plan, "name", "") or "").strip().lower()
        return "paid" if name in PAID_PLAN_NAMES else "free"
    except Exception:  # 구독 조회 실패가 API 를 죽여선 안 된다
        logger.warning(
            "throttle: 플랜 판정 실패 user=%s → free 요율 적용",
            getattr(user, "pk", None),
            exc_info=True,
        )
        return "free"


class PlanScopedRateThrottle(SimpleRateThrottle):
    """``<scope_base>_<tier>_<window>`` scope 로 요율을 고르는 스로틀 베이스.

    서브클래스는 ``scope_base`` 와 ``window`` 만 정의하면 된다.
    """

    scope_base: str = ""
    window: str = ""

    def __init__(self):  # noqa: D107 - 상위 __init__ 의 사전 계산을 의도적으로 건너뛴다
        # SimpleRateThrottle.__init__ 은 self.scope 로 요율을 미리 계산하는데,
        # 우리는 요청자의 플랜을 봐야 scope 가 정해지므로 allow_request 로 미룬다.
        pass

    def allow_request(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            self._ident = str(user.pk)
            tier = resolve_plan_tier(user)
        else:
            # 이 스로틀이 붙는 뷰는 전부 인증 필수라 정상 경로에선 안 오지만,
            # 권한 설정이 바뀌어도 죽지 않도록 IP 폴백을 둔다.
            self._ident = self.get_ident(request)
            tier = "anon"

        self.scope = f"{self.scope_base}_{tier}_{self.window}"
        # 테스트가 override_settings 로 요율을 갈아끼울 수 있도록 클래스 상수가 아니라
        # api_settings 를 매 요청 조회한다(SimpleRateThrottle.THROTTLE_RATES 는 import 시점 고정).
        rate = (api_settings.DEFAULT_THROTTLE_RATES or {}).get(self.scope)
        if not rate:
            return True  # fail-open — 모듈 docstring 참고
        self.rate = rate
        self.num_requests, self.duration = self.parse_rate(rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self._ident}


class AiGenerateHourlyThrottle(PlanScopedRateThrottle):
    """AI 생성 계열 — 시간당(버스트 차단)."""

    scope_base = "ai_generate"
    window = "hour"


class AiGenerateDailyThrottle(PlanScopedRateThrottle):
    """AI 생성 계열 — 하루(총 비용 상한)."""

    scope_base = "ai_generate"
    window = "day"


#: AI 생성 엔드포인트에 붙이는 표준 조합. 새 AI 엔드포인트를 추가하면 이걸 그대로 쓴다.
AI_GENERATE_THROTTLES = [AiGenerateHourlyThrottle, AiGenerateDailyThrottle]
