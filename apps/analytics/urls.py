"""
analytics URL Configuration — /api/v1/track/ 아래 마운트 (config/api_urls.py)
"""

from django.urls import path

from .views import TrackCancellationEventView, TrackCheckoutEventView, TrackVisitView

app_name = "analytics"

urlpatterns = [
    path("visit/", TrackVisitView.as_view(), name="track-visit"),
    path("checkout-event/", TrackCheckoutEventView.as_view(), name="track-checkout-event"),
    path(
        "cancellation-event/",
        TrackCancellationEventView.as_view(),
        name="track-cancellation-event",
    ),
]
