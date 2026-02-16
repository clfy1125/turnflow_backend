"""
Billing URL configuration
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import BillingViewSet

app_name = "billing"

router = DefaultRouter()
router.register(r"billing", BillingViewSet, basename="billing")

urlpatterns = router.urls
