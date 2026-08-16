"""apps/admin_api/auth/challenge.py — 1단계와 2단계를 잇는 단기 티켓.

비밀번호가 맞았다는 사실을 2단계 요청까지 들고 가야 하는데, 그 방법이 두 가지다.

1. 클라이언트에 비밀번호를 들고 있게 하고 2단계에서 함께 보낸다 → 비밀번호가 화면·메모리·
   재시도 로그에 오래 남는다.
2. 서버가 짧은 티켓을 발급한다 → 비밀번호는 1단계 요청 한 번으로 끝난다.

2번을 쓴다. 티켓은 Redis 에만 있고(DB 부담 0) TTL 5분, 시도 5회를 넘기면 스스로 사라진다.

**시도 횟수를 티켓 안에 두는 것이 핵심이다.** DRF 스로틀은 IP 로 키잉되므로 IP 를 바꾸면
초기화되는데, 티켓 카운터는 티켓에 붙어 있어 우회할 수 없다. 5회를 쓰면 처음(비밀번호)부터
다시 해야 한다.

캐시가 비워지면(Redis flush) 진행 중이던 로그인만 실패하고 사용자는 다시 로그인한다 —
5분짜리 데이터라 유실 영향이 없다.
"""

from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_KEY_TMPL = "admin:mfa:chal:{token}"


def create_challenge(
    *,
    user_id: int,
    device_id: str,
    needs_email: bool,
    email_token_id: int | None = None,
) -> str:
    """티켓 발급 → 불투명 문자열. 내용은 서버에만 있다(클라이언트는 문자열만 들고 다닌다)."""
    token = secrets.token_urlsafe(32)
    cache.set(
        _KEY_TMPL.format(token=token),
        {
            "user_id": user_id,
            "device_id": device_id,
            "needs_email": needs_email,
            "email_token_id": email_token_id,
            "attempts": 0,
        },
        timeout=settings.ADMIN_MFA_CHALLENGE_TTL_SECONDS,
    )
    return token


def load_challenge(token: str) -> dict | None:
    """티켓 조회. 만료·없음이면 None."""
    if not token:
        return None
    return cache.get(_KEY_TMPL.format(token=token))


def register_attempt(token: str) -> bool:
    """실패 1회 기록. 한도를 넘겼으면 티켓을 파기하고 False.

    남은 TTL 을 보존하려고 ``cache.touch`` 를 쓰지 않는다 — django-redis 의 기본 ``set`` 은
    TTL 을 새로 매기므로, 시도를 반복해 티켓 수명을 무한히 늘릴 수 있다. 대신 남은 시간을
    다시 계산해 넣는다.
    """
    key = _KEY_TMPL.format(token=token)
    data = cache.get(key)
    if data is None:
        return False
    data["attempts"] = data.get("attempts", 0) + 1
    if data["attempts"] >= settings.ADMIN_MFA_CHALLENGE_MAX_ATTEMPTS:
        cache.delete(key)
        logger.warning(
            "[admin-mfa] challenge 시도 한도 초과 — 티켓 파기 user=%s", data.get("user_id")
        )
        return False
    ttl = cache.ttl(key) if hasattr(cache, "ttl") else None
    # ttl 을 못 읽는 백엔드(LocMem 등)에서는 남은 시간을 알 수 없다 → 보수적으로 짧게 잡는다.
    cache.set(key, data, timeout=ttl if ttl and ttl > 0 else 60)
    return True


def consume_challenge(token: str) -> None:
    """성공 후 즉시 파기 — 같은 티켓으로 토큰을 두 번 받지 못하게."""
    cache.delete(_KEY_TMPL.format(token=token))
