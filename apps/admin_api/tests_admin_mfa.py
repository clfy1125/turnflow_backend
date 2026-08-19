"""어드민 2단계 로그인(MFA) 테스트.

대상:
- apps/admin_api/auth/ (totp · tokens · challenge · devices · views · views_manage)
- apps/admin_api/gate.py · middleware.py (어드민 토큰 게이트)

주의:
- 파일명이 tests_*.py 라 **경로 명시 실행** 필요:
  ``pytest apps/admin_api/tests_admin_mfa.py``.
- 게이트는 **미들웨어**가 하므로 DRF ``force_authenticate`` 로는 재현되지 않는다 →
  게이트 테스트는 Django test Client + 실제 Bearer 헤더를 쓴다.
- pytest DB 가 dev DB 와 같아 계정 이메일은 uuid 로 만든다(중복 방지).
- ``ADMIN_MFA_ENFORCED`` 는 클래스 데코레이터가 아니라 ``settings`` 픽스처로 켠다
  (이 저장소에서 override_settings 클래스 데코레이터가 신뢰할 수 없다는 이력이 있다).
"""

from __future__ import annotations

import time
import uuid

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone

from apps.admin_api.auth import totp as totp_service
from apps.admin_api.auth.tokens import issue_admin_tokens
from apps.admin_api.models import AdminActionLog, AdminBackupCode, AdminDevice, AdminMFADevice
from apps.admin_api.roles import ROLE_MARKETING_VIEWER
from apps.authentication.tokens import AppRefreshToken
from apps.emails.models import EmailToken, EmailTokenPurpose

User = get_user_model()

LOGIN_URL = "/api/v1/admin/auth/login/"
VERIFY_URL = "/api/v1/admin/auth/mfa/verify/"
REFRESH_URL = "/api/v1/admin/auth/refresh/"
SETUP_URL = "/api/v1/admin/auth/mfa/setup/"
CONFIRM_URL = "/api/v1/admin/auth/mfa/confirm/"
STATUS_URL = "/api/v1/admin/auth/mfa/status/"
REGEN_URL = "/api/v1/admin/auth/mfa/backup-codes/regenerate/"
ME_URL = "/api/v1/admin/me/"

PASSWORD = "Pass1234!"
DEVICE = "test-device-0001"


# ── 헬퍼 ──────────────────────────────────────────────────────────────────


def _mk_staff(*, superuser: bool = False) -> User:
    return User.objects.create_user(
        email=f"mfa-{uuid.uuid4().hex[:10]}@test.com",
        password=PASSWORD,
        is_staff=True,
        is_superuser=superuser,
    )


def _enroll(user, *, secret: str | None = None) -> str:
    """TOTP 등록을 완료 상태로 만들고 시드를 돌려준다 (등록 플로우를 거치지 않는 지름길)."""
    secret = secret or totp_service.generate_secret()
    device, _ = AdminMFADevice.objects.get_or_create(user=user)
    device.secret = secret
    device.pending_secret = ""
    device.confirmed_at = timezone.now()
    device.save()
    return secret


def _trust(user, device_id: str = DEVICE) -> AdminDevice:
    device, _ = AdminDevice.objects.get_or_create(user=user, device_id=device_id)
    device.trusted_at = timezone.now()
    device.revoked_at = None
    device.save()
    return device


def _code(secret: str, *, offset_steps: int = 0) -> str:
    """현재(또는 오프셋) 스텝의 TOTP 코드."""
    return pyotp.TOTP(secret).at(int(time.time()) + offset_steps * 30)


def _post(client: Client, url: str, body: dict, *, token: str = "") -> tuple[int, dict]:
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    res = client.post(url, data=body, content_type="application/json", **headers)
    try:
        return res.status_code, res.json()
    except ValueError:  # 204 등 본문 없음
        return res.status_code, {}


def _detail_code(body: dict) -> str:
    return (body.get("error", {}).get("details", {}) or {}).get("code", "")


