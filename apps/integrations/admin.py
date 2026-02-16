from django.contrib import admin
from .models import IGAccountConnection, AutoDMCampaign, SentDMLog


@admin.register(IGAccountConnection)
class IGAccountConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "workspace",
        "external_account_id",
        "username",
        "account_type",
        "status",
        "token_expires_at",
        "last_verified_at",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "workspace", "account_type")
    search_fields = ("external_account_id", "username")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AutoDMCampaign)
class AutoDMCampaignAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "ig_connection",
        "media_id",
        "status",
        "total_sent",
        "total_failed",
        "max_sends_per_hour",
        "created_at",
        "started_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("name", "media_id", "ig_connection__username")
    readonly_fields = (
        "id",
        "total_sent",
        "total_failed",
        "created_at",
        "updated_at",
        "started_at",
        "ended_at",
    )

    fieldsets = (
        ("기본 정보", {"fields": ("name", "description", "ig_connection")}),
        ("타겟 게시물", {"fields": ("media_id", "media_url")}),
        ("메시지 설정", {"fields": ("message_template", "max_sends_per_hour")}),
        ("상태", {"fields": ("status",)}),
        ("통계", {"fields": ("total_sent", "total_failed")}),
        ("시간 정보", {"fields": ("created_at", "updated_at", "started_at", "ended_at")}),
    )


@admin.register(SentDMLog)
class SentDMLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "campaign",
        "recipient_username",
        "status",
        "created_at",
        "sent_at",
    )
    list_filter = ("status", "created_at", "campaign")
    search_fields = ("recipient_username", "comment_id", "comment_text")
    readonly_fields = (
        "id",
        "campaign",
        "comment_id",
        "comment_text",
        "recipient_user_id",
        "recipient_username",
        "message_sent",
        "status",
        "error_message",
        "error_code",
        "webhook_payload",
        "api_response",
        "created_at",
        "sent_at",
    )

    fieldsets = (
        ("캠페인 정보", {"fields": ("campaign",)}),
        ("댓글 정보", {"fields": ("comment_id", "comment_text")}),
        ("수신자 정보", {"fields": ("recipient_user_id", "recipient_username")}),
        ("메시지", {"fields": ("message_sent",)}),
        ("발송 상태", {"fields": ("status", "error_message", "error_code")}),
        ("원본 데이터", {"fields": ("webhook_payload", "api_response"), "classes": ("collapse",)}),
        ("시간 정보", {"fields": ("created_at", "sent_at")}),
    )

    def has_add_permission(self, request):
        # 로그는 자동으로만 생성되도록
        return False

    def has_change_permission(self, request, obj=None):
        # 로그는 수정 불가
        return False
