"""레퍼럴 코드 검증 응답 + 무카드 경로 폐지 테스트.

GET /billing/referral/validate/ — 총 무료 일수(base + 코드 보너스) & 결제 전 미리보기 계약.
핵심: total_trial_days = TRIAL_BASE_DAYS + code.trial_days
      (프론트 '원래 1개월 무료 → 코드 적용 시 2개월 무료' 표기 소스)
POST /billing/referral/redeem/ — 폐지 확인. 이 경로가 base 30일을 빼먹어 14일 쿠폰이
      14일로 나갔다(2026-08-04). 되살아나면 여기서 잡힌다.
더러운 테스트 DB 대응: 코드/이메일은 uuid 로 유일화.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import (
    EXTRA_IG_ACCOUNT_PRICE,
    ReferralCode,
    ReferralRedemption,
    SubscriptionPlan,
    SubscriptionStatus,
)
from apps.billing.subscription_utils import ensure_subscription
from apps.billing.toss_flows import TRIAL_BASE_DAYS


def _code(days=30, **kwargs):
    return ReferralCode.objects.create(
        code=f"REF{uuid.uuid4().hex[:10]}".upper(),
        target_plan=SubscriptionPlan.objects.get(name="pro"),
        trial_days=days,
        **kwargs,
    )


@pytest.mark.django_db
class TestReferralValidate:
    def test_returns_total_trial_days(self, db):
        code = _code(days=30)
        res = APIClient().get(reverse("billing:referral-validate"), {"code": code.code})
        assert res.status_code == 200
        data = res.json()
        assert data["valid"] is True
        assert data["trial_days"] == 30
        assert data["base_trial_days"] == TRIAL_BASE_DAYS
        assert data["total_trial_days"] == TRIAL_BASE_DAYS + 30  # 카드 등록 시 총 무료

    def test_total_scales_with_bonus(self, db):
        code = _code(days=60)  # +2개월 코드
        res = APIClient().get(reverse("billing:referral-validate"), {"code": code.code})
        data = res.json()
        assert data["total_trial_days"] == TRIAL_BASE_DAYS + 60

    def test_invalid_code_has_no_total(self, db):
        res = APIClient().get(reverse("billing:referral-validate"), {"code": "NOPE-DOES-NOT-EXIST"})
        assert res.status_code == 200
        data = res.json()
        assert data["valid"] is False
        assert "total_trial_days" not in data

    def test_payment_preview_fields(self, db):
        """결제 전 미리보기 — 프론트가 카드 입력 **전에** '언제/얼마' 를 보여줄 소스."""
        pro = SubscriptionPlan.objects.get(name="pro")
        code = _code(days=14)
        before = timezone.now()
        res = APIClient().get(reverse("billing:referral-validate"), {"code": code.code})
        data = res.json()

        assert data["requires_card"] is True  # 무카드 경로는 없다
        assert data["first_charge_amount"] == pro.monthly_price
        assert data["extra_ig_account_price"] == EXTRA_IG_ACCOUNT_PRICE
        # 첫 결제 시각 = 무료 체험 종료 시각 = now + 44일
        assert data["first_charge_at"] == data["trial_ends_at"]
        charge_at = timezone.datetime.fromisoformat(data["first_charge_at"])
        expected = before + timedelta(days=TRIAL_BASE_DAYS + 14)
        assert abs((charge_at - expected).total_seconds()) < 60


@pytest.mark.django_db
class TestCardlessRedeemRetired:
    """무카드 쿠폰 경로 폐지 — base 30일 누락 결함의 재발 방어."""

    @pytest.fixture
    def user(self, db):
        return get_user_model().objects.create_user(
            email=f"ref-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
        )

    @pytest.fixture
    def client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_redeem_always_400_with_machine_code(self, client):
        code = _code(days=14)
        res = client.post(reverse("billing:referral-redeem"), {"code": code.code}, format="json")
        assert res.status_code == 400
        assert res.json()["code"] == "REFERRAL_REQUIRES_CARD"

    def test_redeem_does_not_touch_subscription_or_code(self, client, user):
        """가장 중요 — 구독을 TRIALING 으로 만들지도, 사용횟수를 태우지도 않는다."""
        code = _code(days=14)
        sub = ensure_subscription(user)

        client.post(reverse("billing:referral-redeem"), {"code": code.code}, format="json")

        sub.refresh_from_db()
        code.refresh_from_db()
        assert sub.status != SubscriptionStatus.TRIALING
        assert sub.plan.name == "free"
        assert code.current_uses == 0
        assert not ReferralRedemption.objects.filter(user=user).exists()
