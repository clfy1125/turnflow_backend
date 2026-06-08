"""apps/admin_api/permissions.py — 어드민 백오피스 권한 클래스.

기본 권한은 DRF 내장 ``IsAdminUser``(``is_staff=True``). 권한 상승처럼 위험한
동작(예: 회원 ``is_staff`` 부여/회수)은 :class:`IsSuperUser` 로 한 번 더 제한한다.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission


class IsSuperUser(BasePermission):
    """``is_superuser=True`` 슈퍼유저만 허용 (권한 상승 동작 보호용)."""

    message = "이 작업은 슈퍼유저만 수행할 수 있습니다."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_staff and user.is_superuser)
