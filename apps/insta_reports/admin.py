"""어드민 — 리포트 운영 조회(원가·실패사유·게이트 로그)."""

from django.contrib import admin

from .models import IGVideoFeature, InstagramReport, ReportAiCache


@admin.register(InstagramReport)
class InstagramReportAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "ig_username",
        "status",
        "stage",
        "progress",
        "videos_analyzed",
        "comments_analyzed",
        "cost_usd",
        "elapsed_seconds",
        "error_code",
        "quota_consumed",
    )
    list_filter = ("status", "error_code", "quota_consumed", "created_at")
    search_fields = ("ig_username", "ig_name", "id", "celery_task_id")
    readonly_fields = tuple(f.name for f in InstagramReport._meta.fields) + (
        "gate_meta",
        "tokens_json",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        # 생성은 API/태스크만 (어드민에서 만들면 쿼터·게이트를 건너뛴다).
        return False


@admin.register(IGVideoFeature)
class IGVideoFeatureAdmin(admin.ModelAdmin):
    list_display = (
        "shortcode",
        "external_account_id",
        "schema_version",
        "model_name",
        "created_at",
    )
    list_filter = ("schema_version", "model_name")
    search_fields = ("shortcode", "external_account_id")
    ordering = ("-created_at",)


@admin.register(ReportAiCache)
class ReportAiCacheAdmin(admin.ModelAdmin):
    list_display = ("kind", "cache_key", "version", "created_at", "updated_at")
    list_filter = ("kind", "version")
    search_fields = ("cache_key",)
    ordering = ("-created_at",)
