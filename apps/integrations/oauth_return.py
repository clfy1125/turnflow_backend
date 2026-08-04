"""IG OAuth 복귀 대상(`return_to`) 검증 + 결과 리다이렉트 조립 + postMessage 대상 오리진.

**왜 필요한가 (2026-08-04, iOS 제보)**
iOS 는 `https://www.instagram.com/oauth/authorize` 를 **유니버설 링크로 판정해 Instagram 앱을
띄운다.** 그러면 Safari 탭은 `about:blank` 로 남고, 웹에서 시작한 팝업 플로우는 돌아올 길이 없다
(앱 안에서 로그인이 끝나도 부모 창에 알릴 방법이 없음). authorize 호스트를 바꿔 피하는 길은
없다 — Instagram Business Login 이 문서화한 authorize 호스트는 `www.instagram.com` 하나뿐이고,
`facebook.com/dialog/oauth` 로 바꾸면 code 를 `api.instagram.com/oauth/access_token` 에서
교환할 수 없어 플로우 자체가 깨진다.

그래서 **팝업을 안 쓰는 길**을 연다: 시작할 때 복귀 주소(`return_to`)를 받아 두고, 콜백이
HTML 을 렌더하는 대신 그 주소로 302 시킨다. 같은 탭에서 진행되므로 팝업 차단·유니버설 링크
문제에서 완전히 벗어난다(앱이 열려도 앱 안에서 로그인이 끝나고 우리 주소로 되돌아온다).

**보안 — 오픈 리다이렉트 방어가 이 모듈의 존재 이유다.**
`return_to` 를 검증 없이 받으면 `?return_to=https://evil.com` 으로 우리 도메인을 세탁한
피싱 링크가 만들어진다. 그래서:

  1. **origin(scheme+host+port) 완전일치**만 허용한다. `startswith`/부분문자열 비교는
     전형적인 우회 구멍이다 — `https://turnflow.link.evil.com` 이 통과한다.
  2. scheme 은 `http`/`https` 로 제한한다(`javascript:`, `data:` 차단).
  3. netloc 에 userinfo(`@`)가 있으면 거부한다(파서별 해석 차이로 인한 혼동 회피).
  4. 결과 파라미터(`ig_result`/`reason`)는 호출자가 넣어둔 값을 **버리고** 서버가 다시 넣는다
     (클라이언트가 성공을 위조하지 못하게).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.conf import settings

MAX_RETURN_TO_LEN = 500

# 서버가 소유하는 결과 파라미터 — 호출자가 넣어와도 덮어쓴다.
RESULT_PARAM = "ig_result"
REASON_PARAM = "reason"

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(value: str) -> str | None:
    """URL 또는 origin 문자열을 정규화된 origin(`scheme://host[:port]`)으로 바꾼다.

    기본 포트(80/443)는 생략해 `https://a.com` 과 `https://a.com:443` 이 같게 취급되도록 한다.
    scheme/host 가 없거나 http(s) 가 아니면 None.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or "\\" in raw:
        return None
    try:
        p = urlparse(raw)
    except ValueError:
        return None
    scheme = (p.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    try:
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:  # 잘못된 포트 표기
        return None
    if not host:
        return None
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def allowed_origins() -> list[str]:
    """`return_to` / postMessage 로 허용되는 origin 목록 (정규화·중복제거, 순서 유지).

    `IG_OAUTH_RETURN_TO_ORIGINS` 가 비어 있으면 `CORS_ALLOWED_ORIGINS` 를 따른다 —
    이미 "우리가 신뢰하는 프론트" 목록이므로 별도 관리 부담을 만들지 않는다.
    """
    raw = list(getattr(settings, "IG_OAUTH_RETURN_TO_ORIGINS", None) or [])
    if not raw:
        raw = list(getattr(settings, "CORS_ALLOWED_ORIGINS", None) or [])
    out: list[str] = []
    for item in raw:
        o = normalize_origin(item)
        if o and o not in out:
            out.append(o)
    return out


def validate_return_to(value: str) -> tuple[str | None, str | None]:
    """(정규화된 return_to, 거부사유) 를 반환한다. 통과 시 사유는 None.

    통과한 URL 은 경로·쿼리를 그대로 보존하고 fragment 만 제거한다(리다이렉트에 무의미).
    """
    if value is None or value == "":
        return None, "empty"
    if not isinstance(value, str):
        return None, "not_a_string"
    raw = value.strip()
    if not raw:
        return None, "empty"
    if len(raw) > MAX_RETURN_TO_LEN:
        return None, "too_long"
    # 제어문자·역슬래시는 파서별 해석이 갈린다 → 거부
    if "\\" in raw or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        return None, "illegal_characters"

    try:
        p = urlparse(raw)
    except ValueError:
        return None, "unparsable"

    if (p.scheme or "").lower() not in _DEFAULT_PORTS:
        return None, "scheme_not_allowed"
    if "@" in (p.netloc or ""):
        return None, "userinfo_not_allowed"

    origin = normalize_origin(raw)
    if origin is None:
        return None, "unparsable"
    if origin not in allowed_origins():
        return None, "origin_not_allowed"

    cleaned = urlunparse((p.scheme.lower(), p.netloc, p.path, p.params, p.query, ""))
    return cleaned, None


def build_result_redirect(return_to: str, *, result: str, reason: str = "") -> str:
    """검증된 `return_to` 에 결과 파라미터를 붙인 최종 URL.

    호출자가 미리 넣어둔 `ig_result`/`reason` 은 제거하고 서버 값으로 대체한다.
    """
    p = urlparse(return_to)
    pairs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k not in (RESULT_PARAM, REASON_PARAM)
    ]
    pairs.append((RESULT_PARAM, result))
    if reason:
        pairs.append((REASON_PARAM, reason))
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(pairs), ""))


def postmessage_target_origins(preferred: str = "") -> list[str]:
    """콜백 페이지가 `postMessage` 대상으로 쓸 origin 목록.

    과거 코드는 `'*'` 로 브로드캐스트해 **어떤 opener 에게든** connection 페이로드를
    넘겼다. 여기서는 허용목록만 반환하고, 아는 경우 opener 의 origin 을 앞에 둔다.
    브라우저는 targetOrigin 이 일치하는 창에만 전달하므로, 목록 전체에 순서대로 보내도
    유출이 발생하지 않는다(불일치 대상에는 전달되지 않음).
    """
    origins = allowed_origins()
    pref = normalize_origin(preferred) if preferred else None
    if pref and pref in origins:
        return [pref] + [o for o in origins if o != pref]
    return origins
