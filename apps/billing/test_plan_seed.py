"""요금제 개편 시드(0013/0014) 검증 + 공개 플랜 API."""

import pytest
from rest_framework.test import APIClient

from apps.billing.models import SubscriptionPlan

# check_limit 은 키 누락 시 '차단'으로 동작하므로 전 플랜 전 키 존재가 계약이다.
REQUIRED_KEYS = {
    "max_pages",
    "ai_generation",
    "ai_unlimited",
    "remove_logo",
    "custom_css",
    "dm_monthly_limit",
    "analytics_export",
    "spam_filter",
    "max_ig_accounts",
}

EXPECTED = {
    "free": {
        "monthly_price": 0,
        "list_price": 0,
        "max_pages": 1,
        "remove_logo": False,
        "dm_monthly_limit": 200,
        "analytics_export": False,
        "spam_filter": False,
        "max_ig_accounts": 1,
        "ai_unlimited": False,
        "custom_css": True,
    },
    "basic": {
        "monthly_price": 3900,
        "list_price": 5900,
        "max_pages": 5,
        "remove_logo": True,
        "dm_monthly_limit": 200,
        "analytics_export": True,
        "spam_filter": False,
        "max_ig_accounts": 1,
        "ai_unlimited": True,
        "custom_css": True,
    },
    "pro": {
        "monthly_price": 9900,  # 론칭 프로모 (정가 15,900)
        "list_price": 15900,
        "max_pages": 5,
        "remove_logo": True,
        "dm_monthly_limit": -1,
        "analytics_export": True,
        "spam_filter": True,
        "max_ig_accounts": 1,  # 기본 1 — 추가는 구독 extra_ig_accounts
        "ai_unlimited": True,
        "custom_css": True,
    },
}


@pytest.mark.django_db
class TestPlanSeed:
    @pytest.mark.parametrize("name", ["free", "basic", "pro"])
    def test_plan_values(self, name):
        plan = SubscriptionPlan.objects.get(name=name)
        expected = EXPECTED[name]
        assert plan.is_active is True
        assert plan.monthly_price == expected["monthly_price"]
        assert plan.list_price == expected["list_price"]
        assert REQUIRED_KEYS <= set(plan.features.keys())
        for key in REQUIRED_KEYS:
            if key in expected:
                assert plan.features[key] == expected[key], f"{name}.{key}"

    def test_admin_plan_exists_hidden_and_unlimited(self):
        admin = SubscriptionPlan.objects.get(name="admin")
        assert admin.is_active is False  # 공개 목록 비노출
        assert REQUIRED_KEYS <= set(admin.features.keys())
        assert admin.features["max_pages"] == -1
        assert admin.features["dm_monthly_limit"] == -1
        assert admin.features["max_ig_accounts"] == -1

    def test_pro_plus_not_purchasable(self):
        pro_plus = SubscriptionPlan.objects.filter(name="pro_plus").first()
        assert pro_plus is None or pro_plus.is_active is False


@pytest.mark.django_db
class TestPlanListApi:
    def test_public_list_shows_three_tiers_with_list_price(self):
        resp = APIClient().get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        by_name = {p["name"]: p for p in resp.data}

        assert {"free", "basic", "pro"} <= set(by_name.keys())
        assert "admin" not in by_name  # 비활성 — 노출 금지
        assert "pro_plus" not in by_name
        assert by_name["basic"]["list_price"] == 5900
        assert by_name["pro"]["monthly_price"] == 9900
        assert by_name["pro"]["list_price"] == 15900
        assert by_name["pro"]["features"]["dm_monthly_limit"] == -1
