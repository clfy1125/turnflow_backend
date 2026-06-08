"""apps/admin_api/admin.py — Django admin 에서 감사 로그 열람(읽기 전용)."""

from __future__ import annotations

from django.contrib import admin

from .models import AdminActionLog


@admin.register(AdminActionLog)
class AdminActionLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "actor",
        "action",
        "target_type",
        "target_id",
        "target_repr",
    )
    list_filter = ("action", "target_type", "created_at")
    search_fields = ("target_id", "target_repr", "request_id", "actor__email")
    readonly_fields = (
        "actor",
        "action",
        "target_type",
        "target_id",
        "target_repr",
        "changes",
        "request_id",
        "ip",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
