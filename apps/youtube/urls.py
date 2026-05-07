"""YouTube API URL configuration."""

from rest_framework.routers import DefaultRouter

from .views import (
    YouTubeCommentViewSet,
    YouTubeIntegrationViewSet,
    YouTubeSpamFilterViewSet,
    YouTubeVideoViewSet,
)

app_name = "youtube"

router = DefaultRouter()
router.register(r"integration", YouTubeIntegrationViewSet, basename="integration")
router.register(r"videos", YouTubeVideoViewSet, basename="video")
router.register(r"comments", YouTubeCommentViewSet, basename="comment")
router.register(r"spam-filters", YouTubeSpamFilterViewSet, basename="spam-filter")

urlpatterns = router.urls
