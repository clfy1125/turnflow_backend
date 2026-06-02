from django.contrib import admin

from .models import (
    Block,
    BlockClick,
    ContactInquiry,
    Page,
    PageMedia,
    PageSnapshot,
    PageSubscription,
    PageView,
    ReferenceCategory,
)


class BlockInline(admin.TabularInline):
    model = Block
    extra = 0
    fields = ["type", "order", "is_enabled", "data"]
    ordering = ["order"]


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = [
        "slug",
        "user",
        "title",
        "is_public",
        "is_reference",
        "reference_category",
        "reference_snapshot_status",
        "created_at",
    ]
    list_filter = [
        "is_public",
        "is_reference",
        "reference_category",
        "reference_snapshot_status",
    ]
    search_fields = ["slug", "user__email", "title", "reference_title"]
    readonly_fields = [
        "created_at",
        "updated_at",
        "reference_snapshot_updated_at",
        "reference_snapshot_job_id",
        "reference_snapshot_status",
        "reference_snapshot_error",
    ]
    inlines = [BlockInline]
    fieldsets = [
        (
            None,
            {
                "fields": [
                    "user",
                    "slug",
                    "title",
                    "is_public",
                    "is_active",
                    "data",
                    "custom_css",
                ]
            },
        ),
        (
            "외부 임포트",
            {
                "classes": ["collapse"],
                "fields": [
                    "import_source",
                    "import_source_slug",
                    "import_source_url",
                    "imported_at",
                ],
            },
        ),
        (
            "AI 레퍼런스",
            {
                "fields": [
                    "is_reference",
                    "reference_category",
                    "reference_order",
                    "reference_title",
                    "reference_description",
                    "reference_snapshot",
                    "reference_snapshot_status",
                    "reference_snapshot_updated_at",
                    "reference_snapshot_job_id",
                    "reference_snapshot_error",
                ],
            },
        ),
        (
            "메타",
            {
                "classes": ["collapse"],
                "fields": ["created_at", "updated_at"],
            },
        ),
    ]


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


@admin.register(PageSnapshot)
class PageSnapshotAdmin(admin.ModelAdmin):
    list_display = ["id", "page", "reason", "created_by", "created_at"]
    list_filter = ["reason", "created_at"]
    search_fields = ["page__slug", "created_by__email"]
    readonly_fields = ["page", "reason", "snapshot", "created_by", "created_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]


# ─── AI 레퍼런스 카테고리 ─────────────────────────────────────

@admin.register(ReferenceCategory)
class ReferenceCategoryAdmin(admin.ModelAdmin):
    list_display = ["sort_order", "slug", "name", "icon_emoji", "is_active", "updated_at"]
    list_display_links = ["slug", "name"]
    list_filter = ["is_active"]
    list_editable = ["sort_order", "is_active"]
    search_fields = ["slug", "name"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["sort_order", "id"]
