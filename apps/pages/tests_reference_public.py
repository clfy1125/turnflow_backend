"""공개 AI 레퍼런스 API 테스트.

대상 엔드포인트:
  - GET /api/v1/ai/categories/
  - GET /api/v1/ai/categories/{slug}/references/
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from .models import Page, ReferenceCategory

User = get_user_model()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="u1@example.com", password="Pass1234!")


@pytest.fixture
def auth_client(client, user):
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def category_active(db):
    return ReferenceCategory.objects.create(
        slug="active-cat", name="활성", sort_order=10, is_active=True
    )


@pytest.fixture
def category_inactive(db):
    return ReferenceCategory.objects.create(
        slug="inactive-cat", name="비활성", sort_order=11, is_active=False
    )


@pytest.fixture
def ref_page(db, user, category_active):
    """is_public + is_reference + 스냅샷 succeeded — 정상적으로 공개되어야 함."""
    return Page.objects.create(
        user=user,
        slug="ready-ref",
        title="준비된 레퍼런스",
        is_public=True,
        is_reference=True,
        reference_category=category_active,
        reference_order=1,
        reference_snapshot_status=Page.SnapshotStatus.SUCCEEDED,
    )


class TestCategoryList:
    def test_unauthenticated_returns_401(self, client):
        res = client.get("/api/v1/ai/categories/")
        assert res.status_code == 401

    def test_lists_only_active(self, auth_client, category_active, category_inactive):
        res = auth_client.get("/api/v1/ai/categories/")
        assert res.status_code == 200
        slugs = [c["slug"] for c in res.data]
        assert "active-cat" in slugs
        assert "inactive-cat" not in slugs

    def test_response_contains_reference_count(self, auth_client, ref_page):
        res = auth_client.get("/api/v1/ai/categories/")
        assert res.status_code == 200
        body = res.data
        target = next(c for c in body if c["slug"] == "active-cat")
        assert target["reference_count"] == 1

    def test_inactive_category_pages_not_counted(
        self, auth_client, category_active, category_inactive, user
    ):
        # active 안에 1, inactive 안에 1 — 응답에 inactive 항목이 아예 없어야 함.
        Page.objects.create(
            user=user, slug="a", is_public=True, is_reference=True,
            reference_category=category_active,
            reference_snapshot_status="succeeded",
        )
        Page.objects.create(
            user=user, slug="b", is_public=True, is_reference=True,
            reference_category=category_inactive,
            reference_snapshot_status="succeeded",
        )
        res = auth_client.get("/api/v1/ai/categories/")
        slugs = [c["slug"] for c in res.data]
        assert "inactive-cat" not in slugs


class TestCategoryPages:
    def test_404_for_missing_category(self, auth_client):
        res = auth_client.get("/api/v1/ai/categories/no-such/references/")
        assert res.status_code == 404

    def test_lists_reference_pages_in_category(self, auth_client, ref_page):
        res = auth_client.get("/api/v1/ai/categories/active-cat/references/")
        assert res.status_code == 200
        slugs = [p["slug"] for p in res.data]
        assert "ready-ref" in slugs

    def test_excludes_pending_snapshot_pages(
        self, auth_client, user, category_active
    ):
        Page.objects.create(
            user=user, slug="pending", is_public=True, is_reference=True,
            reference_category=category_active,
            reference_snapshot_status="pending",
        )
        res = auth_client.get("/api/v1/ai/categories/active-cat/references/")
        slugs = [p["slug"] for p in res.data]
        assert "pending" not in slugs

    def test_excludes_private_pages(
        self, auth_client, user, category_active
    ):
        Page.objects.create(
            user=user, slug="priv", is_public=False, is_reference=True,
            reference_category=category_active,
            reference_snapshot_status="succeeded",
        )
        res = auth_client.get("/api/v1/ai/categories/active-cat/references/")
        slugs = [p["slug"] for p in res.data]
        assert "priv" not in slugs

    def test_respects_reference_order(self, auth_client, user, category_active):
        Page.objects.create(
            user=user, slug="b", is_public=True, is_reference=True,
            reference_category=category_active, reference_order=2,
            reference_snapshot_status="succeeded",
        )
        Page.objects.create(
            user=user, slug="a", is_public=True, is_reference=True,
            reference_category=category_active, reference_order=1,
            reference_snapshot_status="succeeded",
        )
        res = auth_client.get("/api/v1/ai/categories/active-cat/references/")
        assert res.status_code == 200
        slugs = [p["slug"] for p in res.data]
        assert slugs.index("a") < slugs.index("b")
