from django.contrib import admin

from .models import IGAccountInsight, IGMedia, IGMediaInsight, MediaSyncJob


@admin.register(IGMedia)
class IGMediaAdmin(admin.ModelAdmin):
    list_display = (
        "external_media_id",
        "account",
        "media_product_type",
        "media_type",
        "published_at",
        "insights_last_synced_at",
    )
    list_filter = ("media_product_type", "media_type", "account")
    search_fields = ("external_media_id", "caption", "account__username")
    readonly_fields = ("raw_metadata", "created_at", "updated_at")
    date_hierarchy = "published_at"


@admin.register(IGMediaInsight)
class IGMediaInsightAdmin(admin.ModelAdmin):
    list_display = (
        "media",
        "reach",
        "likes",
        "comments",
        "saved",
        "engagement_rate",
        "viral_score",
        "fetched_at",
    )
    search_fields = ("media__external_media_id",)
    readonly_fields = ("raw_payload", "fetched_at")


@admin.register(IGAccountInsight)
class IGAccountInsightAdmin(admin.ModelAdmin):
    list_display = (
        "account",
        "period_days",
        "follower_reach",
        "non_follower_reach",
        "unknown_reach",
        "fetched_at",
    )
    list_filter = ("period_days",)
    search_fields = ("account__username",)
    readonly_fields = ("raw_payload", "fetched_at")


@admin.register(MediaSyncJob)
class MediaSyncJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "scope",
        "status",
        "processed",
        "total",
        "error_count",
        "created_at",
    )
    list_filter = ("scope", "status")
    readonly_fields = ("created_at", "started_at", "finished_at")
