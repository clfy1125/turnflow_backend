"""
apps/pages/services/external_importers/builder.py

변환된 외부 페이지 페이로드(``body``)에서 ``Page`` + ``Block`` 행을 생성하는
공용 헬퍼. 동기 뷰(``AiImportExternalView``) 와 비동기 Celery 태스크
(``run_external_import_job``) 가 같은 코드를 공유해 일관성을 유지한다.

호출 측이 트랜잭션 경계를 직접 잡는다 (``transaction.atomic`` 블록 안에서 호출).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from django.utils import timezone

from apps.pages.models import Block, Page, _generate_unique_slug
from apps.pages.validators import validate_block_data

logger = logging.getLogger(__name__)


def build_page_from_body(
    *,
    user,
    source: str,
    source_slug: str,
    source_url: str,
    body: dict,
    title_override: str = "",
    is_public: bool = False,
) -> tuple[Page, list[Block], dict[str, Any]]:
    """변환된 ``body`` → ``Page`` 1개 + ``Block`` 여러 개를 DB 에 생성.

    Args:
        user: 페이지를 소유할 사용자 (``request.user`` 또는 ``AiJob.user``).
        source: ``inpock`` / ``litly`` / ``linktree``
        source_slug: 외부 서비스에서의 원본 slug
        source_url: 사용자가 제공한 원본 URL (재임포트 감지·로그용)
        body: ``import_from_url`` 이 리턴한 ``body`` (``_meta`` 가 pop 된 상태든 아니든 무관 — 여기서 다시 pop).
        title_override: 비어 있지 않으면 페이지 제목으로 사용
        is_public: 새 페이지의 공개 여부 (기본 False)

    Returns:
        ``(page, blocks, meta)`` — ``meta`` 는 컨버터가 채운 통계 dict
        (``total_input_blocks``, ``total_output_blocks``, ``skipped_block_types``).
    """
    meta = body.pop("_meta", {}) if isinstance(body, dict) else {}
    new_slug = _generate_unique_slug(user.username)
    page_data = body.get("data") or {}
    page_title = title_override or body.get("title") or ""

    page = Page.objects.create(
        user=user,
        slug=new_slug,
        title=page_title,
        is_public=is_public,
        data=page_data,
        custom_css=body.get("custom_css") or "",
        import_source=source,
        import_source_slug=source_slug,
        import_source_url=source_url,
        imported_at=timezone.now(),
    )

    raw_blocks = body.get("blocks") or []
    new_blocks: list[Block] = []
    for i, b in enumerate(raw_blocks):
        btype = b.get("type")
        bdata = b.get("data") or {}
        if not btype:
            continue
        try:
            validate_block_data(btype, bdata)
        except Exception as e:  # noqa: BLE001
            # 외부 임포트는 best-effort — 한 블록이 우리 validator 에 걸려도 전체 실패 X
            logger.info(
                "external_import: skip invalid block source=%s slug=%s idx=%d type=%s err=%s",
                source, source_slug, i, btype, e,
            )
            continue
        new_blocks.append(
            Block(
                page=page,
                type=btype,
                order=b.get("order") if b.get("order") else (i + 1),
                is_enabled=b.get("is_enabled", True),
                data=bdata,
                custom_css=b.get("custom_css", ""),
            )
        )

    if new_blocks:
        Block.objects.bulk_create(new_blocks)

    return page, new_blocks, meta


def find_existing_import(user, source_url: str) -> Optional[Page]:
    """같은 사용자가 같은 ``source_url`` 로 이미 임포트한 페이지가 있으면 가장 최신 1건 반환.

    재임포트 감지용. 호출 측은 ``force=true`` 가 아닐 때 409 Conflict 응답을 만든다.
    """
    if not source_url:
        return None
    return (
        Page.objects.filter(user=user, import_source_url=source_url)
        .order_by("-imported_at", "-created_at")
        .first()
    )
