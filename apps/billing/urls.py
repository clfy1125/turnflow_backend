"""
Billing URL configuration
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from .payment_views import PaymentHistoryView, RefundEligibilityView, RefundPaymentView
from .referral_views import (
    MyReferralRedemptionView,
    RedeemReferralCodeView,
    ValidateReferralCodeView,
)
from .subscription_views import (
    CancelSubscriptionView,
    ChangePlanPreviewView,
    ChangeSubscriptionView,
    IGAccountActivationView,
    MySubscriptionView,
    PageActivationView,
    PauseSubscriptionView,
    ResumeSubscriptionView,
    RetentionOfferApplyView,
    SubscriptionPlanListView,
)
from .toss_views import (
    ExtraAccountsPreviewView,
    ExtraAccountsView,
    TossConfirmView,
    TossDevIssueView,
    TossPrepareView,
    TossWebhookView,
)
from .views import BillingViewSet

app_name = "billing"

router = DefaultRouter()
router.register(r"billing", BillingViewSet, basename="billing")

urlpatterns = router.urls + [
    # 구독 관리
    path("billing/plans/", SubscriptionPlanListView.as_view(), name="subscription-plans"),
    path("billing/my-subscription/", MySubscriptionView.as_view(), name="my-subscription"),
    path("billing/change-plan/", ChangeSubscriptionView.as_view(), name="change-plan"),
    path(
        "billing/change-plan/preview/",
        ChangePlanPreviewView.as_view(),
        name="change-plan-preview",
    ),
    path("billing/cancel/", CancelSubscriptionView.as_view(), name="cancel-subscription"),
    path("billing/resume/", ResumeSubscriptionView.as_view(), name="resume-subscription"),
    # 리텐션(해지 방어)
    path("billing/pause/", PauseSubscriptionView.as_view(), name="pause-subscription"),
    path(
        "billing/retention-offer/apply/",
        RetentionOfferApplyView.as_view(),
        name="retention-offer-apply",
    ),
    path("billing/page-activation/", PageActivationView.as_view(), name="page-activation"),
    path(
        "billing/ig-account-activation/",
        IGAccountActivationView.as_view(),
        name="ig-account-activation",
    ),
    # 토스페이먼츠 빌링
    path("billing/toss/prepare/", TossPrepareView.as_view(), name="toss-prepare"),
    path("billing/toss/confirm/", TossConfirmView.as_view(), name="toss-confirm"),
    path("billing/toss/webhook/", TossWebhookView.as_view(), name="toss-webhook"),
    path(
        "billing/toss/dev/issue-billing-key/",
        TossDevIssueView.as_view(),
        name="toss-dev-issue",
    ),
    path("billing/extra-accounts/", ExtraAccountsView.as_view(), name="extra-accounts"),
    path(
        "billing/extra-accounts/preview/",
        ExtraAccountsPreviewView.as_view(),
        name="extra-accounts-preview",
    ),
    # 결제 내역 / 환불
    path("billing/payments/history/", PaymentHistoryView.as_view(), name="payment-history"),
    path("billing/refund-eligibility/", RefundEligibilityView.as_view(), name="refund-eligibility"),
    path(
        "billing/payments/<uuid:payment_id>/refund/",
        RefundPaymentView.as_view(),
        name="payment-refund",
    ),
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
