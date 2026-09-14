"""
카카오 로그인 테스트.

⚠️ 이 프로젝트의 pytest DB 는 dev DB 와 같다(memory: test-db-not-clean). 이메일은 uuid 로
만들어 기존 행과 충돌하지 않게 한다.

카카오 HTTP 는 `apps.authentication.kakao` 의 함수 경계에서 막는다 — httpx 를 통째로
목킹하면 우리가 보내는 파라미터(client_secret 동봉 여부 등)가 검증에서 빠진다.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.authentication.kakao import KakaoError, KakaoProfile

User = get_user_model()
URL = "/api/v1/auth/kakao/"


def _email():
    return f"kakao-{uuid.uuid4().hex[:12]}@example.com"


def _profile(email, *, kakao_id=None, verified=True, nickname="테스터"):
    return KakaoProfile(
        # 실제 fetch_profile 이 str 로 정규화해 넘기므로 테스트도 str 로 맞춘다
        # (int 로 두면 `user.kakao_id != profile.kakao_id` 가 항상 참이 되어 버그를 가린다).
        kakao_id=str(kakao_id or uuid.uuid4().int % 10**10),
        email=email,
        email_verified=verified,
        nickname=nickname,
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """스로틀(20/min)을 끈다.

    ⚠️ `settings.REST_FRAMEWORK` 를 덮어써도 **먹지 않는다** — DRF 의 SimpleRateThrottle 은
    `THROTTLE_RATES` 를 import 시점에 클래스 속성으로 굳힌다. 게다가 카운터가 dev Redis 라
    테스트 간에 새서, 파일 단독 실행은 통과하고 전체 실행에서만 429 로 깨지는(그것도 실행마다
    개수가 달라지는) 유령 실패가 난다. 그래서 allow_request 자체를 막는다.
    스로틀 **배선**은 TestKakaoThrottleWiring 이 따로 지킨다.
    """
    monkeypatch.setattr(
        "rest_framework.throttling.SimpleRateThrottle.allow_request",
        lambda self, request, view: True,
    )


@pytest.fixture(autouse=True)
def _capi_off(monkeypatch):
    """Meta CAPI 는 외부 호출이라 테스트에서 막는다."""
    monkeypatch.setattr("apps.analytics.conversions.track_signup", lambda *a, **k: None)


def _patch_resolve(monkeypatch, result):
    """`resolve_profile` 을 **뷰가 import 한 이름**으로 바꿔 끼운다.

    `kakao.resolve_profile` 을 갈아도 뷰는 이미 from-import 로 자기 모듈에 바인딩해 둔
    참조를 쓰므로 목킹이 먹지 않는다 — 실수하기 쉬운 지점이라 헬퍼로 못 박는다.
    """

    def _fake(**kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("apps.authentication.kakao_views.resolve_profile", _fake)


@pytest.mark.django_db
class TestKakaoLoginRequestShape:
    def test_missing_both_credentials_is_400(self, client):
        resp = client.post(URL, {}, format="json")
        assert resp.status_code == 400
        # 시리얼라이저 검증 오류는 DRF 예외 핸들러를 타서 §6 표준 envelope 로 나간다.
        assert resp.data["success"] is False
        assert "code" in resp.data["error"]["details"]

    def test_code_without_redirect_uri_is_400(self, client):
        resp = client.post(URL, {"code": "abc"}, format="json")
        assert resp.status_code == 400
        assert "redirect_uri" in resp.data["error"]["details"]

    def test_both_code_and_token_is_400(self, client):
        """어느 쪽이 검증됐는지 모르게 되므로 조용히 고르지 않고 거절한다."""
        resp = client.post(
            URL,
            {"code": "abc", "redirect_uri": "https://x/y", "access_token": "t"},
            format="json",
        )
        assert resp.status_code == 400

    def test_access_token_only_is_accepted(self, client, monkeypatch):
        email = _email()
        _patch_resolve(monkeypatch, _profile(email))
        resp = client.post(URL, {"access_token": "t"}, format="json")
        assert resp.status_code == 200, resp.data


@pytest.mark.django_db
class TestKakaoLoginSignup:
    def test_new_user_is_created_with_tokens(self, client, monkeypatch):
        email = _email()
        _patch_resolve(monkeypatch, _profile(email, nickname="홍길동"))

        resp = client.post(
            URL,
            {"code": "c", "redirect_uri": "https://app.turnflow.link/auth/kakao/callback"},
            format="json",
        )

        assert resp.status_code == 200, resp.data
        assert resp.data["is_new_user"] is True
        assert resp.data["tokens"]["access"]
        user = User.objects.get(email=email)
        assert user.full_name == "홍길동"
        assert user.is_email_verified is True
        # 소셜 전용 계정 — 비밀번호 로그인이 되면 안 된다.
        assert not user.has_usable_password()

    def test_kakao_id_is_stored_on_signup(self, client, monkeypatch):
        email = _email()
        _patch_resolve(monkeypatch, _profile(email, kakao_id=987654321))
        client.post(URL, {"access_token": "t"}, format="json")
        assert User.objects.get(email=email).kakao_id == "987654321"

    def test_signup_attribution_recorded_as_kakao(self, client, monkeypatch):
        from apps.analytics.models import SignupAttribution

        email = _email()
        _patch_resolve(monkeypatch, _profile(email))
        client.post(
            URL,
            {"access_token": "t", "attribution": {"utm_source": "meta"}},
            format="json",
        )
        row = SignupAttribution.objects.get(user__email=email)
        assert row.signup_kind == "kakao"

    def test_marketing_opt_in_applies_only_on_signup(self, client, monkeypatch):
        email = _email()
        profile = _profile(email)
        _patch_resolve(monkeypatch, profile)

        client.post(URL, {"access_token": "t", "marketing_opt_in": True}, format="json")
        user = User.objects.get(email=email)
        assert user.marketing_opt_in is True

        # 재로그인에서 false 를 보내도 기존 동의를 뒤집지 않는다.
        client.post(URL, {"access_token": "t", "marketing_opt_in": False}, format="json")
        user.refresh_from_db()
        assert user.marketing_opt_in is True


@pytest.mark.django_db
class TestKakaoLoginExistingUser:
    def test_second_login_is_not_new_user(self, client, monkeypatch):
        email = _email()
        _patch_resolve(monkeypatch, _profile(email, kakao_id=111222333))

        first = client.post(URL, {"access_token": "t"}, format="json")
        second = client.post(URL, {"access_token": "t"}, format="json")

        assert first.data["is_new_user"] is True
        assert second.data["is_new_user"] is False
        assert User.objects.filter(email=email).count() == 1

    def test_email_change_on_kakao_side_keeps_same_account(self, client, monkeypatch):
        """⭐ kakao_id 를 저장하는 이유 그 자체 — 이메일이 바뀌어도 계정이 갈라지면 안 된다."""
        old_email, new_email = _email(), _email()
        kakao_id = 555666777

        _patch_resolve(monkeypatch, _profile(old_email, kakao_id=kakao_id))
        client.post(URL, {"access_token": "t"}, format="json")
        user_id = User.objects.get(email=old_email).id

        _patch_resolve(monkeypatch, _profile(new_email, kakao_id=kakao_id))
        resp = client.post(URL, {"access_token": "t"}, format="json")

        assert resp.data["is_new_user"] is False
        assert resp.data["user"]["id"] == user_id
        assert not User.objects.filter(email=new_email).exists()

    def test_existing_email_account_is_linked_when_verified(self, client, monkeypatch):
        """이메일 가입자가 카카오로 로그인하면 같은 계정에 붙고 kakao_id 가 채워진다."""
        email = _email()
        existing = User.objects.create_user(email=email, password="pw12345!")

        _patch_resolve(monkeypatch, _profile(email, kakao_id=444555666, verified=True))
        resp = client.post(URL, {"access_token": "t"}, format="json")

        assert resp.status_code == 200
        assert resp.data["is_new_user"] is False
        existing.refresh_from_db()
        assert existing.kakao_id == "444555666"

    def test_unverified_email_cannot_link_to_existing_account(self, client, monkeypatch):
        """계정 탈취 방지 — 카카오가 소유 확인을 못 해준 이메일로 기존 계정을 열 수 없다."""
        email = _email()
        User.objects.create_user(email=email, password="pw12345!")

        _patch_resolve(monkeypatch, _profile(email, verified=False))
        resp = client.post(URL, {"access_token": "t"}, format="json")

        assert resp.status_code == 403
        assert resp.data["code"] == "KAKAO_EMAIL_UNVERIFIED"
        # §6 envelope 도 함께 나가야 한다 (한 URL 이 두 포맷을 내는 함정 방지).
        assert resp.data["error"]["details"]["code"] == "KAKAO_EMAIL_UNVERIFIED"

    def test_unverified_email_can_still_create_new_account(self, client, monkeypatch):
        """막는 것은 '기존 계정 연결'뿐 — 신규 가입까지 막으면 OAuth 의 이점이 사라진다."""
        email = _email()
        _patch_resolve(monkeypatch, _profile(email, verified=False))

        resp = client.post(URL, {"access_token": "t"}, format="json")

        assert resp.status_code == 200
        user = User.objects.get(email=email)
        # 미확인 이메일로 우리 쪽 '인증됨' 표시를 켜면 안 된다.
        assert user.is_email_verified is False

    def test_pending_deletion_account_gets_409_not_tokens(self, client, monkeypatch):
        from django.utils import timezone

        email = _email()
        user = User.objects.create_user(email=email, password="pw12345!")
        user.is_active = False
        user.deletion_requested_at = timezone.now()
        user.deletion_scheduled_at = timezone.now() + timezone.timedelta(days=7)
        user.save()

        _patch_resolve(monkeypatch, _profile(email))
        resp = client.post(URL, {"access_token": "t"}, format="json")

        assert resp.status_code == 409
        assert resp.data["error"]["details"]["code"] == "account_deletion_pending"
        assert "tokens" not in resp.data


@pytest.mark.django_db
class TestKakaoErrorsPassThrough:
    @pytest.mark.parametrize(
        "err,expected_status",
        [
            (KakaoError("KAKAO_CODE_INVALID", "무효"), 400),
            (KakaoError("KAKAO_EMAIL_REQUIRED", "이메일 동의 필요"), 400),
            (KakaoError("KAKAO_TOKEN_FOREIGN_APP", "다른 앱", status=403), 403),
            (KakaoError("KAKAO_UNAVAILABLE", "카카오 장애", status=502), 502),
            (KakaoError("KAKAO_NOT_CONFIGURED", "미설정", status=503), 503),
        ],
    )
    def test_kakao_error_maps_to_status_and_code(self, client, monkeypatch, err, expected_status):
        _patch_resolve(monkeypatch, err)
        resp = client.post(URL, {"access_token": "t"}, format="json")
        assert resp.status_code == expected_status
        assert resp.data["code"] == err.code
        # detail(구글과 같은 자리) + envelope(§6) 를 동시에 내야 한다.
        assert resp.data["detail"] == err.message
        assert resp.data["error"]["details"]["code"] == err.code
        assert not User.objects.filter(kakao_id__isnull=False, email="").exists()


class TestKakaoClientGuards:
    """`kakao.py` 자체의 방어 — 뷰를 거치지 않는 단위 검증."""

    def test_foreign_app_token_is_rejected(self, monkeypatch, settings):
        """⭐ 이 검사가 빠지면 아무 카카오 앱 개발자나 남의 계정으로 로그인할 수 있다."""
        from apps.authentication import kakao

        settings.KAKAO_APP_ID = "1573264"
        monkeypatch.setattr(kakao, "_get_json", lambda *a, **k: {"app_id": 999999})

        with pytest.raises(KakaoError) as exc:
            kakao.assert_token_belongs_to_us("someone-elses-token")
        assert exc.value.code == "KAKAO_TOKEN_FOREIGN_APP"

    def test_missing_app_id_fails_closed(self, monkeypatch, settings):
        """설정 누락 시 통과시키면 검증이 조용히 꺼진다 — 반드시 막혀야 한다."""
        from apps.authentication import kakao

        settings.KAKAO_APP_ID = ""
        with pytest.raises(KakaoError) as exc:
            kakao.assert_token_belongs_to_us("t")
        assert exc.value.code == "KAKAO_NOT_CONFIGURED"

    def test_email_needs_agreement_is_rejected(self, monkeypatch):
        from apps.authentication import kakao

        monkeypatch.setattr(
            kakao,
            "_get_json",
            lambda *a, **k: {
                "id": 123,
                "kakao_account": {"email_needs_agreement": True, "profile": {"nickname": "n"}},
            },
        )
        with pytest.raises(KakaoError) as exc:
            kakao.fetch_profile("t")
        assert exc.value.code == "KAKAO_EMAIL_REQUIRED"

    def test_client_secret_is_sent_on_exchange(self, monkeypatch, settings):
        """콘솔이 '클라이언트 시크릿 ON' 이라 빠지면 카카오가 KOE010 으로 거절한다."""
        from apps.authentication import kakao

        settings.KAKAO_REST_API_KEY = "restkey"
        settings.KAKAO_CLIENT_SECRET = "secret"
        captured = {}

        def _fake_post(url, data):
            captured.update(data)
            return {"access_token": "at"}

        monkeypatch.setattr(kakao, "_post_form", _fake_post)
        assert kakao.exchange_code("code", "https://app/cb") == "at"
        assert captured["client_secret"] == "secret"
        assert captured["client_id"] == "restkey"
        assert captured["redirect_uri"] == "https://app/cb"

    def test_email_is_normalised_lowercase(self, monkeypatch):
        from apps.authentication import kakao

        monkeypatch.setattr(
            kakao,
            "_get_json",
            lambda *a, **k: {
                "id": 7,
                "kakao_account": {
                    "email": "  MixedCase@Example.COM ",
                    "is_email_verified": True,
                    "profile": {"nickname": "n"},
                },
            },
        )
        assert kakao.fetch_profile("t").email == "mixedcase@example.com"


class TestKakaoThrottleWiring:
    """⚠️ 이 프로젝트의 스로틀은 fail-open — 뷰의 scope 이름이 settings 에 없으면
    예외 없이 **조용히 꺼진다**. 이름을 여기서 못 박는다 (account_deletion 과 같은 이유)."""

    def test_scope_is_registered_in_settings(self, settings):
        from apps.authentication.kakao_views import KakaoLoginView

        scope = KakaoLoginView.throttle_scope
        assert scope == "auth_kakao"
        assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope]
