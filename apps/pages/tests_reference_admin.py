"""어드민 AI 레퍼런스 API 테스트.

대상 엔드포인트:
  - /api/v1/admin/reference-categories/
  - /api/v1/admin/reference-categories/{id}/
  - /api/v1/admin/reference-pages/
  - /api/v1/admin/pages/{slug}/reference/
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
def regular_user(db):
    return User.objects.create_user(email="regular@example.com", password="Pass1234!")


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email="staff@example.com",
        password="Pass1234!",
        is_staff=True,
    )


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def regular_client(client, regular_user):
    client.force_authenticate(user=regular_user)
    return client


@pytest.fixture
def category(db):
    # 마이그레이션 시드와 별개로 테스트 전용 카테고리.
    return ReferenceCategory.objects.create(
        slug="test-cat", name="테스트 카테고리", sort_order=1, is_active=True
    )


@pytest.fixture
def public_page(db, regular_user):
    return Page.objects.create(
        user=regular_user, slug="public-page", title="공개 페이지", is_public=True
    )


@pytest.fixture
def private_page(db, regular_user):
    return Page.objects.create(
        user=regular_user, slug="private-page", title="비공개", is_public=False
    )


# ─── 권한 ───────────────────────────────────────────────────

class TestPermissions:
    def test_unauthenticated_cannot_list_categories(self, client):
        res = client.get("/api/v1/admin/reference-categories/")
        assert res.status_code == 401

    def test_regular_user_cannot_list_categories(self, regular_client):
        res = regular_client.get("/api/v1/admin/reference-categories/")
        assert res.status_code == 403

    def test_staff_can_list_categories(self, staff_client):
        res = staff_client.get("/api/v1/admin/reference-categories/")
        assert res.status_code == 200


# ─── 카테고리 CRUD ──────────────────────────────────────────

class TestCategoryCRUD:
    def test_create_category(self, staff_client):
        res = staff_client.post(
            "/api/v1/admin/reference-categories/",
            {"slug": "new-cat", "name": "신규", "sort_order": 99, "is_active": True},
            format="json",
        )
        assert res.status_code == 201
        assert res.data["slug"] == "new-cat"

    def test_create_duplicate_slug_fails(self, staff_client, category):
        res = staff_client.post(
            "/api/v1/admin/reference-categories/",
            {"slug": category.slug, "name": "또 똑같은", "sort_order": 50},
            format="json",
        )
        assert res.status_code == 400

    def test_patch_category_sort_order(self, staff_client, category):
        res = staff_client.patch(
            f"/api/v1/admin/reference-categories/{category.id}/",
            {"sort_order": 42},
            format="json",
        )
        assert res.status_code == 200
        category.refresh_from_db()
        assert category.sort_order == 42

    def test_reference_page_count_in_response(self, staff_client, category, public_page):
        public_page.is_reference = True
        public_page.reference_category = category
        public_page.save()

        res = staff_client.get("/api/v1/admin/reference-categories/")
        body = res.data
        target = next(c for c in body if c["slug"] == category.slug)
        assert target["reference_page_count"] == 1


# ─── 페이지 ↔ 레퍼런스 토글 ─────────────────────────────────

class TestPageReferenceUpdate:
    def test_set_page_as_reference(self, staff_client, public_page, category):
        res = staff_client.patch(
            f"/api/v1/admin/pages/{public_page.slug}/reference/",
            {
                "is_reference": True,
                "reference_category_id": category.id,
                "reference_order": 5,
                "reference_title": "감성 페이지",
            },
            format="json",
        )
        assert res.status_code == 200
        public_page.refresh_from_db()
        assert public_page.is_reference is True
        assert public_page.reference_category_id == category.id
        assert public_page.reference_order == 5

    def test_cannot_set_private_page_as_reference(self, staff_client, private_page, category):
        res = staff_client.patch(
            f"/api/v1/admin/pages/{private_page.slug}/reference/",
            {"is_reference": True, "reference_category_id": category.id},
            format="json",
        )
        assert res.status_code == 400
        private_page.refresh_from_db()
        assert private_page.is_reference is False

    def test_unset_reference_keeps_page(self, staff_client, public_page, category):
        public_page.is_reference = True
        public_page.reference_category = category
        public_page.save()

        res = staff_client.patch(
            f"/api/v1/admin/pages/{public_page.slug}/reference/",
            {"is_reference": False},
            format="json",
        )
        assert res.status_code == 200
        public_page.refresh_from_db()
        assert public_page.is_reference is False
        # category 는 유지됨 — 사용자가 명시적으로 null 보내야 해제.
        assert public_page.reference_category_id == category.id

    def test_invalid_category_id_returns_400(self, staff_client, public_page):
        res = staff_client.patch(
            f"/api/v1/admin/pages/{public_page.slug}/reference/",
            {"reference_category_id": 999_999},
            format="json",
        )
        assert res.status_code == 400


# ─── 레퍼런스 후보 목록 ─────────────────────────────────────

class TestReferencePageList:
    def test_list_filters_by_category(
        self, staff_client, public_page, regular_user, category
    ):
        # public_page 는 카테고리 매핑 X
        # other_page 는 카테고리 매핑 + is_reference
        other = Page.objects.create(
            user=regular_user, slug="other", is_public=True,
            is_reference=True, reference_category=category,
        )
        res = staff_client.get(
            f"/api/v1/admin/reference-pages/?category={category.slug}"
        )
        assert res.status_code == 200
        slugs = [item["slug"] for item in res.data["results"] if "slug" in item] or [
            item["slug"] for item in res.data
        ]
        assert "other" in slugs
        assert "public-page" not in slugs

    def test_list_excludes_private_by_default(
        self, staff_client, private_page, category
    ):
        private_page.is_reference = True
        private_page.reference_category = category
        private_page.save()
        res = staff_client.get("/api/v1/admin/reference-pages/")
        assert res.status_code == 200
        items = res.data.get("results") or res.data
        slugs = [item["slug"] for item in items]
        assert "private-page" not in slugs