def _latest_email_code(user) -> str:
    row = (
        EmailToken.objects.filter(
            user=user, purpose=EmailTokenPurpose.ADMIN_DEVICE, used_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    return row.code if row else ""


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def email_gate_on(settings):
    """신규 기기 이메일 승인을 **켠 상태**로 만드는 픽스처.

    2026-08-20 부터 기본값이 꺼짐이라, 이메일 경로를 검증하는 테스트는 스스로 켜야 한다.
    (기능을 지우지 않고 플래그로 남긴 이유는 settings/base.py 의 그 플래그 주석 참고.)
    """
    settings.ADMIN_MFA_EMAIL_DEVICE_CODE_ENABLED = True
    return settings


# ── 1단계 로그인 ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAdminLogin:
    def test_wrong_password_is_401_without_leaking_cause(self, client):
        user = _mk_staff()
        _enroll(user)
        code, body = _post(client, LOGIN_URL, {"email": user.email, "password": "nope"})
        assert code == 401
        assert _detail_code(body) == "invalid_credentials"

    def test_non_staff_gets_same_401_as_wrong_password(self, client):
        """계정 열거 방지 — 비스태프에게 '비밀번호는 맞다'를 알려주지 않는다."""
        user = User.objects.create_user(
            email=f"plain-{uuid.uuid4().hex[:8]}@test.com", password=PASSWORD
        )
        code, body = _post(client, LOGIN_URL, {"email": user.email, "password": PASSWORD})
        assert code == 401
        assert _detail_code(body) == "invalid_credentials"

    def test_unenrolled_admin_gets_setup_token(self, client):
        user = _mk_staff()
        code, body = _post(client, LOGIN_URL, {"email": user.email, "password": PASSWORD})
        assert code == 403
        assert _detail_code(body) == "mfa_setup_required"
        assert body["error"]["details"]["setup_token"]

    def test_trusted_device_needs_totp_only(self, client):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        code, body = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": DEVICE}
        )
        assert code == 200
        assert body["mfa_required"] is True
        assert body["device_verification_required"] is False
        assert "email" not in body["methods"]
        assert "tokens" not in body

    def test_new_device_requires_email_code_when_gate_on(self, client, email_gate_on):
        user = _mk_staff()
        _enroll(user)
        code, body = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": "brand-new"}
        )
        assert code == 200
        assert body["device_verification_required"] is True
        assert "email" in body["methods"]
        assert body["email_masked"]
        # 메일 코드가 실제로 발급됐다
        assert _latest_email_code(user)

    def test_new_device_needs_totp_only_by_default(self, client):
        """기본값(이메일 승인 꺼짐) — 처음 보는 기기여도 인증앱 코드 하나로 끝난다.

        관리자 3명 전원이 인증앱을 등록한 뒤로 이메일 코드는 보안을 더하지 못하면서
        실패 지점만 늘렸다(prod 에서 신뢰 등록 실패 기기 행이 5개 쌓였다).
        """
        user = _mk_staff()
        _enroll(user)
        code, body = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": "brand-new"}
        )
        assert code == 200
        assert body["device_verification_required"] is False
        assert "email" not in body["methods"]
        # 메일을 아예 발급하지 않는다 — 발송 실패·지연이 로그인을 막을 여지 자체를 없앤다.
        assert not _latest_email_code(user)

    def test_missing_device_id_is_issued_by_server(self, client):
        user = _mk_staff()
        _enroll(user)
        code, body = _post(client, LOGIN_URL, {"email": user.email, "password": PASSWORD})
        assert code == 200
        assert body["device_id"]

    def test_marketing_viewer_skips_mfa_and_gets_tokens(self, client):
        """외주 계정은 2단계 대상이 아니다 — 프론트가 경로를 하나로 유지할 수 있어야 한다."""
        user = _mk_staff()
        group, _ = Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)
        user.groups.add(group)
        code, body = _post(client, LOGIN_URL, {"email": user.email, "password": PASSWORD})
        assert code == 200
        assert body["mfa_required"] is False
        assert body["tokens"]["access"]
        assert body["admin"]["admin_role"] == ROLE_MARKETING_VIEWER


