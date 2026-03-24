from django.contrib import admin
from .models import Workspace, Membership, WorkspaceInvitation


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ["user", "role", "created_at"]
    readonly_fields = ["created_at"]


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "owner", "plan", "member_count", "created_at"]
    list_filter = ["plan", "created_at"]
    search_fields = ["name", "slug", "owner__email"]
    readonly_fields = ["id", "created_at", "updated_at"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MembershipInline]

    def member_count(self, obj):
        return obj.memberships.count()
    member_count.short_description = "멤버 수"


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "workspace", "role", "created_at"]
    list_filter = ["role", "created_at"]
    search_fields = ["user__email", "workspace__name"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(WorkspaceInvitation)
class WorkspaceInvitationAdmin(admin.ModelAdmin):
    list_display = ["email", "workspace", "role", "status", "invited_by", "expires_at", "created_at"]
    list_filter = ["status", "role", "created_at"]
    search_fields = ["email", "workspace__name"]
    readonly_fields = ["id", "token", "created_at", "accepted_at"]
