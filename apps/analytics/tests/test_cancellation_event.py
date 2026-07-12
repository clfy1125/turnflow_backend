"""POST /api/v1/track/cancellation-event/ 통합 테스트 (APIClient, 로그인 필요).

구독 취소 텔레메트리 — 마케팅 대시보드 해지 사유/취소 방어의 원천. DB 재사용 가능성
때문에 단언은 델타 기반.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.analytics.models import CancellationEvent

URL = "/api/v1/track/cancellation-event/"
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
class TestCancellationEvent:
    def test_requires_auth(self, client):
        res = client.post(URL, {"event": "cancel_button_clicked"}, format="json")
        assert res.status_code == 401

    def test_records_reason(self, auth_client, user):
        before = CancellationEvent.objects.count()
        res = auth_client.post(
            URL,
            {"event": "cancel_reason_submitted", "reason": "low_usage", "from_plan": "pro"},
            format="json",
        )
        assert res.status_code == 201
        assert CancellationEvent.objects.count() == before + 1
        ev = CancellationEvent.objects.filter(user=user).latest("created_at")
        assert ev.event == "cancel_reason_submitted"
        assert ev.reason == "low_usage"

    def test_invalid_event_rejected(self, auth_client):
        before = CancellationEvent.objects.count()
        res = auth_client.post(URL, {"event": "nope"}, format="json")
        assert res.status_code == 400
        assert res.data["success"] is False
        assert CancellationEvent.objects.count() == before
