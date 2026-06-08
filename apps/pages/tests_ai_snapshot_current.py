"""``is_current`` (활성 스냅샷 슬롯) 동작 테스트.

대상:
  - GET    /api/v1/pages/ai/@{slug}/snapshots/           → 각 항목 is_current
  - POST   /api/v1/pages/ai/@{slug}/                      → 편집 직후 latest_ai_result 가 현재
  - POST   /api/v1/pages/ai/@{slug}/snapshots/{id}/restore/ → 복원한 스냅샷이 현재 + 응답에 활성 슬롯
  - 일반 편집 경로(블록 생성/수정/삭제/재정렬, 페이지 메타/CSS) → 포인터 NULL 초기화
  - PageSnapshot 삭제 → Page.current_snapshot 자동 NULL (SET_NULL)

핵심 회귀: "두 스냅샷의 blocks_count 가 같아도" 서버가 권위 있게 is_current 를
내려준다(프론트의 블록 수 휴리스틱 한계 해소).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from .models import Block, Page, PageSnapshot

User = get_user_model()


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="snapcur@example.com", password="Pass1234!")


@pytest.fixture
def auth_client(client, user):
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def page(db, user):
    """초기 블록 1개를 가진 비공개 페이지."""
    p = Page.objects.create(user=user, slug="cur-page", title="원본", is_public=False)
    Block.objects.create(page=p, type=Block.BlockType.PROFILE, order=1, data={"headline": "원본"})
    return p


# ─────────────────────────────────────────────────────────────
# URL 헬퍼
# ─────────────────────────────────────────────────────────────

def _edit_url(slug: str) -> str:
    return f"/api/v1/pages/ai/@{slug}/"


def _snapshots_url(slug: str) -> str:
    return f"/api/v1/pages/ai/@{slug}/snapshots/"


def _restore_url(slug: str, snap_id: int) -> str:
    return f"/api/v1/pages/ai/@{slug}/snapshots/{snap_id}/restore/"


def _ai_edit(auth_client, slug: str, blocks: list[dict], **page_fields):
    payload = {"blocks": blocks, **page_fields}
    return auth_client.post(_edit_url(slug), payload, format="json")


def _snapshots_by_reason(auth_client, slug: str) -> dict[str, dict]:
    res = auth_client.get(_snapshots_url(slug))
    assert res.status_code == 200, res.content
    return {item["reason"]: item for item in res.json()["snapshots"]}


# ─────────────────────────────────────────────────────────────
# 기본 동작
# ─────────────────────────────────────────────────────────────

class TestSnapshotIsCurrent:
    def test_no_snapshots_returns_empty(self, auth_client, page):
        """AI 편집을 한 번도 안 한 페이지 — 스냅샷 0건, is_current 이슈 없음."""
        res = auth_client.get(_snapshots_url(page.slug))
        assert res.status_code == 200
        assert res.json() == {"snapshots": []}

    def test_ai_edit_marks_latest_as_current(self, auth_client, page):
        """AI 편집 직후 → latest_ai_result 가 is_current, ai_edit 는 아님 (현재 1건)."""
        res = _ai_edit(
            auth_client,
            page.slug,
            blocks=[
                {"type": "profile", "data": {"headline": "AI"}},
                {"type": "single_link", "data": {"url": "https://ex.com", "label": "L"}},
            ],
        )
        assert res.status_code == 200, res.content

        by_reason = _snapshots_by_reason(auth_client, page.slug)
        assert by_reason["latest_ai_result"]["is_current"] is True
        assert by_reason["ai_edit"]["is_current"] is False
        # 정상 상태에서 is_current=true 는 최대 1건
        currents = [s for s in by_reason.values() if s["is_current"]]
        assert len(currents) == 1

        page.refresh_from_db()
        assert page.current_snapshot_id is not None
        assert page.current_snapshot.reason == PageSnapshot.Reason.LATEST_AI_RESULT

    def test_is_current_robust_to_equal_block_counts(self, auth_client, page):
        """원본과 편집본의 블록 수가 같아도(휴리스틱 실패 케이스) 서버는 정확히 판정."""
        # 원본 블록 수 = 1. 편집본도 블록 1개 → blocks_count 동일.
        res = _ai_edit(
            auth_client,
            page.slug,
            blocks=[{"type": "profile", "data": {"headline": "수정됨"}}],
        )
        assert res.status_code == 200, res.content

        by_reason = _snapshots_by_reason(auth_client, page.slug)
        assert by_reason["ai_edit"]["blocks_count"] == by_reason["latest_ai_result"]["blocks_count"]
        # 블록 수가 같아도 latest 만 현재
        assert by_reason["latest_ai_result"]["is_current"] is True
        assert by_reason["ai_edit"]["is_current"] is False


# ─────────────────────────────────────────────────────────────
# 복원
# ─────────────────────────────────────────────────────────────

class TestRestoreCurrent:
    def test_restore_moves_current_and_response_includes_active_slot(self, auth_client, page):
        _ai_edit(
            auth_client,
            page.slug,
            blocks=[
                {"type": "profile", "data": {"headline": "AI"}},
                {"type": "single_link", "data": {"url": "https://ex.com", "label": "L"}},
            ],
        )
        by_reason = _snapshots_by_reason(auth_client, page.slug)
        original_id = by_reason["ai_edit"]["id"]

        res = auth_client.post(_restore_url(page.slug, original_id), {}, format="json")
        assert res.status_code == 200, res.content
        body = res.json()
        # (선택) 복원 응답에 활성 슬롯 정보 동봉
        assert body["current_snapshot_id"] == original_id
        assert body["current_reason"] == "ai_edit"

        by_reason = _snapshots_by_reason(auth_client, page.slug)
        assert by_reason["ai_edit"]["is_current"] is True
        assert by_reason["latest_ai_result"]["is_current"] is False

    def test_restore_back_to_latest(self, auth_client, page):
        """원본 ↔ 작업물 토글이 양방향으로 동작."""
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        by_reason = _snapshots_by_reason(auth_client, page.slug)
        latest_id = by_reason["latest_ai_result"]["id"]
        original_id = by_reason["ai_edit"]["id"]

        auth_client.post(_restore_url(page.slug, original_id), {}, format="json")
        auth_client.post(_restore_url(page.slug, latest_id), {}, format="json")

        by_reason = _snapshots_by_reason(auth_client, page.slug)
        assert by_reason["latest_ai_result"]["is_current"] is True
        assert by_reason["ai_edit"]["is_current"] is False


# ─────────────────────────────────────────────────────────────
# 일반 편집 → 포인터 NULL 초기화 (핵심)
# ─────────────────────────────────────────────────────────────

class TestManualEditClearsPointer:
    """복원/AI 편집 후 사용자가 직접 편집하면 어느 슬롯과도 일치하지 않아야 한다 (0건)."""

    def _assert_none_current(self, auth_client, slug):
        by_reason = _snapshots_by_reason(auth_client, slug)
        assert all(not s["is_current"] for s in by_reason.values())

    def test_block_create_clears(self, auth_client, page):
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        res = auth_client.post(
            f"/api/v1/pages/multipages/{page.id}/blocks/",
            {"type": "single_link", "data": {"url": "https://new.com", "label": "새"}},
            format="json",
        )
        assert res.status_code == 201, res.content
        self._assert_none_current(auth_client, page.slug)
        page.refresh_from_db()
        assert page.current_snapshot_id is None

    def test_block_patch_clears(self, auth_client, page):
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        block = page.blocks.first()
        res = auth_client.patch(
            f"/api/v1/pages/multipages/{page.id}/blocks/{block.id}/",
            {"data": {"headline": "손수정"}},
            format="json",
        )
        assert res.status_code == 200, res.content
        self._assert_none_current(auth_client, page.slug)

    def test_block_delete_clears(self, auth_client, page):
        _ai_edit(
            auth_client,
            page.slug,
            blocks=[
                {"type": "profile", "data": {"headline": "AI"}},
                {"type": "single_link", "data": {"url": "https://ex.com", "label": "L"}},
            ],
        )
        block = page.blocks.first()
        res = auth_client.delete(f"/api/v1/pages/multipages/{page.id}/blocks/{block.id}/")
        assert res.status_code == 204
        self._assert_none_current(auth_client, page.slug)

    def test_block_reorder_clears(self, auth_client, page):
        _ai_edit(
            auth_client,
            page.slug,
            blocks=[
                {"type": "profile", "data": {"headline": "AI"}},
                {"type": "single_link", "data": {"url": "https://ex.com", "label": "L"}},
            ],
        )
        # 충돌 없는 새 order 로 이동 (단일 UPDATE 스왑 제약과 무관하게 reorder 성공).
        # 핵심은 "order 가 바뀌면 포인터가 해제된다" 이다.
        blocks = list(page.blocks.order_by("order"))
        orders = [
            {"id": blocks[0].id, "order": 10},
            {"id": blocks[1].id, "order": 11},
        ]
        res = auth_client.post(
            f"/api/v1/pages/multipages/{page.id}/blocks/reorder/",
            {"orders": orders},
            format="json",
        )
        assert res.status_code == 200, res.content
        self._assert_none_current(auth_client, page.slug)

    def test_page_patch_clears(self, auth_client, page):
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        res = auth_client.patch(
            f"/api/v1/pages/multipages/{page.id}/",
            {"title": "직접 바꾼 제목"},
            format="json",
        )
        assert res.status_code == 200, res.content
        self._assert_none_current(auth_client, page.slug)

    def test_restore_then_manual_edit_clears(self, auth_client, page):
        """복원 후 직접 편집 → 두 슬롯 모두 false (프론트가 가장 우려한 시나리오)."""
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        by_reason = _snapshots_by_reason(auth_client, page.slug)
        auth_client.post(_restore_url(page.slug, by_reason["ai_edit"]["id"]), {}, format="json")

        # 복원 직후엔 ai_edit 가 현재
        by_reason = _snapshots_by_reason(auth_client, page.slug)
        assert by_reason["ai_edit"]["is_current"] is True

        # 손으로 한 글자라도 고치면 더 이상 그 스냅샷과 같지 않음
        auth_client.patch(
            f"/api/v1/pages/multipages/{page.id}/",
            {"title": "복원 후 수정"},
            format="json",
        )
        by_reason = _snapshots_by_reason(auth_client, page.slug)
        assert all(not s["is_current"] for s in by_reason.values())


# ─────────────────────────────────────────────────────────────
# 모델 레벨 — SET_NULL / 멱등 no-op
# ─────────────────────────────────────────────────────────────

class TestPointerModelBehavior:
    def test_snapshot_delete_nulls_pointer(self, db, page):
        snap = PageSnapshot.objects.create(
            page=page,
            reason=PageSnapshot.Reason.LATEST_AI_RESULT,
            snapshot={"page": {}, "blocks": []},
        )
        page.current_snapshot = snap
        page.save(update_fields=["current_snapshot", "updated_at"])

        snap.delete()
        page.refresh_from_db()
        assert page.current_snapshot_id is None

    def test_detach_is_noop_when_already_null(self, db, page):
        assert page.current_snapshot_id is None
        # 예외 없이 통과해야 하고 NULL 유지
        page.detach_snapshot_pointer()
        page.refresh_from_db()
        assert page.current_snapshot_id is None

    def test_detach_clears_existing_pointer(self, db, page):
        snap = PageSnapshot.objects.create(
            page=page,
            reason=PageSnapshot.Reason.AI_EDIT,
            snapshot={"page": {}, "blocks": []},
        )
        page.current_snapshot = snap
        page.save(update_fields=["current_snapshot", "updated_at"])

        page.detach_snapshot_pointer()
        page.refresh_from_db()
        assert page.current_snapshot_id is None
