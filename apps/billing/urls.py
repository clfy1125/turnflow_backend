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
    ResumeSubscriptionView,
    PageActivationView,
)
from .payment_views import (
    PayAppFeedbackView,
    PayAppFailView,
    PaymentHistoryView,
    RefundEligibilityView,
    RefundPaymentView,
)
from .referral_views import (
    MyReferralRedemptionView,
    RedeemReferralCodeView,
    ValidateReferralCodeView,
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
    path("billing/resume/", ResumeSubscriptionView.as_view(), name="resume-subscription"),
    path("billing/page-activation/", PageActivationView.as_view(), name="page-activation"),
    # PayApp 웹훅 (PG사 → 백엔드)
    path("billing/payapp/feedback/", PayAppFeedbackView.as_view(), name="payapp-feedback"),
    path("billing/payapp/fail/", PayAppFailView.as_view(), name="payapp-fail"),
    # 결제 내역 / 환불
    path("billing/payments/history/", PaymentHistoryView.as_view(), name="payment-history"),
    path("billing/refund-eligibility/", RefundEligibilityView.as_view(), name="refund-eligibility"),
    path("billing/payments/<uuid:payment_id>/refund/", RefundPaymentView.as_view(), name="payment-refund"),
    # 레퍼럴 (첫달 무료 트라이얼)
    path(
        "billing/referral/validate/",
        ValidateReferralCodeView.as_view(),
        name="referral-validate",
    ),
    path(
        "billing/referral/redeem/",
        RedeemReferralCodeView.as_view(),
        name="referral-redeem",
    ),
    path(
        "billing/referral/my-status/",
        MyReferralRedemptionView.as_view(),
        name="referral-my-status",
    ),
]
