"""
URL configuration for Instagram integrations
"""

from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import InstagramIntegrationViewSet, AutoDMCampaignViewSet, instagram_webhook

app_name = "integrations"

router = DefaultRouter()
router.register(r"instagram", InstagramIntegrationViewSet, basename="instagram")
router.register(r"auto-dm-campaigns", AutoDMCampaignViewSet, basename="auto-dm-campaign")

urlpatterns = [
    path("instagram/webhook/", instagram_webhook, name="instagram-webhook"),
] + router.urls
