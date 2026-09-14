"""이메일 등록·인증 (인스타 로그인 사용자) — 자리표시 전용 게이트 + 2단 확정.

지키려는 계약 넷:
  1. **자리표시 이메일 계정만** — 일반 "이메일 변경" 기능이 아니다(세션 탈취 → 계정 탈취 방지)
  2. 코드가 맞기 전까지 ``email`` 은 **바뀌지 않는다** (오타로 로그인 키를 잃지 않게)
  3. 코드는 1회용, 재신청하면 이전 코드는 즉시 무효
  4. 메일은 **새 주소로** 간다 (기존 주소는 자리표시라 도착할 곳이 없다)
"""

import uuid

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.authentication.models import PLACEHOLDER_EMAIL_DOMAIN
from apps.emails.models import EmailToken, EmailTokenPurpose

REGISTER = "authentication:me-email-register"
VERIFY = "authentication:me-email-verify"


def _new_email():
    return f"real-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """스로틀(5/hour)을 끈다 — 카운터가 dev Redis 라 테스트 실행 사이에 샌다.

    스로틀 **배선**은 TestEmailRegisterThrottleWiring 이 따로 지킨다.
    """
    monkeypatch.setattr(
        "rest_framework.throttling.SimpleRateThrottle.allow_request",
        lambda self, request, view: True,
    )


@pytest.fixture
def ig_user(django_user_model):
    """인스타로 가입한(=자리표시 이메일) 사용자."""
    ig_id = uuid.uuid4().hex[:12]
    user = django_user_model.objects.create(
        email=f"ig_{ig_id}@{PLACEHOLDER_EMAIL_DOMAIN}",
        instagram_user_id=ig_id,
        full_name="테스트",
    )
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture
def client(ig_user):
    c = APIClient()
    c.force_authenticate(user=ig_user)
    return c


def _live_code(user):
    row = (
        EmailToken.objects.filter(
            user=user, purpose=EmailTokenPurpose.EMAIL_CHANGE, used_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    return row.code if row else None


@pytest.mark.django_db
class TestEmailRegister:
    def test_requires_authentication(self):
        assert (
            APIClient().post(reverse(REGISTER), {"email": "a@b.com"}, format="json").status_code
            == 401
        )

    def test_request_stores_pending_without_touching_email(self, client, ig_user):
        new = _new_email()
        res = client.post(reverse(REGISTER), {"email": new}, format="json")

        assert res.status_code == 202, res.json()
        assert res.json()["pending_email"] == new
        ig_user.refresh_from_db()
        # ⚠️ 여기가 핵심 — 인증 전에 email 을 바꾸면 오타 하나로 로그인 키를 잃는다
        assert ig_user.pending_email == new
        assert ig_user.email_is_placeholder is True
        assert ig_user.is_email_verified is False
        assert _live_code(ig_user) is not None

    def test_verify_promotes_pending_to_email(self, client, ig_user):
        new = _new_email()
        client.post(reverse(REGISTER), {"email": new}, format="json")
        code = _live_code(ig_user)

        res = client.post(reverse(VERIFY), {"code": code}, format="json")

        assert res.status_code == 200, res.json()
        body = res.json()["user"]
        assert body["email"] == new
        assert body["email_is_placeholder"] is False
        assert body["pending_email"] == ""
        assert body["is_email_verified"] is True

        ig_user.refresh_from_db()
        assert ig_user.email == new
        assert ig_user.pending_email == ""

    def test_wrong_code_changes_nothing(self, client, ig_user):
        new = _new_email()
        client.post(reverse(REGISTER), {"email": new}, format="json")

        res = client.post(reverse(VERIFY), {"code": "000000"}, format="json")

        assert res.status_code == 400
        assert res.json()["code"] == "EMAIL_CODE_INVALID"
        ig_user.refresh_from_db()
        assert ig_user.email_is_placeholder is True
        assert ig_user.pending_email == new  # 신청은 살아 있다 — 다시 입력하면 된다

    def test_code_is_single_use(self, client, ig_user):
        client.post(reverse(REGISTER), {"email": _new_email()}, format="json")
        code = _live_code(ig_user)
        assert client.post(reverse(VERIFY), {"code": code}, format="json").status_code == 200

        again = client.post(reverse(VERIFY), {"code": code}, format="json")
        assert again.status_code == 400

    def test_rerequest_invalidates_previous_code(self, client, ig_user):
        """직전 주소로 받은 코드로 **새 주소**를 확정해 버리는 것을 막는다."""
        client.post(reverse(REGISTER), {"email": _new_email()}, format="json")
        first_code = _live_code(ig_user)

        second = _new_email()
        client.post(reverse(REGISTER), {"email": second}, format="json")

        res = client.post(reverse(VERIFY), {"code": first_code}, format="json")
        assert res.status_code == 400
        ig_user.refresh_from_db()
        assert ig_user.email_is_placeholder is True

        # 새 코드는 정상 동작
        assert (
            client.post(reverse(VERIFY), {"code": _live_code(ig_user)}, format="json").status_code
            == 200
        )
        ig_user.refresh_from_db()
        assert ig_user.email == second

    def test_verify_without_request_is_rejected(self, client):
        res = client.post(reverse(VERIFY), {"code": "123456"}, format="json")
        assert res.status_code == 400
        assert res.json()["code"] == "NO_PENDING_EMAIL"

    def test_duplicate_email_is_rejected(self, client, django_user_model):
        taken = _new_email()
        django_user_model.objects.create_user(email=taken, password="Pass1234!")

        res = client.post(reverse(REGISTER), {"email": taken}, format="json")
        assert res.status_code == 409
        assert res.json()["code"] == "EMAIL_ALREADY_TAKEN"

    def test_non_placeholder_account_is_refused(self, django_user_model):
        normal = django_user_model.objects.create_user(
            email=f"normal-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
        )
        c = APIClient()
        c.force_authenticate(user=normal)

        res = c.post(reverse(REGISTER), {"email": _new_email()}, format="json")
        assert res.status_code == 400
        assert res.json()["code"] == "EMAIL_CHANGE_NOT_ALLOWED"

    def test_cannot_change_again_after_registering(self, client, ig_user):
        """등록이 끝나면 자리표시가 아니게 되어 이 API 는 닫힌다(일반 변경 기능이 아니다)."""
        client.post(reverse(REGISTER), {"email": _new_email()}, format="json")
        client.post(reverse(VERIFY), {"code": _live_code(ig_user)}, format="json")

        res = client.post(reverse(REGISTER), {"email": _new_email()}, format="json")
        assert res.status_code == 400
        assert res.json()["code"] == "EMAIL_CHANGE_NOT_ALLOWED"


@pytest.mark.django_db
class TestEmailChangeMail:
    def test_code_mail_goes_to_the_new_address(self, ig_user):
        """기존 주소는 자리표시라 도착할 곳이 없다 — 반드시 **새 주소로** 가야 한다."""
        from apps.emails.models import EmailLog
        from apps.emails.tasks import send_email_change_code

        new = _new_email()
        send_email_change_code(ig_user.id, new, "482913", 30)

        log = EmailLog.objects.filter(user=ig_user).order_by("-id").first()
        assert log is not None
        assert log.to_email == new
        assert not log.to_email.endswith(PLACEHOLDER_EMAIL_DOMAIN)
        assert "482913" in log.rendered_html
        assert new in log.rendered_html
        # 템플릿 변수가 통째로 남아 있으면 사용자에게 `{{ ... }}` 가 그대로 보인다
        assert "{{" not in log.rendered_html


@pytest.mark.django_db
class TestEmailRegisterThrottleWiring:
    """⚠️ 이 프로젝트의 스로틀은 fail-open — scope 이름이 settings 에 없으면 조용히 꺼진다."""

    def test_scopes_are_registered(self, settings):
        """뷰의 scope 이름이 settings 의 **키로 존재**하는지만 본다.

        ⚠️ 값이 truthy 인지는 보지 않는다 — local.py 가 인증 계열 스로틀을 dev/테스트에서
           의도적으로 None 으로 덮기 때문이다(테스트가 IP 카운터를 태워 429 로 깨지는 것을
           막으려고). 오타를 잡는 것은 **키 존재** 여부이고, 실제 값은 base.py 가 정한다.
        """
        from apps.authentication.email_views import EmailRegisterVerifyView, EmailRegisterView

        assert EmailRegisterView.throttle_scope == "email_change"
        assert EmailRegisterVerifyView.throttle_scope == "email_verify"
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        assert "email_change" in rates
        assert "email_verify" in rates
