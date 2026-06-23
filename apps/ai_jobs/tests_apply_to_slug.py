"""새 페이지 생성 시 `apply_to_slug` → 백엔드 자동 적용 흐름 테스트.

- 뷰: 빈 페이지 slug 를 `apply_to_slug` 로 받으면 job.page 에 연결한다.
       (성공 시 run_ai_job 이 그 페이지에 result_json 을 적용한다 — Option B)
- 뷰: 없는/타인 페이지 slug 면 404.
- 뷰: 리메이크(slug 전달)면 apply_to_slug 는 무시한다.
- 적용 메커니즘: apply_result_json_to_page 가 페이지를 result_json 으로 덮어쓴다
       (run_ai_job 성공 분기가 호출하는 바로 그 함수의 엔드-스테이트 계약).

실행: tests_*.py 는 pytest python_files(test_*.py) 패턴이 아니라 디렉터리
자동 수집이 안 된다 → 파일 경로를 명시해 실행한다.
    docker compose exec web pytest apps/ai_jobs/tests_apply_to_slug.py
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.pages.models import Block, Page

from .models import AiJob
from .services.page_applier import apply_result_json_to_page

User = get_user_model()


@pytest.fixture(autouse=True)
def _no_celery_dispatch(monkeypatch):
    """run_ai_job.delay 가 실제 Celery 브로커로 안 가도록 stub (AiJob row 만 생성)."""
    monkeypatch.setattr(
        "apps.ai_jobs.views.run_ai_job",
        type("Stub", (), {"delay": staticmethod(lambda *a, **kw: None)}),
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="creator@example.com", password="Pass1234!")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="Pass1234!")


@pytest.fixture
def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def blank_page(db, user):
    """프론트가 새 페이지 생성 직전에 만들어 두는 빈 페이지."""
    return Page.objects.create(user=user, slug="page-blank01", title="", is_public=True)


class TestApplyToSlugLinking:
    def test_links_target_page_to_job(self, auth_client, blank_page):
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "PC방 동아리 부스 홍보", "apply_to_slug": blank_page.slug},
            format="json",
        )
        assert res.status_code == 202
        job = AiJob.objects.latest("created_at")
        # 성공 시 run_ai_job 이 이 페이지에 적용하도록 FK 가 걸려 있어야 한다.
        assert job.page_id == blank_page.id

    def test_unknown_apply_to_slug_returns_404(self, auth_client):
        # 테스트 DB 가 깨끗하지 않으므로(leftover row) 절대수 대신 델타로 단언한다.
        before = AiJob.objects.count()
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "X", "apply_to_slug": "no-such-page"},
            format="json",
        )
        assert res.status_code == 404
        # 404 는 job 생성 전에 반환되므로 새 job 이 생기면 안 된다.
        assert AiJob.objects.count() == before

    def test_other_users_page_returns_404(self, auth_client, other_user):
        Page.objects.create(user=other_user, slug="page-foreign", is_public=True)
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "X", "apply_to_slug": "page-foreign"},
            format="json",
        )
        assert res.status_code == 404

    def test_without_apply_to_slug_page_is_none(self, auth_client):
        res = auth_client.post("/api/v1/ai/jobs/", {"concept": "X"}, format="json")
        assert res.status_code == 202
        job = AiJob.objects.latest("created_at")
        assert job.page_id is None

    def test_remake_ignores_apply_to_slug(self, auth_client, user, blank_page):
        # slug(리메이크) 가 있으면 page=source_page 가 잡히고 apply_to_slug 는 무시된다.
        source = Page.objects.create(user=user, slug="page-source", title="원본", is_public=True)
        Block.objects.create(page=source, type="profile", order=1, data={"headline": "안녕"})
        res = auth_client.post(
            "/api/v1/ai/jobs/",
            {"concept": "리메이크", "slug": source.slug, "apply_to_slug": blank_page.slug},
            format="json",
        )
        assert res.status_code == 202
        job = AiJob.objects.latest("created_at")
        # 리메이크 대상(source)에 묶여야 하고, apply_to_slug(blank_page)는 무시.
        assert job.page_id == source.id


class TestApplyResultJsonToPage:
    """run_ai_job 성공 분기가 호출하는 적용 함수의 엔드-스테이트 계약."""

    def test_applies_meta_and_replaces_blocks(self, db, user):
        page = Page.objects.create(user=user, slug="page-apply", title="", is_public=False)
        # 빈 페이지에 남아 있던 placeholder 블록 1개.
        Block.objects.create(page=page, type="profile", order=1, data={"headline": ""})

        result_json = {
            "title": "PC방 동아리 부스 홍보",
            "is_public": True,
            "data": {"theme": "dark"},
            "custom_css": ".page-container{padding:18px}",
            "blocks": [
                {"type": "profile", "order": 1, "data": {"headline": "동아리 부스"}},
                {"type": "single_link", "order": 2, "data": {"label": "참가 신청", "url": None}},
                {"type": "single_link", "order": 3, "data": {"label": "위치", "url": None}},
            ],
        }
        apply_result_json_to_page(page, result_json)

        page.refresh_from_db()
        assert page.title == "PC방 동아리 부스 홍보"
        assert page.is_public is True
        assert page.data == {"theme": "dark"}
        blocks = list(Block.objects.filter(page=page).order_by("order"))
        assert [b.type for b in blocks] == ["profile", "single_link", "single_link"]
        assert blocks[0].data["headline"] == "동아리 부스"

    def test_zero_based_orders_do_not_collide(self, db, user):
        """LLM 이 0-based order(0,1,2,...) 를 내도 (page_id, order) 유니크 제약을 안 깬다.

        회귀: 예전엔 `raw.order or (i+1)` 이라 order=0(falsy)이 1 로 바뀌어 다음
        블록(order=1)과 충돌 → IntegrityError 로 적용 실패(빈 페이지)했다.
        """
        page = Page.objects.create(user=user, slug="page-zero", title="", is_public=True)
        Block.objects.create(page=page, type="profile", order=0, data={})

        result_json = {
            "title": "PC방 부스 홍보",
            "blocks": [
                (
                    {"type": "profile", "order": i, "data": {}}
                    if i == 0
                    else {"type": "single_link", "order": i, "data": {"label": f"링크{i}"}}
                )
                for i in range(11)  # order 0~10 (0-based)
            ],
        }
        # 예전 코드라면 여기서 IntegrityError 가 났다.
        apply_result_json_to_page(page, result_json)

        orders = list(
            Block.objects.filter(page=page).order_by("order").values_list("order", flat=True)
        )
        assert orders == list(range(1, 12))  # 1~11, 중복 없음
        assert len(orders) == 11
