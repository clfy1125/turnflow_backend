"""
LLM 이 만든 result_json 을 **저장/적용 가능한 형태로 정화**한다.

생성 모델은 종종 페이지 검증기(``apps.pages.validators``)를 통과하지 못하거나
프론트에서 보기 싫게 렌더되는 값을 만든다. 이 모듈은 result_json 을 DB 에 저장하기
직전(``tasks.run_ai_job`` 의 resolve_images 직후)에 한 번 돌려서 그런 값을 정리한다.

다루는 문제
  1. **가짜/플레이스홀더 URL** — ``"#"``, ``javascript:...``, 스킴 없는 쓰레기값 등은
     ``_optional_url`` 검증에서 거부되어 **페이지 저장이 통째로 400** 으로 실패한다.
     → http/https 로 정규화할 수 없는 URL 필드는 **빈 문자열("")** 로 바꾼다.
     (검증기는 ""은 허용하므로 "링크 없음" 으로 안전하게 처리됨.)
  2. **썸네일 없는 그룹링크 grid/carousel** — grid-2/grid-3/carousel 레이아웃은 항목마다
     이미지 슬롯을 렌더하는데, ``links`` 항목에 ``thumbnail_url`` 이 없으면 **빈 이미지
     박스**만 뜬다. → 활성 항목 중 하나라도 썸네일이 없으면 ``group_layout`` 을 ``"list"``
     로 낮춰 빈 박스를 없앤다.

설계 원칙
  - **검증기와 동일한 규칙**을 쓰기 위해 ``validators._normalize_url`` 을 그대로 재사용한다.
  - URL 필드만 정규화한다 (키가 ``url`` 이거나 ``*_url`` 인 문자열, gallery ``images`` 배열).
    → social 블록의 핸들(``instagram``/``phone``/``email`` 등)은 ``_url`` 로 끝나지 않아
       건드리지 않는다. 색상(``custom_*_color``)·ID 등도 안전.
"""

from __future__ import annotations

import logging

from apps.pages.validators import _normalize_url

logger = logging.getLogger(__name__)

# 항목마다 썸네일 이미지를 렌더하는 그룹링크 레이아웃 (썸네일 없으면 빈 박스가 뜸).
_IMAGE_GROUP_LAYOUTS = {"grid-2", "grid-3", "carousel-1", "carousel-2"}


def _is_url_key(key: str) -> bool:
    return key == "url" or key.endswith("_url")


def _clean_url(value: str) -> str:
    """http/https 로 정규화 가능하면 정규화한 값, 아니면 빈 문자열."""
    return _normalize_url(value) or ""


def _has_thumb(link: dict) -> bool:
    t = link.get("thumbnail_url")
    return isinstance(t, str) and bool(t.strip())


def _downgrade_group_layout(data: dict, stats: dict) -> None:
    """썸네일 없는 그룹링크 grid/carousel → list 로 낮춰 빈 이미지 박스를 없앤다."""
    if data.get("_type") != "group_link":
        return
    if data.get("group_layout") not in _IMAGE_GROUP_LAYOUTS:
        return
    links = data.get("links")
    if not isinstance(links, list) or not links:
        return
    enabled = [ln for ln in links if isinstance(ln, dict) and ln.get("is_enabled", True)]
    if not enabled:
        return
    if not all(_has_thumb(ln) for ln in enabled):
        data["group_layout"] = "list"
        stats["group_layout_downgraded"] += 1


def _walk(node, stats: dict) -> None:
    """result_json 트리를 in-place 로 순회하며 URL/그룹링크를 정화."""
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, dict | list):
                _walk(value, stats)
            elif isinstance(value, str) and _is_url_key(key):
                cleaned = _clean_url(value)
                if cleaned != value:
                    stats["urls_cleaned"] += 1
                node[key] = cleaned

        # gallery 등의 이미지 URL 배열 — 정규화 후 빈 값은 제거.
        images = node.get("images")
        if isinstance(images, list):
            new_images = []
            for item in images:
                if isinstance(item, str):
                    cleaned = _clean_url(item)
                    if cleaned:
                        new_images.append(cleaned)
                    else:
                        stats["images_dropped"] += 1
                else:
                    new_images.append(item)
            node["images"] = new_images

        _downgrade_group_layout(node, stats)

    elif isinstance(node, list):
        for item in node:
            _walk(item, stats)


def sanitize_result_json(result: dict) -> dict:
    """result_json 을 in-place 정화하고 같은 객체를 반환한다.

    - 모든 URL 필드(``url``/``*_url``, gallery ``images``)를 http/https 로 정규화하고
      정규화 불가 값(``"#"`` 등)은 ``""`` 로 만든다.
    - 썸네일 없는 그룹링크 grid/carousel 레이아웃은 ``list`` 로 낮춘다.
    """
    if not isinstance(result, dict):
        return result

    stats = {"urls_cleaned": 0, "images_dropped": 0, "group_layout_downgraded": 0}
    _walk(result, stats)

    if any(stats.values()):
        logger.info(
            "result_json 정화: URL %d개 정리, 이미지 %d개 제거, 그룹링크 %d개 list 전환",
            stats["urls_cleaned"],
            stats["images_dropped"],
            stats["group_layout_downgraded"],
        )
    return result
