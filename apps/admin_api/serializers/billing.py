"""apps/admin_api/serializers/billing.py — 어드민 구독 플랜(요금제) 시리얼라이저.

``/api/v1/admin/subscription-plans/`` 에서 ``IsAdminUser`` 권한으로만 접근한다.
사용자용 ``GET /api/v1/billing/plans/`` 와 동일 필드에 ``is_active`` 를 더 노출해
비활성 플랜(예: 운영용 ``admin``)까지 드롭다운/라벨 소스로 쓸 수 있게 한다.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.billing.models import SubscriptionPlan


class AdminSubscriptionPlanSerializer(serializers.ModelSerializer):
    """어드민 구독 플랜 1건 (읽기 전용). 사용자용 + ``is_active``."""

    class Meta:
        model = SubscriptionPlan
        fields = [
            "id",
            "name",
            "display_name",
            "monthly_price",
            "features",
            "sort_order",
            "is_active",
        ]
        read_only_fields = fields
