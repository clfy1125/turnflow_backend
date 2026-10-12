"""블록 순서 적용 — ``UNIQUE(page, order)`` 를 넘지 않고 재배치하는 **단일 소스**.

왜 따로 있나 (2026-10-13)
─────────────────────────────────────────────────────────────────────────
종전 두 reorder 뷰는 이 주석과 함께 단일 UPDATE 를 썼다.

    # 단일 UPDATE CASE/WHEN — PostgreSQL은 문장 단위로 unique 검사하므로 충돌 없음

**이 전제가 틀렸다.** ``pages_block_page_id_order_e625d9aa_uniq`` 는
``NOT DEFERRABLE`` 이라 Postgres 가 UPDATE 를 **행 단위**로 검사한다. 그래서
블록 두 개의 자리를 맞바꾸는 것만으로 중간 상태에서 중복이 나 500 으로 죽었다.

prod 실측(2026-10-13)
  · 로그 8시간치에 ``duplicate key … pages_block_page_id_order`` **3건**
    (page 1761 에서 1.4초 간격 연속 2회 — 사용자가 다시 시도했다는 뜻)
  · 전체 1,462 페이지 중 **1,116개(76%)가 order 1..N 연속** = 빈 자리가 없다.
    빈 자리가 없는 페이지는 순서를 바꿀 때마다 **반드시** 터진다.
  · 실제 피해 페이지 1761 은 블록 34개가 order 127~160 으로 빈틈없이 연속이었다.

어떻게 고쳤나
─────────────────────────────────────────────────────────────────────────
1. **현재 최대 order 보다 뒤로 대피** — 그 구간은 정의상 비어 있다. 대피 자체도
   행 단위 검사를 받지만 서로 다른 값이라 충돌하지 않는다.
   ⚠️ 음수를 대피소로 쓰면 안 된다 — ``order`` 는 ``PositiveIntegerField`` 라
   ``pages_block_order_check`` CHECK 제약에 걸린다 (실제로 밟았다).
2. **페이지 전체를 1..N 으로 다시 매긴다.** 요청에 없는 블록까지 포함하는 이유:
   · 요청 밖 블록과의 충돌로 다시 터지는 경로를 없앤다 (부분 전송을 허용한다고
     API 문서가 약속하고 있다).
   · ``get_next_order()`` 가 ``max+1`` 이라 생성·삭제를 반복하면 order 가 끝없이
     커진다 — prod 최대 **5123** 까지 올라가 있었다. 재정렬이 이걸 되돌린다.
   요청에 없는 블록은 **현재 상대 순서를 그대로 유지**한다.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Case, IntegerField, When

from .models import Block


def apply_block_reorder(page, id_to_order: dict[int, int]) -> None:
    """``id_to_order`` 를 반영해 ``page`` 의 블록 order 를 1..N 으로 다시 매긴다.

    Args:
        page: 대상 페이지.
        id_to_order: ``{블록 pk: 요청된 order}``. 페이지 전체를 담지 않아도 된다.

    호출 전에 **모든 pk 가 이 페이지 소속인지 검증**할 것 (뷰가 이미 한다).
    """
    with transaction.atomic():
        # select_for_update: 같은 페이지에 동시 재정렬이 들어와도 섞이지 않게.
        blocks = list(page.blocks.select_for_update().order_by("order", "pk"))
        if not blocks:
            return

        # 요청된 블록은 요청 order 로, 나머지는 현재 order 로 줄을 세운다.
        # 같은 자리를 다투면 **요청된 쪽이 이긴다** — 드래그는 "여기에 놓겠다"는
        # 명시적 의사이고, 보내지 않은 블록은 그냥 비켜주면 된다.
        # pk 를 마지막 키로 둬 나머지가 겹쳐도 순서가 흔들리지 않는다.
        ordered = sorted(
            blocks,
            key=lambda b: (
                id_to_order.get(b.pk, b.order),
                0 if b.pk in id_to_order else 1,
                b.pk,
            ),
        )

        target = {b.pk: i for i, b in enumerate(ordered, start=1)}
        if all(b.order == target[b.pk] for b in blocks):
            return  # 이미 그 모양이다 — 쓸 필요 없다.

        pks = [b.pk for b in blocks]
        # 현재 최대보다 뒤 = 반드시 비어 있는 구간.
        park = max(b.order for b in blocks) + 1

        # 1단계: 대피소로. 서로 다른 값이라 이 UPDATE 안에서는 충돌하지 않는다.
        Block.objects.filter(pk__in=pks).update(
            order=Case(
                *[When(pk=pk, then=park + i) for i, pk in enumerate(pks)],
                output_field=IntegerField(),
            )
        )
        # 2단계: 목표 자리로. 대피 뒤라 어떤 중간 상태에서도 중복이 없다.
        Block.objects.filter(pk__in=pks).update(
            order=Case(
                *[When(pk=pk, then=o) for pk, o in target.items()],
                output_field=IntegerField(),
            )
        )
