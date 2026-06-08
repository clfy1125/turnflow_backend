"""
apps/pages/link_urls.py

/api/v1/link/ 라우팅 — 외부 링크 메타 조회.
"""

from django.urls import path

from .link_views import LinkMetaView

app_name = "link"

urlpatterns = [
    # 외부 상품/콘텐츠 URL → flat 메타(title/thumbnail/price/original_price)
    path("fetch-meta/", LinkMetaView.as_view(), name="fetch-meta"),
]
