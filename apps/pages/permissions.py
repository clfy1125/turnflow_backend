from rest_framework.permissions import BasePermission, IsAuthenticated, SAFE_METHODS


class IsPageOwner(BasePermission):
    """Page 또는 Block의 소유자인지 확인."""

    def has_object_permission(self, request, view, obj):
        from .models import Page, Block

        if isinstance(obj, Page):
            return obj.user == request.user
        if isinstance(obj, Block):
            return obj.page.user == request.user
        return False


class IsPublicPageOrOwner(BasePermission):
    """
    - is_public=True → 누구나 SAFE_METHODS 허용
    - is_public=False → 소유자만 허용
    """

    def has_object_permission(self, request, view, obj):
        from .models import Page

        if isinstance(obj, Page):
            if obj.is_public and request.method in SAFE_METHODS:
                return True
            return request.user.is_authenticated and obj.user == request.user
        return False
