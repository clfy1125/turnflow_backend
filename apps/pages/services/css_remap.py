"""apps/pages/services/css_remap.py

블록을 **새 PK 로 다시 만드는** 모든 경로(복제·외부 임포트·스냅샷 복원·AI 1-shot
적용)에서, 복사해 온 ``custom_css`` 안의 ``data-block-id`` 셀렉터를
**옛 블록 PK → 새 블록 PK** 로 치환하는 공용 헬퍼.

배경(버그): 공개페이지는 page-level ``custom_css`` 를 raw 전역으로 주입하고, 각 블록
래퍼는 ``[data-block-id="<PK>"]`` 로 타게팅된다(``ai_assets/rules/custom_css_guide.md``
§1·§2 실측). 복제/임포트/복원/AI 적용은 원본 블록을 **새 PK 로 다시 만들면서**
``custom_css`` 는 글자 그대로 복사하므로, 셀렉터가 죽은 옛 PK 를 가리켜 결과물의 블록
단위 스타일(상태 pill·CTA·about 카드 등)이 통째로 깨진다. 재생성 시점엔 옛·새 PK 를
모두 알고 있으므로 정규식 치환 한 번이면 된다.

오직 ``data-block-id`` 의 **숫자 값**만 바꾼다. ``data-block-type`` / ``.page-container`` /
``:nth-child(...)`` / ``[href*="..."]`` / ``:has(...)`` 처럼 ID 를 쓰지 않는 셀렉터는
건드리지 않는다(그대로 둬도 동작한다).
"""

from __future__ import annotations

import re

# ``[data-block-id="123"]`` / ``[data-block-id='123']`` / ``[data-block-id=123]`` 모두 매칭.
# 따옴표 유무·종류와 무관하게 잡되, 여는 따옴표와 같은 것으로 닫히는 경우만 매칭(역참조 ``\1``).
_BLOCK_ID_ATTR_RE = re.compile(r'data-block-id\s*=\s*(["\']?)(\d+)\1')


def remap_block_ids_in_css(css: str, old_to_new: dict[int, int]) -> str:
    """``custom_css`` 안의 ``data-block-id`` 값을 ``old_to_new`` 매핑으로 치환해 반환.

    Args:
        css: 원본 custom_css (page-level 또는 block-level). 비어 있으면 그대로 반환.
        old_to_new: ``{옛 블록 PK: 새 블록 PK}``. 폴더/토글 블록의 **자식 PK 도 빠짐없이**
            포함돼 있어야 폴더 내부 블록 스타일이 안 깨진다. 비어 있으면 그대로 반환.

    Returns:
        치환된 css. 매핑에 없는 ID 는 **원래 값을 유지**한다(임의 드롭·0 치환 금지).
    """
    if not css or not old_to_new:
        return css

    def _repl(m: re.Match) -> str:
        quote, old = m.group(1), int(m.group(2))
        new = old_to_new.get(old, old)  # 매핑에 없으면 원래 값 유지
        return f"data-block-id={quote}{new}{quote}"

    return _BLOCK_ID_ATTR_RE.sub(_repl, css)
