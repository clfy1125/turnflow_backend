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
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.pages.image_pipeline import ImageValidationError, process_upload

logger = logging.getLogger(__name__)

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

    # 2) Pixabay 키워드 치환 (기존).
    keywords = set(_IMAGE_PATTERN.findall(json_str))
    if keywords:
        logger.info("이미지 키워드 %d개 발견, 검색/재호스팅 시작", len(keywords))
        for keyword in keywords:
            final_url = _resolve_one(keyword)
            placeholder = "{{image:" + keyword + "}}"
            json_str = json_str.replace(placeholder, final_url)

    return json.loads(json_str)


# ─────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────


def _resolve_one(keyword: str) -> str:
    """키워드 → (Pixabay 검색) → (다운로드) → (정제) → (R2 저장) → 서비스 URL."""
    pixabay_url = _search_pixabay(keyword)
    if not pixabay_url:
        return _placeholder(keyword)

    try:
        raw = _download(pixabay_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("이미지 다운로드 실패 (%s): %s → 외부 URL fallback", pixabay_url, exc)
        return pixabay_url

    try:
        hosted_url = _store_hosted(raw, source_url=pixabay_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("이미지 재호스팅 실패 (%s): %s → 외부 URL fallback", pixabay_url, exc)
        return pixabay_url

    logger.info("이미지 재호스팅 완료: '%s' → %s", keyword, hosted_url)
    return hosted_url


def _search_pixabay(keyword: str) -> str:
    """Pixabay에서 키워드로 이미지 검색. 실패 시 빈 문자열."""
    if not _PIXABAY_API_KEY:
        logger.warning("PIXABAY_API_KEY 미설정, placeholder 사용")
        return ""

    query = keyword.replace("_", " ").strip()
    try:
        resp = httpx.get(
            _PIXABAY_URL,
            params={
                "key": _PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "per_page": 3,
                "safesearch": "true",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        if hits:
            # 품질 순으로 선호: largeImageURL > webformatURL
            url = hits[0].get("largeImageURL") or hits[0].get("webformatURL", "")
            if url:
                logger.info("Pixabay 검색 성공: '%s' → %s", query, url[:80])
                return url
    except Exception as e:  # noqa: BLE001
        logger.warning("Pixabay API 에러 (%s): %s", query, e)

    return ""


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


def _store_hosted(raw: bytes, *, source_url: str) -> str:
    """
    정제된 이미지를 ``ai_images/<hash[:2]>/<hash>.<ext>`` 로 저장하고 공개 URL 반환.

    같은 바이트(해시 동일)가 이미 스토리지에 있으면 재업로드 생략.
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
        return default_storage.url(key)

    default_storage.save(key, ContentFile(processed.content))
    return default_storage.url(key)


def _guess_name(url: str) -> str:
    """소스 URL에서 확장자만 참고용으로 추출 (실제 저장명과 무관)."""
    path = urlparse(url).path
    base = path.rsplit("/", 1)[-1] or "remote.jpg"
    return base


def _placeholder(keyword: str) -> str:
    safe = keyword.replace(" ", "+").replace("_", "+")
    return f"https://placehold.co/640x360?text={safe}"