# ── 2단계 검증 ────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAdminMfaVerify:
    def _start(self, client, user, device_id=DEVICE):
        _, body = _post(
            client,
            LOGIN_URL,
            {"email": user.email, "password": PASSWORD, "device_id": device_id},
        )
        return body["challenge"]

    def test_valid_totp_issues_admin_tokens(self, client):
        user = _mk_staff()
        secret = _enroll(user)
        _trust(user)
        challenge = self._start(client, user)
        code, body = _post(client, VERIFY_URL, {"challenge": challenge, "code": _code(secret)})
        assert code == 200, body
        assert body["tokens"]["access"] and body["tokens"]["refresh"]
        assert body["admin"]["email"] == user.email
        assert body["device_trusted"] is True

    def test_login_is_audited(self, client):
        user = _mk_staff()
        secret = _enroll(user)
        _trust(user)
        challenge = self._start(client, user)
        _post(client, VERIFY_URL, {"challenge": challenge, "code": _code(secret)})
        log = AdminActionLog.objects.filter(
            actor=user, action=AdminActionLog.Action.ADMIN_LOGIN
        ).first()
        assert log is not None
        assert log.changes["amr"] == ["pwd", "totp"]

    def test_totp_cannot_be_replayed(self, client):
        """같은 30초 창의 코드를 두 번 쓰지 못한다 — 재사용 방지의 핵심."""
        user = _mk_staff()
        secret = _enroll(user)
        _trust(user)
        otp = _code(secret)

        first = self._start(client, user)
        assert _post(client, VERIFY_URL, {"challenge": first, "code": otp})[0] == 200

        second = self._start(client, user)
        code, body = _post(client, VERIFY_URL, {"challenge": second, "code": otp})
        assert code == 400
        assert _detail_code(body) == "invalid_code"

    def test_wrong_code_is_invalid_code(self, client):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        challenge = self._start(client, user)
        code, body = _post(client, VERIFY_URL, {"challenge": challenge, "code": "000000"})
        assert code == 400
        assert _detail_code(body) == "invalid_code"

    def test_challenge_dies_after_max_attempts(self, client, settings):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        challenge = self._start(client, user)
        last = ""
        for _ in range(settings.ADMIN_MFA_CHALLENGE_MAX_ATTEMPTS):
            _, body = _post(client, VERIFY_URL, {"challenge": challenge, "code": "000000"})
            last = _detail_code(body)
        assert last == "challenge_expired"
        # 파기된 티켓은 올바른 코드로도 통과하지 못한다
        secret = AdminMFADevice.objects.get(user=user).secret
        code, body = _post(client, VERIFY_URL, {"challenge": challenge, "code": _code(secret)})
        assert code == 400
        assert _detail_code(body) == "challenge_expired"

    def test_unknown_challenge_is_expired(self, client):
        code, body = _post(client, VERIFY_URL, {"challenge": "nope", "code": "000000"})
        assert code == 400
        assert _detail_code(body) == "challenge_expired"

    def test_new_device_requires_email_code_too_when_gate_on(self, client, email_gate_on):
        user = _mk_staff()
        secret = _enroll(user)
        challenge = self._start(client, user, device_id="fresh-device")
        # 메일 코드 없이 TOTP 만 → 거부
        code, body = _post(client, VERIFY_URL, {"challenge": challenge, "code": _code(secret)})
        assert code == 400
        assert _detail_code(body) == "invalid_email_code"

    def test_new_device_passes_with_totp_only_by_default(self, client):
        """기본값 — 처음 보는 기기에서 인증앱 코드만으로 토큰이 나온다."""
        user = _mk_staff()
        secret = _enroll(user)
        challenge = self._start(client, user, device_id="fresh-device")
        code, body = _post(
            client,
            VERIFY_URL,
            {"challenge": challenge, "code": _code(secret), "remember_device": True},
        )
        assert code == 200, body
        assert body["tokens"]["access"]
        assert body["device_trusted"] is True

    def test_totp_accepts_one_minute_of_clock_drift(self, client):
        """폰 시계가 1분 어긋나도 통과한다 — ±30초는 사람이 옮겨 적는 시간까지 합치면 모자란다."""
        user = _mk_staff()
        secret = _enroll(user)
        _trust(user)
        for offset in (-2, 2):
            challenge = self._start(client, user)
            code, body = _post(
                client,
                VERIFY_URL,
                {"challenge": challenge, "code": _code(secret, offset_steps=offset)},
            )
            assert code == 200, (offset, body)

    def test_new_device_with_email_code_and_remember_becomes_trusted(self, client, email_gate_on):
        user = _mk_staff()
        secret = _enroll(user)
        challenge = self._start(client, user, device_id="fresh-device")
        code, body = _post(
            client,
            VERIFY_URL,
            {
                "challenge": challenge,
                "code": _code(secret),
                "email_code": _latest_email_code(user),
                "remember_device": True,
            },
        )
        assert code == 200, body
        assert body["device_trusted"] is True
        assert AdminDevice.objects.get(user=user, device_id="fresh-device").is_trusted

    def test_backup_code_works_once(self, client):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        codes = totp_service.issue_backup_codes(user)

        challenge = self._start(client, user)
        assert (
            _post(client, VERIFY_URL, {"challenge": challenge, "backup_code": codes[0]})[0] == 200
        )

        challenge = self._start(client, user)
        code, body = _post(client, VERIFY_URL, {"challenge": challenge, "backup_code": codes[0]})
        assert code == 400
        assert _detail_code(body) == "invalid_code"

    def test_backup_code_accepts_lowercase_and_no_hyphen(self, client):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        codes = totp_service.issue_backup_codes(user)
        messy = codes[0].replace("-", "").lower()
        challenge = self._start(client, user)
        assert _post(client, VERIFY_URL, {"challenge": challenge, "backup_code": messy})[0] == 200


