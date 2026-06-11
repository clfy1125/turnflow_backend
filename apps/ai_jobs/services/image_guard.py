"""빈 이미지 슬롯을 ``{{image:키워드}}`` 로 메우는 가드 (resolve_images **이전**에 실행).

생성 모델은 profile 을 cover_bg 로 잡고도 ``cover_image_url`` 을 비우거나, gallery 의
``images`` 를 빈 배열로 두거나, group_link grid 항목 썸네일을 안 채우는 일이 잦다.
→ 빈 회색 히어로 / 빈 갤러리 / 빈 이미지 박스로 렌더되는 핵심 버그.

기존 ``design_guard._fix_empty_hero`` 는 resolve **이후**라 '이미 받은 본문 이미지를 승격'만
가능하고, 새 Pixabay 이미지를 끌어올 수 없었다. 이 가드는 resolve **이전**에 빈 슬롯에
``{{image:카테고리 키워드}}`` 를 심어, 곧이어 도는 resolve_images 가 실제 사진으로 채우게 한다.

추가로 **프로필 히어로 다양성**을 카테고리 전략대로 강제한다:
  - cover 전략(랜딩/포폴/렌탈/공구/브로슈어/초대장/프로모/커미션) → profile_layout=cover_bg + 커버 채움
  - avatar 전략(프로필/명함/제휴/일반) → center 아바타 + 아바타 채움 (cover_bg 남발 방지)
"""

from __future__ import annotations

import logging
import re

from . import category_profiles as CP

logger = logging.getLogger(__name__)

_COVER_LAYOUTS = {"cover", "cover_bg"}
_AVATAR_LAYOUTS = {"center", "left", "right"}
_IMAGE_GROUP_LAYOUTS = {"grid-2", "grid-3", "carousel-1", "carousel-2"}
# list 레이아웃도 항목 좌측에 48px 썸네일을 렌더한다(GroupLinkPreview.tsx) — 썸네일이 없으면
# 그냥 글줄만 남아 "사진이 빠져있다"는 인상을 준다(사용자 핵심 피드백). 단 후기 리스트
# ("이름 ★★★★★")는 텍스트가 자연스러워 예외.
_REVIEW_MARKS = ("★", "⭐")
# 썸네일을 자동으로 채워 줄 단일링크 레이아웃 — large(쇼케이스)는 상단 와이드 이미지가
# 본체라 필수. medium(스탠다드)은 텍스트형 CTA 카드로도 정상 렌더되므로 **채우지 않는다**
# (카톡 문의 CTA 에 엉뚱한 스톡사진이 붙는 사고 방지 — 모델이 직접 준 썸네일은 존중).
_SHOWCASE_LINK_LAYOUTS = {"large"}
_GALLERY_TARGET = 4  # 갤러리 최소 이미지 수(미달이면 풀 키워드로 보충)


def _is_profile(b: dict) -> bool:
    return b.get("type") == "profile" or (b.get("data") or {}).get("_type") == "profile"


def _empty(v) -> bool:
    return not (isinstance(v, str) and v.strip())


class _Cycler:
    """키워드 풀을 순회하며 **중복 없는** 검색어를 발급. 풀이 모자라면 변주어를 덧붙인다.

    ``start`` 로 시작 오프셋을 바꿀 수 있다 — 2차 보강(refill) 패스에서 1차와 다른
    키워드부터 시도하게 해, 같은 키워드로 같은 실패를 반복하지 않게 한다.
    """

    _VARIANTS = ("", " closeup", " scene", " detail", " aesthetic", " bright")

    def __init__(self, pool: list[str], start: int = 0):
        self._pool = list(pool) or ["aesthetic photo"]
        self._used: set[str] = set()
        self._i = start % len(self._pool)
        self._v = 0

    def next(self) -> str:
        for _ in range(len(self._pool) * len(self._VARIANTS)):
            base = self._pool[self._i % len(self._pool)]
            suffix = self._VARIANTS[self._v % len(self._VARIANTS)]
            self._i += 1
            if self._i % len(self._pool) == 0:
                self._v += 1
            kw = (base + suffix).strip()
            if kw not in self._used:
                self._used.add(kw)
                return kw
        # 모두 소진 — 인덱스 접미사로라도 유일화
        kw = f"{self._pool[0]} {len(self._used)}"
        self._used.add(kw)
        return kw


