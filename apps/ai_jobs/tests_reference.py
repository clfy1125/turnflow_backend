"""AI Job 생성 시 reference_page_slug 처리 테스트."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.pages.models import Page, ReferenceCategory

from .models import AiJob

User = get_user_model()


@pytest.fixture(autouse=True)
def _no_celery_dispatch(monkeypatch):
    """run_ai_job.delay 가 실제 Celery 브로커로 안 가도록 stub.

    AiJob row 만 만들어지고 LLM 호출은 안 일어남.
    """
    monkeypatch.setattr(
        "apps.ai_jobs.views.run_ai_job",
        type("Stub", (), {"delay": staticmethod(lambda *a, **kw: None)}),
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="creator@example.com", password="Pass1234!")


@pytest.fixture
def auth_client(client, user):
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def category(db):
    return ReferenceCategory.objects.create(
        slug="lit-cat", name="문학", sort_order=1, is_active=True
    )


@pytest.fixture
def ref_page(db, user, category):
    return Page.objects.create(
        user=user,
        slug="great-ref",
        title="좋은 레퍼런스",
        is_public=True,
        is_reference=True,
        reference_category=category,
        reference_order=1,
        reference_snapshot_status="succeeded",
    )


class TestAiJobCreateWithReference:
    def test_with_valid_reference_slug_records_in_payload(self, auth_client, ref_page):
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {
                "concept": "내 카페 페이지",
                "reference_page_slug": ref_page.slug,
            },
            format="json",
        )
        assert res.status_code == 202
        job = AiJob.objects.latest("created_at")
        assert job.input_payload["reference_page_slug"] == ref_page.slug

    def test_invalid_reference_slug_returns_400(self, auth_client):
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "X", "reference_page_slug": "non-existent"},
            format="json",
        )
        assert res.status_code == 400

    def test_reference_slug_but_not_is_reference_returns_400(self, auth_client, user):
        # 공개 페이지지만 is_reference=False 인 경우.
        Page.objects.create(user=user, slug="not-curated", is_public=True, is_reference=False)
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "X", "reference_page_slug": "not-curated"},
            format="json",
        )
        assert res.status_code == 400

    def test_category_slug_no_longer_auto_picks(self, auth_client, ref_page, user, category):
        # 유저가 reference_page_slug 를 명시하지 않으면, 카테고리에 큐레이션된
        # 레퍼런스 페이지가 있어도 자동 주입하지 않고 레퍼런스 없이 진행한다.
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "X", "reference_category_slug": category.slug},
            format="json",
        )
        assert res.status_code == 202
        job = AiJob.objects.latest("created_at")
        assert job.input_payload["reference_page_slug"] == ""

    def test_without_reference_works_with_fallback(self, auth_client):
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "X"},
            format="json",
        )
        assert res.status_code == 202
        job = AiJob.objects.latest("created_at")
        # 빈 문자열 — 폴백 트리거
        assert job.input_payload["reference_page_slug"] == ""
