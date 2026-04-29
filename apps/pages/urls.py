from django.urls import path

from .aiviews import AiCloneFromSlugView, AiImportExternalView, AiPageEditView
from .image_views import PageMediaDetailView, PageMediaView
from .multi_views import (
    MultiBlockCustomCssView,
    MultiBlockDetailView,
    MultiBlockListCreateView,
    MultiBlockReorderView,
    MultiInquiryDetailView,
    MultiInquiryListView,
    MultiPageCustomCssView,
    MultiPageDetailView,
    MultiPageListView,
    MultiPageMediaDetailView,
    MultiPageMediaView,
    MultiPageSlugChangeView,
    MultiStatsBlocksView,
    MultiStatsChartView,
    MultiStatsLinksView,
    MultiPageStatsView,
    MultiSubscriptionDetailView,
    MultiSubscriptionListView,
)
from .views import (
    BlockClickRecordView,
    BlockCustomCssView,
    BlockDetailView,
    BlockListCreateView,
    BlockReorderView,
    ContactInquiryDetailView,
    ContactInquiryListView,
    ContactInquirySubmitView,
    CustomCssView,
    MyPageView,
    PageStatsView,
    PageSubscriptionDetailView,
    PageSubscriptionListView,
    PageSubscriptionSubmitView,
    PageViewRecordView,
    PublicPageView,
    SlugChangeView,
    SlugCheckView,
    StatsBlocksView,
    StatsChartView,
)

app_name = "pages"