def _ph(keyword: str) -> str:
    return "{{image:" + keyword + "}}"


def _looks_review_group(enabled_links: list[dict]) -> bool:
    """후기 리스트(제목에 별점)인지 판별 — 과반 항목에 ★/⭐ 가 있으면 후기로 본다."""
    if not enabled_links:
        return False
    starred = sum(
        1 for ln in enabled_links if any(m in str(ln.get("title") or "") for m in _REVIEW_MARKS)
    )
    return starred * 2 >= len(enabled_links)


_PRICE_RE = re.compile(r"\d{1,3}(?:,\d{3})*\s*(?:원|krw)", re.IGNORECASE)

# 계좌 안내 리스트(청첩장 '마음 전하실 곳' 등) — 사람/사물 썸네일이 붙으면 기괴하다.
_ACCOUNT_HINTS = (
    "계좌",
    "은행",
    "신한",
    "국민",
    "우리은행",
    "하나은행",
    "농협",
    "카카오뱅크",
    "토스뱅크",
    "마음 전하실",
)


def _looks_account_group(enabled_links: list[dict]) -> bool:
    """계좌번호 안내 리스트인지 판별 — 항목 과반에 은행/계좌 단어가 있으면 계좌 리스트."""
    if not enabled_links:
        return False
    hits = sum(
        1
        for ln in enabled_links
        if any(
            h in (str(ln.get("title") or "") + " " + str(ln.get("description") or ""))
            for h in _ACCOUNT_HINTS
        )
    )
    return hits * 2 >= len(enabled_links)


def _looks_price_table(enabled_links: list[dict]) -> bool:
    """텍스트 가격표(커미션 아이콘/반신/전신 등)인지 판별 — 과반 항목이 가격 행이면 가격표.

    가격표는 썸네일 없이 텍스트가 정석(레시피·블록규칙의 예외 케이스). 단 **grid 레이아웃의
    상품 진열**은 가격이 있어도 썸네일이 필요하므로, 호출부에서 list 레이아웃에만 적용한다.
    """
    if not enabled_links:
        return False
    pricey = sum(
        1
        for ln in enabled_links
        if (isinstance(ln.get("price"), str) and ln["price"].strip())
        or _PRICE_RE.search(str(ln.get("title") or ""))
        or _PRICE_RE.search(str(ln.get("description") or ""))
    )
    return pricey * 2 >= len(enabled_links)


def _guard_profile(
    profile: dict, prof: dict, hero_kw: _Cycler, report: dict, force: bool = True
) -> None:
    d = profile.get("data")
    if not isinstance(d, dict):
        return
    layout = d.get("profile_layout") or d.get("layout") or "center"
    strategy = prof.get("hero", "avatar")

    if not force:
        # 리메이크: 사용자의 현재 레이아웃을 존중 — 레이아웃 강제/기존 이미지 제거 없이
        # **빈 슬롯만** 채운다(빈 커버 회색 띠 / placeholder 아바타 방지).
        # cover 레이아웃도 아바타를 함께 렌더하므로 둘 다 검사한다.
        if layout in _COVER_LAYOUTS and _empty(d.get("cover_image_url")):
            d["cover_image_url"] = _ph(hero_kw.next())
            report["cover_filled"] += 1
        if _empty(d.get("avatar_url")):
            d["avatar_url"] = _ph(hero_kw.next())
            report["avatar_filled"] += 1
        return

    if strategy == "cover":
        # cover 전략: cover_bg 로 통일 + 커버 이미지 보장.
        if layout not in _COVER_LAYOUTS:
            d["profile_layout"] = "cover_bg"
            report["profile_layout_forced_cover"] += 1
        if _empty(d.get("cover_image_url")):
            d["cover_image_url"] = _ph(hero_kw.next())
            report["cover_filled"] += 1
    else:
        # avatar 전략: 커버 남발 방지 → center 아바타 + 아바타 보장.
        if layout in _COVER_LAYOUTS:
            d["profile_layout"] = "center"
            d.pop("cover_image_url", None)
            report["profile_layout_forced_avatar"] += 1
        if _empty(d.get("avatar_url")):
            d["avatar_url"] = _ph(hero_kw.next())
            report["avatar_filled"] += 1


