"""TikTok API URL configuration."""

from rest_framework.routers import DefaultRouter

from .views import (
    TikTokCommentViewSet,
    TikTokIntegrationViewSet,
    TikTokSpamFilterViewSet,
    TikTokVideoViewSet,
)

app_name = "tiktok"

router = DefaultRouter()
router.register(r"integration", TikTokIntegrationViewSet, basename="integration")
router.register(r"videos", TikTokVideoViewSet, basename="video")
router.register(r"comments", TikTokCommentViewSet, basename="comment")
router.register(r"spam-filters", TikTokSpamFilterViewSet, basename="spam-filter")

urlpatterns = router.urls
