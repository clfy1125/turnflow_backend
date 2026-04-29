"""
apps/pages/services/external_importers/reupload.py

외부 페이지에서 변환한 블록의 이미지를 우리 측 미디어 스토리지(``PageMedia``)로
재업로드. hotlink 차단·CORS·CDN 만료에 영향받지 않는 자체 호스팅 URL로 교체.

동기 import 경로는 일반적으로 안 쓰고, ``EXTERNAL_IMPORT`` AiJob 안에서만
호출되는 게 정상이다 (이미지 30장이면 분 단위 작업 → HTTP 응답 대기 시간 초과).

흐름:
1. ``walk_image_urls(blocks)`` — 블록에서 외부 이미지 URL 수집
2. 각 URL 다운로드 (``urllib.request``, timeout / size 캡)
3. ``image_pipeline.process_upload`` 로 sanitize·정규화
4. ``PageMedia.objects.create(...)`` 로 저장 (Page FK 연결)
5. ``replace_in_blocks(blocks, mapping)`` — 블록의 URL 을 새 ``media.file.url`` 로 치환

상한 (어뷰즈 / DoS 방어):
- 페이지당 최대 ``MAX_IMAGES`` 장 (그 이상은 원본 URL 유지)
- 이미지당 최대 ``MAX_BYTES_PER_IMAGE`` (그 이상은 fetch 중단)
- fetch timeout ``FETCH_TIMEOUT_SEC`` 초
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from django.core.files.base import ContentFile

from apps.pages.image_pipeline import (
    ImageValidationError,
    process_upload,
)
from apps.pages.models import Page, PageMedia

logger = logging.getLogger(__name__)


MAX_IMAGES = 30
MAX_BYTES_PER_IMAGE = 10 * 1024 * 1024  # 10 MiB
FETCH_TIMEOUT_SEC = 8

# 블록 ``data`` 안에서 이미지 URL 이 들어가는 표준 필드 목록.
# 컨버터 (inpock/litly/linktree) 가 출력하는 키 이름을 따른다.
SCALAR_IMAGE_FIELDS = (
    "avatar_url",
    "cover_image_url",
    "thumbnail_url",
    "image_url",
)
LIST_IMAGE_FIELDS = ("images",)  # gallery
NESTED_LINK_FIELDS = ("links",)  # group_link items 의 thumbnail_url


@dataclass
class ReuploadReport:
    """뷰/태스크가 응답에 노출하기 위한 통계."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_over_limit: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def add_failure(self, url: str, reason: str) -> None:
        self.failed += 1
        # 응답에 너무 길게 박히지 않게 5건만 보관
        if len(self.failures) < 5:
            self.failures.append({"url": url[:200], "reason": reason})

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped_over_limit": self.skipped_over_limit,
            "failures": self.failures,
        }


# ─────────────────────────────────────────────────────────────
# URL 수집 / 치환
# ─────────────────────────────────────────────────────────────


def _is_external_http_url(url: Any) -> bool:
    if not url or not isinstance(url, str):
        return False
    return url.startswith(("http://", "https://"))


def walk_image_urls(blocks: list[dict]) -> list[str]:
    """블록 리스트에서 외부 이미지 URL 들의 **고유 집합** 을 추출. 순서는 발견순."""
    found: list[str] = []
    seen: set[str] = set()

    def _visit_dict(d: dict) -> None:
        if not isinstance(d, dict):
            return
        for key in SCALAR_IMAGE_FIELDS:
            v = d.get(key)
            if _is_external_http_url(v) and v not in seen:
                seen.add(v)
                found.append(v)
        for key in LIST_IMAGE_FIELDS:
            arr = d.get(key)
            if isinstance(arr, list):
                for item in arr:
                    if _is_external_http_url(item) and item not in seen:
                        seen.add(item)
                        found.append(item)
        for key in NESTED_LINK_FIELDS:
            arr = d.get(key)
            if isinstance(arr, list):
                for item in arr:
                    _visit_dict(item)

    for b in blocks:
        if isinstance(b, dict):
            _visit_dict(b.get("data") or {})
    return found