# ── 등록 (setup → confirm) ────────────────────────────────────────────────


@pytest.mark.django_db
class TestAdminMfaEnrollment:
    def test_bootstrap_flow_end_to_end(self, client):
        """미등록 계정: 로그인 403 → setup → confirm → 토큰 + 백업코드 10개.

        이메일 승인이 꺼진 기본값이므로 ``email_code`` 없이 끝나야 한다 — 로그인과 등록이
        같은 판정(:func:`devices.needs_email_verification`)을 쓰는지 확인하는 자리다.
        """
        user = _mk_staff()
        _, login_body = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": DEVICE}
        )
        setup_token = login_body["error"]["details"]["setup_token"]

        code, setup = _post(client, SETUP_URL, {"setup_token": setup_token})
        assert code == 200, setup
        assert setup["secret"] and setup["otpauth_url"].startswith("otpauth://totp/")
        assert setup["qr_svg"].lstrip().startswith("<")
        assert setup["device_verification_required"] is False

        code, body = _post(
            client,
            CONFIRM_URL,
            {"setup_token": setup["setup_token"], "code": _code(setup["secret"])},
        )
        assert code == 200, body
        assert len(body["backup_codes"]) == 10
        assert body["tokens"]["access"]
        assert body["device_trusted"] is True
        assert AdminMFADevice.objects.get(user=user).is_confirmed
        assert AdminBackupCode.objects.filter(user=user).count() == 10

    def test_pending_secret_is_not_promoted_until_confirmed(self, client):
        """재등록 도중 이탈해도 기존 인증앱이 살아 있어야 한다."""
        user = _mk_staff()
        old_secret = _enroll(user)
        tokens = issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)

        code, setup = _post(
            client,
            SETUP_URL,
            {"password": PASSWORD, "code": _code(old_secret)},
            token=tokens["access"],
        )
        assert code == 200, setup
        assert setup["secret"] != old_secret

        device = AdminMFADevice.objects.get(user=user)
        assert device.secret == old_secret  # 정본은 그대로
        assert device.pending_secret == setup["secret"]
        assert device.is_confirmed

    def test_reenroll_requires_current_code(self, client):
        user = _mk_staff()
        _enroll(user)
        tokens = issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)
        code, body = _post(
            client, SETUP_URL, {"password": PASSWORD, "code": "000000"}, token=tokens["access"]
        )
        assert code == 400
        assert _detail_code(body) == "invalid_code"

    def test_reenroll_requires_password(self, client):
        user = _mk_staff()
        secret = _enroll(user)
        tokens = issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)
        code, body = _post(
            client,
            SETUP_URL,
            {"password": "wrong", "code": _code(secret)},
            token=tokens["access"],
        )
        assert code == 401
        assert _detail_code(body) == "invalid_credentials"

    def test_bootstrap_path_rejected_for_enrolled_account(self, client):
        """비밀번호만으로 인증앱을 갈아끼우지 못하게 — 부트스트랩은 미등록 전용."""
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        # 등록된 계정이라 로그인은 challenge 를 준다. 그 값을 setup_token 자리에 넣어본다.
        _, body = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": DEVICE}
        )
        code, out = _post(client, SETUP_URL, {"setup_token": body["challenge"]})
        assert code == 400
        assert _detail_code(out) == "already_enrolled"

    def test_confirm_without_setup_is_rejected(self, client):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        _, body = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": DEVICE}
        )
        code, out = _post(client, CONFIRM_URL, {"setup_token": body["challenge"], "code": "000000"})
        assert code == 400
        assert _detail_code(out) == "setup_not_started"


