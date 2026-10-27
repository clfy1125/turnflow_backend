from django.urls import path

from .views import ZeroCallWebhookView

app_name = "sms"

urlpatterns = [
    path(
        "zerocall/webhook/<str:secret>/",
        ZeroCallWebhookView.as_view(),
        name="zerocall-webhook",
    ),
]
