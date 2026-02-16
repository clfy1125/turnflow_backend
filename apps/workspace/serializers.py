"""
Serializers for Workspace and Membership
"""

from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import secrets

from .models import Workspace, Membership, WorkspaceInvitation

User = get_user_model()


class WorkspaceSerializer(serializers.ModelSerializer):
    """Serializer for Workspace"""

    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "owner",
            "owner_email",
            "member_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "owner", "created_at", "updated_at"]

    def get_member_count(self, obj):
        """Get total member count"""
        return obj.memberships.count()


class WorkspaceCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating Workspace"""

    class Meta:
        model = Workspace
        fields = ["name", "description"]

    def create(self, validated_data):
        """Create workspace and add owner as member with owner role"""
        user = self.context["request"].user
        workspace = Workspace.objects.create(owner=user, **validated_data)

        # Create owner membership
        Membership.objects.create(user=user, workspace=workspace, role=Membership.Role.OWNER)

        return workspace


class MembershipUserSerializer(serializers.ModelSerializer):
    """Serializer for User in Membership"""

    class Meta:
        model = User
        fields = ["id", "email", "full_name"]
        read_only_fields = ["id", "email", "full_name"]


class MembershipSerializer(serializers.ModelSerializer):
    """Serializer for Membership"""

    user = MembershipUserSerializer(read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)

    class Meta:
        model = Membership
        fields = [
            "id",
            "user",
            "workspace",
            "workspace_name",
            "role",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "user", "workspace", "created_at", "updated_at"]


class MembershipUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating Membership role"""

    class Meta:
        model = Membership
        fields = ["role"]

    def validate_role(self, value):
        """Prevent changing owner role"""
        if self.instance and self.instance.role == Membership.Role.OWNER:
            raise serializers.ValidationError("Cannot change owner role")
        return value


class WorkspaceInvitationSerializer(serializers.ModelSerializer):
    """Serializer for WorkspaceInvitation"""

    invited_by_email = serializers.EmailField(source="invited_by.email", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)

    class Meta:
        model = WorkspaceInvitation
        fields = [
            "id",
            "workspace",
            "workspace_name",
            "email",
            "role",
            "token",
            "invited_by",
            "invited_by_email",
            "status",
            "expires_at",
            "created_at",
            "accepted_at",
        ]
        read_only_fields = [
            "id",
            "token",
            "invited_by",
            "status",
            "expires_at",
            "created_at",
            "accepted_at",
        ]


class WorkspaceInvitationCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating WorkspaceInvitation"""

    class Meta:
        model = WorkspaceInvitation
        fields = ["email", "role"]

    def validate_email(self, value):
        """Validate email is not already a member"""
        workspace = self.context["workspace"]
        if Membership.objects.filter(workspace=workspace, user__email=value).exists():
            raise serializers.ValidationError("User is already a member of this workspace")
        return value

    def create(self, validated_data):
        """Create invitation with token and expiration"""
        workspace = self.context["workspace"]
        invited_by = self.context["request"].user

        # Generate unique token
        token = secrets.token_urlsafe(32)

        # Set expiration to 7 days
        expires_at = timezone.now() + timedelta(days=7)

        invitation = WorkspaceInvitation.objects.create(
            workspace=workspace,
            invited_by=invited_by,
            token=token,
            expires_at=expires_at,
            **validated_data,
        )

        return invitation
