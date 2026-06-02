"""Playwright 기반 페이지 모바일 미리보기 캡쳐.

Headless Chromium 으로 ``settings.SNAPSHOT_BASE_URL`` + ``/@{slug}`` 를 열어
iPhone 12 viewport (390×844, DPR 2) 의 최초 화면(스크롤 전)을 PNG → WebP 로 변환.

비동기 — Celery 태스크 ``capture_reference_snapshot`` 에서만 호출.
sync 컨텍스트에서 동작 (Celery worker 가 sync 라서 sync_playwright 가 적합).

설계 노트:
  - browser 인스턴스 캐싱 X — Celery worker 는 task 별 fork/restart 가능.
  - 한 캡쳐가 ~3~10초 (페이지 로드 + networkidle + 캡쳐).
  - 메모리 ~150MB / Chromium 인스턴스 — concurrency 제어 권장.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image

logger = logging.getLogger(__name__)

# ── 캡쳐 정책 상수 ───────────────────────────────────────────
MOBILE_VIEWPORT = {"width": 390, "height": 844}
DEVICE_SCALE_FACTOR = 2
NETWORKIDLE_TIMEOUT_MS = 15_000
EXTRA_WAIT_MS = 2_000
TOTAL_TIMEOUT_MS = 30_000
WEBP_QUALITY = 82
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1 TurnFlowSnap/1.0"
)


class SnapshotError(Exception):
    """페이지 캡쳐 실패. Celery 태스크가 status=failed 로 기록."""


@dataclass
class SnapshotResult:
    """캡쳐 결과 — Page.reference_snapshot.save() 에 그대로 넘길 수 있는 묶음."""

    content_file: ContentFile  # ContentFile(webp_bytes, name="...")
    suggested_name: str
    width: int
    height: int
    elapsed_seconds: float


def _resolve_target_url(slug: str) -> str:
    """공개 페이지 URL 조립. ``SNAPSHOT_BASE_URL`` → fallback ``FRONTEND_URL`` → fallback localhost."""
    base = (
        getattr(settings, "SNAPSHOT_BASE_URL", None)
        or getattr(settings, "FRONTEND_URL", None)
        or "http://localhost:3000"
    )
    return f"{base.rstrip('/')}/@{slug}"


def capture_page_snapshot(slug: str) -> SnapshotResult:
    """공개 페이지 URL 을 Playwright 로 캡쳐.

    Raises:
        SnapshotError: 페이지 로드 실패, 타임아웃, Playwright 자체 오류 등.
    """
    try:
        from playwright.sync_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
    except ImportError as e:
        raise SnapshotError(
            "playwright 패키지가 설치되어 있지 않습니다. "
            "requirements.txt 의 playwright 설치 후 "
            "`python -m playwright install chromium` 실행 필요."
        ) from e

    url = _resolve_target_url(slug)
    started = time.monotonic()
    png_bytes: bytes | None = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",            # Docker 컨테이너 root 사용자에서 필수
                    "--disable-dev-shm-usage", # /dev/shm 부족 (작은 컨테이너) 회피
                    "--disable-gpu",
                ],
            )
            try:
                context = browser.new_context(
                    viewport=MOBILE_VIEWPORT,
                    device_scale_factor=DEVICE_SCALE_FACTOR,
                    user_agent=USER_AGENT,
                )
                page = context.new_page()
                page.set_default_timeout(TOTAL_TIMEOUT_MS)

                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=TOTAL_TIMEOUT_MS,
                )
                if response is None or response.status >= 400:
                    status_str = (
                        str(response.status) if response is not None else "no-response"
                    )
                    raise SnapshotError(f"페이지 로드 실패 — status={status_str}, url={url}")

                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=NETWORKIDLE_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    logger.info("snapshot networkidle 타임아웃 — 강행: %s", url)

                page.wait_for_timeout(EXTRA_WAIT_MS)
                png_bytes = page.screenshot(
                    type="png",
                    full_page=False,
                    omit_background=False,
                )
            finally:
                browser.close()
    except SnapshotError:
        raise
    except PlaywrightError as e:
        raise SnapshotError(f"Playwright 오류: {e}") from e

    if not png_bytes:
        raise SnapshotError("스크린샷 바이트가 비어 있습니다.")

    webp_bytes, w, h = _png_to_webp(png_bytes)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"snapshot_{slug}_{timestamp}.webp"
    return SnapshotResult(
        content_file=ContentFile(webp_bytes, name=name),
        suggested_name=name,
        width=w,
        height=h,
        elapsed_seconds=round(time.monotonic() - started, 2),
    )


def _png_to_webp(png_bytes: bytes) -> tuple[bytes, int, int]:
    """PNG bytes → WebP bytes. 알파 채널은 제거 (배경 흰색 합성 X — 페이지 색감 보존)."""
    with Image.open(io.BytesIO(png_bytes)) as im:
        if im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        out = io.BytesIO()
        im.save(out, format="WEBP", quality=WEBP_QUALITY, method=6)
        return out.getvalue(), w, h