# ── 토큰 갱신 ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAdminRefresh:
    def test_admin_refresh_rotates(self, client):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        tokens = issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)
        code, body = _post(client, REFRESH_URL, {"refresh": tokens["refresh"]})
        assert code == 200, body
        assert body["access"] and body["refresh"] != tokens["refresh"]

    def test_plain_user_refresh_is_rejected(self, client):
        """일반 로그인 토큰으로 어드민 토큰을 만들 수 있으면 2단계가 통째로 무의미해진다."""
        user = _mk_staff()
        plain = AppRefreshToken.for_user(user)
        code, body = _post(client, REFRESH_URL, {"refresh": str(plain)})
        assert code == 400
        assert _detail_code(body) == "not_admin_token"

    def test_revoked_device_cannot_refresh(self, client):
        user = _mk_staff()
        _enroll(user)
        device = _trust(user)
        tokens = issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)
        device.revoked_at = timezone.now()
        device.save(update_fields=["revoked_at"])
        code, body = _post(client, REFRESH_URL, {"refresh": tokens["refresh"]})
        assert code == 401
        assert _detail_code(body) == "device_revoked"

    def test_inactive_user_cannot_refresh(self, client):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        tokens = issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)
        user.is_active = False
        user.save(update_fields=["is_active"])
        code, body = _post(client, REFRESH_URL, {"refresh": tokens["refresh"]})
        assert code == 401
        assert _detail_code(body) == "user_inactive"


# ── 어드민 토큰 게이트 ────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAdminTokenGate:
    def test_plain_token_is_blocked_when_enforced(self, client, settings):
        settings.ADMIN_MFA_ENFORCED = True
        user = _mk_staff()
        plain = AppRefreshToken.for_user(user)
        res = client.get(ME_URL, HTTP_AUTHORIZATION=f"Bearer {plain.access_token}")
        assert res.status_code == 403
        assert _detail_code(res.json()) == "admin_token_required"

    def test_admin_token_passes_when_enforced(self, client, settings):
        settings.ADMIN_MFA_ENFORCED = True
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        tokens = issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)
        res = client.get(ME_URL, HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        assert res.status_code == 200
        assert res.json()["email"] == user.email

    def test_plain_token_passes_when_flag_off(self, client, settings):
        """롤아웃 순서를 지키기 위한 스위치 — 끄면 종전 동작 그대로."""
        settings.ADMIN_MFA_ENFORCED = False
        user = _mk_staff()
        plain = AppRefreshToken.for_user(user)
        res = client.get(ME_URL, HTTP_AUTHORIZATION=f"Bearer {plain.access_token}")
        assert res.status_code == 200

    def test_auth_endpoints_are_never_gated(self, client, settings):
        """토큰을 받아가는 곳이 토큰을 요구하면 로그인 자체가 불가능하다."""
        settings.ADMIN_MFA_ENFORCED = True
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        code, _ = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": DEVICE}
        )
        assert code == 200

    def test_marketing_viewer_is_exempt_from_token_gate(self, client, settings):
        settings.ADMIN_MFA_ENFORCED = True
        user = _mk_staff()
        user.groups.add(Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)[0])
        plain = AppRefreshToken.for_user(user)
        res = client.get(ME_URL, HTTP_AUTHORIZATION=f"Bearer {plain.access_token}")
        assert res.status_code == 200

    def test_unauthenticated_still_gets_401_not_403(self, client, settings):
        """게이트는 좁히기만 한다 — 미인증은 '권한 없음'이 아니라 '로그인 만료'여야 한다."""
        settings.ADMIN_MFA_ENFORCED = True
        assert client.get(ME_URL).status_code == 401


# ── 상태·백업코드·기기 관리 ───────────────────────────────────────────────


