"""인스타그램 로그인/가입 — 킬스위치·state 1회용·계정 매칭·연동 동시 생성.

지키려는 계약 넷:
  1. 기본 **비활성** (INSTAGRAM_LOGIN_ENABLED=False → 404)
  2. ``state`` 는 1회용 — 콜백 새로고침으로 같은 코드가 두 번 교환되지 않는다
  3. 계정 매칭: ``instagram_user_id`` → 기존 연동의 워크스페이스 owner → 신규
     (**email 로는 절대 찾지 않는다** — 인스타는 이메일을 주지 않는다)
  4. 가입 한 번으로 워크스페이스 + IG 연동까지 생긴다 (별도 연동 단계 없음)

외부 호출은 ``instagram.exchange_code`` 를 대체해 없앤다 — Meta 를 실제로 부르지 않는다.
"""

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication import instagram
from apps.authentication.instagram import InstagramProfile
from apps.integrations.models import IGAccountConnection
from apps.workspace.models import Membership, Workspace

START = "authentication:instagram-login-start"
LOGIN = "authentication:instagram-login"
REDIRECT = "http://localhost:3000/auth/instagram/callback"


def _profile(ig_id=None, username="testhandle"):
    return InstagramProfile(
        user_id=ig_id or f"ig_{uuid.uuid4().hex[:12]}",
        username=username,
        name="테스트 계정",
        account_type="BUSINESS",
        access_token="mock_token_test",
        token_expires_at=timezone.now() + timedelta(days=60),
    )


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """스로틀(20/min)을 끈다 — test_kakao_login.py 와 같은 이유.

    ⚠️ `settings.REST_FRAMEWORK` 를 덮어써도 **먹지 않는다**(DRF 가 THROTTLE_RATES 를
    import 시점에 클래스 속성으로 굳힌다). 게다가 카운터가 dev Redis 라 **테스트 실행
    사이에 새서**, 파일 단독 실행은 통과하고 연속 실행에서만 429 로 깨지는 유령 실패가 난다.
    스로틀 **배선**은 TestInstagramThrottleWiring 이 따로 지킨다.
    """
    monkeypatch.setattr(
        "rest_framework.throttling.SimpleRateThrottle.allow_request",
        lambda self, request, view: True,
    )


@pytest.fixture
def enabled(settings):
    settings.INSTAGRAM_LOGIN_ENABLED = True
    settings.INSTAGRAM_MOCK_MODE = True
    settings.DEBUG = True
    settings.INSTAGRAM_LOGIN_REDIRECT_URI = REDIRECT
    return settings


@pytest.fixture
def stub_exchange(monkeypatch):
    """``exchange_code`` 를 고정 프로필로 대체 — 외부 호출 없이 매칭 로직만 본다."""

    def _install(profile):
        monkeypatch.setattr(instagram, "exchange_code", lambda code, redirect_uri: profile)
        return profile

    return _install


def _login(client, profile_state=None):
    start = client.get(reverse(START))
    assert start.status_code == 200, start.json()
    return client.post(
        reverse(LOGIN),
        {"code": "mock_code_x", "state": profile_state or start.json()["state"]},
        format="json",
    )


@pytest.mark.django_db
class TestKillswitch:
    def test_start_is_404_when_disabled(self, settings):
        settings.INSTAGRAM_LOGIN_ENABLED = False
        res = APIClient().get(reverse(START))
        assert res.status_code == 404
        assert res.json()["code"] == "INSTAGRAM_LOGIN_DISABLED"

    def test_login_is_404_when_disabled(self, settings):
        settings.INSTAGRAM_LOGIN_ENABLED = False
        res = APIClient().post(reverse(LOGIN), {"code": "a", "state": "b"}, format="json")
        assert res.status_code == 404


