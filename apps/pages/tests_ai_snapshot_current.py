"""``is_current`` (활성 스냅샷 슬롯) + bounded history 동작 테스트.

대상:
  - GET    /api/v1/pages/ai/@{slug}/snapshots/           → 각 항목 is_current + 이력 누적
  - POST   /api/v1/pages/ai/@{slug}/                      → 편집마다 ai_result 새 항목 추가(덮어쓰기 X)
  - POST   /api/v1/pages/ai/@{slug}/snapshots/{id}/restore/ → 임의 시점 복원 + 활성 슬롯 + 직전 상태 보관
  - 일반 편집 경로(블록 생성/수정/삭제/재정렬, 페이지 메타/CSS) → 포인터 NULL 초기화
  - PageSnapshot 삭제 → Page.current_snapshot 자동 NULL (SET_NULL)

핵심 회귀:
  1. "두 스냅샷의 blocks_count 가 같아도" 서버가 권위 있게 is_current 를 내려준다.
  2. "리뉴얼할 때마다 직전 작업물이 사라지지 않는다" — AI 편집마다 ai_result 가 누적되고
     과거 작업물로도 복원할 수 있다 (예전 단일 슬롯 upsert 회귀 방지).
  3. 보관 한도(10) 초과 시 오래된 것부터 정리하되 원본(ai_edit)·현재 활성본은 보존.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from .aiviews import MAX_SNAPSHOTS_PER_PAGE
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


def _snapshots(auth_client, slug: str) -> list[dict]:
    """변경 기록 전체 (최신순)."""
    res = auth_client.get(_snapshots_url(slug))
    assert res.status_code == 200, res.content
    return res.json()["snapshots"]


def _by_reason(auth_client, slug: str) -> dict[str, dict]:
    """reason → 항목. 같은 reason 이 여러 건이면 가장 최신 것이 남는다(편의용)."""
    items = _snapshots(auth_client, slug)
    # 최신순이므로 역순으로 넣어 최신이 마지막에 덮어쓰게 → 최신 보존
    by: dict[str, dict] = {}
    for item in reversed(items):
        by[item["reason"]] = item
    return by


def _ai_results(auth_client, slug: str) -> list[dict]:
    """ai_result(+레거시 latest_ai_result) 항목만, 최신순."""
    return [
        s for s in _snapshots(auth_client, slug) if s["reason"] in ("ai_result", "latest_ai_result")
    ]


def _current(auth_client, slug: str) -> list[dict]:
    return [s for s in _snapshots(auth_client, slug) if s["is_current"]]


# ─────────────────────────────────────────────────────────────
# 기본 동작 — is_current
# ─────────────────────────────────────────────────────────────


class TestSnapshotIsCurrent:
    def test_no_snapshots_returns_empty(self, auth_client, page):
        """AI 편집을 한 번도 안 한 페이지 — 스냅샷 0건, is_current 이슈 없음."""
        res = auth_client.get(_snapshots_url(page.slug))
        assert res.status_code == 200
        assert res.json() == {"snapshots": []}

    def test_ai_edit_marks_latest_as_current(self, auth_client, page):
        """AI 편집 직후 → 방금 추가된 ai_result 가 is_current, ai_edit(원본) 는 아님."""
        res = _ai_edit(
            auth_client,
            page.slug,
            blocks=[
                {"type": "profile", "data": {"headline": "AI"}},
                {"type": "single_link", "data": {"url": "https://ex.com", "label": "L"}},
            ],
        )
        assert res.status_code == 200, res.content

        by = _by_reason(auth_client, page.slug)
        assert by["ai_result"]["is_current"] is True
        assert by["ai_edit"]["is_current"] is False
        # 정상 상태에서 is_current=true 는 최대 1건
        assert len(_current(auth_client, page.slug)) == 1

        page.refresh_from_db()
        assert page.current_snapshot_id is not None
        assert page.current_snapshot.reason == PageSnapshot.Reason.AI_RESULT

    def test_is_current_robust_to_equal_block_counts(self, auth_client, page):
        """원본과 편집본의 블록 수가 같아도(휴리스틱 실패 케이스) 서버는 정확히 판정."""
        # 원본 블록 수 = 1. 편집본도 블록 1개 → blocks_count 동일.
        res = _ai_edit(
            auth_client,
            page.slug,
            blocks=[{"type": "profile", "data": {"headline": "수정됨"}}],
        )
        assert res.status_code == 200, res.content

        by = _by_reason(auth_client, page.slug)
        assert by["ai_result"]["blocks_count"] == by["ai_edit"]["blocks_count"]
        # 블록 수가 같아도 ai_result 만 현재
        assert by["ai_result"]["is_current"] is True
        assert by["ai_edit"]["is_current"] is False


# ─────────────────────────────────────────────────────────────
# bounded history — "리뉴얼 전이 없어진다" 회귀 방지 (핵심)
# ─────────────────────────────────────────────────────────────


class TestSnapshotHistory:
    def test_each_ai_edit_appends_not_overwrites(self, auth_client, page):
        """AI 편집 3회 → 원본 1 + 작업물 3 누적. 직전 작업물이 사라지지 않는다."""
        for i in range(3):
            res = _ai_edit(
                auth_client,
                page.slug,
                blocks=[{"type": "profile", "data": {"headline": f"v{i}"}}],
            )
            assert res.status_code == 200, res.content

        snaps = _snapshots(auth_client, page.slug)
        # ai_edit(원본) 1건은 첫 편집 때만 생성
        assert sum(1 for s in snaps if s["reason"] == "ai_edit") == 1
        # 작업물은 편집마다 누적 = 3건 (예전엔 1건으로 덮어써졌음)
        assert len(_ai_results(auth_client, page.slug)) == 3
        # 현재는 가장 최신 1건만
        assert len(_current(auth_client, page.slug)) == 1
        assert snaps[0]["is_current"] is True  # 최신순 정렬

    def test_can_restore_to_previous_renewal(self, auth_client, page):
        """직전 리뉴얼 결과로 되돌릴 수 있다 (단일 슬롯 시절엔 불가능했던 시나리오)."""
        _ai_edit(
            auth_client,
            page.slug,
            blocks=[
                {"type": "profile", "data": {"headline": "B"}},
                {"type": "single_link", "data": {"url": "https://b.com", "label": "B"}},
            ],
        )
        results_after_first = _ai_results(auth_client, page.slug)
        first_result = results_after_first[0]  # 2 블록짜리 "B" 작업물
        assert first_result["blocks_count"] == 2

        # 두 번째 리뉴얼 (블록 1개) — 라이브가 C 로 바뀜
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "C"}}])
        assert page.blocks.count() == 1

        # 첫 작업물(B)로 복원 가능해야 한다
        res = auth_client.post(_restore_url(page.slug, first_result["id"]), {}, format="json")
        assert res.status_code == 200, res.content
        page.refresh_from_db()
        assert page.blocks.count() == 2  # B 상태로 복귀
        assert page.current_snapshot_id == first_result["id"]

    def test_trim_keeps_max_and_protects_original_and_current(self, auth_client, page):
        """보관 한도 초과 시 정리하되 원본(ai_edit)·현재 활성본은 항상 보존."""
        # 한도보다 충분히 많이 편집
        n = MAX_SNAPSHOTS_PER_PAGE + 5
        for i in range(n):
            _ai_edit(
                auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": f"v{i}"}}]
            )

        snaps = _snapshots(auth_client, page.slug)
        # 원본 + 현재 보호 때문에 한도보다 1~2건 많을 수 있으나 과도하게 쌓이지 않음
        assert len(snaps) <= MAX_SNAPSHOTS_PER_PAGE + 2
        # 원본 앵커는 살아 있어야 (맨 처음으로 되돌릴 수 있어야)
        assert any(s["reason"] == "ai_edit" for s in snaps)
        # 현재 활성본도 살아 있어야
        assert len(_current(auth_client, page.slug)) == 1
        page.refresh_from_db()
        assert page.current_snapshot_id in {s["id"] for s in snaps}


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
        by = _by_reason(auth_client, page.slug)
        original_id = by["ai_edit"]["id"]

        res = auth_client.post(_restore_url(page.slug, original_id), {}, format="json")
        assert res.status_code == 200, res.content
        body = res.json()
        # (선택) 복원 응답에 활성 슬롯 정보 동봉
        assert body["current_snapshot_id"] == original_id
        assert body["current_reason"] == "ai_edit"

        by = _by_reason(auth_client, page.slug)
        assert by["ai_edit"]["is_current"] is True
        assert by["ai_result"]["is_current"] is False

    def test_restore_back_to_latest(self, auth_client, page):
        """원본 ↔ 작업물 토글이 양방향으로 동작."""
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        by = _by_reason(auth_client, page.slug)
        latest_id = by["ai_result"]["id"]
        original_id = by["ai_edit"]["id"]

        auth_client.post(_restore_url(page.slug, original_id), {}, format="json")
        auth_client.post(_restore_url(page.slug, latest_id), {}, format="json")

        by = _by_reason(auth_client, page.slug)
        assert by["ai_result"]["is_current"] is True
        assert by["ai_edit"]["is_current"] is False

    def test_toggle_between_tracked_snapshots_creates_no_restore_entries(self, auth_client, page):
        """현재 활성본이 있는 상태(토글)에서의 복원은 restore 항목을 만들지 않는다(중복 방지)."""
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        by = _by_reason(auth_client, page.slug)
        original_id = by["ai_edit"]["id"]
        latest_id = by["ai_result"]["id"]

        # current 가 항상 설정된 채로 토글
        auth_client.post(_restore_url(page.slug, original_id), {}, format="json")
        auth_client.post(_restore_url(page.slug, latest_id), {}, format="json")

        snaps = _snapshots(auth_client, page.slug)
        assert sum(1 for s in snaps if s["reason"] == "restore") == 0

    def test_restore_captures_untracked_manual_state(self, auth_client, page):
        """복원 직전 라이브가 추적되지 않던 수동 편집본이면 reason=restore 로 보관된다."""
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        by = _by_reason(auth_client, page.slug)
        original_id = by["ai_edit"]["id"]

        # 수동으로 블록 추가 → 포인터 해제(current=None), 라이브가 어느 스냅샷과도 불일치
        res = auth_client.post(
            f"/api/v1/pages/multipages/{page.id}/blocks/",
            {"type": "single_link", "data": {"url": "https://manual.com", "label": "수동"}},
            format="json",
        )
        assert res.status_code == 201, res.content
        page.refresh_from_db()
        assert page.current_snapshot_id is None
        manual_block_count = page.blocks.count()  # AI 1개 + 수동 1개 = 2

        # 원본으로 복원 → 직전 수동 상태가 restore 로 보관돼야 잃지 않음
        auth_client.post(_restore_url(page.slug, original_id), {}, format="json")

        restore_snaps = [s for s in _snapshots(auth_client, page.slug) if s["reason"] == "restore"]
        assert len(restore_snaps) == 1
        assert restore_snaps[0]["blocks_count"] == manual_block_count


# ─────────────────────────────────────────────────────────────
# 일반 편집 → 포인터 NULL 초기화 (핵심)
# ─────────────────────────────────────────────────────────────


class TestManualEditClearsPointer:
    """복원/AI 편집 후 사용자가 직접 편집하면 어느 슬롯과도 일치하지 않아야 한다 (0건)."""

    def _assert_none_current(self, auth_client, slug):
        assert len(_current(auth_client, slug)) == 0

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
        """복원 후 직접 편집 → 모든 항목 false (프론트가 가장 우려한 시나리오)."""
        _ai_edit(auth_client, page.slug, blocks=[{"type": "profile", "data": {"headline": "AI"}}])
        by = _by_reason(auth_client, page.slug)
        auth_client.post(_restore_url(page.slug, by["ai_edit"]["id"]), {}, format="json")

        # 복원 직후엔 ai_edit 가 현재
        by = _by_reason(auth_client, page.slug)
        assert by["ai_edit"]["is_current"] is True

        # 손으로 한 글자라도 고치면 더 이상 그 스냅샷과 같지 않음
        auth_client.patch(
            f"/api/v1/pages/multipages/{page.id}/",
            {"title": "복원 후 수정"},
            format="json",
        )
        self._assert_none_current(auth_client, page.slug)


# ─────────────────────────────────────────────────────────────
# 모델 레벨 — SET_NULL / 멱등 no-op
# ─────────────────────────────────────────────────────────────


class TestPointerModelBehavior:
    def test_snapshot_delete_nulls_pointer(self, db, page):
        snap = PageSnapshot.objects.create(
            page=page,
            reason=PageSnapshot.Reason.AI_RESULT,
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
