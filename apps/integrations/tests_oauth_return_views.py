"""`return_to` 뷰 레벨 동작 — connect/start 수용·거부, 콜백 302 vs HTML.

`tests_oauth_return.py` 가 순수 검증 로직을 다루고, 여기서는 **실제 엔드포인트**가
그 결과대로 동작하는지 본다(특히 콜백의 분기 클로저 `_finish`).

실행: pytest apps/integrations/tests_oauth_return_views.py
"""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import IGOAuthState
from apps.workspace.models import Membership, Workspace

ALLOWED = ["https://turnflow.link", "http://localhost:3000"]


@pytest.fixture(autouse=True)
def _allowlist(settings):
    settings.IG_OAUTH_RETURN_TO_ORIGINS = list(ALLOWED)


@pytest.fixture
def owner_ws(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"rt_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="RT"
    )
    ws = Workspace.objects.create(name="RT WS", slug=f"rt-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return user, ws


@pytest.fixture
def api(owner_ws):
    user, _ = owner_ws
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _start_url(ws):
    return f"/api/v1/integrations/instagram/workspaces/{ws.id}/connect/start/"


CALLBACK = "/api/v1/integrations/instagram/connect/callback/"


# ---------------------------------------------------------------------------
# connect/start
# ---------------------------------------------------------------------------


def test_start_without_return_to_keeps_popup_flow(api, owner_ws):
    """return_to 미지정 = 기존 동작. state 행의 return_to 는 빈 문자열."""
    _, ws = owner_ws
    res = api.post(_start_url(ws), {}, format="json")
    assert res.status_code == 200, res.content
    assert "authorization_url" in res.data
    st = IGOAuthState.objects.get(state=res.data["state"])
    assert st.return_to == ""


def test_start_accepts_allowlisted_return_to(api, owner_ws):
    _, ws = owner_ws
    res = api.post(
        _start_url(ws), {"return_to": "https://turnflow.link/settings?ig=done"}, format="json"
    )
    assert res.status_code == 200, res.content
    st = IGOAuthState.objects.get(state=res.data["state"])
    assert st.return_to == "https://turnflow.link/settings?ig=done"


@pytest.mark.parametrize(
    "bad",
    [
        "https://turnflow.link.evil.com/x",  # prefix 우회 시도
        "https://evil.com/x",
        "javascript:alert(1)",
        "//evil.com/x",
        "http://turnflow.link/x",  # scheme 다운그레이드
    ],
)
def test_start_rejects_bad_return_to(api, owner_ws, bad):
    """허용목록 밖은 400 + INVALID_RETURN_TO, 그리고 state 를 만들지 않는다."""
    _, ws = owner_ws
    before = IGOAuthState.objects.count()
    res = api.post(_start_url(ws), {"return_to": bad}, format="json")
    assert res.status_code == 400, f"{bad} 가 통과했다: {res.status_code}"
    body = res.json()
    assert body["error"]["details"]["code"] == "INVALID_RETURN_TO"
    assert "allowed_origins" in body["error"]["details"]
    assert IGOAuthState.objects.count() == before, "거부됐는데 state 가 생성됐다"


def test_start_records_opener_origin_when_trusted(api, owner_ws):
    _, ws = owner_ws
    res = api.post(_start_url(ws), {}, format="json", HTTP_ORIGIN="https://turnflow.link")
    st = IGOAuthState.objects.get(state=res.data["state"])
    assert st.opener_origin == "https://turnflow.link"


def test_start_ignores_untrusted_opener_origin(api, owner_ws):
    _, ws = owner_ws
    res = api.post(_start_url(ws), {}, format="json", HTTP_ORIGIN="https://evil.com")
    st = IGOAuthState.objects.get(state=res.data["state"])
    assert st.opener_origin == ""


# ---------------------------------------------------------------------------
# 콜백 — return_to 있으면 302, 없으면 HTML
# ---------------------------------------------------------------------------


def _make_state(ws, *, return_to="", opener_origin=""):
    return IGOAuthState.objects.create(
        state=f"st_{uuid.uuid4().hex}",
        workspace=ws,
        expires_at=timezone.now() + timedelta(minutes=10),
        return_to=return_to,
        opener_origin=opener_origin,
    )


def test_callback_error_redirects_when_return_to_set(client, owner_ws):
    _, ws = owner_ws
    st = _make_state(ws, return_to="https://turnflow.link/settings?ig=done")
    res = client.get(CALLBACK, {"error": "access_denied", "state": st.state})
    assert res.status_code == 302
    loc = res["Location"]
    assert loc.startswith("https://turnflow.link/settings?")
    assert "ig=done" in loc
    assert "ig_result=failed" in loc
    assert "reason=OAUTH_AUTHORIZATION_FAILED" in loc


def test_callback_error_renders_html_without_return_to(client, owner_ws):
    _, ws = owner_ws
    st = _make_state(ws)
    res = client.get(CALLBACK, {"error": "access_denied", "state": st.state})
    assert res.status_code == 200
    assert b"INSTAGRAM_ERROR" in res.content


def test_callback_invalid_state_never_redirects(client, db):
    """state 를 못 찾으면 복귀 주소를 신뢰할 수 없다 → 절대 리다이렉트하지 않는다."""
    res = client.get(CALLBACK, {"code": "x", "state": "nonexistent-state"})
    assert res.status_code == 200
    assert b"INVALID_STATE" in res.content


def test_callback_missing_params_never_redirects(client, owner_ws):
    _, ws = owner_ws
    st = _make_state(ws, return_to="https://turnflow.link/settings")
    # code 누락 → 리다이렉트하지 않고 HTML (state 는 있지만 계약상 HTML 종료)
    res = client.get(CALLBACK, {"state": st.state})
    assert res.status_code == 200
    assert b"MISSING_PARAMETERS" in res.content


def test_callback_expired_state_renders_html(client, owner_ws):
    _, ws = owner_ws
    st = IGOAuthState.objects.create(
        state=f"st_{uuid.uuid4().hex}",
        workspace=ws,
        expires_at=timezone.now() - timedelta(minutes=1),
        return_to="https://turnflow.link/settings",
    )
    res = client.get(CALLBACK, {"code": "abc", "state": st.state})
    assert res.status_code == 200
    assert b"INVALID_STATE" in res.content


# ---------------------------------------------------------------------------
# postMessage 계약 — source 필드 + '*' 금지
# ---------------------------------------------------------------------------


def test_callback_html_carries_source_field_and_no_wildcard(client, owner_ws):
    _, ws = owner_ws
    st = _make_state(ws, opener_origin="https://turnflow.link")
    res = client.get(CALLBACK, {"error": "access_denied", "state": st.state})
    html = res.content.decode()
    assert "source: 'ig-connect'" in html, "프론트가 식별하는 source 필드가 없다"
    assert "postMessage(payload, origins[i])" in html
    assert ", '*')" not in html, "targetOrigin 와일드카드가 남아 있다"
    # 허용목록이 인라인되고, opener origin 이 앞에 온다
    assert '"https://turnflow.link"' in html
    assert "https://evil.com" not in html


def test_callback_html_origins_exclude_untrusted(client, owner_ws, settings):
    _, ws = owner_ws
    settings.IG_OAUTH_RETURN_TO_ORIGINS = ["https://turnflow.link"]
    st = _make_state(ws)
    res = client.get(CALLBACK, {"error": "access_denied", "state": st.state})
    html = res.content.decode()
    assert '["https://turnflow.link"]' in html.replace(" ", "")
