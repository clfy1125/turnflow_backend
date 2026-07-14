"""페이지 활성화 조정 엔드포인트 테스트.

GET/POST /billing/page-activation/ — 다운그레이드 후 플랜 한도에 맞춰 활성 페이지 재선택.
핵심 계약:
- 응답이 실제 is_public/is_live 를 반영 (is_active 단독 오표시 버그 회귀 방지)
- POST 가 선택/미선택 페이지의 is_active + is_public 을 함께 맞춤
- needs_activation_adjustment 상황에서는 하루 1회 제한 우회
더러운 테스트 DB 대응: 이메일/slug 는 uuid 로 유일화.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.subscription_utils import ensure_subscription
from apps.pages.models import Page

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"pageact-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _page(user, *, is_public=True, is_active=True):
    return Page.objects.create(
        user=user,
        slug=f"p-{uuid.uuid4().hex[:10]}",
        title="probe",
        is_public=is_public,
        is_active=is_active,
    )


@pytest.mark.django_db
class TestPageActivation:
    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_get_flags_adjustment_when_over_allowance(self, user):
        # 무료 플랜 max_pages=1, 페이지 2개 → 조정 필요
        _page(user)
        _page(user)
        ensure_subscription(user)

        res = self._client(user).get(reverse("billing:page-activation"))

        assert res.status_code == 200
        data = res.json()
        assert data["max_pages"] == 1
        assert data["total_pages"] == 2
        assert data["needs_activation_adjustment"] is True
        assert {"id", "slug", "title", "is_active", "is_public", "is_live"} <= set(
            data["pages"][0].keys()
        )

    def test_get_reflects_is_public_change(self, user):
        # 회귀: is_public=false 로 바꾸면 is_live 가 false 로 내려와야 함 (is_active 단독 신뢰 금지)
        p = _page(user, is_public=True, is_active=True)
        ensure_subscription(user)
        c = self._client(user)

        before = c.get(reverse("billing:page-activation")).json()["pages"][0]
        assert before["is_live"] is True

        p.is_public = False
        p.save(update_fields=["is_public"])

        after = c.get(reverse("billing:page-activation")).json()["pages"][0]
        assert after["is_public"] is False
        assert after["is_live"] is False
        assert after["is_active"] is True  # 슬롯은 그대로 (billing 관점)

    def test_post_syncs_active_and_public(self, user):
        keep = _page(user, is_public=False, is_active=True)  # 초안이어도 선택 시 게시됨
        drop = _page(user, is_public=True, is_active=True)
        ensure_subscription(user)

        res = self._client(user).post(
            reverse("billing:page-activation"),
            {"active_page_ids": [keep.id]},
            format="json",
        )

        assert res.status_code == 200
        keep.refresh_from_db()
        drop.refresh_from_db()
        assert keep.is_active is True and keep.is_public is True
        assert drop.is_active is False and drop.is_public is False

    def test_post_bypasses_daily_limit_when_adjustment_needed(self, user):
        # 이미 오늘 변경한 이력이 있어도, 초과 상태면 강제 조정으로 다시 성공해야 함
        keep = _page(user)
        _page(user)  # 총 2개 > max 1 → needs_adjustment
        sub = ensure_subscription(user)
        sub.page_activation_changed_at = timezone.now()
        sub.save(update_fields=["page_activation_changed_at"])

        res = self._client(user).post(
            reverse("billing:page-activation"),
            {"active_page_ids": [keep.id]},
            format="json",
        )

        assert res.status_code == 200

    def test_post_rejects_over_allowance(self, user):
        p1 = _page(user)
        p2 = _page(user)
        ensure_subscription(user)

        res = self._client(user).post(
            reverse("billing:page-activation"),
            {"active_page_ids": [p1.id, p2.id]},  # 2개 > max 1
            format="json",
        )

        assert res.status_code == 400

    def test_post_rejects_foreign_page(self, user):
        other = User.objects.create_user(
            email=f"other-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
        )
        foreign = _page(other)
        _page(user)
        ensure_subscription(user)

        res = self._client(user).post(
            reverse("billing:page-activation"),
            {"active_page_ids": [foreign.id]},
            format="json",
        )

        assert res.status_code == 400
