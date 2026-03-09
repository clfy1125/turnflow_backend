from django.urls import path

from .views import (
    BlockDetailView,
    BlockListCreateView,
    BlockReorderView,
    MyPageView,
    PublicPageView,
    SlugChangeView,
    SlugCheckView,
)

app_name = "pages"

urlpatterns = [
    # 내 페이지
    path("me/", MyPageView.as_view(), name="my-page"),
    # slug 중복 확인 (변경 전 사전 확인)
    path("check-slug/", SlugCheckView.as_view(), name="check-slug"),
    # slug 변경
    path("me/slug/", SlugChangeView.as_view(), name="change-slug"),
    # 공개 페이지 조회
    path("@<slug:slug>/", PublicPageView.as_view(), name="public-page"),
    # 내 블록 목록/생성
    path("me/blocks/", BlockListCreateView.as_view(), name="block-list-create"),
    # 블록 reorder
    path("me/blocks/reorder/", BlockReorderView.as_view(), name="block-reorder"),
    # 블록 상세(수정/삭제)
    path("me/blocks/<int:pk>/", BlockDetailView.as_view(), name="block-detail"),
]
