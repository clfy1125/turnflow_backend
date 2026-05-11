"""TikTok API URL configuration (Business API only)."""

from rest_framework.routers import DefaultRouter

from .views import (
    TikTokAdCommentViewSet,
    TikTokBlockedWordViewSet,
    TikTokIntegrationViewSet,
    TikTokSpamFilterViewSet,
)

app_name = "tiktok"

router = DefaultRouter()
router.register(r"integration", TikTokIntegrationViewSet, basename="integration")
router.register(r"comments", TikTokAdCommentViewSet, basename="comment")
router.register(r"spam-filters", TikTokSpamFilterViewSet, basename="spam-filter")
router.register(r"blocked-words", TikTokBlockedWordViewSet, basename="blocked-word")

urlpatterns = router.urls