urlpatterns = [
    # ─── 기존 단일 페이지 API (프론트 연결 유지) ──────────────────
    # 내 페이지
    path("me/", MyPageView.as_view(), name="my-page"),
    # slug 중복 확인 (변경 전 사전 확인)
    path("check-slug/", SlugCheckView.as_view(), name="check-slug"),
    # slug 변경
    path("me/slug/", SlugChangeView.as_view(), name="change-slug"),
    # 커스텀 CSS 수정
    path("me/css/", CustomCssView.as_view(), name="custom-css"),
    # 통계 (인증 필수)
    path("me/stats/", PageStatsView.as_view(), name="stats"),
    path("me/stats/chart/", StatsChartView.as_view(), name="stats-chart"),
    path("me/stats/blocks/", StatsBlocksView.as_view(), name="stats-blocks"),
    # 문의 관리 (인증 필수 — 관리자)
    path("me/inquiries/", ContactInquiryListView.as_view(), name="inquiry-list"),
    path("me/inquiries/<int:pk>/", ContactInquiryDetailView.as_view(), name="inquiry-detail"),
    # 구독자 관리 (인증 필수 — 관리자)
    path("me/subscriptions/", PageSubscriptionListView.as_view(), name="subscription-list"),
    path("me/subscriptions/<int:pk>/", PageSubscriptionDetailView.as_view(), name="subscription-detail"),
    # 공개 페이지 조회
    path("@<slug:slug>/", PublicPageView.as_view(), name="public-page"),
    # 공개 — 조회·클릭 기록 (인증 불필요)
    path("@<slug:slug>/view/", PageViewRecordView.as_view(), name="record-view"),
    path("@<slug:slug>/blocks/<int:block_id>/click/", BlockClickRecordView.as_view(), name="record-click"),
    # 공개 — 문의 제출 (인증 불필요)
    path("@<slug:slug>/inquiries/", ContactInquirySubmitView.as_view(), name="inquiry-submit"),
    # 공개 — 구독 등록 (인증 불필요)
    path("@<slug:slug>/subscriptions/", PageSubscriptionSubmitView.as_view(), name="subscription-submit"),
    # 미디어 업로드/목록 (인증 필수)
    path("me/media/", PageMediaView.as_view(), name="media-list-upload"),
    path("me/media/<int:pk>/", PageMediaDetailView.as_view(), name="media-detail"),
    # 내 블록 목록/생성
    path("me/blocks/", BlockListCreateView.as_view(), name="block-list-create"),
    # 블록 reorder
    path("me/blocks/reorder/", BlockReorderView.as_view(), name="block-reorder"),
    # 블록 커스텀 CSS 수정
    path("me/blocks/<int:pk>/css/", BlockCustomCssView.as_view(), name="block-custom-css"),
    # 블록 상세(수정/삭제)
    path("me/blocks/<int:pk>/", BlockDetailView.as_view(), name="block-detail"),

    # ─── 다중 페이지 API ──────────────────────────────────────────
    # 페이지 목록 / 생성
    path("multipages/", MultiPageListView.as_view(), name="multipage-list"),
    # 페이지 상세 / 수정 / 삭제
    path("multipages/<int:page_id>/", MultiPageDetailView.as_view(), name="multipage-detail"),
    # 페이지 slug 변경
    path("multipages/<int:page_id>/slug/", MultiPageSlugChangeView.as_view(), name="multipage-slug"),
    # 페이지 커스텀 CSS 수정
    path("multipages/<int:page_id>/css/", MultiPageCustomCssView.as_view(), name="multipage-css"),
    # 블록 목록 / 생성
    path("multipages/<int:page_id>/blocks/", MultiBlockListCreateView.as_view(), name="multipage-block-list"),
    # 블록 reorder (blocks/ 보다 먼저 등록)
    path("multipages/<int:page_id>/blocks/reorder/", MultiBlockReorderView.as_view(), name="multipage-block-reorder"),
    # 블록 커스텀 CSS 수정
    path("multipages/<int:page_id>/blocks/<int:block_id>/css/", MultiBlockCustomCssView.as_view(), name="multipage-block-css"),
    # 블록 수정 / 삭제
    path("multipages/<int:page_id>/blocks/<int:block_id>/", MultiBlockDetailView.as_view(), name="multipage-block-detail"),
    # 통계
    path("multipages/<int:page_id>/stats/", MultiPageStatsView.as_view(), name="multipage-stats"),
    path("multipages/<int:page_id>/stats/chart/", MultiStatsChartView.as_view(), name="multipage-stats-chart"),
    path("multipages/<int:page_id>/stats/blocks/", MultiStatsBlocksView.as_view(), name="multipage-stats-blocks"),
    path("multipages/<int:page_id>/stats/links/", MultiStatsLinksView.as_view(), name="multipage-stats-links"),
    # 문의 관리
    path("multipages/<int:page_id>/inquiries/", MultiInquiryListView.as_view(), name="multipage-inquiry-list"),
    path("multipages/<int:page_id>/inquiries/<int:pk>/", MultiInquiryDetailView.as_view(), name="multipage-inquiry-detail"),
    # 구독자 관리
    path("multipages/<int:page_id>/subscriptions/", MultiSubscriptionListView.as_view(), name="multipage-subscription-list"),
    path("multipages/<int:page_id>/subscriptions/<int:pk>/", MultiSubscriptionDetailView.as_view(), name="multipage-subscription-detail"),
    # 미디어
    path("multipages/<int:page_id>/media/", MultiPageMediaView.as_view(), name="multipage-media-list"),
    path("multipages/<int:page_id>/media/<int:media_id>/", MultiPageMediaDetailView.as_view(), name="multipage-media-detail"),

    # ─── AI 도구 전용 API ─────────────────────────────────────
    # 슬러그로 페이지 복사
    path("ai/clone-from-slug/", AiCloneFromSlugView.as_view(), name="ai-clone-from-slug"),
    # 페이지 전체 편집 (1-shot)
    path("ai/@<slug:slug>/", AiPageEditView.as_view(), name="ai-page-edit"),
    # 외부 서비스(인포크/리틀리/링크트리) 페이지 가져오기
    path("ai/import-external/", AiImportExternalView.as_view(), name="ai-import-external"),
]
