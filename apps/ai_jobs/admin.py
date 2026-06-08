from django.contrib import admin

from .models import AiJob, AiSourceImage


@admin.register(AiJob)
class AiJobAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "page", "job_type", "status", "stage", "model_name", "created_at"]
    list_filter = ["status", "job_type", "model_name"]
    search_fields = ["user__username", "page__slug"]
    readonly_fields = ["id", "created_at", "updated_at", "started_at", "finished_at"]
    ordering = ["-created_at"]


@admin.register(AiSourceImage)
class AiSourceImageAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "user",
        "job",
        "role",
        "usable",
        "labeled",
        "size",
        "created_at",
    ]
    list_filter = ["labeled", "usable", "role"]
    search_fields = ["user__username", "original_name", "job__id"]
    readonly_fields = ["id", "created_at"]
    ordering = ["-created_at"]
