from django.contrib import admin
from .models import UsageCounter


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ["workspace", "year", "month", "comments_collected", "dm_sent", "updated_at"]
    list_filter = ["year", "month"]
    search_fields = ["workspace__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["-year", "-month"]
