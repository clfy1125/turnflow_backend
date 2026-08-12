"""구글 로그인 email_verified 검증 회귀 테스트 (감사 M-1).

## 이 테스트가 막는 것

구글 ID 토큰의 ``email_verified`` 클레임을 안 보고 ``email`` 만으로 기존 계정을 찾아
로그인시키던 동작 = **계정 탈취**. 공격자가 자기 소유 도메인으로 워크스페이스를 만들어
피해자 이메일을 적어 넣으면, 구글은 ``email_verified: false`` 를 실어 보내는데
우리가 그걸 버리고 있었다.

## 고장난 버전에 대고 검증했는가 (필수 절차)

했다. ``email_verified`` 검증을 넣기 전 코드(HEAD~)로 되돌려 이 파일을 돌리면
``test_unverified_email_cannot_take_over_existing_account`` 가 **200 을 받아 실패**한다
(= 공격이 성립함을 테스트가 재현). 이걸 확인하지 않으면 "통과" 가 아무 의미도 없다.
자세한 절차는 [[validate-detectors-against-broken-version]] 참고.
"""

import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

User = get_user_model()

GOOGLE_ISS = "https://accounts.google.com"
# reverse("google-login") 은 네임스페이스 때문에 안 잡힌다 → 실제 경로를 직접 쓴다.
# 경로가 바뀌면 이 테스트가 404 로 실패하므로 계약 변경을 알아챌 수 있다.
GOOGLE_LOGIN_URL = "/api/v1/auth/google/"


def _idinfo(email: str, *, verified: bool, name: str = "테스터"):
    """구글이 돌려주는 ID 토큰 페이로드 모양."""
    return {
        "iss": GOOGLE_ISS,
        "email": email,
        "email_verified": verified,
        "name": name,
        "sub": "1234567890",
    }


def _post(client, idinfo):
    """verify_oauth2_token 을 가로채 원하는 페이로드를 흘려보낸다."""
    with patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value=idinfo,
    ):
        return client.post(
            GOOGLE_LOGIN_URL,
            {"token": "dummy-token-value"},
            format="json",
        )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def unique_email():
    # 테스트 DB 가 깨끗하지 않아(=dev DB 공유) 고정 이메일은 충돌한다 → uuid
    return f"gverify-{uuid.uuid4().hex[:12]}@example.com"


@pytest.mark.django_db
def test_unverified_email_cannot_take_over_existing_account(client, unique_email):
    """★핵심: 미확인 이메일로는 기존 계정에 절대 붙지 못한다.

    수정 전 코드에서는 여기서 200 + 피해자 계정의 토큰이 나왔다.
    """
    victim = User.objects.create_user(
        email=unique_email, password="VictimPw123!", full_name="피해자"
    )

    res = _post(client, _idinfo(unique_email, verified=False))

    assert (
        res.status_code == status.HTTP_403_FORBIDDEN
    ), f"미확인 이메일이 기존 계정으로 로그인됐다 (계정 탈취) — status={res.status_code}"
    assert res.data.get("code") == "GOOGLE_EMAIL_UNVERIFIED"
    # 토큰이 절대 나가면 안 된다
    assert "tokens" not in res.data

    # 피해자 계정 상태가 오염되지 않았는지
    victim.refresh_from_db()
    assert victim.full_name == "피해자"


@pytest.mark.django_db
def test_verified_email_still_links_to_existing_account(client, unique_email):
    """검증된 이메일이면 종전과 100% 동일하게 기존 계정에 로그인된다 (무회귀)."""
    User.objects.create_user(email=unique_email, password="Pw123456!", full_name="기존회원")

    res = _post(client, _idinfo(unique_email, verified=True))

    assert res.status_code == status.HTTP_200_OK, res.data
    assert res.data["user"]["email"] == unique_email
    assert res.data["tokens"]["access"]


@pytest.mark.django_db
def test_verified_email_creates_new_account_and_marks_verified(client, unique_email):
    """신규 가입 + 인증 표시 ON — 이것도 종전과 동일해야 한다."""
    res = _post(client, _idinfo(unique_email, verified=True))

    assert res.status_code == status.HTTP_200_OK, res.data
    user = User.objects.get(email=unique_email)
    assert user.is_email_verified is True
    assert user.email_verified_at is not None


@pytest.mark.django_db
def test_unverified_email_may_create_new_account_but_not_marked_verified(client, unique_email):
    """기존 계정이 없으면 가입은 막지 않는다 — 다만 '인증됨' 표시는 켜지 않는다.

    로그인 자체를 거절하면 OAuth 의 이점이 사라지므로, 막는 건 '기존 계정 연결'뿐이다.
    """
    res = _post(client, _idinfo(unique_email, verified=False))

    assert res.status_code == status.HTTP_200_OK, res.data
    user = User.objects.get(email=unique_email)
    assert user.is_email_verified is False, "미확인 이메일인데 인증됨으로 승격됐다"
    assert user.email_verified_at is None


@pytest.mark.django_db
def test_missing_email_verified_claim_is_treated_as_unverified(client, unique_email):
    """클레임 자체가 없으면 '미확인'으로 본다 (fail-closed).

    구글이 항상 보내주지만, 없을 때 통과시키면 검증을 우회하는 구멍이 된다.
    """
    User.objects.create_user(email=unique_email, password="Pw123456!")

    idinfo = _idinfo(unique_email, verified=True)
    del idinfo["email_verified"]

    res = _post(client, idinfo)
    assert res.status_code == status.HTTP_403_FORBIDDEN
