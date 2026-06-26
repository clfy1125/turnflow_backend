"""페이지 복제/복원 시 ``custom_css`` 의 ``data-block-id`` 재매핑 테스트.

대상 버그: 블록을 새 PK 로 다시 만드는 경로(복제·임포트·스냅샷 복원·AI 적용)에서
page-level ``custom_css`` 의 ``[data-block-id="<옛 PK>"]`` 셀렉터가 그대로 복사돼
죽은 옛 PK 를 가리키면 결과물의 블록 단위 스타일이 통째로 깨진다.

검증:
  1. ``remap_block_ids_in_css`` 가 따옴표 유무·종류, 폴더 자식, 누락 ID, 비-ID 셀렉터를
     스펙대로 처리한다(순수 함수, DB 불필요).
  2. ``POST /api/v1/pages/ai/clone-from-slug/`` 복제본의 custom_css 가 **복제본의 실제
     블록 PK** 로 치환되고 원본 PK 는 하나도 남지 않는다(폴더 자식 포함).
  3. 스냅샷 복원도 동일하게 새 PK 로 치환한다(같은 버그 클래스의 형제 경로).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from .aiviews import _serialize_page_state
from .models import Block, Page, PageSnapshot
from .services.css_remap import remap_block_ids_in_css

User = get_user_model()

_CLONE_URL = "/api/v1/pages/ai/clone-from-slug/"


# ─────────────────────────────────────────────────────────────
# 1. 순수 함수 단위 테스트 — remap_block_ids_in_css
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "css,expected",
    [
        ('[data-block-id="100"]{color:red}', '[data-block-id="200"]{color:red}'),
        ("[data-block-id='100']{color:red}", "[data-block-id='200']{color:red}"),
        ("[data-block-id=100]{color:red}", "[data-block-id=200]{color:red}"),
        # 공백 허용 + 복합 셀렉터(::before)
        (
            '[data-block-id = "100"] > *::before{content:"x"}',
            '[data-block-id="200"] > *::before{content:"x"}',
        ),
    ],
)
def test_remap_quote_variants(css, expected):
    assert remap_block_ids_in_css(css, {100: 200}) == expected


def test_remap_multiple_ids_in_one_css():
    css = '[data-block-id="100"]{a:1}[data-block-id="101"]{b:2}'
    out = remap_block_ids_in_css(css, {100: 200, 101: 201})
    assert out == '[data-block-id="200"]{a:1}[data-block-id="201"]{b:2}'


def test_remap_missing_id_is_kept_not_dropped():
    # 매핑에 없는 ID 는 원래 값 유지 (드롭·0 치환 금지)
    css = '[data-block-id="999"]{x:1}'
    assert remap_block_ids_in_css(css, {100: 200}) == '[data-block-id="999"]{x:1}'


def test_remap_is_single_pass_no_cascade():
    # 100→200 치환 후 그 결과(200)를 다시 200→300 으로 재치환하면 안 된다.
    css = '[data-block-id="100"]{a:1}[data-block-id="200"]{b:2}'
    out = remap_block_ids_in_css(css, {100: 200, 200: 300})
    assert out == '[data-block-id="200"]{a:1}[data-block-id="300"]{b:2}'


def test_remap_leaves_non_id_selectors_untouched():
    css = (
        ".page-container{padding:18px}\n"
        '.block-link[data-block-type="single_link"] > a{border-radius:16px}\n'
        "[data-block-container] > .block-link:nth-child(2){}\n"
        'a[href^="mailto"]{}\n'
        '[href*="example.com"]{}\n'
        ".mt-6.space-y-3:has(img){}\n"
    )
    # 매핑이 있어도 data-block-id 가 없으므로 한 글자도 바뀌지 않는다.
    assert remap_block_ids_in_css(css, {100: 200}) == css


@pytest.mark.parametrize("css", ["", None])
def test_remap_empty_css_passthrough(css):
    assert remap_block_ids_in_css(css, {100: 200}) == css


def test_remap_empty_map_passthrough():
    css = '[data-block-id="100"]{x:1}'
    assert remap_block_ids_in_css(css, {}) == css


# ─────────────────────────────────────────────────────────────
# Fixtures (통합 테스트)
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def user(db):
    return User.objects.create_user(email="cssremap@example.com", password="Pass1234!")


@pytest.fixture
def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _make_designed_page(user, slug: str) -> tuple[Page, Block, Block]:
    """폴더(부모)–자식 구조 + data-block-id 기반 custom_css 를 가진 페이지 생성.

    Returns ``(page, parent_block, child_block)``.
    """
    page = Page.objects.create(
        user=user, slug=slug, title="원본 디자인", is_public=True, custom_css=""
    )
    parent = Block.objects.create(
        page=page, type=Block.BlockType.PROFILE, order=1, data={"headline": "hi"}
    )
    child = Block.objects.create(
        page=page,
        type=Block.BlockType.SINGLE_LINK,
        order=2,
        data={"url": "https://example.com", "label": "x"},
    )
    # 폴더 자식 관계 — child 가 parent 의 child_block_ids 에 포함됨
    parent.data = {"headline": "hi", "child_block_ids": [child.id]}
    parent.save(update_fields=["data"])

    page.custom_css = (
        f'[data-block-id="{parent.id}"]{{color:red}}\n'
        f'[data-block-id="{child.id}"] > *::before{{content:"●"}}\n'  # 폴더 자식 타게팅
        ".page-container{padding:18px}\n"
        '.block-link[data-block-type="single_link"] > a{border-radius:16px}\n'
        "[data-block-container] > .block-link:nth-child(2){}\n"
    )
    page.save(update_fields=["custom_css"])
    return page, parent, child


# ─────────────────────────────────────────────────────────────
# 2. clone-from-slug 통합 테스트
# ─────────────────────────────────────────────────────────────


def test_clone_remaps_custom_css_to_new_block_ids(auth_client, user):
    src, parent, child = _make_designed_page(user, "src-designed")

    resp = auth_client.post(_CLONE_URL, {"slug": "src-designed"}, format="json")
    assert resp.status_code == 201, resp.data

    new_page = Page.objects.get(id=resp.data["id"])
    new_parent, new_child = list(new_page.blocks.order_by("order"))

    css = new_page.custom_css
    # 원본 PK 는 하나도 남아있지 않다
    assert f'data-block-id="{parent.id}"' not in css
    assert f'data-block-id="{child.id}"' not in css
    # 부모 + 폴더 자식 모두 복제본의 실제 PK 로 치환됐다
    assert f'data-block-id="{new_parent.id}"' in css
    assert f'data-block-id="{new_child.id}"' in css
    # 비-ID 셀렉터는 그대로 동작
    assert ".page-container{padding:18px}" in css
    assert 'data-block-type="single_link"' in css
    assert ":nth-child(2)" in css
    # 폴더 child_block_ids 도 새 PK 로 (기존 동작 회귀 방지)
    assert new_parent.data.get("child_block_ids") == [new_child.id]


def test_clone_with_empty_css_does_not_crash(auth_client, user):
    Page.objects.create(user=user, slug="src-nocss", title="no css", is_public=True)
    resp = auth_client.post(_CLONE_URL, {"slug": "src-nocss"}, format="json")
    assert resp.status_code == 201, resp.data
    assert Page.objects.get(id=resp.data["id"]).custom_css == ""


# ─────────────────────────────────────────────────────────────
# 3. 스냅샷 복원 통합 테스트 (형제 경로)
# ─────────────────────────────────────────────────────────────


def test_restore_remaps_custom_css_to_new_block_ids(auth_client, user):
    page, parent, child = _make_designed_page(user, "restore-designed")

    # 현재 상태를 스냅샷으로 저장 (css·blocks 모두 옛 PK 기준)
    snap = PageSnapshot.objects.create(
        page=page,
        reason=PageSnapshot.Reason.AI_RESULT,
        snapshot=_serialize_page_state(page),
    )

    url = f"/api/v1/pages/ai/@{page.slug}/snapshots/{snap.id}/restore/"
    resp = auth_client.post(url, format="json")
    assert resp.status_code == 200, resp.data

    page.refresh_from_db()
    new_parent, new_child = list(page.blocks.order_by("order"))

    css = page.custom_css
    assert f'data-block-id="{parent.id}"' not in css
    assert f'data-block-id="{child.id}"' not in css
    assert f'data-block-id="{new_parent.id}"' in css
    assert f'data-block-id="{new_child.id}"' in css
    assert new_parent.data.get("child_block_ids") == [new_child.id]
