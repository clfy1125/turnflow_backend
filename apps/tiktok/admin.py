"""Django admin for TikTok integration models."""

from django.contrib import admin

from .models import (
    TikTokAccountConnection,
    TikTokCommentLog,
    TikTokOAuthState,
    TikTokSpamFilterConfig,
    TikTokVideoPost,
)


@admin.register(TikTokAccountConnection)
class TikTokAccountConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "external_account_id",
        "workspace",
        "status",
        "is_audited",
        "token_expires_at",
        "created_at",
    )
    list_filter = ("status", "is_audited")
    search_fields = ("username", "external_account_id", "workspace__name")
    readonly_fields = ("created_at", "updated_at", "last_verified_at")


@admin.register(TikTokOAuthState)
class TikTokOAuthStateAdmin(admin.ModelAdmin):
    list_display = ("state", "workspace", "expires_at", "created_at")
    search_fields = ("state",)


@admin.register(TikTokVideoPost)
class TikTokVideoPostAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "connection",
        "status",
        "source_type",
        "effective_privacy",
        "tiktok_video_id",
        "created_at",
    )
    list_filter = ("status", "source_type", "effective_privacy")
    search_fields = ("publish_id", "tiktok_video_id", "connection__username")
    readonly_fields = ("created_at", "updated_at", "initiated_at", "uploaded_at", "published_at")


@admin.register(TikTokSpamFilterConfig)
class TikTokSpamFilterConfigAdmin(admin.ModelAdmin):
    list_display = (
        "connection", "status", "default_action", "score_threshold",
        "total_spam_detected", "total_hidden", "updated_at",
    )
    list_filter = ("status", "default_action")
    search_fields = ("connection__username",)


@admin.register(TikTokCommentLog)
class TikTokCommentLogAdmin(admin.ModelAdmin):
    list_display = (
        "external_comment_id", "commenter_username", "connection", "status",
        "score", "moderated_at", "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "external_comment_id", "commenter_username", "external_video_id",
    )
    readonly_fields = ("created_at", "updated_at", "moderated_at")
