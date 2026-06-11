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
import re

from apps.pages.validators import _normalize_url

logger = logging.getLogger(__name__)

# 항목마다 썸네일 이미지를 렌더하는 그룹링크 레이아웃 (썸네일 없으면 빈 박스가 뜸).
_IMAGE_GROUP_LAYOUTS = {"grid-2", "grid-3", "carousel-1", "carousel-2"}

# 단일링크(_type=single_link)는 url 이 비면 프론트가 통째로 렌더 스킵(SingleLinkBlock.tsx
# `if (!url?.trim()) return null;`). 검증기는 ""를 허용하므로 "저장은 됐는데 블록이 사라지는"
# 함정이 된다. 그래서 비어 있으면 유효한 placeholder 로 채워 최소한 렌더되게 한다.
# (검증기 _normalize_url 을 통과하는 http/https 여야 함. "#" 는 거부됨.)
_LINK_URL_PLACEHOLDER = "https://example.com"

# 텍스트 길이 가드 — 프론트 렌더러는 text 블록 content/profile subline 을 **자르지 않고**
# 그대로 흘려 화면을 넘긴다(긴 글 = 보기 싫음, 사용자 지적). 백스톱으로 길이를 통제한다.
_SUBLINE_MAX = 45  # 프로필 한 줄 소개
_TEXT_COLLAPSE_OVER = 110  # 이보다 길고 headline 이 있으면 toggle 로 접음(정보 보존)
_TEXT_TRIM_OVER = 110  # toggle 로 못 접는 경우(headline 없음) 잘라낼 기준
_TEXT_HARD_MAX = 800  # long_text 카테고리(청첩장 등)도 이건 넘으면 자름


def _is_url_key(key: str) -> bool:
    return key == "url" or key.endswith("_url")


def _strip_zero_prices(node: dict, stats: dict) -> None:
    """0원/숫자 없는 가격 필드를 제거한다 — 프론트가 '0KRW' 로 렌더해 깨져 보인다."""
    for key in ("price", "original_price"):
        v = node.get(key)
        if v is None:
            continue
        digits = "".join(ch for ch in str(v) if ch.isdigit())
        if not digits or int(digits) == 0:
            node.pop(key, None)
            stats["zero_price_stripped"] += 1


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


def _enforce_list_thumb_consistency(data: dict, stats: dict) -> None:
    """list 그룹링크의 썸네일 부분 누락(들쭉날쭉)을 정리한다.

    비전 게이트 거부로 일부 항목만 썸네일이 빠지면 줄마다 들쭉날쭉해 깨져 보인다.
    **과반이 차 있으면 유지**(부분 누락 감수 — 상품 사진이 더 중요), **과반 미만이면 전부
    제거**해 깔끔한 텍스트 리스트로 통일한다. grid/carousel 은 위 강등 규칙이 먼저 처리.
    """
    if data.get("_type") != "group_link":
        return
    if data.get("group_layout") in _IMAGE_GROUP_LAYOUTS:
        return
    links = data.get("links")
    if not isinstance(links, list):
        return
    enabled = [ln for ln in links if isinstance(ln, dict) and ln.get("is_enabled", True)]
    if not enabled:
        return
    with_thumb = sum(1 for ln in enabled if _has_thumb(ln))
    if with_thumb == 0 or with_thumb == len(enabled):
        return
    if with_thumb * 2 < len(enabled):
        for ln in enabled:
            ln["thumbnail_url"] = ""
        stats["list_thumbs_stripped"] += 1


def _fill_empty_single_link_urls(result: dict, stats: dict) -> None:
    """``_type == "single_link"`` 인데 url 이 빈 블록을 placeholder 로 채운다.

    빈 url 단일링크는 프론트에서 통째로 사라지므로(렌더 스킵), 사용자가 "블록이 없어졌다"고
    느낀다. 사용자 요청: 비워두지 말고 예시 url 로라도 채워 렌더되게 한다. social/video/gallery
    등 다른 서브타입은 url 을 다르게 쓰므로 **순수 single_link 만** 대상으로 한다.
    """
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return
    for b in blocks:
        if not isinstance(b, dict):
            continue
        data = b.get("data")
        if not isinstance(data, dict) or data.get("_type") != "single_link":
            continue
        url = data.get("url")
        if not (isinstance(url, str) and url.strip()):
            data["url"] = _LINK_URL_PLACEHOLDER
            stats["link_url_filled"] += 1


def _trim_text(s: str, n: int) -> str:
    """문장/공백 경계에서 자르고 ``…`` 을 붙인다."""
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    for sep in (". ", "! ", "? ", "\n", " "):
        idx = cut.rfind(sep)
        if idx > n * 0.6:
            return cut[:idx].rstrip(" .!?\n") + "…"
    return cut.rstrip() + "…"


