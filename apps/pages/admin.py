from django.contrib import admin

from .models import (
    Block,
    BlockClick,
    ContactInquiry,
    Page,
    PageMedia,
    PageSubscription,
    PageView,
)


class BlockInline(admin.TabularInline):
    model = Block
    extra = 0
    fields = ["type", "order", "is_enabled", "data"]
    ordering = ["order"]


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ["slug", "user", "title", "is_public", "created_at"]
    list_filter = ["is_public"]
    search_fields = ["slug", "user__email", "title"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [BlockInline]


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "type", "order", "is_enabled", "updated_at"]
    list_filter = ["type", "is_enabled"]
    search_fields = ["page__slug", "type"]
    ordering = ["page", "order"]
    readonly_fields = ["created_at", "updated_at"]


# ─── 통계 모델 ─────────────────────────────────────────────────

@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "viewed_at", "referer", "country"]
    list_filter = ["country", "viewed_at"]
    search_fields = ["page__slug", "referer"]
    readonly_fields = ["viewed_at"]
    date_hierarchy = "viewed_at"
    ordering = ["-viewed_at"]


@admin.register(BlockClick)
class BlockClickAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "block", "link_id", "clicked_at", "referer", "country"]
    list_filter = ["country", "clicked_at"]
    search_fields = ["page__slug", "block__type", "link_id"]
    readonly_fields = ["clicked_at"]
    date_hierarchy = "clicked_at"
    ordering = ["-clicked_at"]


# ─── 문의 / 구독 / 미디어 ──────────────────────────────────────

@admin.register(ContactInquiry)
class ContactInquiryAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "name", "category", "subject", "created_at"]
    list_filter = ["category", "created_at"]
    search_fields = ["page__slug", "name", "email", "subject"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]


@admin.register(PageSubscription)
class PageSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "name", "email", "category", "created_at"]
    list_filter = ["category", "created_at"]
    search_fields = ["page__slug", "name", "email"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]


@admin.register(PageMedia)
class PageMediaAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "original_name", "mime_type", "size", "created_at"]
    list_filter = ["mime_type", "created_at"]
    search_fields = ["page__slug", "original_name"]
    readonly_fields = ["created_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]