@pytest.mark.django_db
class TestStart:
    def test_returns_authorize_url_and_state(self, enabled):
        res = APIClient().get(reverse(START))
        assert res.status_code == 200
        body = res.json()
        assert body["state"]
        assert (
            body["authorize_url"].startswith(REDIRECT) or "instagram.com" in body["authorize_url"]
        )

    def test_client_app_marks_state_with_prefix(self, enabled):
        """앱(Capacitor)에서 시작한 로그인 표시 — 웹 콜백 페이지가 앱으로 되돌리는 근거."""
        res = APIClient().get(reverse(START), {"client": "app"})
        assert res.status_code == 200
        state = res.json()["state"]
        assert state.startswith("app_")
        # authorize_url 에도 같은 state 가 실린다 (인스타가 그대로 되돌려 준다)
        assert state in res.json()["authorize_url"]

    def test_web_state_never_starts_with_app_prefix(self, enabled):
        """⚠️ 앱에만 접두어를 붙이면 난수가 우연히 `app_` 로 시작할 때 웹이 앱으로 튕긴다.

        양쪽에 접두어를 붙여 그 경우를 구조적으로 없앴다.
        """
        for params in ({}, {"client": "web"}, {"client": "쓰레기값"}):
            state = APIClient().get(reverse(START), params).json()["state"]
            assert state.startswith("web_"), params
            assert not state.startswith("app_"), params

    def test_client_param_is_case_insensitive(self, enabled):
        assert APIClient().get(reverse(START), {"client": "APP"}).json()["state"].startswith("app_")

    def test_app_state_is_accepted_on_exchange(self, enabled, stub_exchange):
        """접두어가 붙은 state 로도 교환이 그대로 된다 (교환 계약 변경 없음)."""
        stub_exchange(_profile())
        client = APIClient()
        state = client.get(reverse(START), {"client": "app"}).json()["state"]
        assert state.startswith("app_")

        res = client.post(reverse(LOGIN), {"code": "mock_code_x", "state": state}, format="json")
        assert res.status_code == 200, res.json()
        assert res.json()["is_new_user"] is True

    def test_rejects_redirect_uri_outside_allowlist(self, enabled):
        """오픈 리다이렉트 방어 — 임의 주소로 인가 코드를 보낼 수 없다."""
        res = APIClient().get(reverse(START), {"redirect_uri": "https://evil.example.com/cb"})
        assert res.status_code == 400
        assert res.json()["code"] == "INSTAGRAM_INVALID_REDIRECT_URI"


@pytest.mark.django_db
class TestSignup:
    def test_creates_user_workspace_and_connection_in_one_call(self, enabled, stub_exchange):
        profile = stub_exchange(_profile(username="newhandle"))
        client = APIClient()

        res = _login(client)

        assert res.status_code == 200, res.json()
        body = res.json()
        assert body["is_new_user"] is True
        assert body["tokens"]["access"]
        assert body["ig_connection_error"] is None
        assert body["ig_connection"]["username"] == "newhandle"

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(id=body["user"]["id"])
        assert user.instagram_user_id == profile.user_id
        assert user.has_usable_password() is False
        # 자리표시 이메일 — 메일을 보낼 수 없다는 사실이 응답에 드러나야 한다
        assert body["user"]["email_is_placeholder"] is True
        assert user.is_email_verified is False

        ws = Workspace.objects.get(owner=user)
        assert Membership.objects.filter(user=user, workspace=ws, role="owner").exists()
        assert (
            IGAccountConnection.objects.filter(
                workspace=ws, external_account_id=profile.user_id
            ).count()
            == 1
        )

    def test_attribution_is_captured_as_instagram(self, enabled, stub_exchange):
        stub_exchange(_profile())
        client = APIClient()

        res = client.post(
            reverse(LOGIN),
            {
                "code": "mock_code_x",
                "state": client.get(reverse(START)).json()["state"],
                "attribution": {"utm_source": "meta", "utm_medium": "cpc"},
            },
            format="json",
        )

        from apps.analytics.models import SignupAttribution

        attr = SignupAttribution.objects.get(user_id=res.json()["user"]["id"])
        assert attr.signup_kind == "instagram"
        assert attr.channel == "meta_ads"

    def test_placeholder_email_is_never_mailed(self, enabled, stub_exchange):
        from apps.emails.services.sender import send_email

        stub_exchange(_profile())
        res = _login(APIClient())
        email = res.json()["user"]["email"]

        assert send_email("welcome", email, {}) is None


@pytest.mark.django_db
class TestStateIsSingleUse:
    def test_reusing_state_is_rejected(self, enabled, stub_exchange):
        stub_exchange(_profile())
        client = APIClient()
        state = client.get(reverse(START)).json()["state"]

        first = client.post(reverse(LOGIN), {"code": "mock_code_x", "state": state}, format="json")
        assert first.status_code == 200

        second = client.post(reverse(LOGIN), {"code": "mock_code_x", "state": state}, format="json")
        assert second.status_code == 400
        assert second.json()["code"] == "INSTAGRAM_STATE_USED"

    def test_unknown_state_is_rejected(self, enabled, stub_exchange):
        stub_exchange(_profile())
        res = APIClient().post(
            reverse(LOGIN), {"code": "mock_code_x", "state": "nope"}, format="json"
        )
        assert res.status_code == 400
        assert res.json()["code"] == "INSTAGRAM_STATE_INVALID"


