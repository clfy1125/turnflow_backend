"""DM 이전 — 타사 도구가 감싼 링크를 **원본 목적지로 되돌린다**.

왜 필요한가 — 타사 DM 도구는 사용자가 넣은 링크를 자기 도메인으로 감싸서 보낸다.
그 래퍼를 그대로 우리 캠페인에 옮기면 **남의 서비스에 의존하는 캠페인**이 된다.

    · 사용자가 그 도구를 해지하면 우리가 보낸 DM 의 링크가 죽는다.
    · 클릭이 그 도구의 통계로 계속 흘러간다(우리 지표에는 안 남는다).
    · 소셜비즈 래퍼는 ``recipientId`` 가 **수신자마다 다르다**. 한 사람 링크를 그대로
      옮기면 전원에게 "그 한 사람의 링크" 가 나간다(실측: uuid 75개 × 수신자 수백 명).

그래서 옮길 때 원본 목적지로 바꾼다. **단, 바꾸다 실패하면 원래 링크를 그대로 쓴다** —
링크가 바뀌는 것보다 링크가 없어지는 게 나쁘다.

**두 층으로 나눈 이유 — 값이 링크 안에 있으면 호출하지 않는다**

    ① 오프라인(:func:`unwrap_url`) · 호출 0
       인포크(``?url=``) · 인스타(``?u=``) · 리틀리(경로의 JWT 페이로드 ``url``).
       실측(2026-08-18, prod 후보 1,597건): 인포크 522 · 리틀리 92 · 인스타 6 = **620건**
       을 네트워크 없이 푼다. 리틀리 안에 인스타 래퍼가 또 들어 있어 재귀로 푼다.

    ② 네트워크(:class:`Resolver`) · 래퍼 키마다 1회
       소셜비즈(302 ``Location``) · 매니챗(200 본문의 ``<a href>``).
       실측 591 + 118 = 709건이 **uuid 75개 + act 23개 = 98회 조회**로 풀린다.
       키 단위 캐시(30일)라 다음 계정·다음 실행에서는 0회가 된다.

**추적 파라미터만 떼고 제휴 코드는 남긴다.** ``mcp_token``(매니챗이 pid/sid 를 담아 붙임)·
``recipientId`` 는 남의 도구가 붙인 것이라 떼지만, ``refCode``·``sourceId``·``utm_*`` 는
**사장님의 제휴·귀속 코드**다. 이걸 떼면 사장님 수익이 사라진다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

MAX_HOPS = 4  # 래퍼가 래퍼를 감싼 경우(리틀리→인스타→목적지). 순환 방지 겸 상한.
CACHE_TTL = 30 * 86400  # 래퍼 키 → 목적지는 안 바뀐다. 길게 잡아 계정 간에도 재사용.
CACHE_PREFIX = "dmmig:link:v1:"
FETCH_TIMEOUT = 8.0
FETCH_MAX_DEFAULT = 300  # 잡 1건이 쓸 수 있는 조회 수 상한(실측 필요량 98)
_UA = "Mozilla/5.0 (compatible; TurnFlowLinkResolver/1.0)"

# ── ① 오프라인: 목적지가 파라미터에 그대로 있는 래퍼 ──
_PARAM_WRAPPERS: dict[str, tuple[str, ...]] = {
    "link.inpock.co.kr": ("url",),
    "l.instagram.com": ("u",),
    "l.facebook.com": ("u",),
    "lm.facebook.com": ("u",),
    "l.messenger.com": ("u",),
}
# 호스트를 모르는 래퍼에도 쓰는 일반 키. **값이 다른 호스트의 절대 URL 일 때만** 인정한다
# (그냥 `?to=` 가 있는 정상 랜딩 페이지를 리다이렉터로 오인하지 않도록).
_PARAM_KEYS_ANY = frozenset(
    {
        "redirect",
        "redirect_url",
        "redirecturl",
        "target",
        "dest",
        "destination",
        "to",
        "out",
        "link",
    }
)
# ── ① 오프라인: 목적지가 경로의 JWT 페이로드에 있는 래퍼 ──
#   서명은 검증하지 않는다 — 우리는 남의 토큰의 **클레임을 읽을 뿐**이고, 읽은 값은
#   http(s) 절대 URL 인지 다시 확인한다.
_JWT_WRAPPERS: dict[str, str] = {"litt.ly": "/t/"}
_JWT_URL_KEYS = ("url", "u", "target", "link")

# ── ② 네트워크가 필요한 래퍼 ──
_SOCIALBIZ_HOSTS = frozenset({"socialbiz-c.nhndata.com", "socialbiz.nhndata.com"})
_MANYCHAT_HOSTS = frozenset({"my.manychat.com", "manychat.com", "app.manychat.com"})
# 매니챗 본문에는 자기 CSS·파비콘 href 가 섞여 있다 → 자기 자산 호스트는 후보에서 뺀다.
_MANYCHAT_OWN = ("manychat.com", "mccdn.me", "manychat.io")
_HREF_RE = re.compile(r"""href=["']([^"'>]+)["']""", re.I)
_JS_REDIR_RE = re.compile(
    r"""(?:location\.(?:href|replace)\s*[=(]\s*|window\.location\s*=\s*)["'](https?://[^"']+)["']""",
    re.I,
)

# 남의 도구가 붙인 추적 파라미터 — 이것만 뗀다(소문자 비교).
_TRACKERS = frozenset({"mcp_token", "recipientid", "recipient_id", "subscriber_id", "psid"})


def _host_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _is_abs(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _maybe_unquote(value: str) -> str:
    """이중 인코딩(``https%253A``)까지 푼다. 절대 URL 이 되면 **즉시 멈춘다** —
    더 풀면 목적지 쿼리에 정상적으로 들어 있는 ``%XX`` 를 망친다."""
    v = value or ""
    for _ in range(3):
        if _is_abs(v):
            return v
        nxt = unquote(v)
        if nxt == v:
            break
        v = nxt
    return v


def _from_jwt(path: str, prefix: str) -> str:
    tok = path.split(prefix, 1)[1].split("/")[0]
    seg = tok.split(".")
    if len(seg) < 2:
        return ""
    body = seg[1] + "=" * (-len(seg[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — 남의 토큰 포맷이 바뀌어도 그냥 못 푼 것으로 둔다
        return ""
    if not isinstance(data, dict):
        return ""
    for key in _JWT_URL_KEYS:
        v = data.get(key)
        if isinstance(v, str) and _is_abs(v.strip()):
            return v.strip()
    return ""


def strip_trackers(url: str) -> tuple[str, bool]:
    """남의 도구가 붙인 추적 파라미터만 뗀다. ``(url, 뗐나)``.

    **하나도 안 떼면 원문 문자열을 그대로 돌려준다** — 재인코딩으로 링크 모양이
    괜히 바뀌면 사용자가 "우리가 링크를 바꿨다" 고 의심하게 된다.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url, False
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in _TRACKERS]
    if len(kept) == len(pairs):
        return url, False
    return urlunsplit(parts._replace(query=urlencode(kept))), True


