"""S3-b 미디어 다운로드 — 샘플 영상 + 필요한 썸네일만 로컬로 내린다.

IG 서명 URL 은 영상 +32h / 썸네일 +4.4일에 만료되므로 **수집과 같은 런에서** 받아야 한다.
받은 파일은 리포트 렌더까지만 쓰고 런 디렉터리와 함께 파기한다(원본 미디어 무보관).

키 = shortcode (파이프라인 전 레이어 공통):
    MEDIA_DIR/{shortcode}.mp4        Gemini 영상 분석 입력
    MEDIA_DIR/{shortcode}_thumb.jpg  리포트 썸네일(렌더가 data-URI 로 인라인)
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from apps.core.ssrf import UnsafeURLError, assert_public_url

from . import config

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)
SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([^/?]+)")

# 짧은 저용량 릴스가 실존(실측 850KB) → 하한은 200KB. 만료 시 돌아오는 HTML 응답만 걸러 내면 된다.
VIDEO_MIN_BYTES = 200 << 10
THUMB_MIN_BYTES = 2048
MAX_TOTAL_BYTES = 1_500 * 1024 * 1024  # 런당 1.5GB 상한 (워커 디스크 보호)
WORKERS = 5
VIDEO_TIMEOUT = 120
# IG CDN 호스트만 허용 — 수집 응답이 오염돼도 임의 호스트로 나가지 않게 한다.
ALLOWED_HOST_SUFFIXES = ("cdninstagram.com", "fbcdn.net", "instagram.com")


def shortcode_of(post: dict) -> str | None:
    m = SHORTCODE_RE.search(post.get("permalink") or "")
    return m.group(1) if m else None


def pick_thumb_url(post: dict) -> str | None:
    mt = post.get("media_type")
    if mt == "VIDEO":
        return post.get("thumbnail_url")
    if mt == "IMAGE":
        return post.get("media_url")
    if mt == "CAROUSEL_ALBUM":
        for c in (post.get("children") or {}).get("data") or []:
            u = c.get("thumbnail_url") or c.get("media_url")
            if u:
                return u
    return post.get("media_url")


def _url_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        return False
    try:
        assert_public_url(url)
    except (UnsafeURLError, Exception):  # noqa: BLE001 - 판단 불가 시 차단
        return False
    return True


def _download(url: str, dest: Path, min_bytes: int, kind: str) -> str:
    """단일 파일 다운로드. 반환 코드: ok|skip|expired|blocked|toosmall:N|error:X"""
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return "skip"
    if not _url_allowed(url):
        return "blocked"
    tmp = dest.with_suffix(dest.suffix + ".part")
    timeout = VIDEO_TIMEOUT if kind == "video" else 30
    for attempt in range(3):
        try:
            with requests.get(
                url, headers={"User-Agent": UA}, timeout=timeout, stream=True, allow_redirects=True
            ) as r:
                if r.status_code in (403, 410):
                    return "expired"
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if kind == "video" and not ctype.startswith("video"):
                    return f"badtype:{ctype[:30]}"
                written = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        written += len(chunk)
            if written < min_bytes:
                tmp.unlink(missing_ok=True)
                return f"toosmall:{written}"
            os.replace(tmp, dest)
            return "ok"
        except (requests.RequestException, OSError) as e:
            if attempt == 2:
                tmp.unlink(missing_ok=True)
                return f"error:{type(e).__name__}"
    return "error"


def needed_thumbs(metrics: dict, sample: dict) -> set[str]:
    """리포트가 실제로 보여 주는 썸네일 집합 — 전량 다운로드를 피한다."""
    want: set[str] = set()
    for p in metrics.get("top_posts") or []:
        if p.get("shortcode"):
            want.add(p["shortcode"])
    for p in metrics.get("low_posts") or []:
        if p.get("shortcode"):
            want.add(p["shortcode"])
    for v in sample.get("videos") or []:
        if v.get("shortcode"):
            want.add(v["shortcode"])
    want |= set(sample.get("light_images") or [])
    return want


def download_for_run(official_doc: dict, metrics: dict, sample: dict, *, on_progress=None) -> dict:
    """샘플 영상 + 필요한 썸네일 다운로드. 반환: 결과 코드별 카운트 + 총 바이트."""
    by_sc: dict[str, dict] = {}
    for post in official_doc.get("posts") or []:
        sc = shortcode_of(post)
        if sc:
            by_sc[sc] = post

    video_scs = [
        v["shortcode"] for v in (sample.get("videos") or []) if v.get("shortcode") in by_sc
    ]
    thumb_scs = [sc for sc in needed_thumbs(metrics, sample) if sc in by_sc]

    jobs: list[tuple[str, Path, int, str]] = []
    for sc in video_scs:
        post = by_sc[sc]
        if post.get("media_type") == "VIDEO" and post.get("media_url"):
            jobs.append(
                (post["media_url"], config.MEDIA_DIR / f"{sc}.mp4", VIDEO_MIN_BYTES, "video")
            )
    for sc in thumb_scs:
        url = pick_thumb_url(by_sc[sc])
        if url:
            jobs.append((url, config.MEDIA_DIR / f"{sc}_thumb.jpg", THUMB_MIN_BYTES, "thumb"))

    stats: dict[str, int] = {}
    done = 0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_download, *job): job for job in jobs}
        for fut in cf.as_completed(futs):
            code = fut.result()
            bucket = code.split(":")[0]
            stats[bucket] = stats.get(bucket, 0) + 1
            done += 1
            if on_progress:
                on_progress(done, len(jobs))
            if bucket not in ("ok", "skip"):
                logger.info("insta_report: media download %s -> %s", Path(futs[fut][1]).name, code)
            # 디스크 폭주 방어 — 상한을 넘기면 남은 작업을 버리고 있는 것만 쓴다.
            if done % 10 == 0 and _total_bytes() > MAX_TOTAL_BYTES:
                logger.warning("insta_report: media size cap hit — 남은 다운로드 중단")
                break

    stats["total_bytes"] = _total_bytes()
    stats["videos_requested"] = len(video_scs)
    stats["thumbs_requested"] = len(thumb_scs)
    return stats


def _total_bytes() -> int:
    try:
        return sum(f.stat().st_size for f in config.MEDIA_DIR.iterdir() if f.is_file())
    except OSError:
        return 0
