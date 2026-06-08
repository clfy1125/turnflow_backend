"""리뉴얼 모드 결정 + style_only 모드용 블록 샘플링.

선택 기준:
  full_restyle  — 블록 적당, 콘텐츠 작음. AI 가 순서/추가/삭제까지 한다.
  style_only    — 블록 많거나 콘텐츠 많음. AI 는 스타일/세팅만 변경.
                  처음/중간/끝 + 각 _type 1개 강제 포함 으로 샘플링해서 입력 토큰 절감.
"""

from __future__ import annotations

import json

# ─────────────────────────────────────────────────────────────
# 임계값 — 두 조건 AND 일 때만 full_restyle.
# ─────────────────────────────────────────────────────────────
# Chunked 처리 — 단일 호출 한 번에 8k 토큰 출력 한계가 있어 큰 페이지는 LLM 을 여러 번
# 호출해서 합친다. 첫 호출이 design_settings + page.custom_css 를 결정하고, 후속 호출은
# 그 톤을 system 에 주입받아 자신의 chunk 블록만 패치한다.
FULL_RESTYLE_MAX_BLOCKS = 45  # 총 최대 — 초과시 style_only 로 폴백.
FULL_RESTYLE_CHUNK_SIZE = 15  # chunk 당 블록 수. 한 호출 안전 마진.
# chunk 1개가 ~10k char 가정. 3 chunk → 30k. 안전망용 컷.
FULL_RESTYLE_MAX_CONTENT_CHARS = 90_000


def _approx_content_size(blocks: list[dict]) -> int:
    """블록 리스트의 콘텐츠 크기 추정 — JSON 직렬화 길이."""
    try:
        return len(json.dumps(blocks, ensure_ascii=False))
    except (TypeError, ValueError):
        # 직렬화 실패하면 안전하게 큰 값 — style_only 로 강제.
        return FULL_RESTYLE_MAX_CONTENT_CHARS + 1


def select_mode(blocks: list[dict] | None) -> str:
    """현재 페이지 블록을 보고 full_restyle / style_only 결정.

    Returns:
        "full_restyle" | "style_only"
    """
    if not blocks:
        # 빈 페이지 — 새로 만드는 셈이므로 full_restyle 로.
        return "full_restyle"

    if len(blocks) > FULL_RESTYLE_MAX_BLOCKS:
        return "style_only"

    if _approx_content_size(blocks) > FULL_RESTYLE_MAX_CONTENT_CHARS:
        return "style_only"

    return "full_restyle"


# ─────────────────────────────────────────────────────────────
# 샘플링 — style_only 모드 입력용. 디자인 톤 판단에 충분한 만큼만 추출.
# ─────────────────────────────────────────────────────────────

SAMPLE_HEAD_N = 3
SAMPLE_MIDDLE_N = 3
SAMPLE_TAIL_N = 3


def _block_subtype(b: dict) -> str:
    """블록의 식별용 서브타입. profile/contact 는 type 자체, single_link 는 data._type."""
    btype = b.get("_type") or b.get("type") or ""
    if btype == "single_link":
        data = b.get("data") or {}
        sub = data.get("_type")
        if isinstance(sub, str) and sub:
            return f"single_link/{sub}"
    return btype


def chunk_blocks(blocks: list[dict], size: int = FULL_RESTYLE_CHUNK_SIZE) -> list[list[dict]]:
    """블록 리스트를 ``size`` 개씩 잘라 list[list[dict]] 로 반환.

    full_restyle 모드에서 블록 수가 ``size`` 를 초과하면 chunked 호출을 위해 분할한다.
    각 chunk 의 원래 순서는 보존된다.
    """
    if size <= 0 or not blocks:
        return [list(blocks)] if blocks else []
    return [blocks[i : i + size] for i in range(0, len(blocks), size)]


def sample_blocks(blocks: list[dict]) -> list[dict]:
    """style_only 모드용 샘플링.

    처음 N + 중간 N + 마지막 N 을 union 으로 모으되, 모든 ``_subtype`` 이 최소 1개씩
    포함되도록 보강. 결과는 입력 ``blocks`` 의 원래 순서를 유지하며 중복 제거됨.
    """
    if not blocks:
        return []

    n = len(blocks)
    indices: set[int] = set()

    head = list(range(min(SAMPLE_HEAD_N, n)))
    indices.update(head)

    if n > SAMPLE_HEAD_N + SAMPLE_TAIL_N:
        mid_start = max(SAMPLE_HEAD_N, (n // 2) - (SAMPLE_MIDDLE_N // 2))
        for i in range(mid_start, min(mid_start + SAMPLE_MIDDLE_N, n)):
            indices.add(i)

    tail_start = max(0, n - SAMPLE_TAIL_N)
    for i in range(tail_start, n):
        indices.add(i)

    # 각 subtype 강제 포함 — 이미 들어 있지 않으면 첫 등장 인덱스 추가.
    seen_subtypes: set[str] = {_block_subtype(blocks[i]) for i in indices}
    for i, b in enumerate(blocks):
        sub = _block_subtype(b)
        if sub and sub not in seen_subtypes:
            indices.add(i)
            seen_subtypes.add(sub)

    return [blocks[i] for i in sorted(indices)]
