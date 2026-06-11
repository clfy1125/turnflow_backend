"""
{{image:키워드}} 플레이스홀더를 실제 이미지 URL로 치환.

■ 동작
  1) LLM 결과에서 ``{{image:keyword}}`` 패턴을 추출
  2) 각 키워드를 Pixabay에서 검색해 원본 URL을 얻고
  3) 해당 이미지를 **다운로드 → 서비스 미디어 스토리지(R2)에 재호스팅**
  4) 콘텐츠 해시 기반 경로로 저장하므로 동일 이미지는 **재업로드 없이 재사용**
  5) 최종적으로 플레이스홀더를 서비스 도메인의 URL로 치환해 반환

■ 실패 시 폴백
  - PIXABAY_API_KEY 미설정 또는 Pixabay 호출 실패 → 외부 placeholder 이미지 URL
  - 다운로드/업로드 실패 → Pixabay 원본 URL을 그대로 사용 (서비스 동작 유지)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from urllib.parse import urlparse

import httpx
from decouple import config
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.pages.image_pipeline import ImageValidationError, process_upload

logger = logging.getLogger(__name__)

# 비전 관련도 게이트: 후보 N장을 비전 모델에 보여 키워드에 맞는 1장을 고르거나(없으면 0=거부).
_VLM_TOPK = 4
_NUM_RE = re.compile(r"-?\d+")

_PIXABAY_API_KEY = config("PIXABAY_API_KEY", default="")
_PIXABAY_URL = "https://pixabay.com/api/"
_IMAGE_PATTERN = re.compile(r"\{\{image:([^}]+)\}\}")
# 사용자 업로드 이미지 플레이스홀더. N = image_catalog 의 usable 이미지 번호(1-based).
_USER_IMAGE_PATTERN = re.compile(r"\{\{user_image:(\d+)\}\}")

# 다운로드 제한 (Pixabay webformatURL 은 보통 1~2MB 수준)
_MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024  # 15MB
_DOWNLOAD_TIMEOUT = 15.0

# 재호스팅 경로 프리픽스 (R2/로컬 공통)
_HOSTED_PREFIX = "ai_images"


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


def resolve_images(data: dict, *, user_image_urls: dict[str, str] | None = None) -> dict:
    """
    JSON dict를 문자열화 → 플레이스홀더를 실제 URL로 치환 → dict 복원.

    1) ``{{user_image:N}}`` → 사용자가 업로드한 이미지 URL (``user_image_urls`` 매핑).
       매핑에 없는 N(모델 할루시네이션)은 빈 문자열로 치환 — 출력에 플레이스홀더가 남지 않게.
    2) ``{{image:keyword}}`` → Pixabay 검색 후 재호스팅 URL (기존 동작).

    Args:
        user_image_urls: ``{"1": "https://.../a.jpg", "2": "..."}`` 형태. None 이면 (1) 스킵.
    """
    json_str = json.dumps(data, ensure_ascii=False)

    # 1) 사용자 업로드 이미지 먼저 치환.
    user_image_urls = user_image_urls or {}
    user_indices = set(_USER_IMAGE_PATTERN.findall(json_str))
    if user_indices:
        logger.info("사용자 이미지 플레이스홀더 %d종 발견, 치환 시작", len(user_indices))
        for n in user_indices:
            url = user_image_urls.get(str(n), "")
            if not url:
                logger.warning("{{user_image:%s}} 에 매핑된 URL 없음 → 빈 값으로 제거", n)
            json_str = json_str.replace("{{user_image:" + n + "}}", url)

    # 2) Pixabay 키워드 치환. 같은 이미지가 여러 블록에 중복되지 않도록 used 추적.
    keywords = list(dict.fromkeys(_IMAGE_PATTERN.findall(json_str)))  # 등장순 보존 + 중복 제거
    if keywords:
        logger.info("이미지 키워드 %d개 발견, 검색/재호스팅 시작", len(keywords))
        used = {"hashes": set(), "pixabay_ids": set()}
        for keyword in keywords:
            final_url = _resolve_one(keyword, used)
            placeholder = "{{image:" + keyword + "}}"
            json_str = json_str.replace(placeholder, final_url)

    return json.loads(json_str)


# ─────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────


def _vlm_pick_index(keyword: str, urls: list[str], force: bool = False) -> int:
    """비전 모델에게 후보 사진들을 보여주고 키워드에 가장 맞는 번호(1..N)를 고르게 한다.

    Args:
        force: True 면 "적합 없음(0)" 을 허용하지 않고 **가장 덜 어긋난 1장**을 반드시
            고르게 한다 — 더블 거부 후 최종 폴백용(사용자 피드백: 빈 슬롯 < 어긋난 사진.
            단 맹목적 1순위보다는 그나마 가까운 걸 고르는 게 곤충/엉뚱한 사진 사고를 줄인다).

    Returns:
        1..N  → 그 후보가 적합(앞으로 당겨 채택)
        0     → 적합한 후보 없음(거부 → 호출부가 다음 라운드/폴백 진행)
        -1    → 비전 미사용/실패 → 기존 순서대로 진행(폴백)
    """
    if not urls:
        return -1
    from .llm_client import call_llm_messages_with_usage  # 지연 import(순환 방지)

    model = getattr(settings, "AI_IMAGE_VLM_MODEL", "gemma-4")
    if force:
        instruction = (
            f"키워드: '{keyword.replace('_', ' ')}'\n"
            f"아래 {len(urls)}장 중 이 키워드의 주제와 **가장 가까운(가장 덜 어긋난) 1장**의 "
            "번호를 **반드시** 골라라. 완벽히 일치하지 않아도 된다 — 분위기/카테고리가 통하는 "
            "사진이면 OK. 단 곤충·벌레·전혀 무관한 동물/사물이 주인공인 사진만은 피하라. "
            "키워드가 일러스트/그림/캐릭터/아트(illustration·drawing·character·art·anime) 류면 "
            "실사 사진보다 일러스트·그림을 우선하라. "
            "0 출력 금지. **숫자 하나만** 출력."
        )
    else:
        instruction = (
            f"키워드: '{keyword.replace('_', ' ')}'\n"
            f"아래 {len(urls)}장 중, 이 키워드가 가리키는 **실제 사물/주제가 또렷이 주인공으로** "
            "보이는 사진 1장의 번호를 골라라. 키워드와 다른 사물/유사하지만 다른 제품/엉뚱한 풍경·곤충·"
            "무관한 인물이면 고르지 마라(예: 토너 키워드에 와인잔, 세럼에 물병, 라떼에 견과류는 부적합). "
            "딱 맞는 게 없으면 억지로 고르지 말고 **0**. (빈 이미지가 엉뚱한 이미지보다 낫다.) "
            "키워드가 일러스트/그림/캐릭터/드로잉/아트(illustration·drawing·character·art·anime) 류면 "
            "**실제 일러스트·그림만 적합**하고 실사 사진은 전부 0. "
            "**숫자 하나만** 출력."
        )
    content: list[dict] = [{"type": "text", "text": instruction}]
    for i, u in enumerate(urls, 1):
        content.append({"type": "text", "text": f"[{i}]"})
        content.append({"type": "image_url", "image_url": {"url": u}})
    try:
        res = call_llm_messages_with_usage(
            model=model,
            messages=[
                {"role": "system", "content": "너는 이미지 큐레이터다. 숫자만 답한다."},
                {"role": "user", "content": content},
            ],
            max_tokens=8,
            temperature=0.0,
        )
        m = _NUM_RE.search(res.content or "")
        if not m:
            return -1
        idx = int(m.group())
        if idx < 0 or idx > len(urls):
            return -1
        return idx
    except Exception as exc:  # noqa: BLE001
        logger.info("VLM 관련도 게이트 실패(폴백) '%s': %s", keyword, exc)
        return -1


def _host_candidate(pid: int, purl: str, keyword: str, used: dict) -> str | None:
    """후보 1장을 다운로드/정제/R2 재호스팅. 중복(해시)이면 None. 실패 시 외부 URL/None."""
    try:
        raw = _download(purl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("이미지 다운로드 실패 (%s): %s", purl, exc)
        return None
    try:
        hosted_url, digest = _store_hosted(raw, source_url=purl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("이미지 재호스팅 실패 (%s): %s → 외부 URL", purl, exc)
        used["pixabay_ids"].add(pid)
        return purl
    if digest in used["hashes"]:
        return None  # 다른 키워드가 이미 쓴 동일 이미지 → 중복 방지
    used["hashes"].add(digest)
    used["pixabay_ids"].add(pid)
    logger.info("이미지 재호스팅 완료: '%s' → %s", keyword, hosted_url)
    return hosted_url


def _resolve_one(keyword: str, used: dict | None = None) -> str:
    """키워드 → Pixabay 후보(관련도순) → (비전 게이트로) 적합한 1장 → 재호스팅 → 서비스 URL.

    1) 후보를 태그-관련도로 정렬(``_search_pixabay_candidates``).
    2) ``AI_IMAGE_VLM_RERANK`` 켜져 있으면 상위 N장을 비전 모델에 보여 **키워드에 맞는 1장**을 고른다.
       - 비전이 "적합 없음(0)" 이면 엉뚱한 사진을 넣느니 **중립 placeholder** 를 쓴다(잘못된 이미지 < 빈 이미지).
    3) 선택(또는 기존 순서)대로 다운로드/재호스팅. ``used`` 로 페이지 내 중복 방지.
    """
    used = used if used is not None else {"hashes": set(), "pixabay_ids": set()}
    candidates = _search_pixabay_candidates(keyword)
    if not candidates:
        return _placeholder(keyword)

    ordered = [c for c in candidates if c[0] not in used["pixabay_ids"]] or list(candidates)

    if getattr(settings, "AI_IMAGE_VLM_RERANK", True):
        topk = ordered[:_VLM_TOPK]
        idx = _vlm_pick_index(keyword, [u for _pid, u in topk])
        if idx == 0:
            # 상위 후보 전부 부적합 → **같은 키워드로 다음 후보 묶음을 한 번 더** 심사한다.
            # (범용 키워드로 갈아끼우면 '딸기잼 → 파프리카' 같은 주제 이탈이 생긴다 — 주제 유지가 우선.)
            nextk = ordered[_VLM_TOPK : _VLM_TOPK * 2]
            idx2 = _vlm_pick_index(keyword, [u for _pid, u in nextk]) if nextk else 0
            if 1 <= idx2 <= len(nextk):
                chosen = nextk[idx2 - 1]
                ordered = [chosen] + [c for c in ordered if c is not chosen]
            else:
                # 2라운드도 거부 → 빈 슬롯 대신 사진을 쓴다(사용자 피드백: 빈 슬롯이 더 나쁨).
                # 단 맹목적 1순위는 곤충/엉뚱한 사진 사고가 나므로, VLM 에게 **가장 덜 어긋난
                # 1장**을 강제로 고르게 한다(force). 그 호출마저 실패하면 1순위 폴백.
                pool = ordered[: _VLM_TOPK * 2]
                idx3 = _vlm_pick_index(keyword, [u for _pid, u in pool], force=True)
                if 1 <= idx3 <= len(pool):
                    chosen = pool[idx3 - 1]
                    ordered = [chosen] + [c for c in ordered if c is not chosen]
                    logger.info("VLM: '%s' 더블 거부 — 최근접 후보(%d) 강제 채택", keyword, idx3)
                else:
                    logger.info("VLM: '%s' 더블 거부 — 관련도 1순위 폴백 채택", keyword)
        elif 1 <= idx <= len(topk):
            chosen = topk[idx - 1]
            ordered = [chosen] + [c for c in ordered if c is not chosen]

    for pid, purl in ordered:
        hosted = _host_candidate(pid, purl, keyword, used)
        if hosted:
            return hosted
    return ""  # 다운로드/재호스팅 전부 실패 → 빈 슬롯(깨진 외부URL/회색박스 노출 안 함)


def _search_pixabay_candidates(keyword: str, n: int = 20) -> list[tuple[int, str]]:
    """Pixabay 검색 → ``[(id, imageURL)]`` 상위 n개 (인기순). 실패 시 빈 리스트.

    per_page 를 넓혀 후보를 충분히 확보(중복 회피용) + ``order=popular`` + ``min_width`` 로
    품질 하한을 둔다. 키워드 자체의 관련성은 프롬프트(구체적 영문 키워드)가 1차로 담보한다.
    """
    if not _PIXABAY_API_KEY:
        logger.warning("PIXABAY_API_KEY 미설정, placeholder 사용")
        return []

    query = keyword.replace("_", " ").strip()
    params = {
        "key": _PIXABAY_API_KEY,
        "q": query,
        "image_type": "photo",
        "order": "popular",
        "min_width": 1000,
        "per_page": max(3, min(n, 50)),
        "safesearch": "true",
    }
    # Pixabay 검색은 가끔 read timeout 이 난다(전체 페이지 이미지가 placeholder 로 떨어지는 원인).
    # → 1회 재시도 + 넉넉한 timeout 으로 일시 지연을 흡수한다.
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            resp = httpx.get(_PIXABAY_URL, params=params, timeout=15.0)
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            return _rank_by_relevance(hits, query)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 0:
                logger.info("Pixabay 재시도 (%s): %s", query, e)
    logger.warning("Pixabay API 에러 (%s): %s", query, last_err)
    return []


# 키워드 토큰화 시 무시할 일반어 (관련도 점수에 노이즈).
_STOPWORDS = {
    "a",
    "an",
    "the",
    "of",
    "with",
    "and",
    "for",
    "in",
    "on",
    "to",
    "photo",
    "image",
    "background",
    "scene",
    "closeup",
    "detail",
    "aesthetic",
    "bright",
    "modern",
    "korean",
    "professional",
    "studio",
}


def _rank_by_relevance(hits: list, query: str) -> list[tuple[int, str]]:
    """Pixabay hits 를 **태그-키워드 겹침**으로 재정렬한다.

    ``order=popular`` 만으로는 'blueberry jam' 검색에 인기 있는 말벌 사진이 1등으로 올 수 있다
    (관련성 < 인기). 각 hit 의 ``tags`` 와 쿼리 토큰의 겹침 수로 점수를 매겨, 진짜 관련 있는
    이미지를 앞으로 보낸다. 점수 동률이면 원래(인기) 순서 유지.
    """
    tokens = [t for t in re.split(r"[\s_]+", query.lower()) if len(t) >= 3 and t not in _STOPWORDS]
    scored: list[tuple[int, int, int, str]] = []  # (-score, orig_idx, id, url)
    for idx, h in enumerate(hits):
        url = h.get("largeImageURL") or h.get("webformatURL") or ""
        hid = h.get("id")
        if not (url and isinstance(hid, int)):
            continue
        tags = (h.get("tags") or "").lower()
        score = sum(1 for tok in tokens if tok in tags) if tokens else 0
        scored.append((-score, idx, hid, url))
    scored.sort()
    out = [(hid, url) for _s, _i, hid, url in scored]
    if out:
        top_score = -scored[0][0]
        logger.info("Pixabay '%s' 후보 %d개 (top 태그매칭=%d)", query, len(out), top_score)
    return out


def _download(url: str) -> bytes:
    """원격 이미지 다운로드. 크기 상한 초과 시 예외."""
    with httpx.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as r:
        r.raise_for_status()
        buf = io.BytesIO()
        total = 0
        for chunk in r.iter_bytes(chunk_size=64 * 1024):
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise ValueError(f"다운로드 크기 초과: >{_MAX_DOWNLOAD_BYTES} bytes")
            buf.write(chunk)
        return buf.getvalue()


def _store_hosted(raw: bytes, *, source_url: str) -> tuple[str, str]:
    """
    정제된 이미지를 ``ai_images/<hash[:2]>/<hash>.<ext>`` 로 저장하고 ``(공개URL, digest)`` 반환.

    같은 바이트(해시 동일)가 이미 스토리지에 있으면 재업로드 생략.
    digest 는 호출자(_resolve_one)가 페이지 내 이미지 중복 방지에 사용한다.
    """
    # 콘텐츠 해시 기반 dedup
    digest = hashlib.sha256(raw).hexdigest()

    # 정제 파이프라인 통과 — EXIF 제거 / 2048px 상한 / JPEG|WebP|GIF 정규화
    upload = ContentFile(raw, name=_guess_name(source_url))
    try:
        processed = process_upload(upload)
    except ImageValidationError as exc:
        raise ValueError(f"원격 이미지 정제 실패: {exc}") from exc

    key = f"{_HOSTED_PREFIX}/{digest[:2]}/{digest}.{processed.extension}"

    if default_storage.exists(key):
        # 이미 저장돼 있음 → 재업로드 생략
        return default_storage.url(key), digest

    default_storage.save(key, ContentFile(processed.content))
    return default_storage.url(key), digest


def _guess_name(url: str) -> str:
    """소스 URL에서 확장자만 참고용으로 추출 (실제 저장명과 무관)."""
    path = urlparse(url).path
    base = path.rsplit("/", 1)[-1] or "remote.jpg"
    return base


def _placeholder(keyword: str) -> str:
    # 검색/다운로드 전부 실패했을 때의 최후 폴백.
    # 과거엔 영문 키워드를 박은 회색 박스(placehold.co?text=...)라 페이지에 영어가 떠 보기 싫었다.
    # → 글자 없는 **중립 소프트 베이지 블록**(bg=fg 동일색)으로 톤 충돌을 최소화한다.
    return "https://placehold.co/800x800/efe7e0/efe7e0.png"
