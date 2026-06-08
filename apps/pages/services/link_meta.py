"""
apps/pages/services/link_meta.py

외부 상품/콘텐츠 URL → 메타 정보(title / thumbnail / price / original_price) 추출.

■ 동기
  페이지 빌더에서 사용자가 쿠팡·오늘의집 등 파트너스/상품 링크를 붙여넣으면
  프론트가 이 서비스로 메타데이터를 받아 링크 블록 필드를 자동 채운다.

■ 동작 분기
  1) 쿠팡 도메인(`*.coupang.com`) → 기존 ``CoupangPartnersService.lookup_by_url`` 재사용.
  2) 그 외(오늘의집 등) → 서버사이드로 HTML 을 직접 받아
       - title    : og:title → twitter:title → <title>
       - thumbnail: og:image(secure) → twitter:image (절대 URL 로 정규화)
       - price    : meta(product:price 등) → JSON-LD offers → 사이트별 셀렉터 순으로 폴백

■ 보안 (SSRF 방어)
  매 요청(리다이렉트 hop 포함)마다 호스트를 DNS 로 해석해 사설/루프백/링크로컬/
  예약 IP 로 향하면 즉시 차단한다. http/https 외 scheme 도 거절.
  (잔여 위험: DNS rebinding TOCTOU — requests 가 재해석하는 IP 는 핀하지 않음.
   현재 위협모델에선 hop 단위 검증으로 충분하다고 판단.)

■ 캐싱 / rate limit
  같은 URL 은 Redis 에 캐싱(성공 1h / 빈 결과 5m). 호출 측 뷰에서 사용자별 throttle.

■ 응답 / 타임아웃
  반환 dict 은 값이 있는 키만 포함(전부 optional). 가격은 콤마 없는 숫자 문자열,
  썸네일은 절대 http(s) URL. 에러/차단 페이지(403/404/"just a moment" 등)는 빈 dict.
  전체 처리는 15초 안에 끝나도록 connect/read 타임아웃 + 절대 deadline 으로 제한.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Iterator
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.core.cache import cache

from apps.pages.services.coupang import CoupangError, CoupangPartnersService

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────

_CACHE_PREFIX = "linkmeta:"
_CACHE_TTL_OK = 3600  # 값이 있는 결과 1h
_CACHE_TTL_EMPTY = 300  # 빈 결과 5m (일시 오류 재시도 여지)

_CONNECT_TIMEOUT = 4.0
_READ_TIMEOUT = 7.0
_MAX_REDIRECTS = 3
_MAX_BYTES = 1_500_000  # 본문은 1.5MB 까지만 읽음 (메타는 <head> 에 있음)
_DEADLINE_SEC = 12.0  # 전체 fetch 절대 상한 (뷰 15s 응답 보장 마진)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 차단/에러 페이지로 판단되는 title 패턴.
#   strong : 들어있으면 무조건 차단/에러 (challenge·forbidden 류)
#   weak   : title 이 짧을(<= 30자) 때만 에러로 간주 (정상 상품명 오탐 방지).
#            실제 에러 페이지 title 은 거의 다 짧다("404 Not Found", "Service Unavailable" 등)
_ERROR_TITLE_STRONG = (
    "just a moment",
    "attention required",
    "access denied",
    "are you a robot",
    "are you human",
    "captcha",
    "cloudflare",
    "ddos protection",
    "verify you are human",
    "접근이 거부",
    "잠시만 기다",
    "비정상적인 접근",
)
_ERROR_TITLE_WEAK = (
    "403",
    "404",
    "forbidden",
    "not found",
    "page not found",
    "error",
    "blocked",
    "service unavailable",
    "bad gateway",
    "too many requests",
    "오류",
    "페이지를 찾을 수 없",
    "찾을 수 없습니다",
)

# ── 외부 anti-bot 스크랩 서비스 (직접 fetch 가 막힐 때만 폴백) ──────────────
# provider 별 (endpoint, 파라미터 빌더). 대상 사이트 HTML 을 그대로 본문으로 돌려주는
# 서비스만 지원 — 받은 HTML 은 일반 경로와 동일한 파서를 그대로 탄다.
# render(JS 렌더링)/country 는 settings 로 조절, 프리미엄 플래그는 EXTRA_PARAMS 로.
_SCRAPE_PROVIDERS = {
    "scraperapi": {
        "endpoint": "https://api.scraperapi.com/",
        "build": lambda key, url, render, country: {
            "api_key": key,
            "url": url,
            "render": "true" if render else "false",
            **({"country_code": country} if country else {}),
        },
    },
    "scrapingbee": {
        "endpoint": "https://app.scrapingbee.com/api/v1/",
        "build": lambda key, url, render, country: {
            "api_key": key,
            "url": url,
            "render_js": "true" if render else "false",
            **({"country_code": country} if country else {}),
        },
    },
}

# 직접 fetch 가 항상 막혀(Akamai 등) 곧장 스크래퍼로 가는 호스트 (서픽스 매치).
# 막힌 사이트를 매번 직접 한 번 때려보고(=수 초 낭비) 폴백하는 대신 바로 스크랩.
_SCRAPE_FIRST_HOSTS = ("ohou.se",)


class LinkMetaFetchError(Exception):
    """외부 fetch 실패 (SSRF 차단 / DNS / timeout / 네트워크 / 차단 응답).

    ``scrapable`` 이 True 면 외부 anti-bot 스크랩 서비스로 재시도할 가치가 있는
    실패(403/5xx/네트워크 등)임을 뜻한다. SSRF·잘못된 scheme·비-HTML 처럼
    스크랩해도 의미 없거나 보내면 안 되는 경우는 False.
    """

    def __init__(self, message: str, *, scrapable: bool = True):
        super().__init__(message)
        self.scrapable = scrapable


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


def fetch_meta(url: str) -> dict:
    """URL → ``{title?, thumbnail?, price?, original_price?}`` (값 있는 키만).

    실패/차단/비-HTML/빈 메타는 빈 dict 을 돌려준다 (예외를 밖으로 던지지 않음).
    """
    normalized = (url or "").strip()
    if not normalized:
        return {}

    cache_key = _CACHE_PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    host = (urlparse(normalized).hostname or "").lower()
    if host == "coupang.com" or host.endswith(".coupang.com"):
        result = _fetch_coupang(normalized)
    else:
        result = _fetch_generic(normalized)

    cache.set(cache_key, result, _CACHE_TTL_OK if result else _CACHE_TTL_EMPTY)
    return result


# ─────────────────────────────────────────────────────────────
# 쿠팡 분기 — 기존 파트너스 서비스 재사용
# ─────────────────────────────────────────────────────────────


def _fetch_coupang(url: str) -> dict:
    """쿠팡 도메인 → CoupangPartnersService 결과를 flat 메타로 매핑.

    쿠팡은 Akamai 가 서버사이드 HTML fetch 를 IP 단위로 전부 403 차단하므로
    og 메타 스크랩이 불가능 → 공식 Partners Open API 로만 조회한다.
    단, mock 모드(키 미발급)에선 가짜 더미 데이터가 사용자 UI 에 노출되지 않도록
    빈 dict 을 돌려준다 (실 키 + COUPANG_MOCK_MODE=False 면 실데이터).
    """
    if getattr(settings, "COUPANG_MOCK_MODE", True):
        logger.info("link-meta coupang skipped (mock 모드 — 실 Partners 키 필요) url=%s", url[:80])
        return {}

    try:
        data = CoupangPartnersService.lookup_by_url(url)
    except CoupangError as e:
        logger.info("link-meta coupang lookup failed url=%s err=%s", url[:80], e)
        return {}
    except Exception as e:  # noqa: BLE001 — 외부 호출 방어
        logger.warning("link-meta coupang unexpected url=%s err=%s", url[:80], e)
        return {}

    out: dict = {}
    name = (data.get("product_name") or "").strip()
    if name:
        out["title"] = name
    image = (data.get("image_url") or "").strip()
    if image.lower().startswith(("http://", "https://")):
        out["thumbnail"] = image

    price = _price_to_str(data.get("price"))
    if price:
        out["price"] = price
    original = _price_to_str(data.get("original_price"))
    if original and original != price:
        out["original_price"] = original
    return out


def _price_to_str(value) -> str | None:
    """쿠팡의 int 가격 → 콤마 없는 양수 숫자 문자열. 0/None/음수는 None."""
    if isinstance(value, bool):  # bool 은 int 의 subclass — 방어
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    return None


# ─────────────────────────────────────────────────────────────
# 일반 사이트 분기 — HTML fetch + 파싱
# ─────────────────────────────────────────────────────────────


def _fetch_generic(url: str) -> dict:
    """일반 사이트: 직접 fetch(무료) → 차단되면 외부 스크래퍼 폴백 → HTML 파싱."""
    host = (urlparse(url).hostname or "").lower()

    # 항상 막히는 사이트(오늘의집 등)는 직접 fetch 생략하고 스크래퍼 직행.
    if _scraper_configured() and _host_in(host, _SCRAPE_FIRST_HOSTS):
        html = _scrape_get(url)
        return _parse_html(html, url) if html else {}

    try:
        html, final_url = _safe_get(url)
    except LinkMetaFetchError as e:
        if e.scrapable and _scraper_configured():
            logger.info("link-meta direct blocked → scrape fallback url=%s (%s)", url[:80], e)
            html = _scrape_get(url)
            return _parse_html(html, url) if html else {}
        logger.info("link-meta fetch gave up url=%s err=%s", url[:80], e)
        return {}

    return _parse_html(html, final_url)


def _parse_html(html: str | None, base_url: str) -> dict:
    """HTML → flat 메타. 차단/에러 title 이면 빈 dict."""
    if not html:
        return {}

    head = _head_region(html)
    metas = _extract_metas(head)

    title = _extract_title(head, metas)
    if title and _looks_like_error_title(title):
        # 차단/에러 페이지 — title 뿐 아니라 og:image/price 모두 신뢰 불가 → 빈 응답
        return {}

    out: dict = {}
    if title:
        out["title"] = title

    image = _extract_image(metas, base_url)
    if image:
        out["thumbnail"] = image

    price, original = _extract_price(metas, html, base_url)
    if price:
        out["price"] = price
    if original and original != price:
        out["original_price"] = original

    return out


# ── 외부 스크랩 폴백 ─────────────────────────────────────────


def _scraper_configured() -> bool:
    provider = (getattr(settings, "LINK_SCRAPER_PROVIDER", "") or "").strip().lower()
    key = (getattr(settings, "LINK_SCRAPER_API_KEY", "") or "").strip()
    return bool(provider and key and provider in _SCRAPE_PROVIDERS)


def _host_in(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _scrape_get(url: str) -> str | None:
    """외부 anti-bot 스크랩 서비스로 대상 HTML 을 받아온다. 미설정/실패 시 None.

    우리 서버가 대상에 직접 붙지 않고 스크랩 서비스(고정 공인 endpoint)만 호출하므로
    이 경로엔 SSRF 위험이 없다. 단, 사설/비정상 URL 은 유료 호출 낭비라 보내지 않는다.
    """
    if not _scraper_configured():
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None

    provider = settings.LINK_SCRAPER_PROVIDER.strip().lower()
    cfg = _SCRAPE_PROVIDERS[provider]
    render = bool(getattr(settings, "LINK_SCRAPER_RENDER_JS", True))
    country = (getattr(settings, "LINK_SCRAPER_COUNTRY", "") or "").strip()
    timeout = int(getattr(settings, "LINK_SCRAPER_TIMEOUT", 20))
    params = cfg["build"](settings.LINK_SCRAPER_API_KEY.strip(), url, render, country)
    # provider 별 프리미엄/스텔스 플래그 (예: "premium=true,stealth_proxy=true")
    for pair in (getattr(settings, "LINK_SCRAPER_EXTRA_PARAMS", "") or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k.strip()] = v.strip()

    try:
        resp = requests.get(cfg["endpoint"], params=params, timeout=(5.0, timeout))
    except requests.RequestException as e:
        logger.warning("link-meta scrape request failed url=%s err=%s", url[:80], e)
        return None
    if resp.status_code != 200:
        logger.info(
            "link-meta scrape non-200 provider=%s status=%s url=%s",
            provider,
            resp.status_code,
            url[:80],
        )
        return None
    return resp.text or None


def _safe_get(url: str) -> tuple[str, str]:
    """SSRF 가드 + 수동 리다이렉트 추적으로 HTML 을 받아온다.

    Returns:
        ``(html, final_url)`` — 성공 시에만 반환(html 은 항상 non-empty).

    Raises:
        LinkMetaFetchError: 실패. ``scrapable`` 로 스크랩 폴백 대상인지 구분.
            - 4xx/5xx, network, timeout, 리다이렉트 이슈 → scrapable=True
            - scheme 위반 / SSRF(사설 IP) / DNS 실패 / 비-HTML → scrapable=False
    """
    deadline = time.monotonic() + _DEADLINE_SEC
    current = url
    session = requests.Session()
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            if time.monotonic() > deadline:
                raise LinkMetaFetchError("deadline 초과")
            _assert_public_http_url(current)
            try:
                resp = session.get(
                    current,
                    headers=_REQUEST_HEADERS,
                    timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as e:
                raise LinkMetaFetchError(f"요청 실패: {e}") from e

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    raise LinkMetaFetchError("Location 없는 리다이렉트")
                current = urljoin(current, location)
                continue

            if resp.status_code >= 400:
                # 403/404/5xx 등 — 봇 차단 가능 → 스크랩 폴백 대상
                resp.close()
                raise LinkMetaFetchError(f"HTTP {resp.status_code}", scrapable=True)

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                # 이미지/JSON/PDF 등 — 파싱할 메타 없음(스크랩해도 무의미)
                resp.close()
                raise LinkMetaFetchError(f"비-HTML content-type: {ctype}", scrapable=False)

            html = _read_capped(resp, deadline)
            return html, resp.url or current

        raise LinkMetaFetchError("리다이렉트 횟수 초과")
    finally:
        session.close()


def _assert_public_http_url(url: str) -> None:
    """scheme 이 http(s) 이고, 호스트가 공인 IP 로만 해석되는지 검증."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise LinkMetaFetchError(f"허용되지 않은 scheme: {scheme or '(없음)'}", scrapable=False)
    host = parsed.hostname
    if not host:
        raise LinkMetaFetchError("호스트 없음", scrapable=False)

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise LinkMetaFetchError(f"DNS 조회 실패: {host}", scrapable=False) from e

    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        # IPv4-mapped IPv6(::ffff:10.0.0.1) 은 매핑을 벗겨 다시 검사
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise LinkMetaFetchError(f"사설/예약 IP 차단: {host} -> {ip}", scrapable=False)


