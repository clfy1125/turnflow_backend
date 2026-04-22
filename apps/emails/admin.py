from django.contrib import admin

from .models import EmailLog, EmailTemplate, EmailToken, OnboardingSchedule


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ("key", "subject", "is_active", "updated_at", "updated_by")
    list_filter = ("is_active", "key")
    search_fields = ("key", "subject")
    readonly_fields = ("created_at", "updated_at", "available_variables")


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ("template_key", "to_email", "status", "attempts", "created_at", "sent_at")
    list_filter = ("status", "template_key", "created_at")
    search_fields = ("to_email", "subject", "provider_message_id")
    readonly_fields = tuple(f.name for f in EmailLog._meta.fields)


@admin.register(EmailToken)
class EmailTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "purpose", "expires_at", "used_at", "created_at")
    list_filter = ("purpose",)
    search_fields = ("user__email",)


@admin.register(OnboardingSchedule)
class OnboardingScheduleAdmin(admin.ModelAdmin):
    list_display = ("user", "template_key", "scheduled_for", "sent_at", "cancelled_at")
    list_filter = ("template_key",)
    search_fields = ("user__email",)
