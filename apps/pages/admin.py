from django.contrib import admin

from .models import Block, Page


class BlockInline(admin.TabularInline):
    model = Block
    extra = 0
    fields = ["type", "order", "is_enabled", "data"]
    ordering = ["order"]


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ["slug", "user", "title", "is_public", "created_at"]
    list_filter = ["is_public"]
    search_fields = ["slug", "user__username", "title"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [BlockInline]


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "type", "order", "is_enabled", "updated_at"]
    list_filter = ["type", "is_enabled"]
    search_fields = ["page__slug", "type"]
    ordering = ["page", "order"]
    readonly_fields = ["created_at", "updated_at"]
