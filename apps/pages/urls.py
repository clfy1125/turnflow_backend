from django.urls import path

from .views import (
    BlockClickRecordView,
    BlockDetailView,
    BlockListCreateView,
    BlockReorderView,
    ContactInquiryDetailView,
    ContactInquiryListView,
    ContactInquirySubmitView,
    MyPageView,
    PageStatsView,
    PageViewRecordView,
    PublicPageView,
    SlugChangeView,
    SlugCheckView,
    StatsBlocksView,
    StatsChartView,
)

app_name = "pages"

urlpatterns = [
    # 내 페이지
    path("me/", MyPageView.as_view(), name="my-page"),
    # slug 중복 확인 (변경 전 사전 확인)
    path("check-slug/", SlugCheckView.as_view(), name="check-slug"),
    # slug 변경
    path("me/slug/", SlugChangeView.as_view(), name="change-slug"),
    # 통계 (인증 필수)
    path("me/stats/", PageStatsView.as_view(), name="stats"),
    path("me/stats/chart/", StatsChartView.as_view(), name="stats-chart"),
    path("me/stats/blocks/", StatsBlocksView.as_view(), name="stats-blocks"),
    # 문의 관리 (인증 필수 — 관리자)
    path("me/inquiries/", ContactInquiryListView.as_view(), name="inquiry-list"),
    path("me/inquiries/<int:pk>/", ContactInquiryDetailView.as_view(), name="inquiry-detail"),
    # 공개 페이지 조회
    path("@<slug:slug>/", PublicPageView.as_view(), name="public-page"),
    # 공개 — 조회·클릭 기록 (인증 불필요)
    path("@<slug:slug>/view/", PageViewRecordView.as_view(), name="record-view"),
    path("@<slug:slug>/blocks/<int:block_id>/click/", BlockClickRecordView.as_view(), name="record-click"),
    # 공개 — 문의 제출 (인증 불필요)
    path("@<slug:slug>/inquiries/", ContactInquirySubmitView.as_view(), name="inquiry-submit"),
    # 내 블록 목록/생성
    path("me/blocks/", BlockListCreateView.as_view(), name="block-list-create"),
    # 블록 reorder
    path("me/blocks/reorder/", BlockReorderView.as_view(), name="block-reorder"),
    # 블록 상세(수정/삭제)
    path("me/blocks/<int:pk>/", BlockDetailView.as_view(), name="block-detail"),
]


app_name = "pages"

urlpatterns = [
    # 내 페이지
    path("me/", MyPageView.as_view(), name="my-page"),
    # slug 중복 확인 (변경 전 사전 확인)
    path("check-slug/", SlugCheckView.as_view(), name="check-slug"),
    # slug 변경
    path("me/slug/", SlugChangeView.as_view(), name="change-slug"),
    # 통계 (인증 필수)
    path("me/stats/", PageStatsView.as_view(), name="stats"),
    path("me/stats/chart/", StatsChartView.as_view(), name="stats-chart"),
    path("me/stats/blocks/", StatsBlocksView.as_view(), name="stats-blocks"),
    # 공개 페이지 조회
    path("@<slug:slug>/", PublicPageView.as_view(), name="public-page"),
    # 공개 — 조회·클릭 기록 (인증 불필요)
    path("@<slug:slug>/view/", PageViewRecordView.as_view(), name="record-view"),
    path("@<slug:slug>/blocks/<int:block_id>/click/", BlockClickRecordView.as_view(), name="record-click"),
    # 내 블록 목록/생성
    path("me/blocks/", BlockListCreateView.as_view(), name="block-list-create"),
    # 블록 reorder
    path("me/blocks/reorder/", BlockReorderView.as_view(), name="block-reorder"),
    # 블록 상세(수정/삭제)
    path("me/blocks/<int:pk>/", BlockDetailView.as_view(), name="block-detail"),
]
