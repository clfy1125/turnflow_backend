"""
apps/integrations/profile_image.py

IG 프로필 사진을 IG CDN → 우리 측 스토리지(R2/로컬 default_storage)로 재호스팅.

■ 동기
  IG CDN 의 profile_picture_url 은 서명된 일시 URL 이라 만료될 수 있고
  (apps/insights/models.py:114 참고), hotlink 차단/CORS 문제도 잠재한다.
  연동 시점에 한 번 다운로드해서 우리 도메인에 보관하면 프론트가 안정적으로 노출 가능.

■ 정책
  - 다운로드: timeout 8s, 최대 5MB (프로필 사진은 작음)
  - 정제: apps/pages/image_pipeline.process_upload 재사용 (EXIF 제거, 2048px 상한, 정규화)
  - 저장 경로: ig_profiles/{ig_user_id}/{hash}.{ext} — 같은 사용자라도 새 사진은 새 파일
  - 콘텐츠 해시 dedup: 같은 바이트면 재업로드 생략 (URL 만 반환)

■ 사용
    from apps.integrations.profile_image import fetch_and_store_profile_image
    new_url = fetch_and_store_profile_image(remote_url, ig_user_id)
"""

from __future__ import annotations

import hashlib
import io
import logging
import urllib.error
import urllib.request
from typing import Optional

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.pages.image_pipeline import ImageValidationError, process_upload

logger = logging.getLogger(__name__)


_HOSTED_PREFIX = "ig_profiles"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB — 프로필 사진은 보통 수십 KB
_FETCH_TIMEOUT_SEC = 8


class ProfileImageFetchError(Exception):
    """프로필 이미지 다운로드/정제/저장 실패."""


def fetch_and_store_profile_image(remote_url: str, ig_user_id: str) -> str:
    """원격 IG 프로필 사진 → 다운로드 → sanitize → default_storage 저장 → 저장된 URL 반환.

    Args:
        remote_url: IG /me 응답의 profile_picture_url (서명된 CDN URL)
        ig_user_id: IGAccountConnection.external_account_id (스토리지 경로 분리용)

    Returns:
        스토리지(R2/로컬)에 저장된 안정 URL.

    Raises:
        ProfileImageFetchError: 다운로드/정제/저장 어느 단계든 실패.
    """
    if not remote_url:
        raise ProfileImageFetchError("remote_url 이 비어 있음")
    if not ig_user_id:
        raise ProfileImageFetchError("ig_user_id 가 비어 있음")

    try:
        raw = _download(remote_url)
    except (urllib.error.HTTPError, urllib.error.URLError, IOError, TimeoutError) as e:
        raise ProfileImageFetchError(f"프로필 이미지 다운로드 실패: {e}") from e

    name_hint = "profile.jpg"
    upload = ContentFile(raw, name=name_hint)
    try:
        processed = process_upload(upload)
    except ImageValidationError as e:
        raise ProfileImageFetchError(f"프로필 이미지 정제 실패: {e}") from e

    # 콘텐츠 해시 → dedup. 같은 사진이면 재업로드 생략.
    digest = hashlib.sha256(processed.content).hexdigest()
    key = f"{_HOSTED_PREFIX}/{ig_user_id}/{digest}.{processed.extension}"

    if default_storage.exists(key):
        logger.info("profile image dedup hit ig_user_id=%s key=%s", ig_user_id, key)
        return default_storage.url(key)

    try:
        default_storage.save(key, ContentFile(processed.content))
    except Exception as e:  # noqa: BLE001
        raise ProfileImageFetchError(f"프로필 이미지 저장 실패: {e}") from e

    logger.info("profile image stored ig_user_id=%s key=%s", ig_user_id, key)
    return default_storage.url(key)


def _download(url: str) -> bytes:
    """외부 URL → 바이트. 크기 캡 chunk 단위 강제 (대용량 메모리 폭주 방어)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (TurnflowBackend/1.0 ig-profile-fetch)",
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:
        buf = io.BytesIO()
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_BYTES:
                raise IOError(f"이미지 사이즈 한도 초과: {total} > {_MAX_BYTES}")
            buf.write(chunk)
        return buf.getvalue()
