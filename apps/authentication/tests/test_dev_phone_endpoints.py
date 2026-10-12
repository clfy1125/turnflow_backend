"""dev 전용 휴대폰 엔드포인트 — **운영에서 절대 열리면 안 된다**가 핵심.

이 엔드포인트들은 인증이 없고 계정을 만들고 인증번호까지 심는다. 게이트가 뚫리면
누구나 아무 계정의 인증을 통과할 수 있다. 그래서 게이트를 테스트가 못 박는다.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def dev(settings):
    settings.DEBUG = True
    settings.SETTINGS_MODULE = "config.settings.local"
    settings.SMS_MOCK_MODE = True
    settings.PHONE_REQUIRED_SINCE = "2026-10-12T00:00:00+09:00"
    return settings


class TestProductionGate:
    """⚠️ 하나라도 깨지면 운영에 dev 백도어가 열린 것이다."""

    URLS = [
        ("get", "authentication:dev-phone-test-accounts"),
        ("post", "authentication:dev-phone-test-accounts-reseed"),
        ("post", "authentication:dev-phone-code"),
    ]

    @pytest.mark.parametrize("method,name", URLS)
    def test_404_when_debug_off(self, settings, method, name):
        settings.DEBUG = False
        res = getattr(APIClient(), method)(reverse(name), {}, format="json")
        assert res.status_code == 404

    @pytest.mark.parametrize("method,name", URLS)
    def test_404_when_prod_settings_even_if_debug_on(self, settings, method, name):
        """DEBUG 가 실수로 켜져도 prod 설정이면 막는다 (2중 잠금)."""
        settings.DEBUG = True
        settings.SETTINGS_MODULE = "config.settings.prod"
        res = getattr(APIClient(), method)(reverse(name), {}, format="json")
        assert res.status_code == 404


class TestSeeder:
    def test_reseed_creates_all_states(self, dev):
        res = APIClient().post(
            reverse("authentication:dev-phone-test-accounts-reseed"), {}, format="json"
        )
        assert res.status_code == 200
        accounts = {a["code"]: a for a in res.data["accounts"]}
        assert set(accounts) == {"P01", "P02", "P03", "P04", "P05", "P06"}

        # 각 코드가 의도한 상태인지 — 프론트 문서가 약속하는 값 그대로
        assert accounts["P01"]["state"]["phone_verification_required"] is True
        assert accounts["P01"]["state"]["reward"]["kind"] == "none"
        assert accounts["P02"]["state"]["reward"]["kind"] == "trial_extended"
        assert accounts["P02"]["state"]["reward"]["days"] == 7
        assert accounts["P03"]["state"]["reward"]["kind"] == "trial_granted"
        assert accounts["P04"]["state"]["reward"]["kind"] == "none"
        assert accounts["P05"]["state"]["phone_verified"] is True
        assert accounts["P06"]["state"]["phone_verified"] is True

    def test_seeded_phone_is_a_valid_korean_mobile(self, dev):
        """짧은 번호를 심으면 프론트가 그 번호로 재인증할 때 400 이 난다."""
        from apps.authentication.phone import normalize_phone

        res = APIClient().post(
            reverse("authentication:dev-phone-test-accounts-reseed"),
            {"codes": ["P05"]},
            format="json",
        )
        phone = res.data["accounts"][0]["state"]["phone"]
        assert normalize_phone(phone)  # 거절되면 InvalidPhoneNumber 로 터진다

    def test_unknown_code_is_rejected(self, dev):
        res = APIClient().post(
            reverse("authentication:dev-phone-test-accounts-reseed"),
            {"codes": ["ZZZ"]},
            format="json",
        )
        assert res.status_code == 400
        assert res.data["code"] == "UNKNOWN_CODE"

    def test_list_reports_required_since_configured(self, dev):
        res = APIClient().get(reverse("authentication:dev-phone-test-accounts"))
        assert res.status_code == 200
        assert res.data["phone_required_configured"] is True

        dev.PHONE_REQUIRED_SINCE = ""
        res = APIClient().get(reverse("authentication:dev-phone-test-accounts"))
        assert res.data["phone_required_configured"] is False, (
            "PHONE_REQUIRED_SINCE 가 비었는데 설정됐다고 보고하면 프론트가 "
            "'필수 화면이 왜 안 뜨지'를 한참 헤맨다"
        )


class TestCodePlanting:
    def _login(self, email):
        c = APIClient()
        res = c.post(
            reverse("authentication:login"),
            {"email": email, "password": "Test1234!"},
            format="json",
        )
        c.credentials(HTTP_AUTHORIZATION="Bearer " + res.data["tokens"]["access"])
        return c

    def test_full_flow(self, dev):
        from apps.authentication.dev_phone_views import DEV_FIXED_CODE

        APIClient().post(
            reverse("authentication:dev-phone-test-accounts-reseed"),
            {"codes": ["P01"]},
            format="json",
        )
        email = "p01-required@test.turnflow.link"
        c = self._login(email)

        assert (
            c.post(
                reverse("authentication:me-phone"), {"phone": "010-7777-8888"}, format="json"
            ).status_code
            == 202
        )

        res = APIClient().post(
            reverse("authentication:dev-phone-code"), {"email": email}, format="json"
        )
        assert res.status_code == 200
        assert res.data["code"] == DEV_FIXED_CODE

        done = c.post(
            reverse("authentication:me-phone-verify"), {"code": DEV_FIXED_CODE}, format="json"
        )
        assert done.status_code == 200
        assert done.data["user"]["phone_verified"] is True

    def test_without_pending_send_it_refuses(self, dev):
        APIClient().post(
            reverse("authentication:dev-phone-test-accounts-reseed"),
            {"codes": ["P04"]},
            format="json",
        )
        res = APIClient().post(
            reverse("authentication:dev-phone-code"),
            {"email": "p04-noreward@test.turnflow.link"},
            format="json",
        )
        assert res.status_code == 400
        assert res.data["code"] == "NO_PENDING_PHONE_VERIFICATION"

    def test_unknown_email(self, dev):
        res = APIClient().post(
            reverse("authentication:dev-phone-code"),
            {"email": "nobody@nowhere.invalid"},
            format="json",
        )
        assert res.status_code == 404
        assert res.data["code"] == "USER_NOT_FOUND"

    def test_plaintext_is_still_not_stored(self, dev):
        """dev 편의가 저장 모델을 바꾸지 않았는지 — 해시만 바뀌어야 한다."""
        from apps.authentication.dev_phone_views import DEV_FIXED_CODE
        from apps.authentication.models import PhoneVerification
        from apps.authentication.phone import code_matches

        APIClient().post(
            reverse("authentication:dev-phone-test-accounts-reseed"),
            {"codes": ["P01"]},
            format="json",
        )
        email = "p01-required@test.turnflow.link"
        c = self._login(email)
        c.post(reverse("authentication:me-phone"), {"phone": "010-7777-8888"}, format="json")
        APIClient().post(reverse("authentication:dev-phone-code"), {"email": email}, format="json")

        row = PhoneVerification.objects.filter(user__email=email).latest("created_at")
        assert len(row.code_hash) == 64
        assert DEV_FIXED_CODE not in row.code_hash
        assert code_matches(row.phone, DEV_FIXED_CODE, row.code_hash)