def _read_capped(resp: requests.Response, deadline: float) -> str:
    """본문을 최대 ``_MAX_BYTES`` 까지만 읽고 인코딩을 추정해 디코드."""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= _MAX_BYTES or time.monotonic() > deadline:
                break
    except requests.RequestException as e:
        raise LinkMetaFetchError(f"본문 읽기 실패: {e}") from e
    finally:
        resp.close()

    raw = b"".join(chunks)
    return _decode(raw, resp)


def _decode(raw: bytes, resp: requests.Response) -> str:
    """Content-Type charset → <meta charset> → utf-8 순으로 디코드."""
    enc: str | None = None
    ctype = resp.headers.get("Content-Type") or ""
    m = re.search(r"charset=([\w\-]+)", ctype, re.IGNORECASE)
    if m:
        enc = m.group(1)
    if not enc:
        head = raw[:4096].decode("ascii", "ignore").lower()
        mm = re.search(r'charset=["\']?([\w\-]+)', head)
        if mm:
            enc = mm.group(1)
    enc = enc or "utf-8"
    try:
        return raw.decode(enc, "replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", "replace")


# ─────────────────────────────────────────────────────────────
# HTML 파싱 (정규식 — 코드베이스 관례, BeautifulSoup 미사용)
# ─────────────────────────────────────────────────────────────

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z_:][\w:-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LDJSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _head_region(html: str) -> str:
    """og/title 파싱용으로 ``</head>`` 까지만 자른다 (없으면 앞 600KB)."""
    idx = html.lower().find("</head>")
    if idx != -1:
        return html[: idx + 7]
    return html[:600_000]


def _extract_metas(head_html: str) -> dict:
    """``<meta>`` 들을 ``{property|name|itemprop(lower): content}`` 로 수집(첫 값 우선)."""
    metas: dict = {}
    for tag in _META_TAG_RE.findall(head_html):
        attrs: dict = {}
        for m in _ATTR_RE.finditer(tag):
            key = m.group(1).lower()
            val = m.group(2) if m.group(2) is not None else m.group(3)
            attrs[key] = val
        meta_key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        content = attrs.get("content")
        if meta_key and content is not None:
            metas.setdefault(meta_key.lower(), unescape(content.strip()))
    return metas


def _extract_title(head_html: str, metas: dict) -> str | None:
    for key in ("og:title", "twitter:title"):
        val = metas.get(key)
        if val:
            return val
    m = _TITLE_RE.search(head_html)
    if m:
        text = unescape(re.sub(r"\s+", " ", m.group(1)).strip())
        return text or None
    return None


def _extract_image(metas: dict, base_url: str) -> str | None:
    for key in (
        "og:image:secure_url",
        "og:image:url",
        "og:image",
        "twitter:image",
        "twitter:image:src",
    ):
        val = metas.get(key)
        if val:
            absolute = _absolutize(val, base_url)
            if absolute:
                return absolute
    return None


def _absolutize(url: str, base_url: str) -> str | None:
    """상대/프로토콜-상대 URL 을 절대 http(s) URL 로. http(s) 아니면 None."""
    u = (url or "").strip()
    if not u:
        return None
    if u.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        u = f"{scheme}:{u}"
    elif not u.lower().startswith(("http://", "https://")):
        u = urljoin(base_url, u)
    return u if u.lower().startswith(("http://", "https://")) else None


def _looks_like_error_title(title: str) -> bool:
    """차단/에러 페이지 title 인지 판별."""
    t = (title or "").strip().lower()
    if not t:
        return False
    if any(p in t for p in _ERROR_TITLE_STRONG):
        return True
    if len(t) <= 30 and any(p in t for p in _ERROR_TITLE_WEAK):
        return True
    return False


# ─────────────────────────────────────────────────────────────
# 가격 추출 — meta → JSON-LD → 사이트별 셀렉터
# ─────────────────────────────────────────────────────────────


def _extract_price(metas: dict, html: str, final_url: str) -> tuple[str | None, str | None]:
    price, original = _price_from_meta(metas)
    if not price:
        p, o = _price_from_jsonld(html)
        price = price or p
        original = original or o
    if not price:
        host = (urlparse(final_url).hostname or "").lower()
        p, o = _price_from_site_specific(host, html)
        price = price or p
        original = original or o
    return price, original


def _price_from_meta(metas: dict) -> tuple[str | None, str | None]:
    price = None
    for key in (
        "product:price:amount",
        "product:sale_price:amount",
        "og:price:amount",
        "og:product:price:amount",
    ):
        price = _clean_price(metas.get(key))
        if price:
            break
    original = None
    for key in (
        "product:original_price:amount",
        "product:price:standard_amount",
        "og:price:standard_amount",
    ):
        original = _clean_price(metas.get(key))
        if original:
            break
    return price, original


def _price_from_jsonld(html: str) -> tuple[str | None, str | None]:
    """``application/ld+json`` 의 ``offers`` 에서 가격 추출."""
    for m in _LDJSON_RE.finditer(html):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for node in _iter_jsonld_nodes(data):
            price, original = _price_from_offers(node.get("offers"))
            if price:
                return price, original
    return None, None


def _iter_jsonld_nodes(data) -> Iterator[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_nodes(item)
    elif isinstance(data, dict):
        yield data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_jsonld_nodes(item)


def _price_from_offers(offers) -> tuple[str | None, str | None]:
    if offers is None:
        return None, None
    if isinstance(offers, list):
        for off in offers:
            price, original = _price_from_offers(off)
            if price:
                return price, original
        return None, None
    if not isinstance(offers, dict):
        return None, None
    spec = offers.get("priceSpecification")
    spec_price = None
    if isinstance(spec, dict):
        spec_price = spec.get("price")
    elif isinstance(spec, list) and spec and isinstance(spec[0], dict):
        spec_price = spec[0].get("price")
    price = _clean_price(offers.get("price") or offers.get("lowPrice") or spec_price)
    original = _clean_price(offers.get("highPrice"))
    return price, original


def _price_from_site_specific(host: str, html: str) -> tuple[str | None, str | None]:
    """og/JSON-LD 로 가격이 안 잡히는 사이트용 폴백 (오늘의집 등)."""
    if host.endswith("ohou.se"):
        return _price_ohou(html)
    return None, None


def _price_ohou(html: str) -> tuple[str | None, str | None]:
    """오늘의집 상품 페이지 — 페이지 내 임베드 JSON 상태에서 가격 키를 정규식으로 추출."""
    price = None
    for key in ("salePrice", "sellingPrice", "discountedPrice", "sellPrice", "price"):
        m = re.search(rf'"{key}"\s*:\s*(\d{{3,}})', html)
        if m:
            price = m.group(1)
            break
    original = None
    for key in ("originalPrice", "originPrice", "listPrice", "consumerPrice"):
        m = re.search(rf'"{key}"\s*:\s*(\d{{3,}})', html)
        if m:
            original = m.group(1)
            break
    return price, original


def _clean_price(raw) -> str | None:
    """숫자/문자 가격 → 콤마 없는 양수 숫자 문자열. 0/없음/추출불가는 None.

    정수면 정수 문자열, 소수면 소수 유지 (KRW 는 정수).
    """
    if raw is None or isinstance(raw, bool):
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return None
    num = m.group(0).replace(",", "")
    if "." in num:
        try:
            f = float(num)
        except ValueError:
            return None
        if f <= 0:
            return None
        return str(int(f)) if f == int(f) else num
    if num.lstrip("0") == "":
        return None
    return str(int(num))
