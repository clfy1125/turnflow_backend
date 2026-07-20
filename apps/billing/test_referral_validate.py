"""레퍼럴 코드 검증 응답 테스트.

GET /billing/referral/validate/ — 카드 등록 시 총 무료 일수(base + 코드 보너스) 노출 계약.
핵심: total_trial_days = TRIAL_BASE_DAYS + code.trial_days
      (프론트 '원래 1개월 무료 → 코드 적용 시 2개월 무료' 표기 소스)
더러운 테스트 DB 대응: 코드는 uuid 로 유일화.
"""

import uuid

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing.models import ReferralCode, SubscriptionPlan
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
