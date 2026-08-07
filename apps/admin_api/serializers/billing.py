"""apps/admin_api/serializers/billing.py — 어드민 구독 플랜·결제 이력 시리얼라이저.

``/api/v1/admin/subscription-plans/`` 에서 ``IsAdminUser`` 권한으로만 접근한다.
사용자용 ``GET /api/v1/billing/plans/`` 와 동일 필드에 ``is_active`` 를 더 노출해
비활성 플랜(예: 운영용 ``admin``)까지 드롭다운/라벨 소스로 쓸 수 있게 한다.

회원별 결제 이력(USR-5)은 사용자용 ``PaymentHistorySerializer`` 를 **그대로 상속**해
필드가 갈라지지 않게 하고, 어드민에만 필요한 ``refunded_at`` 만 더한다.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.billing.models import SubscriptionPlan
from apps.billing.serializers import PaymentHistorySerializer


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


class AdminUserPaymentHistorySerializer(PaymentHistorySerializer):
    """USR-5 — 회원별 결제 이력 1건 (읽기 전용).

    사용자용 ``PaymentHistorySerializer`` 를 상속해 필드 정의가 두 벌로 갈라지지 않게 하고,
    어드민 화면에만 필요한 ``refunded_at`` 을 더한다.

    ⚠️ ``toss_payment_key`` · ``toss_idempotency_key`` 는 **직렬화하지 않는다**(부모에도 없음).
    ``toss_order_id`` 는 토스 콘솔 대조용으로 남긴다.
    """

    class Meta(PaymentHistorySerializer.Meta):
        fields = PaymentHistorySerializer.Meta.fields + ["refunded_at"]
        read_only_fields = fields
        ref_name = "AdminUserPaymentHistory"
