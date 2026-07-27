"""어드민 마케팅 채널 링크 CRUD(M-4) 테스트.

대상: apps/admin_api/views/marketing.py
(``/api/v1/admin/marketing/channel-links/``, IsAdminUser).

주의: 파일명이 tests_*.py 라 **경로 명시 실행** 필요:
``pytest apps/admin_api/tests_marketing_links.py``.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.admin_api.models import AdminActionLog, MarketingChannelLink

User = get_user_model()

URL = "/api/v1/admin/marketing/channel-links/"


def _mk_user(staff=False):
    return User.objects.create_user(
        email=f"u-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!", is_staff=staff
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def staff_user(db):
    return _mk_user(staff=True)


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def regular_client(client, db):
    client.force_authenticate(user=_mk_user())
    return client


PAYLOAD = {
    "name": "7월 틱톡 리타겟팅",
    "base_url": "https://turnflow.link/",
    "utm_source": "tiktok",
    "utm_medium": "cpc",
    "utm_campaign": "2026-07-retargeting",
    "utm_content": "video_a",
}


class TestAuth:
    def test_unauthenticated_401(self, client, db):
        assert client.get(URL).status_code == 401

    def test_non_staff_403(self, regular_client):
        assert regular_client.get(URL).status_code == 403
        assert regular_client.post(URL, PAYLOAD, format="json").status_code == 403


class TestCreate:
    def test_create_computes_url_and_channel(self, staff_client, staff_user):
        res = staff_client.post(URL, PAYLOAD, format="json")
        assert res.status_code == 201
        assert res.data["channel"] == "tiktok_ads"  # M-5 매핑과 계약 일치
        assert res.data["url"] == (
            "https://turnflow.link/?utm_source=tiktok&utm_medium=cpc"
            "&utm_campaign=2026-07-retargeting&utm_content=video_a"
        )
        assert res.data["created_by_email"] == staff_user.email
        # 감사 로그
        log = AdminActionLog.objects.filter(
            action=AdminActionLog.Action.CHANNEL_LINK_CREATE
        ).first()
        assert log is not None and log.actor_id == staff_user.id

    def test_existing_query_preserved_and_same_utm_replaced(self, staff_client):
        res = staff_client.post(
            URL,
            {
                "name": "기존 쿼리 병합",
                "base_url": "https://turnflow.link/pricing?ref=abc&utm_source=old",
                "utm_source": "kakao",
            },
            format="json",
        )
        assert res.status_code == 201
        assert res.data["url"] == "https://turnflow.link/pricing?ref=abc&utm_source=kakao"
        assert res.data["channel"] == "kakao_ads"

    def test_empty_utm_falls_back_to_other_channels(self, staff_client):
        res = staff_client.post(
            URL, {"name": "utm 없음", "base_url": "https://turnflow.link/"}, format="json"
        )
        assert res.status_code == 201
        assert res.data["url"] == "https://turnflow.link/"
        assert res.data["channel"] == "direct"

    def test_unmapped_source_with_paid_medium_is_paid_other(self, staff_client):
        res = staff_client.post(
            URL,
            {
                "name": "미매핑 광고",
                "base_url": "https://turnflow.link/",
                "utm_source": "some_network",
                "utm_medium": "cpc",
            },
            format="json",
        )
        assert res.status_code == 201
        assert res.data["channel"] == "paid_other"

    @pytest.mark.parametrize(
        "bad_url", ["ftp://turnflow.link/", "not-a-url", "javascript:alert(1)", ""]
    )
    def test_invalid_base_url_400(self, staff_client, bad_url):
        res = staff_client.post(
            URL, {"name": "x", "base_url": bad_url, "utm_source": "tiktok"}, format="json"
        )
        assert res.status_code == 400

    def test_name_required_400(self, staff_client):
        res = staff_client.post(URL, {"base_url": "https://turnflow.link/"}, format="json")
        assert res.status_code == 400


class TestListSharedScope:
    def test_links_shared_across_admins(self, client, db):
        admin_a, admin_b = _mk_user(staff=True), _mk_user(staff=True)
        client.force_authenticate(user=admin_a)
        assert client.post(URL, PAYLOAD, format="json").status_code == 201

        client.force_authenticate(user=admin_b)  # 다른 관리자도 조회 가능 (전 관리자 공용)
        res = client.get(URL)
        assert res.status_code == 200
        assert res.data["count"] == 1
        assert res.data["results"][0]["name"] == PAYLOAD["name"]

    def test_channel_filter(self, staff_client):
        staff_client.post(URL, PAYLOAD, format="json")
        staff_client.post(
            URL,
            {"name": "메타", "base_url": "https://turnflow.link/", "utm_source": "meta"},
            format="json",
        )
        res = staff_client.get(URL, {"channel": "tiktok_ads"})
        assert res.data["count"] == 1
        assert res.data["results"][0]["channel"] == "tiktok_ads"


class TestRenameAndDelete:
    def test_patch_renames_only(self, staff_client):
        link_id = staff_client.post(URL, PAYLOAD, format="json").data["id"]
        res = staff_client.patch(
            f"{URL}{link_id}/",
            {"name": "새 이름", "utm_source": "meta"},  # utm 은 무시돼야 함
            format="json",
        )
        assert res.status_code == 200
        assert res.data["name"] == "새 이름"
        assert res.data["utm_source"] == "tiktok"  # 불변
        assert res.data["channel"] == "tiktok_ads"  # 불변
        assert AdminActionLog.objects.filter(
            action=AdminActionLog.Action.CHANNEL_LINK_UPDATE
        ).exists()

    def test_delete_204(self, staff_client):
        link_id = staff_client.post(URL, PAYLOAD, format="json").data["id"]
        res = staff_client.delete(f"{URL}{link_id}/")
        assert res.status_code == 204
        assert not MarketingChannelLink.objects.filter(pk=link_id).exists()
        assert AdminActionLog.objects.filter(
            action=AdminActionLog.Action.CHANNEL_LINK_DELETE
        ).exists()

    def test_delete_missing_404(self, staff_client):
        assert staff_client.delete(f"{URL}999999/").status_code == 404
