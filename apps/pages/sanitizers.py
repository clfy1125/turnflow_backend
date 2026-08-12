"""공개 링크페이지 ``custom_css`` 검사기 (감사 M-9).

## 무엇을 막나

``custom_css`` 는 공개 페이지 응답(`GET /api/v1/pages/@{slug}/`)에 그대로 실려 나가고,
프론트가 ``<style>`` 로 주입한다. 지금까지 서버 검증이 전혀 없었다.

가장 위험한 건 **``</style>`` 브레이크아웃**이다. CSS 안에 ``</style><img src=x onerror=...>``
를 넣으면 스타일 요소를 빠져나와 **HTML 로 실행**된다. 공개 페이지는 로그인 앱과 **같은
오리진(`turnflow.link`)** 이라, 그 페이지를 방문한 로그인 사용자의 세션까지 노린다.
페이지 소유자가 자기 페이지에 심어 두면 방문자마다 터지는 **저장형 XSS** 다.

그다음이 URL 이다. ``url()`` / ``@import`` 로 외부 주소를 부르면 방문자 정보가 그 서버로
새고, 남의 CSS 가 시간이 지나 바뀌는 공급망 위험도 생긴다.

## 기존 스타일을 깨뜨리지 않는다 — 허용목록 방식

교과서는 "``@import`` 를 막아라" 라고 하지만, **실서비스 19곳이 구글 폰트를 그걸로 부르고
있다**(2026-08-12 전수 조사). 무조건 막으면 그 19개 페이지의 글꼴이 즉시 깨진다.
그래서 **차단이 아니라 허용목록**으로 간다 — 신뢰할 수 있는 호스트면 통과, 나머지만 제거.

같은 조사에서 위험 패턴(``<script`` · ``javascript:`` · ``expression()`` · ``behavior:`` ·
``-moz-binding`` · ``vbscript:``)은 **한 건도 없었다**. 즉 이 검사기를 켜도 **현재 데이터는
아무것도 바뀌지 않는다**(배포 전 전수 대조로 확인할 것).

## 두 단계로 처리한다

1. **브레이크아웃·스크립트 실행 구문** → **저장 거부**(``ValidationError``).
   정상 CSS 에는 절대 나올 수 없는 것들이라, 조용히 지우기보다 알려주는 게 맞다.
2. **허용목록 밖 URL** → 해당 ``url()`` 만 ``none`` 으로 치환하고 나머지는 살린다.
   AI 페이지 생성이 예상 못 한 이미지 주소를 넣어도 저장 자체가 실패하지 않게 한다.

## CSS 이스케이프

CSS 는 ``\6a\61...`` 로 ``javascript`` 를 쓸 수 있다. 그래서 패턴을 보기 전에 **이스케이프를
먼저 푼다**. (HTML 문자참조 ``&#106;`` 는 ``<style>`` 안에서 해석되지 않으므로 우회로가 아니다.)
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

#: url() / @import 에서 허용할 호스트. settings 로 확장 가능하게 둔다 —
#: 고객이 다른 호스트를 필요로 하면 배포 없이 늘릴 수 있다.
DEFAULT_ALLOWED_URL_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "media.turnflow.clfy.ai.kr",  # 우리 R2 (고객 업로드 이미지)
    "turnflow-api.clfy.ai.kr",
)

#: 저장을 거부하는 구문. 정상 CSS 에는 나올 수 없다.
#:   · 앞의 셋은 <style> 을 빠져나가 HTML 로 실행되는 경로
#:   · 뒤의 넷은 CSS 에서 스크립트를 실행시키던 레거시 구문
FORBIDDEN_PATTERNS = (
    (r"</\s*style", "</style> — 스타일 태그를 빠져나가는 구문"),
    (r"<\s*/?\s*script", "<script> 태그"),
    (r"<\s*iframe", "<iframe> 태그"),
    (r"javascript\s*:", "javascript: 주소"),
    (r"vbscript\s*:", "vbscript: 주소"),
    (r"expression\s*\(", "expression() — 스크립트 실행 구문"),
    (r"(?:^|[^-\w])behavior\s*:", "behavior: — 스크립트 실행 구문"),
    (r"-moz-binding\s*:", "-moz-binding — 스크립트 실행 구문"),
)

_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)")
_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]*)\1\s*\)", re.IGNORECASE)
_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*(['\"]?)([^'\")]*)\1\s*\)|(['\"])([^'\"]*)\3)", re.IGNORECASE
)


class CssSanitizeError(ValueError):
    """저장을 거부해야 하는 CSS. 어떤 구문 때문인지 ``reason`` 에 담는다."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _unescape_css(css: str) -> str:
    """CSS 이스케이프(``\\6a`` 등)를 풀어 패턴 매칭이 우회당하지 않게 한다.

    검사 **전용** 이다 — 저장되는 값은 원본 그대로다(사용자가 쓴 CSS 를 우리가 다시 쓰지 않는다).
    """

    def sub(m):
        hexpart, literal = m.group(1), m.group(2)
        if hexpart:
            try:
                return chr(int(hexpart, 16))
            except (ValueError, OverflowError):
                return ""
        return literal or ""

    return _ESCAPE_RE.sub(sub, css)


