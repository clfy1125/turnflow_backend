from django.contrib import admin
from .models import UsageCounter, SubscriptionPlan, UserSubscription, PaymentHistory, AiTokenBalance, AiTokenLedger


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ["workspace", "year", "month", "comments_collected", "dm_sent", "updated_at"]
    list_filter = ["year", "month"]
    search_fields = ["workspace__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["-year", "-month"]


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ["display_name", "name", "monthly_price", "yearly_price", "is_active", "sort_order"]
    list_filter = ["is_active"]
    search_fields = ["name", "display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["sort_order"]


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["user", "plan", "status", "billing_cycle", "current_period_start", "current_period_end"]
    list_filter = ["status", "billing_cycle", "plan"]
    search_fields = ["user__email"]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["user", "plan"]


@admin.register(PaymentHistory)
class PaymentHistoryAdmin(admin.ModelAdmin):
    list_display = ["user", "amount", "status", "payment_method", "paid_at", "created_at"]
    list_filter = ["status", "payment_method"]
    search_fields = ["user__email", "toss_order_id"]
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
