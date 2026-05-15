"""
Insights API 필터.

화면 1 통합 테이블에서 마케터가 자주 거르는 차원:
    - account_id          : 다중 IG 계정 사용 시
    - media_product_type  : FEED / REELS / STORY
    - published_after / before  : 기간
    - has_paid            : 광고 연동 게시물만
"""

from __future__ import annotations

import django_filters

from .models import IGMedia, MediaProductType


class IGMediaFilter(django_filters.FilterSet):
    account_id = django_filters.UUIDFilter(field_name="account_id")
    media_product_type = django_filters.ChoiceFilter(
        choices=MediaProductType.choices,
        help_text="FEED / REELS / STORY / AD",
    )
    published_after = django_filters.IsoDateTimeFilter(
        field_name="published_at", lookup_expr="gte"
    )
    published_before = django_filters.IsoDateTimeFilter(
        field_name="published_at", lookup_expr="lt"
    )
    has_paid = django_filters.BooleanFilter(method="filter_has_paid")

    min_reach = django_filters.NumberFilter(field_name="insight__reach", lookup_expr="gte")
    min_er = django_filters.NumberFilter(
        field_name="insight__engagement_rate", lookup_expr="gte"
    )

    class Meta:
        model = IGMedia
        fields = (
            "account_id",
            "media_product_type",
            "published_after",
            "published_before",
            "has_paid",
            "min_reach",
            "min_er",
        )

    def filter_has_paid(self, queryset, name, value: bool):
        if value:
            return queryset.filter(insight__paid_reach__isnull=False)
        return queryset.filter(insight__paid_reach__isnull=True)
