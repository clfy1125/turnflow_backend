"""
apps/integrations/media_thumbnail.py

캠페인 게시물 썸네일을 IG CDN → 우리 측 스토리지(R2/로컬 default_storage)로 재호스팅.

■ 동기 (이 파일이 존재하는 이유)
  IG Graph 가 주는 ``media_url``/``thumbnail_url`` 은 **서명된 일시 URL** 이다
  (``?_nc_ohc=…&oe=…&oh=…``). 그래서 DB 에 저장해 두면 얼마 뒤 반드시 깨진다.
  컬럼을 넓히는 것으로는 해결되지 않는다 — 만료가 길이 문제가 아니기 때문이다.

  ① 저장 안 하고 매 요청마다 Graph 재조회  → 응답마다 외부 HTTP N번(느리고 rate limit)
  ② CDN URL 저장                          → 수시간~수일 뒤 깨진 이미지
  ③ **한 번 받아서 우리 도메인에 사본 보관** ← 채택. 만료가 없고, 게시물이 삭제된 뒤에도 남고,
                                            프론트는 <img src> 에 그대로 쓸 수 있다.

  IG 프로필 사진에 이미 같은 결론을 적용해 두었다(:mod:`apps.integrations.profile_image`) —
  이 모듈은 그 규약을 게시물 썸네일로 확장한 것이다.

■ 정책
  - 다운로드: timeout 10s, 최대 20MB (IG 원본 이미지는 보통 100~500KB)
  - 정제: apps/pages/image_pipeline.process_upload 재사용 (EXIF 제거·포맷 정규화)
    + 최대 변 640px 로 축소 — 카드 썸네일 용도라 원본 해상도가 필요 없다
  - 저장 경로: ig_thumbnails/{ig_user_id}/{content_hash}.{ext}
  - 콘텐츠 해시 dedup: 같은 이미지면 재업로드 생략 (같은 게시물에 캠페인이 여러 개일 때 유용)

■ 사용
    from apps.integrations.media_thumbnail import fetch_and_store_campaign_thumbnail
    hosted_url = fetch_and_store_campaign_thumbnail(cdn_url, ig_user_id)
"""

from __future__ import annotations

import hashlib
import io
import logging
import urllib.error
import urllib.request

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.pages.image_pipeline import ImageValidationError, process_upload

logger = logging.getLogger(__name__)


_HOSTED_PREFIX = "ig_thumbnails"
_MAX_BYTES = 20 * 1024 * 1024  # 20 MiB — IG 원본 이미지 상한 여유분
_FETCH_TIMEOUT_SEC = 10
# 카드/요약 썸네일 용도라 640px 이면 2배 DPI 에서도 충분하다 (원본 2048px 는 과대).
THUMBNAIL_MAX_EDGE = 640


class ThumbnailFetchError(Exception):
    """썸네일 다운로드/정제/저장 실패."""


def fetch_and_store_campaign_thumbnail(remote_url: str, ig_user_id: str) -> str:
    """원격 IG 이미지 → 다운로드 → 정제·축소 → default_storage 저장 → 저장된 URL 반환.

    Args:
        remote_url: Graph 가 준 CDN 이미지 URL (:meth:`InstagramMediaService.get_media_thumbnail_source`).
        ig_user_id: IGAccountConnection.external_account_id (스토리지 경로 분리용).

    Returns:
        스토리지(R2/로컬)에 저장된 영구 URL.

    Raises:
        ThumbnailFetchError: 다운로드/정제/저장 어느 단계든 실패.
    """
    if not remote_url:
        raise ThumbnailFetchError("remote_url 이 비어 있음")
    if not ig_user_id:
        raise ThumbnailFetchError("ig_user_id 가 비어 있음")

    try:
        raw = _download(remote_url)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as e:
        raise ThumbnailFetchError(f"썸네일 다운로드 실패: {e}") from e

    upload = ContentFile(raw, name="thumbnail.jpg")
    try:
        processed = process_upload(upload, max_edge=THUMBNAIL_MAX_EDGE)
    except ImageValidationError as e:
        raise ThumbnailFetchError(f"썸네일 정제 실패: {e}") from e

    # 콘텐츠 해시 → dedup. 같은 게시물에 캠페인이 여러 개면 파일 1개를 공유한다.
    digest = hashlib.sha256(processed.content).hexdigest()
    key = f"{_HOSTED_PREFIX}/{ig_user_id}/{digest}.{processed.extension}"

    try:
        if default_storage.exists(key):
            logger.info("campaign thumbnail dedup hit ig_user_id=%s key=%s", ig_user_id, key)
            return default_storage.url(key)
        default_storage.save(key, ContentFile(processed.content))
    except Exception as e:  # noqa: BLE001 - 스토리지 예외 종류가 백엔드마다 다름
        raise ThumbnailFetchError(f"썸네일 저장 실패: {e}") from e

    logger.info(
        "campaign thumbnail stored ig_user_id=%s key=%s bytes=%s", ig_user_id, key, processed.size
    )
    return default_storage.url(key)


def _download(url: str) -> bytes:
    """외부 URL → 바이트. 크기 캡을 chunk 단위로 강제 (대용량 메모리 폭주 방어)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (TurnflowBackend/1.0 ig-thumbnail-fetch)",
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:  # noqa: S310
        buf = io.BytesIO()
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_BYTES:
                raise OSError(f"이미지 사이즈 한도 초과: {total} > {_MAX_BYTES}")
            buf.write(chunk)
        return buf.getvalue()
