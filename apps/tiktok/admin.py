"""Django admin for TikTok integration models (Business API)."""

from django.contrib import admin

from .models import (
    TikTokAccountConnection,
    TikTokBlockedWord,
    TikTokCommentLog,
    TikTokOAuthState,
    TikTokSpamFilterConfig,
)


@admin.register(TikTokAccountConnection)
class TikTokAccountConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "advertiser_name",
        "external_account_id",
        "workspace",
        "bc_id",
        "status",
        "token_expires_at",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "advertiser_name", "external_account_id", "bc_id", "workspace__name",
    )
    readonly_fields = ("created_at", "updated_at", "last_verified_at")


@admin.register(TikTokOAuthState)
class TikTokOAuthStateAdmin(admin.ModelAdmin):
    list_display = ("state", "workspace", "expires_at", "created_at")
    search_fields = ("state",)


@admin.register(TikTokSpamFilterConfig)
class TikTokSpamFilterConfigAdmin(admin.ModelAdmin):
    list_display = (
        "connection", "status", "default_action", "score_threshold",
        "total_spam_detected", "total_hidden", "total_deleted", "updated_at",
    )
    list_filter = ("status", "default_action")
    search_fields = ("connection__advertiser_name", "connection__external_account_id")


@admin.register(TikTokCommentLog)
class TikTokCommentLogAdmin(admin.ModelAdmin):
    list_display = (
        "external_comment_id", "commenter_username", "connection", "status",
        "score", "ad_id", "moderated_at", "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "external_comment_id", "commenter_username", "ad_id",
        "connection__advertiser_name",
    )
    readonly_fields = ("created_at", "updated_at", "moderated_at")


@admin.register(TikTokBlockedWord)
class TikTokBlockedWordAdmin(admin.ModelAdmin):
    list_display = (
        "word", "connection", "external_id", "is_synced", "last_synced_at",
        "updated_at",
    )
    list_filter = ("is_synced",)
    search_fields = ("word", "external_id", "connection__advertiser_name")
