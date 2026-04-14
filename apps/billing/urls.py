"""
Billing URL configuration
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import BillingViewSet
from .subscription_views import (
    SubscriptionPlanListView,
    MySubscriptionView,
    ChangeSubscriptionView,
    CancelSubscriptionView,
)
from .payment_views import (
    PaymentConfirmView,
    PaymentWebhookView,
    PaymentHistoryView,
)

app_name = "billing"

router = DefaultRouter()
router.register(r"billing", BillingViewSet, basename="billing")

urlpatterns = router.urls + [
    # 구독 관리
    path("billing/plans/", SubscriptionPlanListView.as_view(), name="subscription-plans"),
    path("billing/my-subscription/", MySubscriptionView.as_view(), name="my-subscription"),
    path("billing/change-plan/", ChangeSubscriptionView.as_view(), name="change-plan"),
    path("billing/cancel/", CancelSubscriptionView.as_view(), name="cancel-subscription"),
    # 결제 (토스페이먼츠)
    path("billing/payments/confirm/", PaymentConfirmView.as_view(), name="payment-confirm"),
    path("billing/payments/webhook/", PaymentWebhookView.as_view(), name="payment-webhook"),
    path("billing/payments/history/", PaymentHistoryView.as_view(), name="payment-history"),
]