def _unwrap_once(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = _host_of(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    qmap = {k.lower(): v for k, v in query}

    for key in _PARAM_WRAPPERS.get(host, ()):
        v = _maybe_unquote(qmap.get(key.lower(), ""))
        if _is_abs(v):
            return v, "param"

    prefix = _JWT_WRAPPERS.get(host)
    if prefix and prefix in parts.path:
        v = _from_jwt(parts.path, prefix)
        if _is_abs(v):
            return v, "jwt"

    for key, raw in query:
        if key.lower() not in _PARAM_KEYS_ANY:
            continue
        v = _maybe_unquote(raw)
        if _is_abs(v) and _host_of(v) != host:
            return v, "param"
    return "", ""


def unwrap_url(url: str, *, strip: bool = True) -> tuple[str, str]:
    """**호출 0으로** 풀 수 있는 만큼 푼다. ``(최종 URL, 방법)``.

    방법은 감사용 표시다 — ``""``(안 바뀜) · ``param`` · ``jwt`` · ``strip`` 을 ``+`` 로 이었다.
    푸는 데 실패하면 **입력을 그대로** 돌려준다(안전장치).

    ``strip=False`` 는 **아직 조회해야 하는 래퍼**에 쓴다. 소셜비즈는 ``recipientId`` 가
    없으면 302 를 주지 않는데, 그게 우리가 떼려는 추적 파라미터다 — 떼고 조회하면
    전부 실패한다(실측: 이 순서를 틀려 75개 uuid 가 모두 실패했다).
    """
    cur = (url or "").strip()
    if not _is_abs(cur):
        return url or "", ""
    hops: list[str] = []
    seen = {cur}
    for _ in range(MAX_HOPS):
        nxt, how = _unwrap_once(cur)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cur = nxt  # 트래커 제거는 마지막 한 번만 (중간 홉의 파라미터는 다음 홉의 재료다)
        hops.append(how)
    if strip:
        cur, stripped = strip_trackers(cur)
        if stripped:
            hops.append("strip")
    return cur, "+".join(h for h in hops if h)


def needs_network(url: str) -> bool:
    """오프라인으로는 못 풀지만 **조회하면 풀리는** 래퍼인가."""
    host = _host_of(url or "")
    return host in _SOCIALBIZ_HOSTS or host in _MANYCHAT_HOSTS


def is_wrapped(url: str) -> bool:
    """남의 도구 래퍼로 보이나(옮기기 전에 풀어야 하는가)."""
    if not _is_abs((url or "").strip()):
        return False
    if needs_network(url):
        return True
    final, how = unwrap_url(url)
    return bool(how) and final != url


def cache_key_for(url: str) -> str:
    """래퍼 **키** 단위 캐시 키. 소셜비즈는 수신자마다 URL 이 달라서 uuid 로 묶어야
    조회가 1회로 줄어든다(591건 → 75회). 매니챗은 ``act`` 가 링크 1개를 가리킨다."""
    host = _host_of(url)
    qmap = {k.lower(): v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    if host in _SOCIALBIZ_HOSTS and qmap.get("uuid"):
        return f"socialbiz:{qmap['uuid']}"
    if host in _MANYCHAT_HOSTS and qmap.get("act"):
        return f"manychat:{qmap['act']}"
    return f"url:{hashlib.sha1(url.encode('utf-8', 'replace')).hexdigest()}"  # noqa: S324


def _probe_url(url: str) -> str:
    """조회에 쓸 URL. 소셜비즈는 ``recipientId=0`` 으로 **바꾸거나 없으면 붙인다**.

    바꾸는 이유는 **실제 사용자의 클릭으로 집계되지 않게** 하려는 것이다 — 사장님의
    타사 통계를 우리 조회로 오염시키면 안 된다. 없으면 붙이는 이유는 소셜비즈가 이
    파라미터 없이는 302 를 주지 않기 때문이다.
    """
    host = _host_of(url)
    if host not in _SOCIALBIZ_HOSTS:
        return url
    parts = urlsplit(url)
    pairs = [
        (k, "0" if k.lower() in ("recipientid", "recipient_id") else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    if not any(k.lower() in ("recipientid", "recipient_id") for k, _v in pairs):
        pairs.append(("recipientId", "0"))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def _pick_manychat_target(body: str) -> str:
    """매니챗 중간 페이지에서 목적지를 고른다.

    매니챗은 목적지에 ``mcp_token``(pid/sid 를 담은 JWT)을 붙여 심는다 → 그게 있으면
    그것이 목적지다. 없으면 자기 자산(CSS·파비콘)이 아닌 첫 외부 링크를 쓴다.
    """
    hrefs = [h.strip() for h in _HREF_RE.findall(body) if _is_abs(h.strip())]
    for h in hrefs:
        if "mcp_token=" in h:
            return h
    for h in hrefs:
        if not any(own in _host_of(h) for own in _MANYCHAT_OWN):
            return h
    js = _JS_REDIR_RE.findall(body)
    for h in js:
        if not any(own in _host_of(h) for own in _MANYCHAT_OWN):
            return h
    return ""


class Resolver:
    """래퍼 링크를 조회로 푼다 — **키마다 한 번, 잡마다 상한, 실패는 원본 유지.**

    ``httpx`` 를 지연 임포트하고 클라이언트를 재사용한다. Meta Graph 와 무관한 호출이라
    Graph 페이서(``RateLimiter``)를 쓰지 않지만, ``fetch_max`` 로 스스로 상한을 둔다.
    """

    def __init__(self, *, fetch_max: int | None = None, enabled: bool | None = None):
        self.fetch_max = (
            fetch_max
            if fetch_max is not None
            else int(getattr(settings, "DM_MIGRATION_LINK_FETCH_MAX", FETCH_MAX_DEFAULT))
        )
        self.enabled = (
            enabled
            if enabled is not None
            else bool(getattr(settings, "DM_MIGRATION_RESOLVE_LINKS", True))
        )
        self.fetched = 0
        self.failed = 0
        self._client = None
        self._mem: dict[str, str] = {}

    # ── 내부 ──
    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                follow_redirects=False,
                timeout=FETCH_TIMEOUT,
                headers={"User-Agent": _UA},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> Resolver:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _fetch(self, url: str) -> str:
        """한 번 조회해서 목적지를 얻는다. 못 얻으면 ``""``."""
        client = self._get_client()
        resp = client.get(_probe_url(url))
        loc = (resp.headers.get("location") or "").strip()
        if 300 <= resp.status_code < 400 and _is_abs(loc) and _host_of(loc) != _host_of(url):
            return loc
        if resp.status_code == 200 and _host_of(url) in _MANYCHAT_HOSTS:
            return _pick_manychat_target(resp.text)
        return ""

    # ── 공개 ──
    def resolve(self, url: str) -> tuple[str, str]:
        """``(최종 URL, 방법)``. 어떤 이유로든 못 풀면 **입력을 그대로** 돌려준다."""
        raw = (url or "").strip()
        if not _is_abs(raw):
            return url or "", ""
        # 조회가 필요한 래퍼면 **트래커를 떼지 않은 채로** 홉을 푼다 (_probe_url 이 그 값을 쓴다).
        offline, how = unwrap_url(raw, strip=False)
        if not needs_network(offline):
            return unwrap_url(raw)
        if not self.enabled:
            return offline, how

        key = cache_key_for(offline)
        hit = self._mem.get(key)
        if hit is None:
            hit = cache.get(CACHE_PREFIX + key)
            if hit is not None:
                self._mem[key] = hit
        if hit:
            final, h2 = unwrap_url(hit)
            return final, "+".join(x for x in (how, "cache", h2) if x)
        if hit == "":  # 캐시에 '못 풀었다' 가 박혀 있으면 다시 조회하지 않는다
            return offline, how
        if self.fetched >= self.fetch_max:
            logger.warning(
                "DM이전 링크 조회 상한 도달 (%d) — 원본 링크를 유지합니다", self.fetch_max
            )
            return offline, how

        self.fetched += 1
        try:
            got = self._fetch(offline)
        except Exception as exc:  # noqa: BLE001 — 링크 하나 못 풀어서 이전이 멈추면 안 된다
            self.failed += 1
            logger.info("DM이전 링크 조회 실패 (%s): %s", _host_of(offline), exc)
            return offline, how
        # 성공/실패 모두 캐시에 남긴다. 실패를 남기면 같은 죽은 래퍼를 계정마다 다시 두드리지 않는다.
        self._mem[key] = got
        cache.set(CACHE_PREFIX + key, got, CACHE_TTL if got else 3600)
        if not got:
            self.failed += 1
            return offline, how
        final, h2 = unwrap_url(got)
        return final, "+".join(x for x in (how, "fetch", h2) if x)

    def resolve_many(self, urls) -> dict[str, str]:
        """``{원본 URL: 최종 URL}``. 같은 URL 은 한 번만 처리한다."""
        out: dict[str, str] = {}
        for u in urls:
            u = (u or "").strip()
            if not u or u in out:
                continue
            final, _how = self.resolve(u)
            out[u] = final
        return out


def rewrite_text(text: str, mapping: dict[str, str]) -> str:
    """DM 본문 안에 박힌 래퍼 링크도 바꾼다.

    본문에 URL 이 그대로 들어있는 캠페인이 많다("자료는 https://... 에서"). 버튼만 바꾸고
    본문을 놔두면 **한 DM 안에서 두 링크가 갈린다.** 긴 URL 부터 치환해 부분 문자열
    겹침(래퍼가 다른 래퍼의 접두사인 경우)을 피한다.
    """
    if not text:
        return text
    out = text
    for raw in sorted(mapping, key=len, reverse=True):
        final = mapping[raw]
        if final and final != raw and raw in out:
            out = out.replace(raw, final)
    return out
