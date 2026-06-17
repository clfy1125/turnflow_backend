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
import threading
from concurrent.futures import ThreadPoolExecutor
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

# 이미지 해석 병렬도 — 키워드별 검색+다운로드+재호스팅이 I/O 바운드라 스레드풀로 동시 처리해
# 이미지 단계(직렬 ~15-25s)를 단축한다. Pixabay 레이트리밋을 고려해 4~6 정도가 적정.
_RESOLVE_MAX_WORKERS = config("AI_IMAGE_RESOLVE_WORKERS", default=5, cast=int)


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

    # 2) Pixabay 키워드 치환 — **병렬**(스레드풀)로 검색/재호스팅 후 **사후 dedup**.
    #    각 키워드를 독립 실행(자체 used)해 레이스를 없애고, 결과를 등장순으로 훑으며 같은
    #    이미지(digest)가 두 슬롯에 들어가면 그 키워드만 직렬 재선택한다(충돌은 드물다).
    keywords = list(dict.fromkeys(_IMAGE_PATTERN.findall(json_str)))  # 등장순 보존 + 중복 제거
    if keywords:
        logger.info("이미지 키워드 %d개 발견, 병렬 검색/재호스팅 시작", len(keywords))
        results: dict[str, tuple[str, str | None]] = {}
        workers = max(1, min(_RESOLVE_MAX_WORKERS, len(keywords)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_map = {ex.submit(_resolve_one_detailed, kw): kw for kw in keywords}
            for fut, kw in fut_map.items():
                try:
                    results[kw] = fut.result()
                except Exception as exc:  # noqa: BLE001 — 한 슬롯 실패가 전체를 막지 않게
                    logger.warning("이미지 resolve 실패(폴백) '%s': %s", kw, exc)
                    results[kw] = (_placeholder(kw), None)

        seen: set[str] = set()  # 이미 배치된 이미지 digest — 중복 방지(등장순 결정적)
        for keyword in keywords:
            url, digest = results.get(keyword) or (_placeholder(keyword), None)
            if digest and digest in seen:
                # 다른 키워드가 이미 같은 이미지를 썼다 → seen 을 피해 직렬 재선택.
                url2, digest2 = _resolve_one_detailed(keyword, forbidden_digests=seen)
                if url2:
                    url, digest = url2, digest2
            if digest:
                seen.add(digest)
            json_str = json_str.replace("{{image:" + keyword + "}}", url)

    return json.loads(json_str)


# ─────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────


def _vlm_pick_index(keyword: str, urls: list[str]) -> int:
    """비전 모델에게 후보 사진들을 보여주고 키워드에 가장 맞는 번호(1..N)를 고르게 한다.

    **키워드당 단 1회 호출** — 기존의 1라운드→2라운드→최근접 강제(최대 3회)는 비전 서버
    부하 시 이미지 단계가 분 단위로 늘어지는 주범이었다. 한 번에 "정확 일치 > 최근접 >
    전부 무관일 때만 0" 을 판단시킨다.

    Returns:
        1..N  → 그 후보 채택
        0     → 전부 무관(곤충/엉뚱한 사물 수준) → 빈 슬롯(가드/refill 이 정리)
        -1    → 비전 미사용/실패 → 기존 순서대로 진행(폴백)
    """
    if not urls:
        return -1
    from .llm_client import call_llm_messages_with_usage  # 지연 import(순환 방지)

    model = getattr(settings, "AI_IMAGE_VLM_MODEL", "gemma-4")
    instruction = (
        f"키워드: '{keyword.replace('_', ' ')}'\n"
        f"아래 {len(urls)}장 중 1장을 골라라. 우선순위:\n"
        "1) 키워드의 **실제 사물/주제가 또렷이 주인공**인 사진이 있으면 그 번호.\n"
        "2) 없으면 **주제/분위기가 가장 가까운(덜 어긋난) 1장** — 완벽하지 않아도 된다.\n"
        "3) **모든 후보가 완전히 무관**(곤충·벌레·전혀 다른 사물이 주인공)할 때만 0.\n"
        "키워드가 일러스트/그림/캐릭터/아트(illustration·drawing·character·art·anime) 류면 "
        "실사 사진보다 일러스트·그림을 우선하고, 일러스트가 하나도 없으면 0. "
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


# Pixabay 다운로드 429(레이트리밋) 서킷브레이커 — 한 번 걸리면 일정 시간 재호스팅을 생략하고
# 외부 URL 을 그대로 쓴다(요청 폭주 방지 + 이미지 단계 수 분 지연 방지). 병렬 실행이므로 lock 보호.
_RATE_LIMIT_COOLDOWN_S = 180.0
_rate_limited_until = 0.0
_rate_limit_lock = threading.Lock()


def _host_candidate(pid: int, purl: str, keyword: str, used: dict) -> tuple[str | None, str | None]:
    """후보 1장 다운로드/정제/R2 재호스팅. 반환 ``(url, digest)``.

    - 성공: ``(hosted_url, digest)``  - 외부 URL 폴백(쿨다운/429/재호스팅 실패): ``(purl, None)``
    - 다운로드 실패/중복(해시): ``(None, None)``
    """
    global _rate_limited_until
    import time as _time

    with _rate_limit_lock:
        cooling = _time.time() < _rate_limited_until
    if cooling:
        # 쿨다운 중 — 재호스팅 생략, Pixabay 외부 URL 직사용(렌더는 정상).
        used["pixabay_ids"].add(pid)
        return purl, None
    try:
        raw = _download(purl)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            with _rate_limit_lock:
                _rate_limited_until = _time.time() + _RATE_LIMIT_COOLDOWN_S
            logger.warning(
                "Pixabay 429 — %ds 쿨다운 시작, 외부 URL 폴백", int(_RATE_LIMIT_COOLDOWN_S)
            )
            used["pixabay_ids"].add(pid)
            return purl, None
        logger.warning("이미지 다운로드 실패 (%s): %s", purl, exc)
        return None, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("이미지 다운로드 실패 (%s): %s", purl, exc)
        return None, None
    try:
        hosted_url, digest = _store_hosted(raw, source_url=purl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("이미지 재호스팅 실패 (%s): %s → 외부 URL", purl, exc)
        used["pixabay_ids"].add(pid)
        return purl, None
    if digest in used["hashes"]:
        return None, None  # 다른 키워드가 이미 쓴 동일 이미지 → 중복 방지
    used["hashes"].add(digest)
    used["pixabay_ids"].add(pid)
    logger.info("이미지 재호스팅 완료: '%s' → %s", keyword, hosted_url)
    return hosted_url, digest


def _resolve_one_detailed(
    keyword: str, forbidden_digests: frozenset[str] | set[str] = frozenset()
) -> tuple[str, str | None]:
    """키워드 → Pixabay 후보(관련도순) → (비전 게이트로) 1장 → 재호스팅 → ``(url, digest)``.

    각 호출은 **독립**이라 병렬 안전(자체 ``used``). ``forbidden_digests`` 를 주면 그 이미지는
    피해 재선택한다(사후 dedup 충돌 처리용). digest 는 hosted 일 때만 채워지고, placeholder/외부
    URL 폴백/빈 슬롯이면 None(그런 슬롯은 dedup 대상이 아니다).
    """
    used = {"hashes": set(forbidden_digests), "pixabay_ids": set()}
    candidates = _search_pixabay_candidates(keyword)
    if not candidates:
        return _placeholder(keyword), None

    ordered = [c for c in candidates if c[0] not in used["pixabay_ids"]] or list(candidates)

    if getattr(settings, "AI_IMAGE_VLM_RERANK", True):
        # 키워드당 **비전 1회** — 상위 8장을 한 번에 심사("정확 일치 > 최근접 > 전부 무관 0").
        topk = ordered[: _VLM_TOPK * 2]
        idx = _vlm_pick_index(keyword, [u for _pid, u in topk])
        if idx == 0:
            logger.info("VLM: '%s' 후보 전부 무관 → 빈 슬롯", keyword)
            return "", None
        if 1 <= idx <= len(topk):
            chosen = topk[idx - 1]
            ordered = [chosen] + [c for c in ordered if c is not chosen]

    # 재호스팅은 최대 3개 후보만 시도 — 다운로드 장애(429 등) 시 20개를 끝까지 도는 게
    # 이미지 단계가 분 단위로 늘어지던 주범. 실패하면 선택 후보의 외부 URL 로 폴백(빈 슬롯 X).
    for pid, purl in ordered[:3]:
        res = _host_candidate(pid, purl, keyword, used)
        if res is None:  # 패치/예외 폴백 — 실패로 간주
            continue
        hosted, digest = res
        if hosted:
            return hosted, digest
    if ordered:
        pid, purl = ordered[0]
        used["pixabay_ids"].add(pid)
        logger.info("이미지 재호스팅 3회 실패: '%s' → 외부 URL 폴백", keyword)
        return purl, None
    return "", None


def _resolve_one(keyword: str, used: dict | None = None) -> str:
    """하위호환 thin wrapper — URL 만 반환. 페이지 내 dedup 은 resolve_images 의 사후 dedup 담당."""
    return _resolve_one_detailed(keyword)[0]


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
    """원격 이미지 다운로드. 크기 상한 초과 시 예외. (429 대응은 _host_candidate 서킷브레이커가 담당.)"""
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