def _guard_block_images(data: dict, gallery_kw: _Cycler, thumb_kw: _Cycler, report: dict) -> None:
    sub = data.get("_type")

    if sub == "gallery":
        imgs = data.get("images")
        imgs = [x for x in imgs if isinstance(x, str)] if isinstance(imgs, list) else []
        real = [x for x in imgs if not _empty(x)]
        if len(real) < _GALLERY_TARGET:
            need = _GALLERY_TARGET - len(real)
            real += [_ph(gallery_kw.next()) for _ in range(need)]
            data["images"] = real
            report["gallery_filled"] += need
        return

    if sub == "group_link":
        links = data.get("links")
        if not isinstance(links, list):
            return
        enabled = [ln for ln in links if isinstance(ln, dict) and ln.get("is_enabled", True)]
        if not enabled:
            return
        if _looks_review_group(enabled) or _looks_account_group(enabled):
            # 후기/계좌 리스트는 텍스트가 정석 — 모델이 심은 {{image:}} 도 떼어낸다.
            for ln in enabled:
                if str(ln.get("thumbnail_url") or "").startswith("{{image:"):
                    ln["thumbnail_url"] = ""
            return
        # list 레이아웃의 텍스트 가격표는 썸네일 없는 게 정석 — 심지 않는다.
        if data.get("group_layout") not in _IMAGE_GROUP_LAYOUTS and _looks_price_table(enabled):
            return
        # grid/carousel 은 물론 list 도 항목 썸네일을 렌더한다 — 전부 채운다.
        for ln in enabled:
            if _empty(ln.get("thumbnail_url")):
                ln["thumbnail_url"] = _ph(thumb_kw.next())
                report["link_thumb_filled"] += 1
        return

    if sub == "single_link":
        if data.get("layout") in _SHOWCASE_LINK_LAYOUTS and _empty(data.get("thumbnail_url")):
            data["thumbnail_url"] = _ph(thumb_kw.next())
            report["link_thumb_filled"] += 1


def ensure_image_placeholders(
    result: dict,
    category: str,
    concept: str = "",
    salt: int = 0,
    force_hero_strategy: bool = True,
) -> dict:
    """빈 이미지 슬롯에 카테고리 키워드 placeholder 를 심는다 (in-place, 같은 객체 반환).

    **resolve_images 이전**에 호출해야 한다 — 심은 ``{{image:..}}`` 를 resolve 가 실제 사진으로
    치환한다. 어떤 예외도 비치명적으로 삼킨다(이미지 보강 실패가 생성 전체를 막지 않게).

    Args:
        salt: 키워드 풀 시작 오프셋. resolve 실패(검색 0건 등)로 빈 슬롯이 남았을 때
            **2차 보강(refill) 패스**에서 0이 아닌 값을 줘 1차와 다른 키워드로 재시도한다.
            (게이트 거부는 resolve 가 관련도 1순위 폴백으로 자체 처리하므로, refill 이 메꾸는 건
            주로 검색 자체가 빈 경우다 — 사용자 피드백: 빈 슬롯 < 다소 어긋난 사진.)
    """
    if not isinstance(result, dict):
        return result
    try:
        prof = CP.get_profile(category)
        hero_kw = _Cycler(prof.get("hero_keywords") or [], start=salt)
        gallery_kw = _Cycler(prof.get("gallery_keywords") or [], start=salt)
        thumb_kw = _Cycler(prof.get("thumb_keywords") or [], start=salt)

        report = {
            "cover_filled": 0,
            "avatar_filled": 0,
            "gallery_filled": 0,
            "link_thumb_filled": 0,
            "profile_layout_forced_cover": 0,
            "profile_layout_forced_avatar": 0,
        }

        blocks = result.get("blocks")
        if isinstance(blocks, list):
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if _is_profile(b):
                    _guard_profile(b, prof, hero_kw, report, force=force_hero_strategy)
                    continue
                d = b.get("data")
                if isinstance(d, dict):
                    _guard_block_images(d, gallery_kw, thumb_kw, report)

        if any(report.values()):
            logger.info(
                "image_guard(%s) 보강: %s",
                category,
                {k: v for k, v in report.items() if v},
            )
    except Exception:  # noqa: BLE001
        logger.exception("image_guard 실패(무시): category=%s", category)
    return result
