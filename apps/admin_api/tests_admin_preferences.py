"""어드민 화면 설정(UI-1) 테스트 — GET/PATCH /api/v1/admin/me/preferences/.

주의:
- 파일명이 tests_*.py 라 **경로 명시 실행** 필요.
- RBAC(마케팅 전용 계정) 검증은 **미들웨어**를 타야 하므로 Django test Client +
  ``force_login`` 을 쓴다 (DRF force_authenticate 로는 재현되지 않는다).
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from rest_framework.test import APIClient

from apps.admin_api.models import AdminPreference
from apps.admin_api.roles import ROLE_MARKETING_VIEWER
from apps.admin_api.views.preferences import MAX_PREFERENCES_BYTES

User = get_user_model()

URL = "/api/v1/admin/me/preferences/"


def _mk_staff(**kwargs) -> User:
    return User.objects.create_user(
        email=f"pref-{uuid.uuid4().hex[:10]}@test.com",
        password="Pass1234!",
        is_staff=True,
        **kwargs,
    )


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.mark.django_db
class TestPermissions:
    def test_anonymous_401(self, api):
        assert api.get(URL).status_code == 401

    def test_non_staff_403(self, api):
        user = User.objects.create_user(
            email=f"plain-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
        )
        api.force_authenticate(user=user)
        assert api.get(URL).status_code == 403

    def test_marketing_viewer_is_whitelisted(self):
        """외주도 자기 모바일 탭은 골라야 한다 — 여기 담기는 값은 권한 판정에 쓰이지 않는다."""
        user = _mk_staff()
        user.groups.add(Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)[0])
        client = Client()
        client.force_login(user)

        assert client.get(URL).status_code == 200
        res = client.patch(
            URL, data=json.dumps({"mobile_nav": ["/dashboard"]}), content_type="application/json"
        )
        assert res.status_code == 200
        assert res.json()["preferences"]["mobile_nav"] == ["/dashboard"]


@pytest.mark.django_db
class TestReadWrite:
    def test_empty_before_first_save(self, api):
        api.force_authenticate(user=_mk_staff())
        assert api.get(URL).data["preferences"] == {}

    def test_patch_returns_full_state(self, api):
        api.force_authenticate(user=_mk_staff())
        res = api.patch(URL, {"mobile_nav": ["/dashboard", "/users"]}, format="json")
        assert res.status_code == 200
        assert res.data["preferences"] == {"mobile_nav": ["/dashboard", "/users"]}

    def test_round_trip(self, api):
        user = _mk_staff()
        api.force_authenticate(user=user)
        api.patch(URL, {"mobile_nav": ["/pages"]}, format="json")
        assert api.get(URL).data["preferences"] == {"mobile_nav": ["/pages"]}

    def test_is_per_user(self, api):
        a, b = _mk_staff(), _mk_staff()
        api.force_authenticate(user=a)
        api.patch(URL, {"mobile_nav": ["/a"]}, format="json")
        api.force_authenticate(user=b)
        assert api.get(URL).data["preferences"] == {}


@pytest.mark.django_db
class TestMergeSemantics:
    def test_patch_merges_top_level_keys(self, api):
        """통째로 덮으면 두 화면이 서로의 설정을 지운다 — 그래서 키 단위 병합이다."""
        api.force_authenticate(user=_mk_staff())
        api.patch(URL, {"mobile_nav": ["/a"], "table_columns": {"users": ["email"]}}, format="json")
        res = api.patch(URL, {"mobile_nav": ["/b"]}, format="json")
        assert res.data["preferences"] == {
            "mobile_nav": ["/b"],
            "table_columns": {"users": ["email"]},  # 건드리지 않은 키는 살아남는다
        }

    def test_nested_values_are_replaced_not_deep_merged(self, api):
        """배열을 재귀 병합하면 '탭 3개로 줄이기' 가 불가능해진다."""
        api.force_authenticate(user=_mk_staff())
        api.patch(URL, {"mobile_nav": ["/a", "/b", "/c", "/d", "/e"]}, format="json")
        res = api.patch(URL, {"mobile_nav": ["/a", "/b", "/c"]}, format="json")
        assert res.data["preferences"]["mobile_nav"] == ["/a", "/b", "/c"]

    def test_null_deletes_key(self, api):
        api.force_authenticate(user=_mk_staff())
        api.patch(URL, {"mobile_nav": ["/a"], "keep": 1}, format="json")
        res = api.patch(URL, {"mobile_nav": None}, format="json")
        assert res.data["preferences"] == {"keep": 1}

    def test_no_schema_validation(self, api):
        """프론트 화면 설정이라 키가 자주 바뀐다 — 서버는 보관만 한다."""
        api.force_authenticate(user=_mk_staff())
        payload = {"whatever_new_key": {"deeply": ["nested", 1, True, None]}}
        assert api.patch(URL, payload, format="json").data["preferences"] == payload


@pytest.mark.django_db
class TestSizeLimit:
    def test_over_limit_is_rejected(self, api):
        api.force_authenticate(user=_mk_staff())
        res = api.patch(URL, {"blob": "x" * (MAX_PREFERENCES_BYTES + 100)}, format="json")
        assert res.status_code == 400
        assert res.data["error"]["details"]["code"] == "preferences_too_large"
        assert res.data["error"]["details"]["limit"] == MAX_PREFERENCES_BYTES

    def test_existing_value_survives_a_rejected_write(self, api):
        """상한 검사는 저장 **전에** — 넘겼다고 기존 설정이 날아가면 안 된다."""
        user = _mk_staff()
        api.force_authenticate(user=user)
        api.patch(URL, {"mobile_nav": ["/keep"]}, format="json")
        api.patch(URL, {"blob": "x" * (MAX_PREFERENCES_BYTES + 100)}, format="json")
        assert api.get(URL).data["preferences"] == {"mobile_nav": ["/keep"]}
        assert AdminPreference.objects.get(user=user).data == {"mobile_nav": ["/keep"]}

    def test_realistic_payload_fits_easily(self, api):
        """실제 탭 구성(경로 5개)은 상한의 5% 도 안 된다 — 4KB 가 빡빡하지 않다는 확인."""
        api.force_authenticate(user=_mk_staff())
        nav = ["/dashboard", "/pages", "/dashboard/marketing", "/auto-dm/campaigns", "/users"]
        res = api.patch(URL, {"mobile_nav": nav}, format="json")
        assert res.status_code == 200
        size = len(json.dumps(res.data["preferences"], ensure_ascii=False).encode("utf-8"))
        assert size < MAX_PREFERENCES_BYTES // 10

    def test_non_object_body_is_400(self, api):
        api.force_authenticate(user=_mk_staff())
        res = api.patch(URL, ["not", "an", "object"], format="json")
        assert res.status_code == 400
