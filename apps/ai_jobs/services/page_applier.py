"""
AI 작업의 result_json을 실제 Page / Block 상태로 적용하는 헬퍼.

`AiJobRollbackView`에서 사용된다.

result_json 스키마 (LLM 출력 + few-shot 예시 기반):
    {
      "title": str,
      "is_public": bool,
      "data": dict,
      "custom_css": str,
      "blocks": [
        {
          "id": int,                 # 기존 블록 ID (폴더/토글 자식 재매핑용, optional)
          "type" | "_type": str,     # 블록 타입 (프롬프트/예시에 따라 키가 달라짐)
          "order": int,
          "is_enabled": bool,
          "data": dict,
          "custom_css": str,
          "schedule_enabled": bool,
          "publish_at": str | null,
          "hide_at": str | null
        }, ...
      ]
    }
"""

from __future__ import annotations

from django.db import transaction

from apps.pages.models import Block, Page
from apps.pages.validators import validate_block_data


_PAGE_META_FIELDS = ("title", "is_public", "data", "custom_css")


def _block_type(raw: dict) -> str | None:
    """block dict에서 타입 키를 꺼낸다. 프롬프트별로 `type` / `_type` 혼용될 수 있음."""
    return raw.get("type") or raw.get("_type")


@transaction.atomic
def apply_result_json_to_page(page: Page, result_json: dict) -> Page:
    """AI 작업 result_json을 페이지에 전체 덮어쓰기.

    - `title`, `is_public`, `data`, `custom_css`: 존재하는 필드만 업데이트
    - `blocks`: 배열이 존재하면 기존 블록 전체 삭제 후 재생성
                (폴더/토글 블록의 `child_block_ids`는 새 ID로 재매핑)
    """
    if not isinstance(result_json, dict):
        raise ValueError("result_json이 dict가 아닙니다.")

    # ── 페이지 메타 업데이트 ─────────────────────────────
    for field in _PAGE_META_FIELDS:
        if field in result_json:
            setattr(page, field, result_json[field])
    page.save()

    blocks_data = result_json.get("blocks")
    if not isinstance(blocks_data, list):
        # blocks 자체가 없으면 메타만 반영하고 종료
        return page

    # ── 블록 검증 (저장 전에 전부 검증해서 부분 적용 방지) ──
    for i, raw in enumerate(blocks_data):
        btype = _block_type(raw)
        if btype is None:
            raise ValueError(f"blocks[{i}]: 블록 타입(type)이 없습니다.")
        validate_block_data(btype, raw.get("data") or {})

    # ── 기존 블록 삭제 → 재생성 ──────────────────────────
    page.blocks.all().delete()

    new_blocks: list[Block] = []
    old_ids: list[int | None] = []
    for i, raw in enumerate(blocks_data):
        old_ids.append(raw.get("id"))
        new_blocks.append(
            Block(
                page=page,
                type=_block_type(raw),
                order=raw.get("order") or (i + 1),
                is_enabled=raw.get("is_enabled", True),
                data=raw.get("data") or {},
                custom_css=raw.get("custom_css", ""),
                schedule_enabled=raw.get("schedule_enabled", False),
                publish_at=raw.get("publish_at"),
                hide_at=raw.get("hide_at"),
            )
        )
    if not new_blocks:
        return page

    created = Block.objects.bulk_create(new_blocks)

    # ── child_block_ids 재매핑 (폴더/토글 블록) ──────────
    id_map: dict[int, int] = {}
    for old_id, new_block in zip(old_ids, created):
        if old_id is not None:
            id_map[old_id] = new_block.id
    if not id_map:
        return page

    to_update: list[Block] = []
    for block in created:
        data = block.data
        if isinstance(data, dict) and isinstance(data.get("child_block_ids"), list):
            data["child_block_ids"] = [id_map.get(cid, cid) for cid in data["child_block_ids"]]
            block.data = data
            to_update.append(block)
    if to_update:
        Block.objects.bulk_update(to_update, ["data"])

    return page
