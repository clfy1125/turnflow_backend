"""PATCH /pages/multipages/{id}/ 공개 토글 ↔ 활성 슬롯(is_active) 연동 테스트.

계약:
- is_public=true 로 켤 때 슬롯이 없으면(is_active=false):
  · 빈 슬롯 있으면(active < max_pages) is_active 자동 부여 → is_live=true
  · 슬롯 꽉 찼으면 409(ACTIVE_PAGE_SLOT_FULL), 상태 불변
- 자동 부여는 page-activation 하루 1회 제한과 무관
- is_public=false(언퍼블리시)는 is_active(슬롯) 유지
- 응답에 is_active/is_live 포함
더러운 테스트 DB 대응: 이메일/slug 는 uuid 로 유일화.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import SubscriptionPlan, SubscriptionStatus
from apps.billing.subscription_utils import ensure_subscription
from apps.pages.models import Page

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"slot-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _set_plan(user, name):
    """구독 플랜을 지정(get_effective_plan 은 non-cancelled 면 sub.plan 반환)."""
    sub = ensure_subscription(user)
    sub.plan = SubscriptionPlan.objects.get(name=name)
    sub.status = SubscriptionStatus.ACTIVE
    sub.save(update_fields=["plan", "status"])
    return sub


def _page(user, *, is_public, is_active):
    return Page.objects.create(
        user=user,
        slug=f"p-{uuid.uuid4().hex[:10]}",
        title="probe",
        is_public=is_public,
        is_active=is_active,
    )


def _url(page):
    return reverse("pages:multipage-detail", kwargs={"page_id": page.id})


@pytest.mark.django_db
class TestPublicSlotLinkage:
    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_publish_grants_empty_slot(self, user):
        # basic max_pages=5, 활성 1 + 비활성 1 → 빈 슬롯 있음
        _set_plan(user, "basic")
        _page(user, is_public=True, is_active=True)
        target = _page(user, is_public=False, is_active=False)

        res = self._client(user).patch(_url(target), {"is_public": True}, format="json")

        assert res.status_code == 200
        body = res.json()
        assert body["is_public"] is True
        assert body["is_active"] is True  # 자동 부여
        assert body["is_live"] is True
        target.refresh_from_db()
        assert target.is_active is True

    def test_publish_rejects_when_slots_full(self, user):
        # free max_pages=1, 활성 1(슬롯 꽉 참) + 비활성 1
        _set_plan(user, "free")
        _page(user, is_public=True, is_active=True)
        target = _page(user, is_public=False, is_active=False)

        res = self._client(user).patch(_url(target), {"is_public": True}, format="json")

        assert res.status_code == 409
        assert res.json()["error"]["details"]["reason"] == "ACTIVE_PAGE_SLOT_FULL"
        # 상태 불변 — 조용히 is_public 만 세팅되면 안 됨
        target.refresh_from_db()
        assert target.is_public is False
        assert target.is_active is False

    def test_publish_already_active_does_not_need_slot(self, user):
        # 이미 슬롯 보유(is_active=True) → 슬롯 꽉 차 있어도 그냥 공개
        _set_plan(user, "free")
        target = _page(user, is_public=False, is_active=True)  # 활성 1 = max 1

        res = self._client(user).patch(_url(target), {"is_public": True}, format="json")

        assert res.status_code == 200
        assert res.json()["is_live"] is True

    def test_toggle_not_rate_limited(self, user):
        # page-activation 을 오늘 이미 쓴 상태여도 공개 토글은 제한 없이 동작
        sub = _set_plan(user, "basic")
        sub.page_activation_changed_at = timezone.now()
        sub.save(update_fields=["page_activation_changed_at"])
        target = _page(user, is_public=False, is_active=False)
        c = self._client(user)

        assert c.patch(_url(target), {"is_public": True}, format="json").status_code == 200
        assert c.patch(_url(target), {"is_public": False}, format="json").status_code == 200
        assert c.patch(_url(target), {"is_public": True}, format="json").status_code == 200

    def test_unpublish_keeps_slot(self, user):
        _set_plan(user, "basic")
        target = _page(user, is_public=True, is_active=True)

        res = self._client(user).patch(_url(target), {"is_public": False}, format="json")

        assert res.status_code == 200
        target.refresh_from_db()
        assert target.is_public is False
        assert target.is_active is True  # 슬롯 유지 (재공개 시 스왑 불필요)

    def test_non_public_patch_ignores_slot_logic(self, user):
        # is_public 을 안 건드리는 PATCH(title 등)는 슬롯 검사 무관 — 슬롯 꽉 차도 통과
        _set_plan(user, "free")
        _page(user, is_public=True, is_active=True)
        target = _page(user, is_public=False, is_active=False)

        res = self._client(user).patch(_url(target), {"title": "renamed"}, format="json")

        assert res.status_code == 200
        target.refresh_from_db()
        assert target.title == "renamed"
        assert target.is_active is False  # 미변경
