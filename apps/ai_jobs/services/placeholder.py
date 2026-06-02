"""URL / 이미지 placeholder freeze / thaw.

AI 페이지 리뉴얼 시 기존 페이지의 URL·이미지·연락처 등을 ``[URL_1]`` / ``[IMG_3]`` 같은
토큰으로 치환해서 LLM 에 보낸다. 목적은 두 가지:

1. **콘텐츠 보호** — LLM 이 url 을 마음대로 수정/환각하지 못하게.
2. **토큰 절약** — 긴 URL 문자열을 짧은 토큰으로 압축.

LLM 응답에 placeholder 가 그대로 echo 되면 ``thaw_placeholders`` 가 원본 값으로 복원한다.
매핑은 ``AiJob.input_payload["_placeholder_map"]`` 에 저장돼 작업 끝까지 살아 있다.
"""

from __future__ import annotations

import re
from typing import Any

# ─────────────────────────────────────────────────────────────
# 치환 대상 필드 — 키 이름 정확 매칭 (재귀 탐색)
# ─────────────────────────────────────────────────────────────

# 카테고리별 키 (placeholder prefix)
_URL_KEYS: frozenset[str] = frozenset({
    "url", "link_url", "gallery_url",
})
_IMG_KEYS: frozenset[str] = frozenset({
    "image_url", "thumbnail_url", "avatar_url", "cover_image_url", "bgImage",
})
_VIDEO_LIST_KEYS: frozenset[str] = frozenset({
    "video_urls",
})
_IMG_LIST_KEYS: frozenset[str] = frozenset({
    "images",
})
_CONTACT_KEYS: frozenset[str] = frozenset({
    "phone", "email", "whatsapp",
})

# placeholder 패턴: 토큰 prefix → 카테고리
_TOKEN_PATTERN = re.compile(r"^\[(URL|IMG|VIDEO|CONTACT)_(\d+)\]$")


def _make_token(prefix: str, n: int) -> str:
    return f"[{prefix}_{n}]"


def freeze_placeholders(payload: Any) -> tuple[Any, dict[str, str]]:
    """``payload`` 내부의 URL/이미지/연락처 값을 placeholder 토큰으로 치환.

    Args:
        payload: dict / list / 임의의 중첩 구조. (보통 ``existing_blocks`` 또는
            ``existing_page_meta`` 같은 dict 트리)

    Returns:
        (frozen_payload, mapping) — frozen_payload 는 ``payload`` 의 깊은 복사본이며
        해당 필드들이 ``[URL_1]`` / ``[IMG_3]`` 등으로 바뀐 상태. mapping 은
        ``{"[URL_1]": "https://원본..."}`` 형태.

    규칙:
      - 동일 원본 값은 같은 토큰 재사용 (dedup).
      - 빈 문자열 / None / 비문자열은 치환 안 함 (그대로 둠).
      - 카운터는 카테고리별 1 부터 증가.
    """
    counters: dict[str, int] = {"URL": 0, "IMG": 0, "VIDEO": 0, "CONTACT": 0}
    # 원본값 → 토큰 (dedup)
    reverse: dict[tuple[str, str], str] = {}
    # 토큰 → 원본값 (반환용)
    mapping: dict[str, str] = {}

    def _allocate(prefix: str, original: str) -> str:
        key = (prefix, original)
        existing = reverse.get(key)
        if existing is not None:
            return existing
        counters[prefix] += 1
        token = _make_token(prefix, counters[prefix])
        reverse[key] = token
        mapping[token] = original
        return token

    def _convert(value: Any, *, list_prefix: str | None = None) -> Any:
        # ``list_prefix`` 가 주어지면 리스트의 각 문자열 원소를 그 prefix 로 치환.
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                if isinstance(v, str) and v.strip():
                    if k in _URL_KEYS:
                        out[k] = _allocate("URL", v)
                        continue
                    if k in _IMG_KEYS:
                        out[k] = _allocate("IMG", v)
                        continue
                    if k in _CONTACT_KEYS:
                        out[k] = _allocate("CONTACT", v)
                        continue
                if isinstance(v, list):
                    if k in _IMG_LIST_KEYS:
                        out[k] = _convert(v, list_prefix="IMG")
                        continue
                    if k in _VIDEO_LIST_KEYS:
                        out[k] = _convert(v, list_prefix="VIDEO")
                        continue
                out[k] = _convert(v)
            return out
        if isinstance(value, list):
            return [
                _allocate(list_prefix, item) if (list_prefix and isinstance(item, str) and item.strip())
                else _convert(item)
                for item in value
            ]
        return value

    frozen = _convert(payload)
    return frozen, mapping


def thaw_placeholders(value: Any, mapping: dict[str, str], *, drop_unknown: bool = True) -> Any:
    """``freeze_placeholders`` 의 역방향. value 안의 토큰을 원본으로 복원.

    Args:
        value: AI 가 출력한 결과 dict/list/scalar. 토큰 문자열은 그대로 또는 다른
            텍스트와 섞여 있을 수 있다 (예: ``"이미지: [IMG_1]"``). 텍스트 안에 끼어
            있는 토큰도 모두 치환한다 (regex sub).
        mapping: ``freeze_placeholders`` 가 만든 매핑.
        drop_unknown: True 면 매핑에 없는 토큰 (LLM 환각으로 만들어진 [URL_99] 같은
            것) 을 빈 문자열로 대체. False 면 토큰 그대로 둠.

    Returns:
        토큰이 복원된 새 값.
    """
    if not mapping and not drop_unknown:
        return value

    def _replace_in_string(s: str) -> str:
        # 토큰이 통째로 일치하면 깔끔하게 매핑 값으로 바꿈. 부분 매칭도 처리.
        all_token_pat = re.compile(r"\[(URL|IMG|VIDEO|CONTACT)_(\d+)\]")

        def _sub(m: re.Match) -> str:
            token = m.group(0)
            if token in mapping:
                return mapping[token]
            return "" if drop_unknown else token

        return all_token_pat.sub(_sub, s)

    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            return _replace_in_string(v)
        if isinstance(v, list):
            return [_walk(item) for item in v]
        if isinstance(v, dict):
            return {k: _walk(val) for k, val in v.items()}
        return v

    return _walk(value)


def is_placeholder(s: Any) -> bool:
    """문자열이 단일 placeholder 토큰 한 개로만 구성됐는지."""
    return isinstance(s, str) and bool(_TOKEN_PATTERN.match(s))
