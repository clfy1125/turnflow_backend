"""공용 SSRF 가드 — 외부에서 받은 URL 을 서버가 fetch 하기 전에 검증한다.

link_meta._assert_public_http_url 와 동일한 정책을 공용 유틸로 추출한 것(H-4).
- scheme 은 http/https 만 허용
- 호스트가 해석되는 **모든 IP** 가 공인 IP 여야 함(사설/루프백/링크로컬/예약/멀티캐스트/미지정 차단)
- IPv4-mapped IPv6(::ffff:10.0.0.1) 는 매핑을 벗겨 재검사
- urllib 사용 시 리다이렉트 대상도 **매 hop 마다** 재검증(302 로 내부망 이동하는 우회 차단)

DNS rebinding(검증 IP ≠ 실제 연결 IP)까지 완벽히 막지는 못하지만(TOCTOU),
가장 현실적인 공격면(사설 대역 직접 지정 + 리다이렉트 우회)을 차단한다.
"""

import ipaddress
import socket
import urllib.request
from urllib.parse import urlparse


class UnsafeURLError(Exception):
    """공인 URL 정책을 위반한 URL(사설 IP·비허용 scheme 등)."""


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped:
        addr = mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """URL 이 공인 http(s) 대상인지 검증. 위반 시 UnsafeURLError."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnsafeURLError(f"허용되지 않은 scheme: {scheme or '(없음)'}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("호스트 없음")
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS 조회 실패: {host}") from e
    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise UnsafeURLError(f"사설/예약 IP 차단: {host} -> {ip}")


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """리다이렉트 대상 URL 을 매 hop 마다 공인 IP 로 재검증한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # unsafe 면 UnsafeURLError 를 던져 리다이렉트 추적을 즉시 중단.
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_safe_opener() -> urllib.request.OpenerDirector:
    """리다이렉트 hop 마다 SSRF 재검증을 수행하는 urllib opener."""
    return urllib.request.build_opener(_ValidatingRedirectHandler)
