"""공개 페이지 블록 가시성 판정 — 단일 소스.

공개 응답(`GET /api/v1/pages/@{slug}/`)에서 빼야 하는 블록은 두 종류다.

1. **스스로 숨겨진 블록** — ``is_enabled=False`` 이거나 예약 노출(``schedule_enabled``)
   구간 밖인 블록.
2. **숨겨진 폴더의 하위 블록** — 폴더 블록(``data.child_block_ids`` 보유)이 응답에서
   빠지면 프론트는 ``child_block_ids`` 를 읽을 수 없어 그 자식들이 폴더 소속이라는
   사실 자체를 알 수 없다. 그대로 내려주면 자식들이 폴더 밖 낱개 블록으로 페이지
   맨 아래에 노출된다 (CS #e86ac9e8 / `@jageummaster` 실측: 폴더 1개를 껐더니
   하위 5개가 SNS 아이콘 아래에 그대로 표시).

두 번째 경우에도 하위 블록의 ``is_enabled`` 는 **바꾸지 않는다** — 폴더를 다시 켜면
원래대로 보여야 하므로 응답에서 빼는 것만 한다.

폴더 판정은 ``data._type == "folder"`` 가 아니라 **``child_block_ids`` 보유 여부**로
한다. 프론트가 폴더 소유권을 판단하는 기준이 그것이고, 앞으로 다른 컨테이너 타입이
생겨도 같은 규칙이 적용돼야 하기 때문이다.
"""

from datetime import datetime

from django.utils import timezone

from .models import Block


def is_block_visible(block: Block, now: datetime) -> bool:
    """공개 페이지에 이 블록 자체가 노출되는지 (폴더 소속 여부는 보지 않음).

    예약 노출 규칙:
      - ``schedule_enabled=False`` → ``is_enabled`` 만 적용
      - ``publish_at`` 지정 → 도래 후, ``hide_at`` 전까지
      - ``hide_at`` 만 지정 → 지금부터 ``hide_at`` 전까지
      - 예약을 켜 놓고 두 시각을 모두 비워두면 숨김 (구간이 정의되지 않음)
    """
    if not block.is_enabled:
        return False
    if not block.schedule_enabled:
        return True
    if block.publish_at is not None:
        if block.publish_at > now:
            return False
        return block.hide_at is None or block.hide_at > now
    if block.hide_at is not None:
        return block.hide_at > now
    return False


def child_block_ids(block: Block) -> list[int]:
    """블록이 거느린 하위 블록 ID. 컨테이너가 아니면 빈 리스트.

    AI 생성/외부 임포트 경로를 거치면서 문자열 ID 가 섞일 수 있어 정수로 정규화한다.
    """
    data = block.data if isinstance(block.data, dict) else {}
    raw = data.get("child_block_ids")
    if not isinstance(raw, list):
        return []

    out: list[int] = []
    for cid in raw:
        if isinstance(cid, bool):
            continue
        if isinstance(cid, int):
            out.append(cid)
        elif isinstance(cid, str) and cid.strip().lstrip("-").isdigit():
            out.append(int(cid.strip()))
    return out


def hidden_container_descendant_ids(blocks: list[Block], now: datetime) -> set[int]:
    """숨겨진 컨테이너(폴더) 아래에 매달린 블록 ID 전체 — 중첩 폴더까지 재귀.

    반환 집합에는 이 페이지에 존재하지 않는 ID(삭제된 자식의 잔재)가 섞일 수 있다.
    필터링에만 쓰이므로 무해하다. 자기 자신을 자식으로 참조하는 순환 데이터가 있어도
    방문 집합으로 막는다.
    """
    by_id = {b.id: b for b in blocks}
    excluded: set[int] = set()

    stack: list[int] = []
    for block in blocks:
        if not is_block_visible(block, now):
            stack.extend(child_block_ids(block))

    while stack:
        cid = stack.pop()
        if cid in excluded:
            continue
        excluded.add(cid)
        child = by_id.get(cid)
        if child is not None:
            # 숨겨진 폴더 안의 (켜져 있는) 하위 폴더도 통째로 사라져야 한다.
            stack.extend(child_block_ids(child))

    return excluded


def public_blocks(page, now: datetime | None = None) -> list[Block]:
    """공개 페이지에 실제로 렌더될 블록을 ``order`` 오름차순으로 반환."""
    now = now or timezone.now()
    blocks = list(page.blocks.all().order_by("order"))
    hidden_children = hidden_container_descendant_ids(blocks, now)
    return [b for b in blocks if b.id not in hidden_children and is_block_visible(b, now)]