def _allowed_hosts() -> tuple[str, ...]:
    extra = getattr(settings, "CUSTOM_CSS_ALLOWED_URL_HOSTS", None)
    if extra:
        return tuple(extra)
    return DEFAULT_ALLOWED_URL_HOSTS


def _is_url_allowed(raw: str) -> bool:
    """``url()`` / ``@import`` 대상이 허용되는가."""
    u = (raw or "").strip()
    if not u:
        return True  # url() 빈 값 — 무해

    low = u.lower()

    # data: 는 이미지·폰트만. data:text/html 은 브레이크아웃 통로가 된다.
    if low.startswith("data:"):
        return low.startswith(("data:image/", "data:font/", "data:application/font"))

    # 페이지 내부 참조(SVG 필터 등)와 상대 경로는 허용. 예: url(#noise), url(%23noise)
    if u.startswith(("#", "%23", "/", "./", "../")):
        return True

    # 프로토콜 상대 // 도 절대 URL 로 취급
    if low.startswith("//"):
        low = "https:" + low

    if low.startswith(("http://", "https://")):
        host = low.split("//", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0]
        return host in _allowed_hosts()

    # 스킴 없는 상대 경로 (예: images/bg.png)
    if ":" not in low.split("/", 1)[0]:
        return True

    # 그 밖의 스킴(javascript:, vbscript: 등)은 위에서 이미 거부되지만 방어적으로
    return False


def sanitize_custom_css(css: str) -> tuple[str, list[str]]:
    """``(정리된 CSS, 제거된 항목 설명 목록)`` 을 돌려준다.

    브레이크아웃·스크립트 실행 구문이 있으면 ``CssSanitizeError`` 를 던진다(저장 거부).
    허용목록 밖 URL 은 ``none`` 으로 치환하고 그 사실을 목록에 담는다.
    """
    if not css:
        return css or "", []

    probe = _unescape_css(css)
    for pattern, label in FORBIDDEN_PATTERNS:
        if re.search(pattern, probe, re.IGNORECASE):
            raise CssSanitizeError(label)

    removed: list[str] = []

    def _url_sub(m):
        target = m.group(2)
        if _is_url_allowed(_unescape_css(target)):
            return m.group(0)
        removed.append(f"url({target[:80]})")
        return "none"

    def _import_sub(m):
        target = m.group(2) or m.group(4) or ""
        if _is_url_allowed(_unescape_css(target)):
            return m.group(0)
        removed.append(f"@import {target[:80]}")
        return ""

    cleaned = _IMPORT_RE.sub(_import_sub, css)
    cleaned = _URL_RE.sub(_url_sub, cleaned)
    return cleaned, removed


def clean_custom_css_field(css: str, *, context: str = "") -> str:
    """시리얼라이저용 얇은 래퍼 — 제거 항목을 로그로 남기고 정리된 CSS 를 돌려준다.

    ``CssSanitizeError`` 는 호출측이 ``serializers.ValidationError`` 로 바꿔 던진다.
    """
    cleaned, removed = sanitize_custom_css(css)
    if removed:
        logger.warning(
            "custom_css: 허용되지 않은 URL %d건 제거 (%s) — %s",
            len(removed),
            context or "-",
            removed[:5],
        )
    return cleaned
