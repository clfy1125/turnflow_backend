"""
Permissions for Workspace and Membership
"""

from rest_framework import permissions
from .models import Membership


class IsWorkspaceMember(permissions.BasePermission):
    """
    Permission to check if user is a member of the workspace
    """

    def has_object_permission(self, request, view, obj):
        """Check if user is a member of the workspace"""
        # obj can be Workspace or any model with workspace attribute
        workspace = obj if hasattr(obj, "memberships") else obj.workspace

        return Membership.objects.filter(user=request.user, workspace=workspace).exists()


class IsWorkspaceAdmin(permissions.BasePermission):
    """
    Permission to check if user is admin or owner of the workspace
    """

    def has_object_permission(self, request, view, obj):
        """Check if user is admin or owner"""
        workspace = obj if hasattr(obj, "memberships") else obj.workspace

        return Membership.objects.filter(
            user=request.user,
            workspace=workspace,
            role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
        ).exists()


class IsWorkspaceOwner(permissions.BasePermission):
    """
    Permission to check if user is the owner of the workspace
    """

    def has_object_permission(self, request, view, obj):
        """Check if user is the owner"""
        workspace = obj if hasattr(obj, "memberships") else obj.workspace

        # Check if user is the workspace owner
        if hasattr(workspace, "owner"):
            return workspace.owner == request.user

        # Fallback: check membership role
        return Membership.objects.filter(
            user=request.user, workspace=workspace, role=Membership.Role.OWNER
        ).exists()