def replace_in_blocks(blocks: list[dict], url_map: dict[str, str]) -> int:
    """블록 안의 이미지 URL 을 ``url_map`` (원본 URL → 새 URL) 으로 치환.
    치환 횟수를 리턴 (테스트/통계용)."""
    if not url_map:
        return 0
    count = 0

    def _replace_in_dict(d: dict) -> None:
        nonlocal count
        if not isinstance(d, dict):
            return
        for key in SCALAR_IMAGE_FIELDS:
            v = d.get(key)
            if isinstance(v, str) and v in url_map:
                d[key] = url_map[v]
                count += 1
        for key in LIST_IMAGE_FIELDS:
            arr = d.get(key)
            if isinstance(arr, list):
                for i, item in enumerate(arr):
                    if isinstance(item, str) and item in url_map:
                        arr[i] = url_map[item]
                        count += 1
        for key in NESTED_LINK_FIELDS:
            arr = d.get(key)
            if isinstance(arr, list):
                for item in arr:
                    _replace_in_dict(item)

    for b in blocks:
        if isinstance(b, dict):
            _replace_in_dict(b.get("data") or {})
    return count


# ─────────────────────────────────────────────────────────────
# 다운로드 + 업로드
# ─────────────────────────────────────────────────────────────


def _download(url: str) -> bytes:
    """외부 URL → 바이트. 사이즈 캡을 chunk 단위로 강제 — 큰 파일 메모리 폭주 방어."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (TurnflowLink/1.0 image-reupload)",
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES_PER_IMAGE:
                raise IOError(f"이미지 사이즈 한도 초과: {total} > {MAX_BYTES_PER_IMAGE}")
            chunks.append(chunk)
        return b"".join(chunks)


def _upload_one(page: Page, url: str, source_name: str) -> Optional[PageMedia]:
    """단일 외부 이미지 → PageMedia 생성. 실패 시 None."""
    try:
        raw = _download(url)
    except (urllib.error.HTTPError, urllib.error.URLError, IOError, TimeoutError) as e:
        logger.info("reupload download failed url=%s err=%s", url, e)
        return None

    # process_upload 는 file-like 를 기대 — ContentFile 로 감싸서 통과
    name_hint = url.rsplit("/", 1)[-1].split("?", 1)[0] or "image.bin"
    cf_in = ContentFile(raw, name=name_hint)
    try:
        processed = process_upload(cf_in)
    except ImageValidationError as e:
        logger.info("reupload sanitize failed url=%s err=%s", url, e)
        return None

    suggest_name = processed.suggest_filename(name_hint)
    cf_out = ContentFile(processed.content, name=suggest_name)
    try:
        media = PageMedia.objects.create(
            page=page,
            file=cf_out,
            mime_type=processed.mime_type,
            size=processed.size,
            original_name=name_hint[:200],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("reupload PageMedia.create failed url=%s err=%s", url, e)
        return None

    return media


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────


def reupload_images(
    page: Page,
    blocks: list[dict],
    *,
    source_name: str = "external",
    progress_cb=None,
) -> ReuploadReport:
    """블록에서 외부 이미지 URL 을 모아 PageMedia 로 재업로드 후 블록 안 URL 을 교체.

    Args:
        page: 새로 생성된 Page (FK 부착용).
        blocks: 변환된 블록 리스트 — 호출 후 in-place 로 URL 이 바뀐다.
        source_name: 통계 로그용 ('inpock' / 'litly' / 'linktree').
        progress_cb: ``(done: int, total: int) -> None`` 콜백 — Celery 진행률 갱신용.

    Returns:
        ReuploadReport — 시도/성공/실패/스킵 통계.
    """
    urls = walk_image_urls(blocks)
    report = ReuploadReport()
    if not urls:
        return report

    capped = urls[:MAX_IMAGES]
    if len(urls) > MAX_IMAGES:
        report.skipped_over_limit = len(urls) - MAX_IMAGES

    url_map: dict[str, str] = {}
    total = len(capped)
    for i, url in enumerate(capped, 1):
        report.attempted += 1
        media = _upload_one(page, url, source_name)
        if media is not None and media.file:
            try:
                new_url = media.file.url
            except Exception:
                new_url = ""
            if new_url:
                url_map[url] = new_url
                report.succeeded += 1
            else:
                report.add_failure(url, "uploaded but URL unavailable")
        else:
            report.add_failure(url, "download or sanitize failed")
        if progress_cb is not None:
            try:
                progress_cb(i, total)
            except Exception:
                pass  # 진행률 콜백은 best-effort

    replace_in_blocks(blocks, url_map)
    return report
