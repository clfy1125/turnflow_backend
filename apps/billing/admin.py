from django.contrib import admin
from .models import (
    UsageCounter, SubscriptionPlan, UserSubscription, PaymentHistory,
    AiTokenBalance, AiTokenLedger, PayAppWebhookLog,
    ReferralCode, ReferralRedemption,
)


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ["workspace", "year", "month", "comments_collected", "dm_sent", "updated_at"]
    list_filter = ["year", "month"]
    search_fields = ["workspace__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["-year", "-month"]


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ["display_name", "name", "monthly_price", "is_active", "sort_order"]
    list_filter = ["is_active"]
    search_fields = ["name", "display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["sort_order"]


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["user", "plan", "status", "payapp_rebill_no", "current_period_start", "current_period_end"]
    list_filter = ["status", "plan"]
    search_fields = ["user__email", "payapp_rebill_no"]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["user", "plan"]


@admin.register(PaymentHistory)
class PaymentHistoryAdmin(admin.ModelAdmin):
    list_display = ["user", "amount", "status", "payment_method", "payapp_mul_no", "paid_at", "created_at"]
    list_filter = ["status", "payment_method"]
    search_fields = ["user__email", "payapp_mul_no", "payapp_rebill_no"]
    readonly_fields = ["id", "created_at"]
    raw_id_fields = ["user", "subscription"]


@admin.register(AiTokenBalance)
class AiTokenBalanceAdmin(admin.ModelAdmin):
    list_display = ["user", "balance", "total_used", "updated_at"]
    search_fields = ["user__email"]
    readonly_fields = ["id", "updated_at"]
    raw_id_fields = ["user"]


@admin.register(AiTokenLedger)
class AiTokenLedgerAdmin(admin.ModelAdmin):
    list_display = ["user", "amount", "balance_after", "description", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["user__email", "description"]
    readonly_fields = ["id", "created_at"]
    raw_id_fields = ["user"]


@admin.register(PayAppWebhookLog)
class PayAppWebhookLogAdmin(admin.ModelAdmin):
    list_display = ["webhook_type", "mul_no", "rebill_no", "pay_state", "processed", "created_at"]
    list_filter = ["webhook_type", "processed", "pay_state"]
    search_fields = ["mul_no", "rebill_no"]
    readonly_fields = ["id", "raw_data", "created_at"]
    ordering = ["-created_at"]


@admin.register(ReferralCode)
class ReferralCodeAdmin(admin.ModelAdmin):
    list_display = [
        "code", "target_plan", "trial_days", "is_active",
        "current_uses", "max_uses", "valid_from", "valid_until", "created_at",
    ]
    list_filter = ["is_active", "target_plan"]
    search_fields = ["code", "description"]
    readonly_fields = ["id", "current_uses", "created_at", "updated_at"]
    raw_id_fields = ["target_plan"]
    ordering = ["-created_at"]


@admin.register(ReferralRedemption)
class ReferralRedemptionAdmin(admin.ModelAdmin):
    list_display = [
        "user", "referral_code", "trial_started_at", "trial_ends_at",
        "converted_to_paid", "converted_at",
    ]
    list_filter = ["converted_to_paid", "referral_code"]
    search_fields = ["user__email", "referral_code__code"]
    readonly_fields = ["id", "created_at"]
    raw_id_fields = ["user", "referral_code"]
    ordering = ["-created_at"]
