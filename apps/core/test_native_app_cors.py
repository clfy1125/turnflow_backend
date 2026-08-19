"""네이티브 앱(Capacitor) origin 의 CORS 허용 회귀 테스트.

## 이 테스트가 막는 것

1. **앱 전멸** — Capacitor 앱의 origin(`https://localhost` / `capacitor://localhost`)이
   허용목록에서 빠지면 preflight 응답에 `Access-Control-*` 가 하나도 붙지 않아
   **엔드포인트 단위가 아니라 origin 단위로** 앱의 모든 API 호출이 막힌다.
   (2026-08-19 실제 제보: 갤럭시 S24+ 에서 `track/visit/` 부터 전부 CORS 차단)
2. **비표준 스킴 회귀** — `capacitor://` 를 `CORS_ALLOWED_ORIGINS` 가 못 받는다고 오판해
   `CORS_ALLOWED_ORIGIN_REGEXES` 로 옮기면(= 정규식이 되면) `capacitor://localhost.evil.com`
   같은 접두 일치가 새어 들어올 수 있다. **정확 일치 목록으로 되는 것**을 여기서 못 박는다.
3. **OAuth 리다이렉트 허용목록 오염** — `IG_OAUTH_RETURN_TO_ORIGINS` 는 비어 있으면
   `CORS_ALLOWED_ORIGINS` 를 상속한다. 앱을 붙이려고 넣은 `https://localhost` 가
   OAuth `return_to`/postMessage 대상까지 **조용히** 넓히면 안 된다.

## 고장난 버전에 대고 검증했는가 (필수 절차)

했다. `config/settings/local.py` 의 병합 루프를 지우면 `test_preflight_*` 두 개가
"`access-control-allow-origin` 없음" 으로 실패하고, `oauth_return.allowed_origins()` 의
`excluded` 처리를 지우면 `test_native_origin_is_not_an_oauth_return_target` 이 실패한다.
[[validate-detectors-against-broken-version]]
"""

import pytest
from django.conf import settings
from django.test import Client

from apps.integrations.oauth_return import allowed_origins, validate_return_to

# 앱 바이너리에 박히는 두 origin. 값이 바뀌면 앱을 다시 빌드해야 하므로 상수로 못 박는다.
ANDROID_ORIGIN = "https://localhost"
IOS_ORIGIN = "capacitor://localhost"

# preflight 는 CorsMiddleware 가 뷰 앞에서 가로채므로 인증이 필요한 경로여도 200 이 된다.
# 프론트 제보에 쓰인 경로를 그대로 쓴다.
PROBE_PATH = "/api/v1/auth/me/"


def _preflight(origin: str):
    return Client().options(
        PROBE_PATH,
        HTTP_ORIGIN=origin,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
    )


def test_settings_carry_both_native_origins():
    """env 를 건드리지 않은 상태에서도 두 origin 이 허용목록에 있어야 한다.

    이 값을 `.env` 로 관리하면 새 환경·DR 복구본에서 조용히 빠진다 → 코드로 합류시킨다.
    """
    assert ANDROID_ORIGIN in settings.NATIVE_APP_CORS_ORIGINS
    assert IOS_ORIGIN in settings.NATIVE_APP_CORS_ORIGINS
    assert ANDROID_ORIGIN in settings.CORS_ALLOWED_ORIGINS
    assert IOS_ORIGIN in settings.CORS_ALLOWED_ORIGINS


@pytest.mark.parametrize("origin", [ANDROID_ORIGIN, IOS_ORIGIN])
def test_preflight_allows_native_origin(origin):
    res = _preflight(origin)
    assert res.status_code == 200
    # 브라우저는 이 헤더가 요청 origin 과 정확히 같을 때만 응답을 스크립트에 넘긴다.
    assert res.headers.get("access-control-allow-origin") == origin
    assert res.headers.get("access-control-allow-credentials") == "true"
    # 앱은 JWT 를 Authorization 으로 보낸다 → 허용 헤더에 반드시 있어야 한다.
    assert "authorization" in res.headers.get("access-control-allow-headers", "").lower()
    # 허용목록 방식이면 반드시 Vary: Origin 이 있어야 한다 —
    # 없으면 CDN/프록시가 한 origin 의 응답을 다른 origin 에 재사용할 수 있다.
    assert "origin" in res.headers.get("vary", "").lower()


def test_preflight_still_rejects_unknown_origin():
    """와일드카드로 열리지 않았음을 확인 — 모르는 origin 에는 헤더가 없어야 한다."""
    res = _preflight("https://evil.example")
    assert "access-control-allow-origin" not in res.headers


@pytest.mark.django_db
def test_actual_request_carries_cors_header_on_401():
    """preflight 만 통과해도 소용없다 — 본 요청(여기선 인증 실패 401)에도 헤더가 붙어야
    프론트가 401 을 CORS 오류가 아닌 '로그인 필요' 로 읽을 수 있다."""
    res = Client().get(PROBE_PATH, HTTP_ORIGIN=ANDROID_ORIGIN)
    assert res.status_code == 401
    assert res.headers.get("access-control-allow-origin") == ANDROID_ORIGIN


def test_native_origin_is_not_an_oauth_return_target(settings):
    """CORS 허용(응답을 보여줌) ≠ OAuth 결과를 넘겨줄 주소.

    `IG_OAUTH_RETURN_TO_ORIGINS` 가 비어 CORS 목록을 상속하더라도 네이티브 origin 은
    제외돼야 한다. (`capacitor://` 는 http(s) 가 아니라 애초에 걸러지므로 관건은
    `https://localhost` 다.)
    """
    settings.IG_OAUTH_RETURN_TO_ORIGINS = []
    settings.CORS_ALLOWED_ORIGINS = ["https://turnflow.link", ANDROID_ORIGIN, IOS_ORIGIN]
    settings.NATIVE_APP_CORS_ORIGINS = [ANDROID_ORIGIN, IOS_ORIGIN]

    assert allowed_origins() == ["https://turnflow.link"]

    cleaned, reason = validate_return_to("https://localhost/oauth/done")
    assert cleaned is None
    assert reason == "origin_not_allowed"


def test_explicit_setting_can_still_opt_in(settings):
    """앱에서 IG 연동을 붙일 때는 명시적으로 넣을 수 있어야 한다(제외는 폴백에만 적용)."""
    settings.IG_OAUTH_RETURN_TO_ORIGINS = ["https://turnflow.link", ANDROID_ORIGIN]
    settings.NATIVE_APP_CORS_ORIGINS = [ANDROID_ORIGIN, IOS_ORIGIN]

    assert allowed_origins() == ["https://turnflow.link", ANDROID_ORIGIN]
