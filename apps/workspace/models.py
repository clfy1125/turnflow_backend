"""
Workspace and Membership models for multi-tenancy
"""

from django.db import models
from django.contrib.auth import get_user_model
from django.utils.text import slugify
import uuid

User = get_user_model()


class Workspace(models.Model):
    """
    Workspace (Organization/Tenant) model for multi-tenancy
    Each workspace represents a separate customer/organization
    """

    class Meta:
        db_table = "workspaces"
        verbose_name = "Workspace"
        verbose_name_plural = "Workspaces"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["owner"]),
            models.Index(fields=["plan"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name="Workspace Name")
    slug = models.SlugField(max_length=255, unique=True, verbose_name="Workspace Slug")
    description = models.TextField(blank=True, verbose_name="Description")
    owner = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="owned_workspaces",
        verbose_name="Owner",
    )

    # Billing plan
    plan = models.CharField(
        max_length=20,
        choices=[
            ("starter", "Starter"),
            ("pro", "Pro"),
            ("enterprise", "Enterprise"),
        ],
        default="starter",
        verbose_name="Subscription Plan",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created At")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Updated At")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """Auto-generate slug from name if not provided"""
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            while Workspace.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)


class Membership(models.Model):
    """
    Membership model representing user-workspace relationship with roles
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    class Meta:
        db_table = "memberships"
        verbose_name = "Membership"
        verbose_name_plural = "Memberships"
        unique_together = [["user", "workspace"]]
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "workspace"]),
            models.Index(fields=["workspace", "role"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships", verbose_name="User"
    )
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Workspace",
    )
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.MEMBER, verbose_name="Role"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created At")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Updated At")

    def __str__(self):
        return f"{self.user.email} - {self.workspace.name} ({self.role})"


class WorkspaceInvitation(models.Model):
    """
    Workspace invitation model for inviting users to workspaces
    Uses token-based invitation (no email sending in MVP)
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"

    class Meta:
        db_table = "workspace_invitations"
        verbose_name = "Workspace Invitation"
        verbose_name_plural = "Workspace Invitations"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["workspace", "status"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="invitations",
        verbose_name="Workspace",
    )
    email = models.EmailField(verbose_name="Invitee Email")
    role = models.CharField(
        max_length=20,
        choices=Membership.Role.choices,
        default=Membership.Role.MEMBER,
        verbose_name="Role",
    )
    token = models.CharField(max_length=64, unique=True, verbose_name="Invitation Token")
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
        verbose_name="Invited By",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, verbose_name="Status"
    )
    expires_at = models.DateTimeField(verbose_name="Expires At")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created At")
    accepted_at = models.DateTimeField(null=True, blank=True, verbose_name="Accepted At")

    def __str__(self):
        return f"{self.email} -> {self.workspace.name} ({self.status})"
