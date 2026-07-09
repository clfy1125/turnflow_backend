"""
apps/pages/services/external_importers/dispatch.py

URL → 소스 자동 감지 + 외부 페이지 fetch + 변환을 한 번에 묶는 진입점.

지원 호스트는 ``SOURCES`` 화이트리스트(인포크 ``link.inpock.co.kr``/``inpk.link``
/ 리틀리 ``litt.ly`` / 링크트리 ``linktr.ee``/``linktree.com``) 뿐이다 — SSRF 방어
차원에서 그 외의 호스트는 ``UnsupportedSourceError`` 로 즉시 거절한다. 판별은 URL 의
**hostname 정확 일치** (``www.`` 접두만 허용) 로 하고, 실제 fetch 도 사용자
URL 을 그대로 쓰지 않고 **화이트리스트 호스트 + 추출한 slug 로 재구성한 URL**
로만 나간다 — 경로에 지원 호스트명을 박은 우회 URL(SECURITY_AUDIT M-7) 무력화.

인포크는 단축 도메인 ``inpk.link`` 를 병행 운영한다(2026-07 관측: 같은 Next.js
앱이라 페이지 HTML 바이트 동일, 단 구페이지는 ``link.inpock.co.kr`` 에서만
서빙되고 ``inpk.link`` 는 404). 링크트리도 ``linktree.com`` 을 ``linktr.ee`` 로
301 리다이렉트하는 별칭으로 운영한다. 그래서 한 호스트가 404 를 주면 같은
소스의 다른 호스트로 1회 폴백한다 — 사용자가 어느 도메인 주소를 붙여넣어도 동작.

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
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

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
        # hosts[0] = canonical. 이후는 별칭 도메인 — 404 시 이 순서로 폴백.
        "hosts": ("link.inpock.co.kr", "inpk.link"),
        # IGNORECASE: detect_source 가 hostname 을 .lower() 로 판별하므로 slug 추출도 동일 기준
        "url_regex": re.compile(r"(?:link\.inpock\.co\.kr|inpk\.link)/([^/?#]+)", re.IGNORECASE),
        "public_url_tmpl": "https://link.inpock.co.kr/{slug}",
    },
    "litly": {
        "label": "리틀리",
        "fetch": litly.fetch_payload,
        "convert": litly.convert,
        "hosts": ("litt.ly",),
        "url_regex": re.compile(r"litt\.ly/([^/?#]+)", re.IGNORECASE),
        "public_url_tmpl": "https://litt.ly/{slug}",
    },
    "linktree": {
        "label": "링크트리",
        "fetch": linktree.fetch_payload,
        "convert": linktree.convert,
        # linktree.com 은 linktr.ee 로 301 리다이렉트되는 별칭(프로필 실재) — canonical 은 linktr.ee.
        "hosts": ("linktr.ee", "linktree.com"),
        "url_regex": re.compile(r"(?:linktr\.ee|linktree\.com)/([^/?#]+)", re.IGNORECASE),
        "public_url_tmpl": "https://linktr.ee/{slug}",
    },
}


SUPPORTED_HOST_LABEL = ", ".join(h for cfg in SOURCES.values() for h in cfg["hosts"])


# ──────────────────────────────────────────────────────────────────────
# Mock 모드
# ──────────────────────────────────────────────────────────────────────


MOCK_MODE_ENV = "EXTERNAL_IMPORT_MOCK_MODE"
MOCK_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "_mock_fixtures")


def _is_mock_mode() -> bool:
    return os.environ.get(MOCK_MODE_ENV, "").strip().lower() in ("1", "true", "yes")


def _load_mock(source: str, slug: str) -> dict | None:
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


def _url_hostname(url: str) -> str:
    """URL 의 hostname 을 소문자로 추출 (``www.`` 접두 제거). 실패 시 빈 문자열.

    스킴 없는 입력(``litt.ly/foo``)도 판별되도록 ``//`` 가 없으면 붙여서 파싱.
    """
    try:
        parsed = urlsplit(url if "//" in url else f"//{url}")
        host = (parsed.hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def detect_source(url: str) -> str | None:
    """URL 호스트로 소스 키를 판별. 화이트리스트 외 호스트는 ``None``.

    부분문자열 포함이 아니라 **hostname 정확 일치** — 경로/서브도메인에 지원
    호스트명을 박은 우회 URL(``https://evil.com/link.inpock.co.kr/x``)을 거절한다.
    """
    if not url or not isinstance(url, str):
        return None
    host = _url_hostname(url)
    if not host:
        return None
    for name, cfg in SOURCES.items():
        if host in cfg["hosts"]:
            return name
    return None


def parse_slug(url: str, source: str) -> str | None:
    """URL 에서 source 별 정규식으로 slug 만 추출."""
    cfg = SOURCES.get(source)
    if not cfg:
        return None
    m = cfg["url_regex"].search(url or "")
    return m.group(1) if m else None


def _candidate_fetch_urls(url: str, source: str, slug: str) -> list[str]:
    """화이트리스트 호스트 + slug 로 재구성한 fetch 후보 URL 목록.

    사용자 URL 을 그대로 fetch 하지 않는 게 핵심 — 호스트는 항상 ``SOURCES``
    화이트리스트에서만 나온다. 사용자가 입력한 호스트를 첫 후보로, 같은 소스의
    나머지 별칭 호스트를 404 폴백 순서로 뒤에 붙인다 (인포크 신/구 도메인처럼
    페이지가 한쪽 호스트에만 존재할 수 있음).
    """
    hosts = list(SOURCES[source]["hosts"])
    user_host = _url_hostname(url)
    hosts.sort(key=lambda h: 0 if h == user_host else 1)
    return [f"https://{h}/{slug}" for h in hosts]


def fetch_payload(url: str, source: str) -> dict:
    """URL → 외부 페이지 페이로드 dict.

    - Mock 모드: ``_mock_fixtures`` 에서 로드 (없으면 ExternalFetchError).
    - 실모드: slug 로 재구성한 후보 URL 들에 소스별 ``fetch_*`` 호출.
      404 면 같은 소스의 다음 호스트로 폴백, 전부 404 → SourcePageNotFoundError.
      그 외 HTTP/네트워크 에러 → ExternalFetchError (폴백 없이 즉시).
    """
    cfg = SOURCES.get(source)
    if not cfg:
        raise UnsupportedSourceError(f"지원하지 않는 소스: {source!r}")

    slug = parse_slug(url, source)
    if not slug:
        raise UnsupportedSourceError(f"URL 에서 slug 를 추출할 수 없습니다: {url!r}")

    if _is_mock_mode():
        payload = _load_mock(source, slug)
        if payload is None:
            raise ExternalFetchError(
                f"Mock 픽스처 없음: {MOCK_FIXTURES_DIR}/{source}/api-{slug}-nextdata.json"
            )
        return payload

    fetch_fn: Callable[[str], dict] = cfg["fetch"]
    not_found: urllib.error.HTTPError | None = None
    for fetch_url in _candidate_fetch_urls(url, source, slug):
        try:
            return fetch_fn(fetch_url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                not_found = e
                continue  # 같은 소스의 다음 호스트로 폴백
            raise ExternalFetchError(f"외부 호스트 응답 {e.code}: {fetch_url}") from e
        except urllib.error.URLError as e:
            # DNS / connection / SSL — 일시적 또는 호스트 이슈
            raise ExternalFetchError(f"외부 호스트 연결 실패: {fetch_url} ({e.reason})") from e
        except (TimeoutError, OSError) as e:
            raise ExternalFetchError(f"외부 호스트 timeout/IO 오류: {fetch_url} ({e})") from e
        except Exception as e:  # noqa: BLE001 — 컨버터 모듈이 RuntimeError 등을 던질 수 있음
            # 예: __NEXT_DATA__ 못 찾았다 / base64 디코드 실패 — 대상 페이지 형식이 우리가 알던 것과 다름
            raise ExternalFetchError(f"외부 페이지 페이로드 추출 실패: {fetch_url} ({e})") from e
    raise SourcePageNotFoundError(f"외부 페이지를 찾을 수 없습니다: {url}") from not_found


def import_from_url(url: str) -> tuple[str, str, dict]:
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
        raise UnsupportedSourceError(f"URL 에서 slug 를 추출할 수 없습니다: {url}")

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
        raise EmptyPageError(f"외부 페이지에 변환 가능한 콘텐츠가 없습니다: {url}")

    return source, slug, body
