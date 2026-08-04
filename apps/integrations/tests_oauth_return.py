"""`return_to` 검증 / 결과 리다이렉트 조립 / postMessage 대상 오리진 테스트.

⚠️ 이 파일의 핵심은 **오픈 리다이렉트 방어**다. 여기가 뚫리면 우리 도메인으로 시작하는
피싱 링크(`.../connect/start?...&return_to=https://evil.com`)가 만들어진다.

실행: pytest apps/integrations/tests_oauth_return.py
(`tests_*.py` 는 pytest 기본 수집 패턴이 아니므로 **경로를 명시**해야 한다 — CLAUDE.md 참고.)
"""

import pytest

from apps.integrations import oauth_return

ALLOWED = [
    "https://turnflow.link",
    "https://app.turnflow.link",
    "http://localhost:3000",
]


@pytest.fixture(autouse=True)
def _allowlist(settings):
    """모든 테스트에서 허용목록을 고정한다(환경 의존 제거)."""
    settings.IG_OAUTH_RETURN_TO_ORIGINS = list(ALLOWED)


# ---------------------------------------------------------------------------
# 허용되어야 하는 것
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://turnflow.link",
        "https://turnflow.link/",
        "https://turnflow.link/settings",
        "https://turnflow.link/settings?ig=done",
        "https://turnflow.link/settings?ig=done&x=1",
        "https://app.turnflow.link/onboarding/step2",
        "http://localhost:3000/settings",
        "https://TurnFlow.Link/settings",  # host 대소문자 무시
        "https://turnflow.link:443/settings",  # 기본 포트 명시 = 같은 origin
    ],
)
def test_allowed_urls_pass(url):
    value, reason = oauth_return.validate_return_to(url)
    assert reason is None, f"{url} 이 거부됐다: {reason}"
    assert value


def test_path_and_query_are_preserved():
    value, reason = oauth_return.validate_return_to("https://turnflow.link/a/b?c=1&d=2")
    assert reason is None
    assert value == "https://turnflow.link/a/b?c=1&d=2"


def test_fragment_is_dropped():
    value, _ = oauth_return.validate_return_to("https://turnflow.link/s?a=1#frag")
    assert value == "https://turnflow.link/s?a=1"


# ---------------------------------------------------------------------------
# 거부되어야 하는 것 — 오픈 리다이렉트 공격 벡터
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_reason",
    [
        # ★ prefix/부분문자열 비교였다면 통과했을 것들 (전형적 우회)
        ("https://turnflow.link.evil.com/x", "origin_not_allowed"),
        ("https://turnflow.link-evil.com/x", "origin_not_allowed"),
        ("https://evil.com/https://turnflow.link", "origin_not_allowed"),
        ("https://evil.com/?next=https://turnflow.link", "origin_not_allowed"),
        # 서브도메인은 별개 origin
        ("https://sub.turnflow.link/x", "origin_not_allowed"),
        # scheme 불일치 (http 로 다운그레이드)
        ("http://turnflow.link/x", "origin_not_allowed"),
        # 포트 불일치
        ("https://turnflow.link:8443/x", "origin_not_allowed"),
        ("http://localhost:3001/x", "origin_not_allowed"),
        # 위험한 scheme
        ("javascript:alert(1)", "scheme_not_allowed"),
        ("data:text/html,<script>alert(1)</script>", "scheme_not_allowed"),
        ("file:///etc/passwd", "scheme_not_allowed"),
        # userinfo 로 호스트 혼동 유도
        ("https://turnflow.link@evil.com/x", "userinfo_not_allowed"),
        ("https://evil.com@turnflow.link/x", "userinfo_not_allowed"),
        # 스킴 없는 프로토콜-상대 URL (브라우저는 //evil.com 을 evil.com 으로 해석)
        ("//evil.com/x", "scheme_not_allowed"),
        # 역슬래시 (파서별 해석 차이)
        ("https://turnflow.link\\@evil.com", "illegal_characters"),
        ("https:\\\\turnflow.link/x", "illegal_characters"),
        # 제어문자 삽입
        ("https://turnflow.link/x\r\nLocation: https://evil.com", "illegal_characters"),
        ("https://turnflow.link/x\x00", "illegal_characters"),
        # 빈 값
        ("", "empty"),
        ("   ", "empty"),
    ],
)
def test_attack_vectors_rejected(url, expected_reason):
    value, reason = oauth_return.validate_return_to(url)
    assert value is None, f"{url!r} 이 통과했다 — 오픈 리다이렉트!"
    assert reason == expected_reason, f"{url!r} 사유가 {reason} (기대 {expected_reason})"