def _cap_text_lengths(result: dict, long_text_ok: bool, stats: dict) -> None:
    """긴 본문/소개문을 통제한다.

    - profile ``subline`` 은 항상 짧게(45자 컷).
    - ``text`` 블록 ``content``: long_text 카테고리가 아니면, 길고 headline 이 있으면
      ``text_layout="toggle"`` 로 접고(정보 보존), headline 이 없으면 잘라낸다.
    - long_text 카테고리(청첩장/커미션)도 _TEXT_HARD_MAX 초과는 자른다.
    """
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return
    for b in blocks:
        if not isinstance(b, dict):
            continue
        d = b.get("data")
        if not isinstance(d, dict):
            continue

        if b.get("type") == "profile" or d.get("_type") == "profile":
            sub = d.get("subline")
            if isinstance(sub, str) and len(sub.strip()) > _SUBLINE_MAX:
                d["subline"] = _trim_text(sub, _SUBLINE_MAX)
                stats["text_trimmed"] += 1
            continue

        if d.get("_type") == "text":
            content = d.get("content")
            if not isinstance(content, str):
                continue
            c = content.strip()
            if len(c) > _TEXT_HARD_MAX:
                c = _trim_text(c, _TEXT_HARD_MAX)
                d["content"] = c
                stats["text_trimmed"] += 1
            if long_text_ok:
                continue
            if len(c) > _TEXT_COLLAPSE_OVER:
                headline = d.get("headline")
                layout = d.get("text_layout")
                if (
                    isinstance(headline, str)
                    and headline.strip()
                    and layout in (None, "", "plain", "default")
                ):
                    d["text_layout"] = "toggle"
                    stats["text_collapsed"] += 1
                else:
                    d["content"] = _trim_text(c, _TEXT_TRIM_OVER)
                    stats["text_trimmed"] += 1


def _drop_empty_galleries(result: dict, stats: dict) -> None:
    """이미지가 0장이 된 gallery 블록을 제거한다(빈 갤러리=깨진 영역).

    비전 게이트가 후보를 거부해 빈 슬롯이 되거나(``""``) URL 정화로 다 떨어져 ``images`` 가
    비면, 빈 회색 갤러리가 렌더된다. 차라리 블록 자체를 없앤다.
    """
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return
    kept = []
    for b in blocks:
        d = b.get("data") if isinstance(b, dict) else None
        if isinstance(d, dict) and d.get("_type") == "gallery":
            imgs = d.get("images")
            valid = (
                [x for x in imgs if isinstance(x, str) and x.strip()]
                if isinstance(imgs, list)
                else []
            )
            if not valid:
                stats["empty_gallery_dropped"] += 1
                continue
        kept.append(b)
    result["blocks"] = kept


def _dedup_form_blocks(result: dict, stats: dict) -> None:
    """새-페이지에서 폼 블록(customer/inquiry)을 최대 1개로 제한한다.

    생성 모델이 '협업 문의' 류를 customer 폼 + inquiry 폼으로 **중복** 만들면 영어 입력
    라벨(Enter your name)이 줄줄이 떠 어색하다(레시피 규칙 9). 첫 폼만 남기고 제거.
    """
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return
    kept = []
    form_seen = False
    for b in blocks:
        sub = (b.get("data") or {}).get("_type") if isinstance(b, dict) else None
        if sub in ("customer", "inquiry"):
            if form_seen:
                stats["form_deduped"] += 1
                continue
            form_seen = True
        kept.append(b)
    result["blocks"] = kept


# 컨셉 텍스트에서 영상 URL 을 추출하는 패턴 — 사용자가 직접 준 URL 만 진짜로 취급.
_VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|tiktok\.com|vimeo\.com|dailymotion\.com)[^\s\"'<>)]*",
    re.IGNORECASE,
)


def extract_video_urls(text: str) -> set[str]:
    """컨셉 등 자유 텍스트에서 영상 플랫폼 URL 을 추출한다 (video 블록 허용 목록용)."""
    if not isinstance(text, str) or not text:
        return set()
    return {m.rstrip(".,;") for m in _VIDEO_URL_RE.findall(text)}


def _normalize_video_blocks(
    result: dict, stats: dict, allowed_video_urls: set[str] | None = None
) -> None:
    """video 블록을 **유지**하며 URL 만 정리한다.

    정책 변경(2026-06-11, 사용자 결정): 생성 페이지는 유저가 고쳐 쓰는 **스캐폴드**다 —
    영상 자리가 잡혀 있어야 유저가 자기 영상으로 교체한다. 환각 URL 이어도 블록을
    제거하지 않는다(임베드가 깨져 보여도 자리가 있는 게 낫다).

    하는 일:
      - 컨셉에 실제 영상 URL(``allowed_video_urls``)이 있으면 그 URL 을 **맨 앞으로** 승격.
      - http/https 로 정규화 불가한 값(`#` 등)만 제거(페이지 저장 400 방지).
      - URL 이 하나도 안 남은 video 블록만 제거(렌더할 게 없음).
    """
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return
    allowed = allowed_video_urls or set()
    kept = []
    for b in blocks:
        d = (b.get("data") or {}) if isinstance(b, dict) else {}
        if isinstance(b, dict) and d.get("_type") == "video":
            urls = d.get("video_urls") if isinstance(d.get("video_urls"), list) else []
            cleaned = []
            for u in urls:
                if not isinstance(u, str):
                    continue
                cu = _clean_url(u)
                if cu:
                    cleaned.append(cu)
            # 컨셉의 실제 영상 URL 우선(있으면 맨 앞 — 진짜가 placeholder 보다 먼저 보이게).
            real = [u for u in allowed if u not in cleaned]
            cleaned = real + cleaned
            if not cleaned:
                stats["video_dropped"] += 1
                continue
            d["video_urls"] = cleaned
        kept.append(b)
    result["blocks"] = kept


