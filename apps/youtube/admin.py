"""Django admin for YouTube integration models."""

from django.contrib import admin

from .models import (
    YouTubeAccountConnection,
    YouTubeCommentLog,
    YouTubeOAuthState,
    YouTubeQuotaUsage,
    YouTubeSpamFilterConfig,
    YouTubeVideoPost,
)


@admin.register(YouTubeAccountConnection)
class YouTubeAccountConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "channel_title",
        "external_account_id",
        "workspace",
        "google_email",
        "status",
        "token_expires_at",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("channel_title", "external_account_id", "google_email", "workspace__name")
    readonly_fields = ("created_at", "updated_at", "last_verified_at")


@admin.register(YouTubeOAuthState)
class YouTubeOAuthStateAdmin(admin.ModelAdmin):
    list_display = ("state", "workspace", "expires_at", "created_at")
    search_fields = ("state",)


@admin.register(YouTubeVideoPost)
class YouTubeVideoPostAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "connection",
        "status",
        "privacy_status",
        "youtube_video_id",
        "quota_units_consumed",
        "created_at",
    )
    list_filter = ("status", "privacy_status")
    search_fields = ("title", "youtube_video_id", "connection__channel_title")
    readonly_fields = ("created_at", "updated_at", "started_at", "published_at")


@admin.register(YouTubeQuotaUsage)
class YouTubeQuotaUsageAdmin(admin.ModelAdmin):
    list_display = ("day", "units_used", "updated_at")
    ordering = ("-day",)


@admin.register(YouTubeSpamFilterConfig)
class YouTubeSpamFilterConfigAdmin(admin.ModelAdmin):
    list_display = (
        "connection", "status", "default_action", "score_threshold",
        "ban_authors_on_reject",
        "total_spam_detected", "total_moderated", "updated_at",
    )
    list_filter = ("status", "default_action")
    search_fields = ("connection__channel_title",)


@admin.register(YouTubeCommentLog)
class YouTubeCommentLogAdmin(admin.ModelAdmin):
    list_display = (
        "external_comment_id", "commenter_display_name", "connection", "status",
        "score", "moderated_at", "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "external_comment_id", "commenter_display_name", "external_video_id",
    )
    readonly_fields = ("created_at", "updated_at", "moderated_at")
