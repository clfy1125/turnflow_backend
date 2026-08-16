"""apps/admin_api/gate.py — 어드민 토큰 게이트 판정 (미들웨어가 호출).

## 왜 미들웨어인가

뷰마다 permission 을 다는 방식은 새 어드민 엔드포인트가 추가될 때 **조용히 누락**된다.
`/api/v1/admin/**` 는 이미 :class:`apps.admin_api.middleware.AdminRoleGuardMiddleware` 라는
단일 초크포인트를 갖고 있으므로, 2단계 인증 게이트도 같은 자리에 붙인다. 이 프리픽스에는
admin_api 뿐 아니라 ``admin/emails/``(apps.emails) · ``admin/pages/...``(apps.pages) 마운트도
함께 들어온다 — 그 둘까지 한 번에 덮인다.

## 무엇을 막는가

일반 로그인(`/api/v1/auth/login/`)으로 받은 토큰에는 ``adm`` 클레임이 없다. 그 토큰으로
어드민 API 를 부르면 403 ``admin_token_required``. 즉 **2단계를 통과하지 않으면 어드민
데이터에 닿지 못한다.**

## 무엇을 막지 않는가 (의도)

- ``ADMIN_MFA_ENFORCED=False`` (기본): 전부 통과. 롤아웃 순서를 강제하기 위한 스위치다 —
  관리자 전원이 인증앱을 등록하기 전에 켜면 세 명이 동시에 잠긴다.
- ``marketing_viewer`` (외주): 제외. 화이트리스트로 마케팅 대시보드 외 전 구간이 이미
  막혀 있고, 외주에 인증앱·기기 관리를 요구하는 건 현실적으로 관리가 안 된다.
- ``/api/v1/admin/auth/**``: 토큰을 발급받는 곳이라 제외.
- Django admin(``/admin/``): 세션 인증이라 JWT 게이트가 의미 없다. 세션 MFA 는 Phase 3a
  (`django-otp` 로그인 폼)에서 별도로 붙인다.
- 미인증 요청: 아무 판단도 하지 않는다. 어차피 뷰의 ``IsAdminUser`` 가 401 을 낸다 —
  **이 게이트는 권한을 넓히지 않고 좁히기만 한다.**
"""

from __future__ import annotations

from django.conf import settings

from apps.admin_api.auth.tokens import is_admin_token
from apps.admin_api.roles import ADMIN_API_PREFIX, is_restricted

# 게이트 제외 프리픽스 — 로그인·등록 엔드포인트 자신.
ADMIN_AUTH_PREFIX = f"{ADMIN_API_PREFIX}auth/"

# 403 사유 코드 (프론트가 `section_forbidden`·401 세션만료와 다른 화면을 띄운다).
ADMIN_TOKEN_REQUIRED_CODE = "admin_token_required"
ADMIN_TOKEN_REQUIRED_MESSAGE = "관리자 인증이 필요합니다. 다시 로그인해 주세요."


def requires_admin_token(path: str, role: str) -> bool:
    """이 요청에 어드민 토큰을 요구해야 하는가 (경로·역할·플래그만으로 판정)."""
    if not settings.ADMIN_MFA_ENFORCED:
        return False
    if not path.startswith(ADMIN_API_PREFIX) or path.startswith(ADMIN_AUTH_PREFIX):
        return False
    return not is_restricted(role)


def has_admin_token(validated_token) -> bool:
    """검증된 JWT 가 어드민 토큰인가. 토큰이 없으면(세션·미인증) False."""
    payload = getattr(validated_token, "payload", None)
    return is_admin_token(payload) if isinstance(payload, dict) else False
