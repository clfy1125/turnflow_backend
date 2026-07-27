"""apps/admin_api/middleware.py — 어드민 역할 게이트 (deny-by-default).

``/api/v1/admin/**`` 전 구간을 **경로 프리픽스 한 곳**에서 막는다 (RBAC-2).
개별 뷰의 ``permission_classes`` 로 막으면 새 어드민 엔드포인트가 추가될 때 누락되어
조용히 열리므로, 제한 역할(:data:`apps.admin_api.roles.ROLE_ALLOWED_ENDPOINTS`)은
**화이트리스트에 없으면 전부 403** 으로 처리한다. 이 프리픽스에는 admin_api 뿐 아니라
``admin/emails/``(apps.emails) · ``admin/pages/...``(apps.pages) 마운트도 함께 들어온다.

인증 해석: 세션 사용자(AuthenticationMiddleware)가 있으면 그대로 쓰고, 없으면 DRF 와
**동일한 JWTAuthentication 클래스**로 Authorization 헤더를 해석한다(토큰 파싱 로직 복제
아님). 해석 실패 시에는 아무 판단도 하지 않고 통과시킨다 — 어차피 뷰의 IsAdminUser 가
401/403 을 낸다. 즉 이 미들웨어는 **권한을 넓히지 않고 좁히기만** 한다.

차단은 감사 로그(AdminActionLog)에 남긴다 — 외주 계정은 외부인이므로 접근 시도 이력이
남아야 한다.
"""

from __future__ import annotations

import logging

from django.http import HttpResponseForbidden, JsonResponse

from apps.admin_api.roles import (
    ADMIN_API_PREFIX,
    DJANGO_ADMIN_PREFIX,
    FORBIDDEN_CODE,
    FORBIDDEN_MESSAGE,
    admin_role,
    allowed_sections,
    is_endpoint_allowed,
    is_restricted,
)

logger = logging.getLogger(__name__)


def _jwt_user(request):
    """Authorization 헤더의 JWT → 사용자 (DRF 와 동일 클래스). 실패 시 None."""
    if not request.META.get("HTTP_AUTHORIZATION"):
        return None
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication

        result = JWTAuthentication().authenticate(request)
    except Exception:  # noqa: BLE001 — 인증 실패는 뷰가 401 로 처리한다
        return None
    return result[0] if result else None


class AdminRoleGuardMiddleware:
    """제한 역할(marketing_viewer)의 어드민 접근을 화이트리스트로 좁힌다."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        is_api = path.startswith(ADMIN_API_PREFIX)
        # Django admin(/admin/)은 API 프리픽스(/api/v1/admin/)와 접두가 겹치지 않는다.
        is_django_admin = path.startswith(DJANGO_ADMIN_PREFIX) and not is_api

        if is_api or is_django_admin:
            user = getattr(request, "user", None)
            if not getattr(user, "is_authenticated", False):
                user = _jwt_user(request)
            role = admin_role(user)
            request.admin_role = role  # 뷰/시리얼라이저 재사용 (역할 조회 1회)
            if is_restricted(role) and (
                is_django_admin or not is_endpoint_allowed(role, request.method, path)
            ):
                return self._deny(request, user, role)

        return self.get_response(request)

    def _deny(self, request, user, role: str):
        self._audit(request, user, role)
        if request.path.startswith(DJANGO_ADMIN_PREFIX) and not request.path.startswith(
            ADMIN_API_PREFIX
        ):
            return HttpResponseForbidden(FORBIDDEN_MESSAGE)
        # 프로젝트 표준 에러 포맷 (apps.core.exceptions.custom_exception_handler 와 동일 형태)
        return JsonResponse(
            {
                "success": False,
                "error": {
                    "code": 403,
                    "message": FORBIDDEN_MESSAGE,
                    "details": {
                        "code": FORBIDDEN_CODE,
                        "admin_role": role,
                        "allowed_sections": allowed_sections(role),
                    },
                },
            },
            status=403,
        )

    @staticmethod
    def _audit(request, user, role: str) -> None:
        """차단된 시도를 감사 로그 + 표준 로그에 남긴다 (실패해도 응답은 그대로 403)."""
        request_id = getattr(request, "id", "") or ""
        logger.warning(
            "[admin-rbac] req=%s actor=%s role=%s DENIED %s %s",
            request_id,
            getattr(user, "email", None),
            role,
            request.method,
            request.path,
        )
        try:
            from apps.admin_api.audit import log_admin_action

            log_admin_action(
                request=request,
                action="admin.access_denied",
                target_type="endpoint",
                target_id=request.path[:64],
                target_repr=f"{request.method} {request.path}"[:255],
                changes={"admin_role": role},
            )
        except Exception:  # noqa: BLE001 — 감사 실패가 차단을 막지 않는다
            logger.exception("[admin-rbac] 차단 감사 로그 실패 req=%s", request_id)
