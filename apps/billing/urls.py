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
    PayAppFeedbackView,
    PayAppFailView,
    PaymentHistoryView,
    RefundPaymentView,
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
    # PayApp 웹훅 (PG사 → 백엔드)
    path("billing/payapp/feedback/", PayAppFeedbackView.as_view(), name="payapp-feedback"),
    path("billing/payapp/fail/", PayAppFailView.as_view(), name="payapp-fail"),
    # 결제 내역 / 환불
    path("billing/payments/history/", PaymentHistoryView.as_view(), name="payment-history"),
    path("billing/payments/<uuid:payment_id>/refund/", RefundPaymentView.as_view(), name="payment-refund"),
]
