"""
analytics Django admin — 디버깅/검수용 읽기 전용 뷰.
집계는 마케팅 대시보드(별도 서브시스템)가 담당하므로 여기서는 원시 행 열람만 제공.
"""

from django.contrib import admin

from .models import LandingVisit, SignupAttribution


@admin.register(LandingVisit)
class LandingVisitAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "visitor_id",
        "channel",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "landing_path",
        "country",
        "ua_class",
        "created_at",
    )
    list_filter = ("channel", "ua_class", "country", "created_at")
    search_fields = ("visitor_id", "utm_source", "utm_campaign", "referrer")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SignupAttribution)
class SignupAttributionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "channel",
        "signup_kind",
        "utm_source",
        "utm_campaign",
        "visitor_id",
        "created_at",
    )
    list_filter = ("channel", "signup_kind", "created_at")
    search_fields = ("user__email", "visitor_id", "utm_source", "utm_campaign")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
