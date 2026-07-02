from django.contrib import admin

from .models import (
    AiTokenBalance,
    AiTokenLedger,
    PaymentHistory,
    ReferralCode,
    ReferralRedemption,
    SubscriptionPlan,
    TossWebhookLog,
    UsageCounter,
    UserSubscription,
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
    list_display = [
        "display_name",
        "name",
        "monthly_price",
        "list_price",
        "is_active",
        "sort_order",
    ]
    list_filter = ["is_active"]
    search_fields = ["name", "display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["sort_order"]


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "plan",
        "status",
        "card_company",
        "monthly_amount_snapshot",
        "extra_ig_accounts",
        "current_period_start",
        "current_period_end",
        "next_billing_retry_at",
    ]
    list_filter = ["status", "plan"]
    search_fields = ["user__email", "toss_customer_key"]
    readonly_fields = [
        "id",
        "toss_customer_key",
        "toss_billing_key_hash",
        "_encrypted_toss_billing_key",
        "created_at",
        "updated_at",
    ]
    raw_id_fields = ["user", "plan", "pending_plan"]


@admin.register(PaymentHistory)
class PaymentHistoryAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "amount",
        "status",
        "payment_method",
        "toss_order_id",
        "paid_at",
        "created_at",
    ]
    list_filter = ["status", "payment_method"]
    search_fields = ["user__email", "toss_order_id", "toss_payment_key"]
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


@admin.register(TossWebhookLog)
class TossWebhookLogAdmin(admin.ModelAdmin):
    list_display = ["event_type", "payment_key", "order_id", "processed", "created_at"]
    list_filter = ["event_type", "processed"]
    search_fields = ["payment_key", "order_id", "dedup_key"]
    readonly_fields = ["id", "dedup_key", "raw_data", "created_at"]
    ordering = ["-created_at"]


@admin.register(ReferralCode)
class ReferralCodeAdmin(admin.ModelAdmin):
    list_display = [
        "code",
        "target_plan",
        "trial_days",
        "is_active",
        "current_uses",
        "max_uses",
        "valid_from",
        "valid_until",
        "created_at",
    ]
    list_filter = ["is_active", "target_plan"]
    search_fields = ["code", "description"]
    readonly_fields = ["id", "current_uses", "created_at", "updated_at"]
    raw_id_fields = ["target_plan"]
    ordering = ["-created_at"]


@admin.register(ReferralRedemption)
class ReferralRedemptionAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "referral_code",
        "trial_started_at",
        "trial_ends_at",
        "converted_to_paid",
        "converted_at",
    ]
    list_filter = ["converted_to_paid", "referral_code"]
    search_fields = ["user__email", "referral_code__code"]
    readonly_fields = ["id", "created_at"]
    raw_id_fields = ["user", "referral_code"]
    ordering = ["-created_at"]
