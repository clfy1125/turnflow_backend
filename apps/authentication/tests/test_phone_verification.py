"""휴대폰 본인확인 — 번호 정규화·OTP·어뷰즈 방어·보상 지급.

⚠️ 이 저장소의 pytest 는 **dev DB 를 그대로 쓴다**(memory: test-db-not-clean).
   이메일은 uuid 로 만들고, 집계는 절대값이 아니라 델타로 단언한다.
⚠️ 문자는 ``SMS_MOCK_MODE`` 로 막는다 — 테스트가 실제 요금을 태우면 안 된다.
   ``settings`` 픽스처를 쓴다(클래스 데코레이터 override_settings 는 이 저장소에서
   깨진다 — memory: override-settings-class-decorator-broken).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.models import PhoneVerification
from apps.authentication.phone import (
    InvalidPhoneNumber,
    code_matches,
    format_phone,
    hash_code,
    mask_phone,
    normalize_phone,
)
from apps.sms.aligo import sms_byte_length
from apps.sms.models import SmsLog, SmsStatus, mask_code

User = get_user_model()
pytestmark = pytest.mark.django_db


# ──────────────────────────────────────────────
# 픽스처
# ──────────────────────────────────────────────


@pytest.fixture
def sms_off(settings):
    """실제 발송 차단 + 상한을 테스트가 예측 가능한 값으로 고정.

    ⚠️ IP 일일 카운터는 **Redis 에 있어 트랜잭션 롤백으로 안 지워진다.** 지우지 않으면
       테스트를 몇 번 돌리는 사이 상한에 닿아 전부 429 로 깨진다(실제로 겪었다).
       캐시 전체 flush 는 금지 — DM 발송이 1시간 멈춘다(memory: prod-deploy-f96e732).
    """
    from django.core.cache import cache

    from apps.authentication.phone_guard import ip_counter_key

    cache.delete(ip_counter_key("127.0.0.1"))
    settings.SMS_MOCK_MODE = True
    settings.PHONE_VERIFY_RESEND_COOLDOWN_SECONDS = 60
    settings.PHONE_VERIFY_MAX_PER_PHONE_PER_DAY = 5
    settings.PHONE_VERIFY_MAX_PER_IP_PER_DAY = 10000
    settings.PHONE_VERIFY_GLOBAL_DAILY_CAP = 0  # 다른 테스트의 로그에 영향받지 않게
    settings.PHONE_VERIFY_MAX_ATTEMPTS = 5
    settings.PHONE_VERIFY_CODE_TTL_SECONDS = 180
    settings.PHONE_REQUIRED_SINCE = ""
    settings.PHONE_REWARD_ENABLED = True
    # 스로틀은 별도 테스트에서만 켠다 — 켜 둔 채로 두면 다른 테스트가 429 로 깨진다.
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
            "phone_send": "1000/hour",
            "phone_verify": "1000/min",
        },
    }
    return settings


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"phone-{uuid.uuid4().hex[:10]}@test.com", password="Pw!23456xyz"
    )


@pytest.fixture
def api(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _send(api, phone="010-1234-5678"):
    return api.post(reverse("authentication:me-phone"), {"phone": phone}, format="json")


def _live_code(user) -> str:
    """살아있는 인증 행의 평문 코드를 **역산한다**.

    서버는 평문을 저장하지 않으므로(HMAC) 테스트도 100만 번 돌릴 수는 없다 —
    대신 발송 직후 행을 찾아 알려진 코드로 덮어쓴다. 해시 경로는 그대로 타므로
    ``code_matches`` 검증은 유효하다.
    """
    row = PhoneVerification.objects.filter(
        user=user, verified_at__isnull=True, invalidated_at__isnull=True
    ).latest("created_at")
    known = "135790"
    row.code_hash = hash_code(row.phone, known)
    row.save(update_fields=["code_hash"])
    return known


# ──────────────────────────────────────────────
# 번호 정규화 — 표기가 섞이면 같은 사람이 두 번 쌓인다
# ──────────────────────────────────────────────


class TestNormalize:
    @pytest.mark.parametrize(
        "raw",
        [
            "01012345678",
            "010-1234-5678",
            "010 1234 5678",
            " 010.1234.5678 ",
            "+82 10-1234-5678",
            "+821012345678",
            "821012345678",
            "8201012345678",
        ],
    )
    def test_all_spellings_collapse_to_one(self, raw):
        assert normalize_phone(raw) == "01012345678"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "0212345678",  # 서울 유선 — 알림톡 불가
            "15881588",  # 대표번호
            "+1 650 253 0000",  # 해외
            "0101234567",  # 010 은 11자리 고정
            "010123456789",  # 너무 김
            "abc",
        ],
    )
    def test_rejects_non_mobile(self, raw):
        with pytest.raises(InvalidPhoneNumber):
            normalize_phone(raw)

    def test_legacy_prefixes_allowed(self):
        assert normalize_phone("011-123-4567") == "0111234567"
        assert normalize_phone("016-1234-5678") == "01612345678"

    def test_mask_and_format(self):
        assert mask_phone("01012345678") == "010-****-5678"
        assert format_phone("01012345678") == "010-1234-5678"
        assert format_phone("0111234567") == "011-123-4567"


class TestCodeHash:
    def test_code_never_matches_other_phone(self):
        """번호를 해시 입력에 섞기 때문에 남의 행에 내 코드를 들이밀 수 없다."""
        stored = hash_code("01011112222", "123456")
        assert code_matches("01011112222", "123456", stored)
        assert not code_matches("01033334444", "123456", stored)

    def test_plaintext_is_not_recoverable_from_row(self, sms_off, api, user):
        _send(api)
        row = PhoneVerification.objects.filter(user=user).latest("created_at")
        assert len(row.code_hash) == 64
        # 어떤 필드에도 6자리 평문이 없다
        assert not any(
            str(getattr(row, f.name, "")).isdigit() and len(str(getattr(row, f.name, ""))) == 6
            for f in PhoneVerification._meta.fields
            if f.name != "phone"
        )


# ──────────────────────────────────────────────
# 비용 — 본문이 길어지면 LMS 로 승급돼 건당 3배가 된다
# ──────────────────────────────────────────────


class TestSmsCost:
    def test_otp_body_stays_within_sms_limit(self):
        from apps.authentication.phone_views import build_otp_message
        from apps.sms.aligo import SMS_BYTE_LIMIT

        body = build_otp_message("123456")
        assert sms_byte_length(body) <= SMS_BYTE_LIMIT, (
            f"OTP 본문이 {sms_byte_length(body)}byte — {SMS_BYTE_LIMIT} 를 넘으면 "
            "LMS 로 승급돼 건당 비용이 3배가 된다. 문구를 줄일 것."
        )

    def test_log_masks_the_code(self):
        assert mask_code("[턴플로우] 인증번호 123456 (3분)") == "[턴플로우] 인증번호 ****** (3분)"


# ──────────────────────────────────────────────
# 발송
# ──────────────────────────────────────────────


class TestSend:
    def test_sends_and_creates_row(self, sms_off, api, user):
        before = SmsLog.objects.count()
        res = _send(api)
        assert res.status_code == 202, res.data
        assert res.data["phone_masked"] == "010-****-5678"
        assert res.data["expires_in"] == 180
        assert PhoneVerification.objects.filter(user=user, phone="01012345678").exists()
        # Mock 이라도 로그는 남아야 한다 (비용·CS 추적)
        assert SmsLog.objects.count() == before + 1
        assert SmsLog.objects.latest("created_at").status == SmsStatus.MOCKED

    def test_rejects_landline(self, sms_off, api):
        res = _send(api, "02-123-4567")
        assert res.status_code == 400
        assert res.data["code"] == "PHONE_INVALID"
        # §6 envelope 과 detail 을 **같이** 낸다
        assert res.data["success"] is False
        assert res.data["error"]["details"]["code"] == "PHONE_INVALID"

    def test_resend_cooldown(self, sms_off, api):
        assert _send(api).status_code == 202
        res = _send(api)
        assert res.status_code == 429
        assert res.data["code"] == "PHONE_RESEND_TOO_SOON"
        assert res.data["retry_after"] > 0

    def test_resend_invalidates_previous_code(self, sms_off, api, user):
        _send(api)
        first = PhoneVerification.objects.filter(user=user).latest("created_at")
        PhoneVerification.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        assert _send(api).status_code == 202
        first.refresh_from_db()
        assert first.invalidated_at is not None, "직전 코드가 살아 있으면 시도 제한이 2배가 된다"

    def test_per_phone_daily_cap(self, sms_off, api, user):
        sms_off.PHONE_VERIFY_MAX_PER_PHONE_PER_DAY = 2
        sms_off.PHONE_VERIFY_RESEND_COOLDOWN_SECONDS = 0
        assert _send(api).status_code == 202
        assert _send(api).status_code == 202
        res = _send(api)
        assert res.status_code == 429
        assert res.data["code"] == "PHONE_DAILY_LIMIT"

    def test_daily_cap_counts_the_phone_not_the_account(self, sms_off, api, user):
        """계정을 바꿔도 **같은 번호**면 막혀야 한다 — 이게 실질 방어선이다."""
        sms_off.PHONE_VERIFY_MAX_PER_PHONE_PER_DAY = 1
        sms_off.PHONE_VERIFY_RESEND_COOLDOWN_SECONDS = 0
        assert _send(api).status_code == 202

        other = User.objects.create_user(
            email=f"other-{uuid.uuid4().hex[:10]}@test.com", password="Pw!23456xyz"
        )
        other_api = APIClient()
        other_api.force_authenticate(user=other)
        res = _send(other_api)
        assert res.status_code == 429
        assert res.data["code"] == "PHONE_DAILY_LIMIT"

    def test_global_cap_returns_503(self, sms_off, api):
        """최후의 금액 상한. **실제 과금된 건(SENT)만** 센다 — Mock 은 돈이 안 나간다."""
        SmsLog.objects.create(purpose="phone_verify", to_phone="01099998888", status=SmsStatus.SENT)
        sms_off.PHONE_VERIFY_GLOBAL_DAILY_CAP = 1
        res = _send(api)
        assert res.status_code == 503
        assert res.data["code"] == "SMS_CAPACITY_EXCEEDED"

    def test_ip_daily_cap(self, sms_off, api):
        sms_off.PHONE_VERIFY_MAX_PER_IP_PER_DAY = 1
        sms_off.PHONE_VERIFY_RESEND_COOLDOWN_SECONDS = 0
        assert _send(api).status_code == 202
        res = _send(api, "010-9999-8888")
        assert res.status_code == 429
        assert res.data["code"] == "PHONE_IP_LIMIT"

    def test_failed_send_does_not_burn_the_ip_quota(self, sms_off, api):
        """발송 실패(형식 오류)가 정상 사용자의 하루 할당을 깎으면 안 된다."""
        from django.core.cache import cache

        from apps.authentication.phone_guard import ip_counter_key

        _send(api, "02-123-4567")  # 400
        assert cache.get(ip_counter_key("127.0.0.1")) in (None, 0)

    def test_mock_sends_do_not_burn_the_global_cap(self, sms_off, api):
        """Mock 발송이 상한을 깎으면 dev 에서 실수로 운영 상한 테스트가 통과한다."""
        sms_off.PHONE_VERIFY_GLOBAL_DAILY_CAP = 100000
        assert _send(api).status_code == 202
        assert SmsLog.objects.latest("created_at").status == SmsStatus.MOCKED

    def test_already_verified_same_number_does_not_spend_sms(self, sms_off, api, user):
        user.phone = "01012345678"
        user.phone_verified_at = timezone.now()
        user.save(update_fields=["phone", "phone_verified_at"])
        before = SmsLog.objects.count()
        res = _send(api)
        assert res.status_code == 400
        assert res.data["code"] == "PHONE_ALREADY_VERIFIED"
        assert SmsLog.objects.count() == before, "문자를 보내면 안 된다"

    def test_requires_auth(self, sms_off):
        res = APIClient().post(reverse("authentication:me-phone"), {"phone": "01012345678"})
        assert res.status_code == 401


# ──────────────────────────────────────────────
# 확인
# ──────────────────────────────────────────────


class TestVerify:
    def _verify(self, api, code, **extra):
        return api.post(
            reverse("authentication:me-phone-verify"), {"code": code, **extra}, format="json"
        )

    def test_happy_path(self, sms_off, api, user):
        _send(api)
        res = self._verify(api, _live_code(user))
        assert res.status_code == 200, res.data
        user.refresh_from_db()
        assert user.phone == "01012345678"
        assert user.phone_verified is True
        assert user.phone_source == "sms"
        assert res.data["user"]["phone"] == "010-1234-5678"
        assert res.data["user"]["phone_verified"] is True

    def test_wrong_code_counts_down(self, sms_off, api, user):
        _send(api)
        _live_code(user)
        res = self._verify(api, "000000")
        assert res.status_code == 400
        assert res.data["code"] == "PHONE_CODE_INVALID"
        assert res.data["attempts_left"] == 4

    def test_five_failures_kill_the_row(self, sms_off, api, user):
        _send(api)
        code = _live_code(user)
        for _ in range(5):
            self._verify(api, "000000")
        # 죽은 뒤에는 **맞는 코드도** 통하지 않는다 — 재발송 강제
        res = self._verify(api, code)
        assert res.status_code == 400
        assert res.data["code"] == "NO_PENDING_PHONE_VERIFICATION"
        user.refresh_from_db()
        assert user.phone == ""

    def test_expired_code(self, sms_off, api, user):
        _send(api)
        code = _live_code(user)
        PhoneVerification.objects.filter(user=user).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        res = self._verify(api, code)
        assert res.status_code == 400
        assert res.data["code"] == "PHONE_CODE_EXPIRED"

    def test_no_pending(self, sms_off, api):
        res = self._verify(api, "123456")
        assert res.status_code == 400
        assert res.data["code"] == "NO_PENDING_PHONE_VERIFICATION"

    def test_sms_marketing_consent_is_recorded_separately(self, sms_off, api, user):
        _send(api)
        self._verify(api, _live_code(user), sms_marketing_opt_in=True)
        user.refresh_from_db()
        assert user.sms_marketing_opt_in is True
        assert user.sms_marketing_opt_in_at is not None
        # 메일 동의는 **건드리지 않는다** — 매체가 다르면 동의도 다르다
        assert user.marketing_opt_in is False

    def test_consent_defaults_to_false(self, sms_off, api, user):
        _send(api)
        self._verify(api, _live_code(user))
        user.refresh_from_db()
        assert user.sms_marketing_opt_in is False


# ──────────────────────────────────────────────
# 보상 — 기존 회원만, 1인 1회, 번호당 1회
# ──────────────────────────────────────────────


class TestReward:
    def _verify_flow(self, api, user, **extra):
        _send(api)
        return api.post(
            reverse("authentication:me-phone-verify"),
            {"code": _live_code(user), **extra},
            format="json",
        )

    def test_trialing_user_gets_seven_more_days(self, sms_off, api, user):
        from apps.billing.models import SubscriptionPlan, SubscriptionStatus
        from apps.billing.subscription_utils import ensure_subscription

        pro = SubscriptionPlan.objects.filter(name="pro", is_active=True).first()
        if pro is None:
            pytest.skip("dev DB 에 pro 플랜이 없다")
        sub = ensure_subscription(user)
        ends = timezone.now() + timedelta(days=10)
        sub.plan = pro
        sub.status = SubscriptionStatus.TRIALING
        sub.current_period_end = ends
        sub.trial_used_at = timezone.now()
        sub.save()

        res = self._verify_flow(api, user)
        assert res.status_code == 200, res.data
        assert res.data["reward"]["kind"] == "trial_extended"
        assert res.data["reward"]["days"] == 7
        sub.refresh_from_db()
        assert abs((sub.current_period_end - (ends + timedelta(days=7))).total_seconds()) < 5

    def test_new_signup_gets_nothing(self, sms_off, api, user):
        """인증이 **필수**인 신규 가입자는 보상 대상이 아니다 (중복 지급 방지)."""
        sms_off.PHONE_REQUIRED_SINCE = (timezone.now() - timedelta(days=1)).isoformat()
        res = self._verify_flow(api, user)
        assert res.data["reward"]["kind"] == "none"
        assert res.data["reward"]["reason"] == "new_signup"

    def test_second_account_with_same_phone_gets_nothing(self, sms_off, api, user):
        """계정 복제로 같은 번호에 보상을 두 번 태우지 못한다."""
        self._verify_flow(api, user)
        user.refresh_from_db()
        if user.phone_reward_granted_at is None:
            pytest.skip("첫 계정이 보상 대상이 아니었다 (dev DB 구독 상태 의존)")

        other = User.objects.create_user(
            email=f"dup-{uuid.uuid4().hex[:10]}@test.com", password="Pw!23456xyz"
        )
        other_api = APIClient()
        other_api.force_authenticate(user=other)
        sms_off.PHONE_VERIFY_MAX_PER_PHONE_PER_DAY = 99
        sms_off.PHONE_VERIFY_RESEND_COOLDOWN_SECONDS = 0
        res = self._verify_flow(other_api, other)
        assert res.data["reward"]["kind"] == "none"
        assert res.data["reward"]["reason"] == "phone_already_rewarded"

    def test_preview_matches_grant(self, sms_off, api, user):
        """발송 응답의 예고(reward)가 실제 지급과 같아야 문구가 거짓이 되지 않는다."""
        sent = _send(api)
        predicted = sent.data["reward"]["kind"]
        res = api.post(
            reverse("authentication:me-phone-verify"),
            {"code": _live_code(user)},
            format="json",
        )
        assert res.data["reward"]["kind"] == predicted


# ──────────────────────────────────────────────
# 필수화 플래그 + 삭제
# ──────────────────────────────────────────────


class TestRequiredFlagAndDelete:
    def test_required_false_for_legacy_user(self, sms_off, api, user):
        sms_off.PHONE_REQUIRED_SINCE = (timezone.now() + timedelta(days=1)).isoformat()
        res = api.get(reverse("authentication:me"))
        assert res.data["phone_verification_required"] is False

    def test_required_true_for_new_signup(self, sms_off, api, user):
        sms_off.PHONE_REQUIRED_SINCE = (timezone.now() - timedelta(days=1)).isoformat()
        res = api.get(reverse("authentication:me"))
        assert res.data["phone_verification_required"] is True

    def test_required_turns_false_after_verification(self, sms_off, api, user):
        sms_off.PHONE_REQUIRED_SINCE = (timezone.now() - timedelta(days=1)).isoformat()
        _send(api)
        api.post(
            reverse("authentication:me-phone-verify"), {"code": _live_code(user)}, format="json"
        )
        res = api.get(reverse("authentication:me"))
        assert res.data["phone_verification_required"] is False

    def test_empty_setting_requires_nobody(self, sms_off, api):
        """기능을 켜기 전 상태 — 아무에게도 강제하지 않는다."""
        sms_off.PHONE_REQUIRED_SINCE = ""
        res = api.get(reverse("authentication:me"))
        assert res.data["phone_verification_required"] is False

    def test_legacy_user_can_delete(self, sms_off, api, user):
        _send(api)
        api.post(
            reverse("authentication:me-phone-verify"), {"code": _live_code(user)}, format="json"
        )
        res = api.delete(reverse("authentication:me-phone"))
        assert res.status_code == 200
        user.refresh_from_db()
        assert user.phone == ""
        assert user.phone_verified is False

    def test_delete_does_not_refund_the_reward(self, sms_off, api, user):
        _send(api)
        api.post(
            reverse("authentication:me-phone-verify"), {"code": _live_code(user)}, format="json"
        )
        user.refresh_from_db()
        granted = user.phone_reward_granted_at
        api.delete(reverse("authentication:me-phone"))
        user.refresh_from_db()
        assert user.phone_reward_granted_at == granted, "지웠다 다시 등록해 두 번 받으면 안 된다"

    def test_mandatory_user_cannot_delete(self, sms_off, api, user):
        sms_off.PHONE_REQUIRED_SINCE = (timezone.now() - timedelta(days=1)).isoformat()
        _send(api)
        api.post(
            reverse("authentication:me-phone-verify"), {"code": _live_code(user)}, format="json"
        )
        res = api.delete(reverse("authentication:me-phone"))
        assert res.status_code == 409
        assert res.data["code"] == "PHONE_REQUIRED"


# ──────────────────────────────────────────────
# 카카오 — 동의항목으로 받은 번호
# ──────────────────────────────────────────────


class TestKakaoPhone:
    def test_parses_kakao_format(self):
        from apps.authentication.kakao import _parse_phone

        assert _parse_phone({"phone_number": "+82 10-1234-5678"}) == "01012345678"

    def test_skips_when_not_agreed(self):
        from apps.authentication.kakao import _parse_phone

        assert (
            _parse_phone({"phone_number": "+82 10-1234-5678", "phone_number_needs_agreement": True})
            == ""
        )

    def test_overseas_number_is_dropped_not_crashed(self):
        from apps.authentication.kakao import _parse_phone

        assert _parse_phone({"phone_number": "+1 650-253-0000"}) == ""
        assert _parse_phone({}) == ""


# ──────────────────────────────────────────────
# Meta CAPI — 본인확인된 번호만 매칭에 실린다
# ──────────────────────────────────────────────


class TestCapiMatching:
    def test_verified_phone_is_sent(self, user):
        from apps.analytics.conversions import _match_params

        user.phone = "01012345678"
        user.phone_verified_at = timezone.now()
        assert _match_params(user).get("phone") == "01012345678"

    def test_unverified_phone_is_not_sent(self, user):
        from apps.analytics.conversions import _match_params

        user.phone = "01012345678"
        user.phone_verified_at = None
        assert "phone" not in _match_params(user)
