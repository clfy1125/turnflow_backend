"""블록 순서 재정렬 — **순서를 맞바꾸면 500 이던** 결함의 회귀 테스트.

사고 (2026-10-13 prod)
─────────────────────────────────────────────────────────────────────────
두 reorder 뷰가 이 주석과 함께 단일 UPDATE 를 썼다.

    # 단일 UPDATE CASE/WHEN — PostgreSQL은 문장 단위로 unique 검사하므로 충돌 없음

전제가 틀렸다. ``UNIQUE(page, order)`` 가 ``NOT DEFERRABLE`` 이라 Postgres 는
**행 단위**로 검사한다 — 두 블록의 자리를 맞바꾸는 것만으로 중간 상태에서 중복이 나
``IntegrityError`` → 500.

  · prod 로그 8시간치에 ``duplicate key … pages_block_page_id_order`` 3건.
    page 1761 은 **1.4초 간격 연속 2회** (사용자가 다시 눌렀다).
  · 전체 1,462 페이지 중 **1,116개(76%)가 order 1..N 연속** — 빈 자리가 없는
    페이지는 순서를 바꿀 때마다 반드시 터진다.

이 파일이 지키는 것
─────────────────────────────────────────────────────────────────────────
1. 빈틈없이 연속인 페이지에서 **자리 맞바꾸기가 200** 이다. (핵심 — 옛 코드는 500)
2. 전체 뒤집기·부분 전송·요청 밖 블록과의 충돌 전부 200.
3. 적용 결과가 1..N 으로 빈틈없이 정규화된다 (order 폭주 복구).
4. 보내지 않은 블록의 **상대 순서가 보존**된다.

⚠️ 테스트 DB 가 dev DB 라 전역 단언은 금지 — 전부 우리가 만든 페이지 스코프다.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.pages.models import Block, Page

User = get_user_model()


def _url(page) -> str:
    return f"/api/v1/pages/multipages/{page.pk}/blocks/reorder/"


@pytest.fixture
def client_and_page(db):
    user = User.objects.create_user(
        email=f"reorder-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    c = APIClient(SERVER_NAME="127.0.0.1")
    c.force_authenticate(user=user)
    page, _ = Page.get_or_create_for_user(user)
    page.blocks.all().delete()
    return c, page, user


def _seed(page, orders):
    """주어진 order 로 블록을 만든다. ``orders`` 가 연속이면 빈 자리가 없다."""
    return [
        Block.objects.create(
            page=page, type="link", order=o, data={"title": f"b{o}", "_type": "single_link"}
        )
        for o in orders
    ]


def _orders(page):
    return list(page.blocks.order_by("order").values_list("pk", flat=True))


# ─── 1. 핵심: 빈틈없는 페이지에서 자리 맞바꾸기 ────────────────────────


@pytest.mark.django_db
class TestSwapOnDensePage:
    def test_swap_two_adjacent_blocks(self, client_and_page):
        """옛 코드가 **여기서 500** 이었다. 가장 흔한 드래그 동작이다."""
        c, page, _ = client_and_page
        a, b, d = _seed(page, [1, 2, 3])

        res = c.post(
            _url(page),
            {"orders": [{"id": a.pk, "order": 2}, {"id": b.pk, "order": 1}]},
            format="json",
        )
        assert res.status_code == 200, f"자리 맞바꾸기가 {res.status_code} — 500 회귀다"
        assert _orders(page) == [b.pk, a.pk, d.pk]

    def test_swap_on_high_offset_orders(self, client_and_page):
        """prod page 1761 재현 — order 가 127..160 처럼 **높고 연속**이어도 된다."""
        c, page, _ = client_and_page
        blocks = _seed(page, list(range(127, 137)))
        first, last = blocks[0], blocks[-1]

        res = c.post(
            _url(page),
            {"orders": [{"id": first.pk, "order": 136}, {"id": last.pk, "order": 127}]},
            format="json",
        )
        assert res.status_code == 200, res.content[:300]
        got = _orders(page)
        assert got[0] == last.pk and got[-1] == first.pk

    def test_full_reverse(self, client_and_page):
        """전체 뒤집기 — 모든 행이 서로의 자리를 차지한다."""
        c, page, _ = client_and_page
        blocks = _seed(page, [1, 2, 3, 4, 5])
        n = len(blocks)
        res = c.post(
            _url(page),
            {"orders": [{"id": b.pk, "order": n - i} for i, b in enumerate(blocks)]},
            format="json",
        )
        assert res.status_code == 200, res.content[:300]
        assert _orders(page) == [b.pk for b in reversed(blocks)]


# ─── 2. 부분 전송 ─────────────────────────────────────────────────────


@pytest.mark.django_db
class TestPartialPayload:
    def test_collides_with_unsent_block(self, client_and_page):
        """요청에 **없는** 블록이 이미 그 자리를 쓰고 있어도 터지지 않아야 한다.

        API 문서가 '전체 블록을 포함할 필요 없음' 이라고 약속한다.
        """
        c, page, _ = client_and_page
        a, b, _d = _seed(page, [1, 2, 3])

        res = c.post(_url(page), {"orders": [{"id": a.pk, "order": 3}]}, format="json")
        assert res.status_code == 200, f"부분 전송이 {res.status_code}"
        got = _orders(page)
        assert got.index(a.pk) > got.index(b.pk)

    def test_unsent_blocks_keep_relative_order(self, client_and_page):
        """보내지 않은 블록끼리의 순서는 흔들리면 안 된다."""
        c, page, _ = client_and_page
        a, b, d, e = _seed(page, [1, 2, 3, 4])

        res = c.post(_url(page), {"orders": [{"id": e.pk, "order": 1}]}, format="json")
        assert res.status_code == 200
        got = _orders(page)
        assert got[0] == e.pk
        assert got.index(a.pk) < got.index(b.pk) < got.index(d.pk)


# ─── 3. 결과가 정규화되는가 ───────────────────────────────────────────


@pytest.mark.django_db
class TestNormalization:
    def test_result_is_one_to_n(self, client_and_page):
        """order 가 1..N 으로 빈틈없이 다시 매겨져야 한다.

        ``get_next_order()`` 가 max+1 이라 생성·삭제를 반복하면 order 가 끝없이
        커진다 — prod 최대 **5123** 까지 올라가 있었다.
        """
        c, page, _ = client_and_page
        blocks = _seed(page, [10, 240, 5000])
        res = c.post(_url(page), {"orders": [{"id": blocks[-1].pk, "order": 1}]}, format="json")
        assert res.status_code == 200

        vals = sorted(page.blocks.values_list("order", flat=True))
        assert vals == [1, 2, 3], f"정규화되지 않았다: {vals}"

    def test_response_matches_db(self, client_and_page):
        """응답 순서와 DB 순서가 같아야 한다 (프론트가 응답을 그대로 쓴다)."""
        c, page, _ = client_and_page
        blocks = _seed(page, [1, 2, 3])
        res = c.post(_url(page), {"orders": [{"id": blocks[2].pk, "order": 1}]}, format="json")
        assert res.status_code == 200
        assert [r["id"] for r in res.data] == _orders(page)

    def test_noop_is_safe(self, client_and_page):
        """이미 그 순서면 조용히 200 (쓸데없는 쓰기도 없다)."""
        c, page, _ = client_and_page
        blocks = _seed(page, [1, 2, 3])
        res = c.post(
            _url(page),
            {"orders": [{"id": b.pk, "order": i + 1} for i, b in enumerate(blocks)]},
            format="json",
        )
        assert res.status_code == 200
        assert _orders(page) == [b.pk for b in blocks]
