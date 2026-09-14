"""팝업 노출 상태 — 최상위 키 단위 병합 + 삭제 + 크기/키 제한.

병합 규칙이 계약의 전부다: **깊은 병합을 하지 않는다.** 프론트가 읽은 객체를 펼쳐
통째로 다시 보내는 것을 전제로 한다.
"""

import uuid

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

URL_NAME = "authentication:me-popup-state"


@pytest.mark.django_db
class TestPopupState:
    @pytest.fixture
    def user(self, django_user_model):
        return django_user_model.objects.create_user(
            email=f"popup-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
        )

    @pytest.fixture
    def client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_requires_authentication(self):
        assert APIClient().get(reverse(URL_NAME)).status_code == 401

    def test_starts_empty(self, client):
        res = client.get(reverse(URL_NAME))
        assert res.status_code == 200
        assert res.json() == {"popup_state": {}}

    def test_patch_merges_top_level_keys(self, client):
        client.patch(reverse(URL_NAME), {"pro_trial": {"shows": 1}}, format="json")
        res = client.patch(reverse(URL_NAME), {"signup": {"shows": 2}}, format="json")

        assert res.json()["popup_state"] == {"pro_trial": {"shows": 1}, "signup": {"shows": 2}}

    def test_patch_replaces_a_key_wholesale(self, client):
        """깊은 병합이 아니다 — 한 팝업 객체를 보내면 그 팝업 상태가 통째로 교체된다."""
        client.patch(reverse(URL_NAME), {"pro_trial": {"shows": 2, "dismisses": 1}}, format="json")
        res = client.patch(reverse(URL_NAME), {"pro_trial": {"converted": True}}, format="json")

        assert res.json()["popup_state"]["pro_trial"] == {"converted": True}

    def test_null_deletes_key(self, client):
        client.patch(reverse(URL_NAME), {"pro_trial": {"shows": 1}}, format="json")
        res = client.patch(reverse(URL_NAME), {"pro_trial": None}, format="json")
        assert "pro_trial" not in res.json()["popup_state"]

    def test_persists_across_requests(self, client, user):
        client.patch(reverse(URL_NAME), {"pro_trial": {"shows": 3}}, format="json")
        user.refresh_from_db()
        assert user.popup_state == {"pro_trial": {"shows": 3}}
        assert client.get(reverse(URL_NAME)).json()["popup_state"] == {"pro_trial": {"shows": 3}}

    def test_rejects_non_object_value(self, client):
        res = client.patch(reverse(URL_NAME), {"pro_trial": "문자열"}, format="json")
        assert res.status_code == 400

    def test_rejects_too_many_keys(self, client):
        payload = {f"k{i}": {"a": 1} for i in range(40)}
        assert client.patch(reverse(URL_NAME), payload, format="json").status_code == 400

    def test_rejects_oversized_state(self, client):
        res = client.patch(reverse(URL_NAME), {"huge": {"x": "a" * 9000}}, format="json")
        assert res.status_code == 400
        assert "8192" in str(res.json())
