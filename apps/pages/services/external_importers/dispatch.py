"""
apps/pages/services/external_importers/dispatch.py

URL → 소스 자동 감지 + 외부 페이지 fetch + 변환을 한 번에 묶는 진입점.

지원 호스트는 ``SOURCES`` 화이트리스트 안의 셋(``link.inpock.co.kr`` /
``litt.ly`` / ``linktr.ee``) 뿐이다 — SSRF 방어 차원에서 그 외의 호스트는
``UnsupportedSourceError`` 로 즉시 거절한다.

Mock 모드 (`EXTERNAL_IMPORT_MOCK_MODE=true`) 가 켜져 있으면 외부로 HTTP 요청을
보내지 않고 ``_mock_fixtures/{source}/{slug}.json`` 에서 페이로드를 로드한다.
오프라인 개발 / 테스트에서 외부 의존성을 끊기 위함.

뷰 레이어가 잡아야 하는 예외 4종:
    UnsupportedSourceError    → 400 (지원 호스트 아님 또는 slug 추출 실패)
    EmptyPageError            → 400 (페이지는 받았는데 의미 있는 콘텐츠 0)
    ExternalFetchError        → 502 (외부 호스트 timeout / 5xx / 네트워크 오류)
    SourcePageNotFoundError   → 404 (외부 페이지 자체가 없음)
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
from typing import Any, Callable, Optional, Tuple

from . import inpock, linktree, litly

# ──────────────────────────────────────────────────────────────────────
# 예외
# ──────────────────────────────────────────────────────────────────────


class UnsupportedSourceError(ValueError):
    """URL 호스트가 화이트리스트에 없거나 slug 를 추출할 수 없음."""


class ExternalFetchError(RuntimeError):
    """외부 호스트 fetch 실패 (timeout / 5xx / 네트워크 오류)."""


class SourcePageNotFoundError(RuntimeError):
    """외부 호스트가 404 를 돌려줘 페이지가 존재하지 않음."""


class EmptyPageError(RuntimeError):
    """fetch 는 성공했지만 변환 결과 의미 있는 블록이 0 — 임포트해도 빈 페이지."""


# ──────────────────────────────────────────────────────────────────────
# 소스 디스패치 테이블
# ──────────────────────────────────────────────────────────────────────


SOURCES: dict[str, dict[str, Any]] = {
    "inpock": {
        "label": "인포크",
        "fetch": inpock.fetch_nextdata,
        "convert": inpock.convert,
        "url_host": "link.inpock.co.kr",
        "url_regex": re.compile(r"link\.inpock\.co\.kr/([^/?#]+)"),
        "public_url_tmpl": "https://link.inpock.co.kr/{slug}",
    },
    "litly": {
        "label": "리틀리",
        "fetch": litly.fetch_payload,
        "convert": litly.convert,
        "url_host": "litt.ly",
        "url_regex": re.compile(r"litt\.ly/([^/?#]+)"),
        "public_url_tmpl": "https://litt.ly/{slug}",
    },
    "linktree": {
        "label": "링크트리",
        "fetch": linktree.fetch_payload,
        "convert": linktree.convert,
        "url_host": "linktr.ee",
        "url_regex": re.compile(r"linktr\.ee/([^/?#]+)"),
        "public_url_tmpl": "https://linktr.ee/{slug}",
    },
}


SUPPORTED_HOST_LABEL = ", ".join(cfg["url_host"] for cfg in SOURCES.values())


# ──────────────────────────────────────────────────────────────────────
# Mock 모드
# ──────────────────────────────────────────────────────────────────────


MOCK_MODE_ENV = "EXTERNAL_IMPORT_MOCK_MODE"
MOCK_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "_mock_fixtures")


def _is_mock_mode() -> bool:
    return os.environ.get(MOCK_MODE_ENV, "").strip().lower() in ("1", "true", "yes")


def _load_mock(source: str, slug: str) -> Optional[dict]:
    """``_mock_fixtures/{source}/api-{slug}-nextdata.json`` 또는 그냥
    ``{source}/{slug}.json`` 에서 페이로드를 로드. 둘 다 시도."""
    candidates = [
        os.path.join(MOCK_FIXTURES_DIR, source, f"api-{slug}-nextdata.json"),
        os.path.join(MOCK_FIXTURES_DIR, source, f"{slug}.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return None


# ──────────────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────────────


def detect_source(url: str) -> Optional[str]:
    """URL 호스트로 소스 키를 판별. 화이트리스트 외 호스트는 ``None``."""
    if not url or not isinstance(url, str):
        return None
    for name, cfg in SOURCES.items():
        if cfg["url_host"] in url:
            return name
    return None


def parse_slug(url: str, source: str) -> Optional[str]:
    """URL 에서 source 별 정규식으로 slug 만 추출."""
    cfg = SOURCES.get(source)
    if not cfg:
        return None
    m = cfg["url_regex"].search(url or "")
    return m.group(1) if m else None


def fetch_payload(url: str, source: str) -> dict:
    """URL → 외부 페이지 페이로드 dict.

    - Mock 모드: ``_mock_fixtures`` 에서 로드 (없으면 ExternalFetchError).
    - 실모드: 소스별 ``fetch_*`` 호출. 4xx → SourcePageNotFoundError(404 만),
      그 외 HTTP/네트워크 에러 → ExternalFetchError.
    """
    cfg = SOURCES.get(source)
    if not cfg:
        raise UnsupportedSourceError(f"지원하지 않는 소스: {source!r}")

    slug = parse_slug(url, source)

    if _is_mock_mode():
        if not slug:
            raise UnsupportedSourceError(
                f"Mock 모드에선 slug 가 추출 가능한 URL 만 허용: {url!r}"
            )
        payload = _load_mock(source, slug)
        if payload is None:
            raise ExternalFetchError(
                f"Mock 픽스처 없음: {MOCK_FIXTURES_DIR}/{source}/api-{slug}-nextdata.json"
            )
        return payload

    fetch_fn: Callable[[str], dict] = cfg["fetch"]
    try:
        return fetch_fn(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SourcePageNotFoundError(
                f"외부 페이지를 찾을 수 없습니다: {url}"
            ) from e
        raise ExternalFetchError(
            f"외부 호스트 응답 {e.code}: {url}"
        ) from e
    except urllib.error.URLError as e:
        # DNS / connection / SSL — 일시적 또는 호스트 이슈
        raise ExternalFetchError(
            f"외부 호스트 연결 실패: {url} ({e.reason})"
        ) from e
    except (TimeoutError, OSError) as e:
        raise ExternalFetchError(
            f"외부 호스트 timeout/IO 오류: {url} ({e})"
        ) from e
    except Exception as e:  # noqa: BLE001 — 컨버터 모듈이 RuntimeError 등을 던질 수 있음
        # 예: __NEXT_DATA__ 못 찾았다 / base64 디코드 실패 — 대상 페이지 형식이 우리가 알던 것과 다름
        raise ExternalFetchError(
            f"외부 페이지 페이로드 추출 실패: {url} ({e})"
        ) from e


def import_from_url(url: str) -> Tuple[str, str, dict]:
    """URL → ``(source_key, source_slug, body)`` 3-튜플.

    ``body`` 는 TurnflowLink 페이지 페이로드 (``title``/``is_public``/``data``/
    ``custom_css``/``blocks``/``_meta``). ``_meta`` 에 변환 통계 포함.

    호출 측은 보통 ``body`` 를 가지고 ``Page`` + ``Block`` 을 생성한다.
    """
    if not url or not isinstance(url, str):
        raise UnsupportedSourceError("URL 이 비어있습니다")

    source = detect_source(url)
    if source is None:
        raise UnsupportedSourceError(
            f"지원 호스트가 아닙니다 (지원: {SUPPORTED_HOST_LABEL}): {url}"
        )
    slug = parse_slug(url, source)
    if not slug:
        raise UnsupportedSourceError(
            f"URL 에서 slug 를 추출할 수 없습니다: {url}"
        )

    payload = fetch_payload(url, source)
    convert_fn: Callable[..., dict] = SOURCES[source]["convert"]
    body = convert_fn(payload, slug_override=slug)

    # 변환 후 의미 있는 블록 0개면 임포트 의미 없음 — 빈 페이지 (Litt.ly 미작성 등)
    blocks = body.get("blocks") or []
    meaningful = [
        b
        for b in blocks
        if b.get("type") != "profile"
        or (b.get("data") or {}).get("headline")
        or (b.get("data") or {}).get("avatar_url")
    ]
    if not meaningful:
        raise EmptyPageError(
            f"외부 페이지에 변환 가능한 콘텐츠가 없습니다: {url}"
        )

    return source, slug, body