# 가장자리 클리핑(peek)으로 '깨진 것처럼' 보이는 캐러셀 → 안전한 그리드로.
def _normalize_visual_layout(data: dict, stats: dict) -> None:
    sub = data.get("_type")
    if sub == "gallery":
        imgs = data.get("images")
        if isinstance(imgs, list):
            n = sum(1 for x in imgs if isinstance(x, str) and x.strip())
            cur = data.get("gallery_layout")
            if n >= 2 and cur in (None, "", "carousel", "single", "free", "list"):
                data["gallery_layout"] = "thumbnail"  # 2열 그리드 — 가장자리 클리핑 없음
                if cur not in (None, "", "thumbnail"):
                    stats["layout_normalized"] += 1
    elif sub == "group_link":
        if data.get("group_layout") in ("carousel-1", "carousel-2"):
            links = data.get("links")
            enabled = (
                [ln for ln in links if isinstance(ln, dict) and ln.get("is_enabled", True)]
                if isinstance(links, list)
                else []
            )
            has_all_thumb = bool(enabled) and all(_has_thumb(ln) for ln in enabled)
            data["group_layout"] = "grid-2" if has_all_thumb else "list"
            stats["layout_normalized"] += 1
    elif sub == "single_link":
        # 쇼케이스(large)는 상단 와이드 이미지가 본체라 이미지가 비면 큰 빈 영역이 생긴다
        # → small 로 강등. medium(스탠다드)은 썸네일이 조건부 렌더라 없어도 정상 카드 —
        # 주요 전환 CTA(카톡 문의/무료체험 등)의 표준 크기이므로 유지한다(사용자 피드백).
        if data.get("layout") == "large" and not (
            isinstance(data.get("thumbnail_url"), str) and data["thumbnail_url"].strip()
        ):
            data["layout"] = "small"
            stats["layout_normalized"] += 1


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
        _normalize_visual_layout(node, stats)
        _enforce_list_thumb_consistency(node, stats)
        _strip_zero_prices(node, stats)

    elif isinstance(node, list):
        for item in node:
            _walk(item, stats)


def sanitize_result_json(
    result: dict,
    *,
    long_text_ok: bool = False,
    drop_fabricated_video: bool = False,
    allowed_video_urls: set[str] | None = None,
) -> dict:
    """result_json 을 in-place 정화하고 같은 객체를 반환한다.

    - 모든 URL 필드(``url``/``*_url``, gallery ``images``)를 http/https 로 정규화하고
      정규화 불가 값(``"#"`` 등)은 ``""`` 로 만든다.
    - 썸네일 없는 그룹링크 grid/carousel 레이아웃은 ``list`` 로 낮춘다.
    - 빈 single_link url 을 placeholder 로 채운다.
    - 긴 텍스트(text content / profile subline)를 통제한다.

    Args:
        long_text_ok: 청첩장·커미션 등 긴 문단이 자연스러운 카테고리면 True
            (그래도 _TEXT_HARD_MAX 초과는 자름).
    """
    if not isinstance(result, dict):
        return result

    stats = {
        "urls_cleaned": 0,
        "images_dropped": 0,
        "group_layout_downgraded": 0,
        "link_url_filled": 0,
        "text_collapsed": 0,
        "text_trimmed": 0,
        "video_dropped": 0,
        "layout_normalized": 0,
        "empty_gallery_dropped": 0,
        "list_thumbs_stripped": 0,
        "form_deduped": 0,
        "zero_price_stripped": 0,
    }
    if drop_fabricated_video:
        # drop_fabricated_video=True 는 새-페이지 생성 경로 — video URL 정리 + 폼 중복 정리.
        # (이름과 달리 이제 video 블록은 유지한다 — 스캐폴드 정책, 함수 docstring 참조.)
        _normalize_video_blocks(result, stats, allowed_video_urls)
        _dedup_form_blocks(result, stats)
    _walk(result, stats)
    _drop_empty_galleries(result, stats)
    # url 정규화(빈 값 치환)가 끝난 뒤에 빈 single_link url 을 채운다.
    _fill_empty_single_link_urls(result, stats)
    _cap_text_lengths(result, long_text_ok, stats)

    if any(stats.values()):
        logger.info(
            "result_json 정화: URL %d 정리, 이미지 %d 제거, 그룹링크 %d list 전환, "
            "빈 url %d 채움, 텍스트 %d 접음/%d 자름, video %d 제거, 레이아웃 %d 정규화",
            stats["urls_cleaned"],
            stats["images_dropped"],
            stats["group_layout_downgraded"],
            stats["link_url_filled"],
            stats["text_collapsed"],
            stats["text_trimmed"],
            stats["video_dropped"],
            stats["layout_normalized"],
        )
    return result
