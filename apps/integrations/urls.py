"""
URL configuration for Instagram integrations
"""

from django.urls import path
from rest_framework.routers import DefaultRouter
from .verification_views import DMVerificationViewSet
from .views import (
    AutoDMCampaignViewSet,
    InstagramIntegrationViewSet,
    SpamFilterViewSet,
    instagram_webhook,
)

app_name = "integrations"

router = DefaultRouter()
router.register(r"instagram", InstagramIntegrationViewSet, basename="instagram")
router.register(r"auto-dm-campaigns", AutoDMCampaignViewSet, basename="auto-dm-campaign")
router.register(r"spam-filters", SpamFilterViewSet, basename="spam-filter")
router.register(r"dm-verification", DMVerificationViewSet, basename="dm-verification")

urlpatterns = [
    path("instagram/webhook/", instagram_webhook, name="instagram-webhook"),
] + router.urls
