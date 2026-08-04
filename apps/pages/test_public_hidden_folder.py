"""공개 페이지 응답에서 '숨겨진 폴더의 하위 블록'이 제외되는지 검증.

배경: 폴더 블록을 끄면 폴더만 응답에서 빠지고 하위 블록은 계속 내려가,
프론트가 폴더 소속임을 모른 채 페이지 맨 아래에 낱개로 렌더하던 결함
(CS #e86ac9e8 / `@jageummaster`).

더러운 테스트 DB 대응: 이메일/slug 는 uuid 로 유일화.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.pages.models import Block, Page

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"folder-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


@pytest.fixture
def page(user):
    return Page.objects.create(
        user=user,
        slug=f"pf-{uuid.uuid4().hex[:10]}",
        title="folder probe",
        is_public=True,
        is_active=True,
    )


def _block(page, order, *, enabled=True, data=None, **kwargs):
    return Block.objects.create(
        page=page,
        type="single_link",
        order=order,
        is_enabled=enabled,
        data=data if data is not None else {"_type": "single_link", "label": f"b{order}"},
        **kwargs,
    )


def _folder(page, order, child_ids, *, enabled=True, **kwargs):
    return _block(
        page,
        order,
        enabled=enabled,
        data={"_type": "folder", "label": f"folder{order}", "child_block_ids": child_ids},
        **kwargs,
    )


def _get_blocks(page):
    res = APIClient().get(reverse("pages:public-page", kwargs={"slug": page.slug}))
    assert res.status_code == 200
    return res.json()["blocks"]


def _ids(blocks):
    return [b["id"] for b in blocks]


@pytest.mark.django_db
class TestHiddenFolderChildren:
    def test_disabled_folder_hides_its_children(self, page):
        """폴더를 끄면 폴더도 자식도 응답에 없어야 한다."""
        keep = _block(page, 1)
        c1 = _block(page, 3)
        c2 = _block(page, 4)
        folder = _folder(page, 2, [c1.id, c2.id], enabled=False)

        ids = _ids(_get_blocks(page))

        assert ids == [keep.id]
        assert folder.id not in ids
        assert c1.id not in ids and c2.id not in ids

    def test_enabled_folder_keeps_children(self, page):
        """폴더가 켜져 있으면 지금처럼 자식까지 전부 내려간다(프론트가 접어서 렌더)."""
        c1 = _block(page, 2)
        c2 = _block(page, 3)
        folder = _folder(page, 1, [c1.id, c2.id])

        ids = _ids(_get_blocks(page))

        assert ids == [folder.id, c1.id, c2.id]

    def test_children_is_enabled_is_not_mutated(self, page):
        """응답에서만 빼야 한다 — 폴더를 다시 켜면 원래대로 보여야 하므로."""
        child = _block(page, 2)
        folder = _folder(page, 1, [child.id], enabled=False)

        _get_blocks(page)

        child.refresh_from_db()
        assert child.is_enabled is True

        # 폴더를 다시 켜면 즉시 복귀
        folder.is_enabled = True
        folder.save(update_fields=["is_enabled"])
        assert _ids(_get_blocks(page)) == [folder.id, child.id]

    def test_nested_folder_is_recursive(self, page):
        """꺼진 폴더 안의 (켜져 있는) 하위 폴더와 그 손자까지 통째로 제외."""
        grandchild = _block(page, 4)
        inner = _folder(page, 3, [grandchild.id])  # 켜져 있음
        child = _block(page, 2)
        outer = _folder(page, 1, [child.id, inner.id], enabled=False)

        ids = _ids(_get_blocks(page))

        assert ids == []
        for b in (outer, inner, child, grandchild):
            assert b.id not in ids

    def test_schedule_hidden_folder_also_hides_children(self, page):
        """예약 노출 구간이 지나 숨겨진 폴더도 동일하게 자식을 데려간다."""
        past = timezone.now() - timedelta(hours=1)
        child = _block(page, 2)
        folder = _folder(page, 1, [child.id], schedule_enabled=True, hide_at=past)

        ids = _ids(_get_blocks(page))

        assert folder.id not in ids
        assert child.id not in ids

    def test_disabled_child_of_visible_folder_still_hidden(self, page):
        """폴더가 켜져 있어도 자식 자신이 꺼져 있으면 기존대로 제외."""
        on = _block(page, 2)
        off = _block(page, 3, enabled=False)
        folder = _folder(page, 1, [on.id, off.id])

        assert _ids(_get_blocks(page)) == [folder.id, on.id]

    def test_dangling_child_id_does_not_break(self, page):
        """삭제된 자식 ID 가 남아 있어도 500 나면 안 된다."""
        alive = _block(page, 2)
        folder = _folder(page, 1, [alive.id, 999_999_999], enabled=False)

        ids = _ids(_get_blocks(page))

        assert ids == []
        assert folder.id not in ids

    def test_cyclic_child_ids_terminate(self, page):
        """순환 참조 데이터가 들어와도 무한 루프에 빠지지 않는다."""
        a = _folder(page, 1, [], enabled=False)
        b = _folder(page, 2, [a.id])
        a.data = {"_type": "folder", "label": "a", "child_block_ids": [b.id]}
        a.save(update_fields=["data"])

        assert _ids(_get_blocks(page)) == []

    def test_string_child_ids_are_normalized(self, page):
        """AI/임포트 경로에서 문자열 ID 가 섞여 들어와도 매칭돼야 한다."""
        child = _block(page, 2)
        _folder(page, 1, [str(child.id)], enabled=False)

        assert _ids(_get_blocks(page)) == []

    def test_page_without_folders_is_unchanged(self, page):
        """폴더가 없는 페이지의 응답은 기존과 동일 — 회귀 방지."""
        b1 = _block(page, 1)
        _block(page, 2, enabled=False)
        b3 = _block(page, 3)

        assert _ids(_get_blocks(page)) == [b1.id, b3.id]