@pytest.mark.django_db
class TestAccountMatching:
    def test_same_instagram_account_logs_into_same_user(self, enabled, stub_exchange):
        from django.contrib.auth import get_user_model

        profile = stub_exchange(_profile())
        client = APIClient()

        first = _login(client).json()
        second = _login(client).json()

        assert first["is_new_user"] is True
        assert second["is_new_user"] is False
        assert second["user"]["id"] == first["user"]["id"]
        # ⚠️ 여기가 핵심 — 두 번째 로그인이 계정을 하나 더 만들면 워크스페이스·구독이 갈린다
        assert get_user_model().objects.filter(instagram_user_id=profile.user_id).count() == 1
        assert Workspace.objects.filter(owner_id=first["user"]["id"]).count() == 1

    def test_other_accounts_connection_blocks_login_with_reason(
        self, enabled, stub_exchange, django_user_model
    ):
        """⭐ 남의 계정에 연동된 IG 로는 **로그인 자체가 거부된다** (2026-09-13 정책).

        - 그 계정으로 들여보내지 않는다 (대행사·직원이 주인 계정에 들어가던 문제)
        - 새 계정도 만들지 않는다 (IG 를 못 붙이는 빈 계정만 남는다)
        - 프론트가 안내할 수 있도록 **이유와 마스킹된 이메일**을 준다
        """
        from django.contrib.auth import get_user_model

        legacy = django_user_model.objects.create_user(
            email=f"legacy-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
        )
        ws = Workspace.objects.create(owner=legacy, name="레거시")
        Membership.objects.create(user=legacy, workspace=ws, role=Membership.Role.OWNER)
        profile = _profile(username="myhandle")
        conn = IGAccountConnection(
            workspace=ws,
            external_account_id=profile.user_id,
            username=profile.username,
            account_type="BUSINESS",
            token_expires_at=timezone.now() + timedelta(days=30),
        )
        conn.access_token = "old_token"
        conn.save()

        stub_exchange(profile)
        res = _login(APIClient())

        assert res.status_code == 409
        body = res.json()
        assert body["code"] == "INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE"
        assert body["instagram_username"] == "myhandle"
        # 원본 이메일은 절대 나가지 않는다
        assert legacy.email not in str(body)
        assert body["masked_email"].endswith("@example.com")
        assert "***" in body["masked_email"]
        # 표준 envelope 도 함께
        assert body["error"]["code"] == 409

        # 아무 계정도 만들어지지 않았다
        assert not get_user_model().objects.filter(instagram_user_id=profile.user_id).exists()
        # legacy 도 건드려지지 않았다
        legacy.refresh_from_db()
        assert legacy.instagram_user_id is None

    def test_new_account_can_connect_after_old_account_disconnects(
        self, enabled, stub_exchange, django_user_model
    ):
        """옛 계정이 **연동을 해제**하면(REVOKED) 409 가 풀리고 **새 계정으로 가입**된다."""
        legacy = django_user_model.objects.create_user(
            email=f"legacy-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
        )
        ws = Workspace.objects.create(owner=legacy, name="레거시")
        Membership.objects.create(user=legacy, workspace=ws, role=Membership.Role.OWNER)
        profile = _profile()
        conn = IGAccountConnection(
            workspace=ws,
            external_account_id=profile.user_id,
            username=profile.username,
            account_type="BUSINESS",
            token_expires_at=timezone.now() + timedelta(days=30),
            status=IGAccountConnection.Status.REVOKED,  # 해제됨
        )
        conn.access_token = ""
        conn.save()

        stub_exchange(profile)
        body = _login(APIClient()).json()

        assert body["is_new_user"] is True
        assert body["ig_connection_error"] is None
        assert body["ig_connection"]["username"] == profile.username

    def test_ig_signup_account_logs_in_again(self, enabled, stub_exchange):
        """IG 로 가입한 계정은 다음부터 ``instagram_user_id`` 로 바로 로그인된다."""
        from django.contrib.auth import get_user_model

        profile = stub_exchange(_profile())
        first = _login(APIClient()).json()
        second = _login(APIClient()).json()

        assert first["is_new_user"] is True
        assert second["is_new_user"] is False
        assert second["user"]["id"] == first["user"]["id"]
        assert get_user_model().objects.filter(instagram_user_id=profile.user_id).count() == 1


@pytest.mark.django_db
class TestInstagramThrottleWiring:
    """⚠️ 이 프로젝트의 스로틀은 fail-open — 뷰의 scope 이름이 settings 에 없으면
    예외 없이 **조용히 꺼진다**. 이름을 여기서 못 박는다."""

    def test_scope_is_registered_in_settings(self, settings):
        from apps.authentication.instagram_views import InstagramLoginStartView, InstagramLoginView

        for view in (InstagramLoginStartView, InstagramLoginView):
            assert view.throttle_scope == "auth_instagram"
        assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["auth_instagram"]
