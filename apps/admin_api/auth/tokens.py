"""apps/admin_api/auth/tokens.py — 어드민 전용 JWT.

일반 사용자 토큰과 **같은 키로 서명하되 클레임으로 구분**한다. 서명 키를 나누는 방법도
있지만, 키가 늘면 회전·배포·DR 복구 절차가 그만큼 늘고 RS256 전환 때 겪은 "전 사용자
재로그인" 사고 표면이 넓어진다. 여기서 필요한 것은 "이 토큰이 2단계를 통과해 나왔는가"를
서버가 확인할 수 있으면 되는 것이고, 그건 클레임으로 충분하다.

클레임
    adm  1        어드민 토큰 표식. 이게 없는 토큰은 ``/api/v1/admin/**`` 에서 403.
    amr  [...]    실제로 통과한 수단 (["pwd","totp"] / ["pwd","totp","email"] /
                  ["pwd","backup_code"]). 사후 감사용 — 어떤 경로로 들어왔는지 남는다.
    did  "uuid"   기기 ID. 갱신 때 이 기기가 회수됐는지 확인한다.

수명 (settings)
    access  2시간          — 권한 회수(스태프 해제·기기 회수)가 반영되는 최대 지연.
    refresh 12시간 / 7일   — 비신뢰 기기 / 신뢰 기기. 신뢰 기기 7일은 프론트 Q3 수락분이며,
                            access 는 그대로 2시간이라 회수 지연은 늘지 않는다.

비밀번호 변경 시 토큰을 죽이는 클레임(``pwh``)은 **의도적으로 넣지 않았다.** simplejwt 의
``CHECK_REVOKE_TOKEN`` 을 켜면 전 사용자가 재로그인해야 하고, 어드민만 따로 구현해도
얻는 것은 "최대 2시간" 을 "즉시" 로 줄이는 것뿐이다. 계정을 진짜로 끊어야 할 때는
``is_active=False``(발급된 토큰까지 즉시 401) 와 기기 회수가 이미 있다.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.authentication.tokens import AppRefreshToken

# 어드민 토큰 표식 클레임. 게이트(apps/admin_api/gate.py)와 발급부가 공유하는 단일 소스.
ADMIN_CLAIM = "adm"
DEVICE_CLAIM = "did"
AMR_CLAIM = "amr"


class AdminAccessToken(AccessToken):
    """수명만 다른 access 토큰 (2시간).

    ``token_type`` 은 그대로 "access" 라 기존 ``JWTAuthentication`` 이 그대로 검증한다 —
    settings 의 ``AUTH_TOKEN_CLASSES`` 를 건드릴 필요가 없다(수명은 발급 시점에만 쓰인다).
    """

    lifetime: timedelta = settings.ADMIN_ACCESS_TOKEN_LIFETIME


class AdminRefreshToken(AppRefreshToken):
    """어드민 refresh 토큰 — email/full_name 클레임은 부모에서 그대로 상속."""

    access_token_class = AdminAccessToken


def is_admin_token(payload) -> bool:
    """검증된 토큰 페이로드가 어드민 토큰인가 (게이트·갱신 공용 판정)."""
    try:
        return bool(payload.get(ADMIN_CLAIM))
    except AttributeError:
        return False


def issue_admin_tokens(user, *, device_id: str, amr: list[str], trusted: bool) -> dict[str, str]:
    """어드민 access/refresh 발급.

    Args:
        device_id: 클라이언트 기기 ID (``did`` 클레임 → 갱신 때 회수 확인).
        amr: 통과한 인증 수단 목록. 항상 "pwd" 로 시작한다.
        trusted: 신뢰 기기면 refresh 수명을 길게 준다.
    """
    refresh = AdminRefreshToken.for_user(user)
    refresh[ADMIN_CLAIM] = 1
    refresh[AMR_CLAIM] = list(amr)
    refresh[DEVICE_CLAIM] = device_id or ""
    lifetime = (
        settings.ADMIN_REFRESH_TRUSTED_LIFETIME
        if trusted
        else settings.ADMIN_REFRESH_TOKEN_LIFETIME
    )
    # 부모 __init__ 이 기본 수명(7일)으로 이미 exp 를 박아뒀으므로 여기서 덮어쓴다.
    refresh.set_exp(lifetime=lifetime)
    access = refresh.access_token  # adm/amr/did 는 no_copy 대상이 아니라 그대로 복사된다
    return {"access": str(access), "refresh": str(refresh)}


def rotate_admin_refresh(raw_refresh: str) -> tuple[RefreshToken, int, str]:
    """들어온 refresh 를 검증·폐기하고 (원본 토큰, user_id, device_id) 를 돌려준다.

    검증은 ``RefreshToken(raw)`` 생성자가 한다(서명·만료·블랙리스트). 어드민 여부는
    호출부가 :func:`is_admin_token` 으로 확인한다 — 일반 refresh 로 어드민 토큰을 만들 수
    있으면 2단계 인증을 통째로 우회하게 된다.

    Raises:
        rest_framework_simplejwt.exceptions.TokenError: 서명·만료·블랙리스트 실패.
    """
    token = AdminRefreshToken(raw_refresh)
    user_id = token.payload.get("user_id")
    device_id = token.payload.get(DEVICE_CLAIM, "") or ""
    # 회전 — 쓴 refresh 는 즉시 블랙리스트. 실패해도(블랙리스트 앱 미설치 등) 갱신은 진행한다.
    try:
        token.blacklist()
    except AttributeError:  # pragma: no cover — blacklist 앱이 빠진 설정
        pass
    return token, user_id, device_id
