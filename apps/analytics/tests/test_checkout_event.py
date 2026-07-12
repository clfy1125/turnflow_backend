"""POST /api/v1/track/checkout-event/ 통합 테스트 (APIClient, 로그인 필요).

결제 진입 텔레메트리 — 마케팅 대시보드 entry_paths 의 원천. DB 는 재사용될 수 있어
단언은 델타 기반.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.analytics.models import CheckoutEvent

URL = "/api/v1/track/checkout-event/"
User = get_user_model()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"ce-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )


@pytest.fixture
def auth_client(client, user):
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
class TestCheckoutEvent:
    def test_requires_auth(self, client):
        res = client.post(URL, {"event": "paywall_viewed"}, format="json")
        assert res.status_code == 401

    def test_records_event_with_trigger(self, auth_client, user):
        before = CheckoutEvent.objects.count()
        res = auth_client.post(
            URL,
            {
                "event": "paywall_viewed",
                "entry_source": "paywall",
                "trigger_feature": "dm_limit",
                "current_plan": "free",
                "required_plan": "pro",
                "usage_count": 200,
                "limit_count": 200,
            },
            format="json",
        )
        assert res.status_code == 201
        assert CheckoutEvent.objects.count() == before + 1
        ev = CheckoutEvent.objects.filter(user=user).latest("created_at")
        assert ev.event == "paywall_viewed"
        assert ev.trigger_feature == "dm_limit"
        assert ev.usage_count == 200

    def test_invalid_event_rejected(self, auth_client):
        before = CheckoutEvent.objects.count()
        res = auth_client.post(URL, {"event": "not_a_real_event"}, format="json")
        assert res.status_code == 400
        assert res.data["success"] is False
        assert CheckoutEvent.objects.count() == before

    def test_event_only_minimal_payload(self, auth_client):
        res = auth_client.post(URL, {"event": "checkout_started"}, format="json")
        assert res.status_code == 201