def test_too_long_rejected():
    url = "https://turnflow.link/" + "a" * oauth_return.MAX_RETURN_TO_LEN
    value, reason = oauth_return.validate_return_to(url)
    assert value is None
    assert reason == "too_long"


def test_non_string_rejected():
    for bad in (None, 123, [], {}):
        value, _ = oauth_return.validate_return_to(bad)
        assert value is None


def test_empty_allowlist_rejects_everything(settings):
    """허용목록이 비면(설정 누락) fail-closed 여야 한다 — 전부 거부."""
    settings.IG_OAUTH_RETURN_TO_ORIGINS = []
    settings.CORS_ALLOWED_ORIGINS = []
    value, reason = oauth_return.validate_return_to("https://turnflow.link/x")
    assert value is None
    assert reason == "origin_not_allowed"


def test_falls_back_to_cors_allowed_origins(settings):
    settings.IG_OAUTH_RETURN_TO_ORIGINS = []
    settings.CORS_ALLOWED_ORIGINS = ["https://only-here.example"]
    ok, reason = oauth_return.validate_return_to("https://only-here.example/x")
    assert reason is None and ok
    bad, reason2 = oauth_return.validate_return_to("https://turnflow.link/x")
    assert bad is None and reason2 == "origin_not_allowed"


# ---------------------------------------------------------------------------
# 결과 리다이렉트 조립
# ---------------------------------------------------------------------------


def test_build_redirect_appends_result():
    url = oauth_return.build_result_redirect("https://turnflow.link/s", result="connected")
    assert url == "https://turnflow.link/s?ig_result=connected"


def test_build_redirect_preserves_existing_query():
    url = oauth_return.build_result_redirect("https://turnflow.link/s?ig=done", result="connected")
    assert url.startswith("https://turnflow.link/s?")
    assert "ig=done" in url and "ig_result=connected" in url


def test_build_redirect_includes_reason_on_failure():
    url = oauth_return.build_result_redirect(
        "https://turnflow.link/s", result="failed", reason="PLAN_LIMIT_EXCEEDED"
    )
    assert "ig_result=failed" in url
    assert "reason=PLAN_LIMIT_EXCEEDED" in url


def test_build_redirect_strips_caller_supplied_result_params():
    """클라이언트가 미리 심어둔 ig_result 를 서버 값이 덮어써야 한다(성공 위조 방지)."""
    url = oauth_return.build_result_redirect(
        "https://turnflow.link/s?ig_result=connected&reason=NONE",
        result="failed",
        reason="INTERNAL_ERROR",
    )
    assert url.count("ig_result=") == 1
    assert "ig_result=failed" in url
    assert "ig_result=connected" not in url
    assert url.count("reason=") == 1
    assert "reason=INTERNAL_ERROR" in url


# ---------------------------------------------------------------------------
# postMessage 대상 오리진 — '*' 금지
# ---------------------------------------------------------------------------


def test_postmessage_origins_never_wildcard():
    origins = oauth_return.postmessage_target_origins()
    assert "*" not in origins
    assert origins == ALLOWED


def test_postmessage_prefers_known_opener_origin():
    origins = oauth_return.postmessage_target_origins("https://app.turnflow.link")
    assert origins[0] == "https://app.turnflow.link"
    assert sorted(origins) == sorted(ALLOWED), "허용목록 전체가 유지돼야 한다"


def test_postmessage_ignores_untrusted_opener_origin():
    origins = oauth_return.postmessage_target_origins("https://evil.com")
    assert "https://evil.com" not in origins
    assert origins == ALLOWED


# ---------------------------------------------------------------------------
# origin 정규화
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://a.com", "https://a.com"),
        ("https://a.com:443", "https://a.com"),
        ("http://a.com:80/x", "http://a.com"),
        ("https://a.com:8443", "https://a.com:8443"),
        ("HTTPS://A.COM/x?y=1", "https://a.com"),
        ("ftp://a.com", None),
        ("not a url", None),
        ("", None),
    ],
)
def test_normalize_origin(raw, expected):
    assert oauth_return.normalize_origin(raw) == expected
