"""apps/admin_api/views/billing.py — 어드민 구독 플랜(요금제) 조회.

라우팅: ``GET /api/v1/admin/subscription-plans/`` (``IsAdminUser``, is_staff=True).

사용자용 ``GET /api/v1/billing/plans/`` 는 ``is_active=True`` 만, ``AllowAny`` 로 노출한다.
이 어드민 별칭은 **비활성 플랜까지 전부** 반환해(`is_active` 포함) 백오피스가 플랜
드롭다운/라벨 소스를 하드코딩 없이 DB-driven 으로 렌더하도록 돕는다.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import generics
from rest_framework.permissions import IsAdminUser

from apps.admin_api.serializers.billing import AdminSubscriptionPlanSerializer
from apps.billing.models import SubscriptionPlan


class AdminSubscriptionPlanListView(generics.ListAPIView):
    """전체 구독 플랜 목록 (비활성 포함). 페이지네이션 없음 — 플랜은 소수."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminSubscriptionPlanSerializer
    pagination_class = None

    def get_queryset(self):
        return SubscriptionPlan.objects.all().order_by("sort_order", "name")

    @extend_schema(
        tags=["admin-users"],
        summary="[관리자] 구독 플랜 목록 조회",
        description="""
## 개요
서비스의 **모든 구독 플랜**(SubscriptionPlan)을 반환합니다. 사용자용
`GET /api/v1/billing/plans/`(활성만, 비로그인 허용)와 달리 **비활성 플랜까지 포함**하고
`is_active` 를 노출하여, 백오피스의 플랜 드롭다운/라벨 소스로 사용합니다.

## 사용 시나리오
- 회원 구독 강제 변경(`PATCH /admin/users/{id}/subscription/`) UI 의 플랜 선택 드롭다운
- 플랜 값을 하드코딩하지 않고 DB-driven 으로 렌더 (플랜 집합은 가변)

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 응답
`sort_order` 오름차순 정렬 배열(페이지네이션 없음). 각 항목:
`id, name, display_name, monthly_price, features, sort_order, is_active`.
        """,
        responses={
            200: AdminSubscriptionPlanSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                response_only=True,
                value=[
                    {
                        "id": "550e8400-e29b-41d4-a716-446655440001",
                        "name": "free",
                        "display_name": "무료",
                        "monthly_price": 0,
                        "features": {"max_pages": 3, "ai_generation": False},
                        "sort_order": 0,
                        "is_active": True,
                    },
                    {
                        "id": "550e8400-e29b-41d4-a716-446655440002",
                        "name": "pro",
                        "display_name": "프로",
                        "monthly_price": 14900,
                        "features": {"max_pages": -1, "ai_generation": True},
                        "sort_order": 1,
                        "is_active": True,
                    },
                    {
                        "id": "550e8400-e29b-41d4-a716-446655440003",
                        "name": "admin",
                        "display_name": "관리자",
                        "monthly_price": 18900,
                        "features": {"max_pages": -1, "ai_generation": True},
                        "sort_order": 2,
                        "is_active": False,
                    },
                ],
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
