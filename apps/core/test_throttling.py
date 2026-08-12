"""AI 엔드포인트 속도 제한 + 429 두 종류 구분 회귀 테스트 (감사 H-8).

## 이 테스트가 막는 것

1. **비용 폭발** — AI 엔드포인트에 제한도 토큰 차감도 없어, 로그인만 하면 LLM 비용을
   무제한으로 태울 수 있었다.
2. **결제 분석 오염** — 429 를 이미 "요금제 한도 초과"(``PLAN_LIMIT_EXCEEDED``)가 쓰고 있어,
   스로틀 429 를 구분 없이 내보내면 프론트가 "너무 빨라요" 를 "돈 내세요" 로 착각해
   paywall 분석 데이터가 되돌릴 수 없게 오염된다.

## 고장난 버전에 대고 검증했는가 (필수 절차)

했다. ``throttle_classes`` 를 떼고 돌리면 ``test_free_user_is_throttled_after_limit`` 가
"제한이 안 걸림" 으로 실패하고, 예외 핸들러의 ``Throttled`` 분기를 지우면
``test_throttle_429_is_distinguishable_from_plan_limit_429`` 가 실패한다.
[[validate-detectors-against-broken-version]]

## 캐시 격리

스로틀은 Redis 캐시에 카운터를 쌓는다. 테스트가 실제 캐시를 오염시키지 않도록
로컬 메모리 캐시로 갈아끼우고 매 테스트마다 비운다.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.exceptions import Throttled
from rest_framework.test import APIClient, APIRequestFactory

from apps.core.exceptions import PlanLimitExceededError, custom_exception_handler
from apps.core.throttling import (
    AiGenerateDailyThrottle,
    AiGenerateHourlyThrottle,
    resolve_plan_tier,
)

User = get_user_model()

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "throttle-tests",
    }
}


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"throttle-{uuid.uuid4().hex[:12]}@example.com", password="Pw123456!"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. 429 두 종류가 구분되는가 (결제 분석 오염 방지)
# ──────────────────────────────────────────────────────────────────────────────


def test_throttle_429_is_distinguishable_from_plan_limit_429():
    """스로틀 429 와 요금제 429 는 같은 상태코드지만 code 로 갈려야 한다."""
    throttled = custom_exception_handler(Throttled(wait=42), {})
    plan_limit = custom_exception_handler(
        PlanLimitExceededError(metric="dm", current=100, limit=100, plan="free"), {}
    )

    assert throttled.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert plan_limit.status_code == status.HTTP_429_TOO_MANY_REQUESTS

    assert throttled.data["error"]["code"] == "RATE_LIMITED"
    assert plan_limit.data["error"]["code"] == "PLAN_LIMIT_EXCEEDED"
    # 프론트가 이 둘로 분기한다 — 같아지면 paywall 분석이 오염된다
    assert throttled.data["error"]["code"] != plan_limit.data["error"]["code"]


def test_throttle_response_carries_retry_after():
    """프론트가 '몇 초 뒤 재시도' 를 안내할 수 있어야 한다."""
    res = custom_exception_handler(Throttled(wait=42), {})
    assert res.data["error"]["details"]["code"] == "RATE_LIMITED"
    # 올림 — 딱 그 초에 다시 쏘면 경계에서 또 막힌다
    assert res.data["error"]["details"]["retry_after"] == 43


def test_throttle_response_without_wait_omits_retry_after():
    """wait 를 못 구하는 경우에도 죽지 않는다."""
    res = custom_exception_handler(Throttled(wait=None), {})
    assert res.data["error"]["code"] == "RATE_LIMITED"
    assert "retry_after" not in res.data["error"]["details"]


# ──────────────────────────────────────────────────────────────────────────────
# 2. 플랜 티어 판정
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_new_user_resolves_to_free_tier(user):
    assert resolve_plan_tier(user) == "free"


@pytest.mark.django_db
def test_plan_resolution_failure_falls_back_to_free(user, monkeypatch):
    """구독 조회가 터져도 API 가 죽지 않고 보수적으로 free 요율을 쓴다."""

    def _boom(_u):
        raise RuntimeError("DB 순간 장애")

    monkeypatch.setattr("apps.billing.subscription_utils.get_effective_plan", _boom, raising=True)
    assert resolve_plan_tier(user) == "free"


# ──────────────────────────────────────────────────────────────────────────────
# 3. 실제로 제한이 걸리는가
# ──────────────────────────────────────────────────────────────────────────────


class _DummyView:
    throttle_scope = None


def _drain(throttle_cls, request, limit):
    """limit 번까지는 통과해야 하고, 그 다음 호출의 통과 여부를 돌려준다."""
    view = _DummyView()
    for i in range(limit):
        t = throttle_cls()
        assert t.allow_request(request, view) is True, f"{i + 1}번째에서 이미 막혔다"
    return throttle_cls().allow_request(request, view)


@pytest.mark.django_db
@override_settings(
    CACHES=LOCMEM,
    REST_FRAMEWORK={
        "DEFAULT_THROTTLE_RATES": {
            "ai_generate_free_hour": "3/hour",
            "ai_generate_free_day": "5/day",
        }
    },
)
def test_free_user_is_throttled_after_limit(user):
    """무료 사용자는 상한을 넘기면 막힌다 (제한이 실제로 걸리는지)."""
    request = APIRequestFactory().post("/api/v1/ai/classify-posts/")
    request.user = user

    assert (
        _drain(AiGenerateHourlyThrottle, request, 3) is False
    ), "상한을 넘겼는데도 통과했다 — 스로틀이 동작하지 않는다"


@pytest.mark.django_db
@override_settings(
    CACHES=LOCMEM,
    REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {"ai_generate_free_day": "2/day"}},
)
def test_hourly_and_daily_windows_are_independent(user):
    """시간당 요율이 없어도 하루 창은 따로 동작한다 (두 창이 독립적인지)."""
    request = APIRequestFactory().post("/api/v1/ai/classify-posts/")
    request.user = user

    # 시간당 scope 가 설정에 없다 → fail-open 으로 무제한 통과
    hourly = AiGenerateHourlyThrottle()
    assert hourly.allow_request(request, _DummyView()) is True

    # 하루 창은 2회에서 막힌다
    assert _drain(AiGenerateDailyThrottle, request, 2) is False


@pytest.mark.django_db
@override_settings(CACHES=LOCMEM, REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {}})
def test_missing_rate_config_fails_open(user):
    """요율 설정이 없으면 제한 없이 통과한다 (설정 누락이 기능 정지로 이어지지 않게).

    뒤집어 말하면 요율 키를 잘못 쓰면 스로틀이 조용히 꺼진다는 뜻이다.
    """
    request = APIRequestFactory().post("/api/v1/ai/classify-posts/")
    request.user = user
    assert AiGenerateHourlyThrottle().allow_request(request, _DummyView()) is True


@pytest.mark.django_db
@override_settings(
    CACHES=LOCMEM,
    REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {"ai_generate_free_hour": "2/hour"}},
)
def test_users_do_not_share_a_bucket(user):
    """사용자별로 따로 센다 — 한 사람이 다 쓰면 남이 막히는 일이 없어야 한다.

    (인증 요청은 IP 가 아니라 user.pk 로 키잉되므로 NUM_PROXIES 와도 무관하다)
    """
    other = User.objects.create_user(
        email=f"throttle-{uuid.uuid4().hex[:12]}@example.com", password="Pw123456!"
    )
    factory = APIRequestFactory()

    r1 = factory.post("/api/v1/ai/classify-posts/")
    r1.user = user
    assert _drain(AiGenerateHourlyThrottle, r1, 2) is False  # user 소진

    r2 = factory.post("/api/v1/ai/classify-posts/")
    r2.user = other
    assert AiGenerateHourlyThrottle().allow_request(r2, _DummyView()) is True


@pytest.mark.django_db
def test_ai_endpoints_actually_have_throttles_attached():
    """뷰에 스로틀이 실제로 붙어 있는지 — 붙이는 걸 빠뜨리면 위 테스트가 다 통과해도 무의미하다."""
    from apps.ai_jobs.views import AiClassifyPostsView
    from apps.integrations.views import AutoDMCampaignViewSet

    names = {c.__name__ for c in AiClassifyPostsView.throttle_classes}
    assert {"AiGenerateHourlyThrottle", "AiGenerateDailyThrottle"} <= names

    action_kwargs = AutoDMCampaignViewSet.ai_suggest.kwargs
    action_names = {c.__name__ for c in action_kwargs["throttle_classes"]}
    assert {"AiGenerateHourlyThrottle", "AiGenerateDailyThrottle"} <= action_names


@pytest.mark.django_db
@override_settings(
    CACHES=LOCMEM,
    REST_FRAMEWORK={
        "DEFAULT_THROTTLE_RATES": {"ai_generate_free_hour": "1/hour"},
        # ⚠️ override_settings 는 REST_FRAMEWORK 를 통째로 갈아끼운다 → 이 줄이 없으면
        #    EXCEPTION_HANDLER 가 사라져 DRF 기본 응답({"detail": ...})이 나온다.
        #    즉 우리 포맷을 검사하는 이 테스트가 제품 결함이 아니라 테스트 결함으로 실패한다.
        "EXCEPTION_HANDLER": "apps.core.exceptions.custom_exception_handler",
    },
)
def test_end_to_end_returns_rate_limited_code(user):
    """실제 HTTP 호출로 429 + RATE_LIMITED 가 나오는지 (배관 전체 확인)."""
    client = APIClient()
    client.force_authenticate(user=user)

    # 1회차: 스로틀은 통과(뷰 자체는 입력 검증에서 4xx 가 날 수 있으나 429 만 아니면 된다)
    first = client.post("/api/v1/ai/classify-posts/", {}, format="json")
    assert first.status_code != status.HTTP_429_TOO_MANY_REQUESTS

    second = client.post("/api/v1/ai/classify-posts/", {}, format="json")
    assert second.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert second.data["error"]["code"] == "RATE_LIMITED"
