from django.contrib import admin

from .models import SmsLog


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "purpose", "masked_phone", "status", "result_code", "user")
    list_filter = ("purpose", "status", "provider")
    search_fields = ("to_phone", "provider_message_id")
    readonly_fields = tuple(f.name for f in SmsLog._meta.fields)
    date_hierarchy = "created_at"

    @admin.display(description="수신번호")
    def masked_phone(self, obj):
        from apps.authentication.phone import mask_phone

        return mask_phone(obj.to_phone)

    def has_add_permission(self, request):
        return False