@pytest.mark.django_db
class TestAdminMfaManagement:
    def _tokens(self, user):
        return issue_admin_tokens(user, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)

    def test_status_reports_enrollment_and_devices(self, client, settings):
        user = _mk_staff()
        _enroll(user)
        _trust(user)
        totp_service.issue_backup_codes(user)
        tokens = self._tokens(user)
        res = client.get(STATUS_URL, HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        assert res.status_code == 200
        body = res.json()
        assert body["enrolled"] is True
        assert body["backup_codes_remaining"] == 10
        assert body["backup_codes_low_threshold"] == settings.ADMIN_BACKUP_CODE_LOW_THRESHOLD
        assert len(body["trusted_devices"]) == 1
        row = body["trusted_devices"][0]
        assert row["is_current"] is True
        assert row["is_trusted"] is True
        assert row["expires_at"] is None

    def test_regenerate_replaces_all_codes(self, client):
        user = _mk_staff()
        secret = _enroll(user)
        old = totp_service.issue_backup_codes(user)
        tokens = self._tokens(user)
        code, body = _post(
            client,
            REGEN_URL,
            {"password": PASSWORD, "code": _code(secret)},
            token=tokens["access"],
        )
        assert code == 200, body
        assert len(body["backup_codes"]) == 10
        assert set(body["backup_codes"]).isdisjoint(old)
        assert AdminBackupCode.objects.filter(user=user).count() == 10
        # 옛 코드는 더 이상 통하지 않는다
        assert totp_service.consume_backup_code(user, old[0]) is False

    def test_regenerate_requires_current_totp(self, client):
        user = _mk_staff()
        _enroll(user)
        tokens = self._tokens(user)
        code, body = _post(
            client, REGEN_URL, {"password": PASSWORD, "code": "000000"}, token=tokens["access"]
        )
        assert code == 400
        assert _detail_code(body) == "invalid_code"

    def test_revoke_device(self, client):
        user = _mk_staff()
        _enroll(user)
        device = _trust(user)
        tokens = self._tokens(user)
        res = client.delete(
            f"/api/v1/admin/auth/devices/{device.pk}/",
            HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
        )
        assert res.status_code == 204
        device.refresh_from_db()
        assert device.revoked_at is not None

    def test_cannot_revoke_someone_elses_device(self, client):
        owner = _mk_staff()
        other = _mk_staff()
        _enroll(other)
        device = _trust(owner, "victim-device")
        tokens = issue_admin_tokens(other, device_id=DEVICE, amr=["pwd", "totp"], trusted=True)
        res = client.delete(
            f"/api/v1/admin/auth/devices/{device.pk}/",
            HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
        )
        assert res.status_code == 404
        device.refresh_from_db()
        assert device.revoked_at is None

    def test_revoked_device_is_new_again_on_next_login(self, client, email_gate_on):
        """해제는 다음 로그인 한 번으로 무효화되면 안 된다 — 이메일 승인을 다시 받아야 한다.

        (이메일 승인이 꺼진 기본값에서는 이 신호가 없다. 해제의 실효는 그때도 남는다 —
        :func:`devices.get_or_create_device` 가 회수 상태를 되살리지 않고, 갱신은
        :func:`devices.find_live_device` 가 막는다. 그 두 개는 아래 테스트들이 지킨다.)
        """
        user = _mk_staff()
        _enroll(user)
        device = _trust(user)
        device.revoked_at = timezone.now()
        device.save(update_fields=["revoked_at"])
        _, body = _post(
            client, LOGIN_URL, {"email": user.email, "password": PASSWORD, "device_id": DEVICE}
        )
        assert body["device_verification_required"] is True


# ── 비상 리셋 커맨드 ──────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAdminMfaResetCommand:
    def test_reset_clears_totp_and_backup_codes(self):
        from django.core.management import call_command

        user = _mk_staff()
        _enroll(user)
        totp_service.issue_backup_codes(user)
        call_command("admin_mfa_reset", user.email)
        assert not AdminMFADevice.objects.filter(user=user).exists()
        assert not AdminBackupCode.objects.filter(user=user).exists()
        assert AdminActionLog.objects.filter(
            action=AdminActionLog.Action.ADMIN_MFA_RESET, target_id=str(user.pk)
        ).exists()

    def test_reset_keeps_device_trust_by_default(self):
        """분실한 것은 인증앱이지 기기가 아니다 — 복구 중인 사람에게 메일 코드까지 요구하지 않는다."""
        from django.core.management import call_command

        user = _mk_staff()
        _enroll(user)
        device = _trust(user)
        call_command("admin_mfa_reset", user.email)
        device.refresh_from_db()
        assert device.is_trusted

    def test_reset_with_revoke_devices(self):
        from django.core.management import call_command

        user = _mk_staff()
        _enroll(user)
        device = _trust(user)
        call_command("admin_mfa_reset", user.email, "--revoke-devices")
        device.refresh_from_db()
        assert device.revoked_at is not None
