"""apps/admin_api/views/pages.py — 어드민 페이지 관리/모더레이션 뷰.

라우팅: ``/api/v1/admin/pages/`` 아래. 권한: ``IsAdminUser``(is_staff=True).
크로스 워크스페이스 전역 스코프 — 요청자의 워크스페이스로 절대 필터하지 않는다.

엔드포인트:
  - 페이지 목록:        ``GET   /api/v1/admin/pages/``
  - 페이지 상세/모더레이션: ``GET|PATCH /api/v1/admin/pages/<slug>/``
  - 페이지 문의 목록:    ``GET   /api/v1/admin/pages/<slug>/inquiries/``
  - 페이지 구독자 목록:  ``GET   /api/v1/admin/pages/<slug>/subscriptions/``

모더레이션(PATCH) 성공 시 ``AdminActionLog`` 에 감사 로그를 적재한다 (CLAUDE.md 관측성/감사 원칙).
"""

from __future__ import annotations

import logging

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import filters, generics
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog
from apps.admin_api.serializers.pages import (
    AdminPageDetailSerializer,
    AdminPageInquirySerializer,
    AdminPageListSerializer,
    AdminPageSubscriptionSerializer,
    AdminPageUpdateSerializer,
)
from apps.pages.models import ContactInquiry, Page, PageSubscription

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# E-1. 페이지 목록
# ─────────────────────────────────────────────────────────────


class AdminPageListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminPageListSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_public", "is_active", "import_source", "is_reference"]
    search_fields = ["title", "slug", "user__email"]
    ordering_fields = ["created_at", "updated_at", "title"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = Page.objects.select_related("user", "reference_category")
        params = self.request.query_params

        owner = params.get("owner")
        if owner:
            qs = qs.filter(user_id=owner)

        # is_reference 는 filterset_fields 로도 처리되지만, 명시적 분기로 truthy 문자열도 수용.
        is_ref = params.get("is_reference")
        if is_ref is not None:
            qs = qs.filter(is_reference=is_ref.lower() in ("true", "1"))

        return qs

    @extend_schema(
        tags=["admin-pages"],
        summary="[관리자] 페이지 목록 조회",
        description="""
## 개요
전체 워크스페이스에 걸친 모든 페이지를 페이지네이션과 함께 반환합니다. 각 항목에는 소유자(id/email),
공개/활성 상태, 외부 임포트 출처, AI 레퍼런스 여부, 최근 30일 조회수가 포함됩니다.

## 사용 시나리오
- 백오피스 모더레이션 화면에서 페이지를 검색/필터링할 때 호출.
- 신고/정책 위반 페이지를 찾아 상세(`PATCH .../pages/<slug>/`)로 차단하기 직전 단계.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만 접근 가능)

## 비즈니스 로직
- 전역 스코프: 요청자의 워크스페이스와 무관하게 모든 페이지를 조회합니다.
- `select_related(user, reference_category)` 로 N+1 을 회피합니다.
- 필터: `is_public`, `is_active`, `import_source`, `is_reference` (모델 필드 직접 매핑).
- 추가 쿼리: `owner`(소유자 User PK)로 특정 사용자의 페이지만 필터.
- 검색(`search`): `title`, `slug`, `user__email`.
- 정렬(`ordering`): `created_at`, `updated_at`, `title` (기본 `-created_at`).

## 주의사항
- 응답은 `{count, next, previous, results}` 형태(PageNumberPagination, PAGE_SIZE=20)입니다.
- `views_30d` 는 각 행마다 집계되므로 매우 큰 결과셋에서는 좁은 필터와 함께 사용하세요.
        """,
        parameters=[
            OpenApiParameter(
                "is_public",
                bool,
                OpenApiParameter.QUERY,
                description="공개 여부 필터 (true/false)",
                required=False,
            ),
            OpenApiParameter(
                "is_active",
                bool,
                OpenApiParameter.QUERY,
                description="활성 여부 필터 (true/false). false=차단된 페이지",
                required=False,
            ),
            OpenApiParameter(
                "import_source",
                str,
                OpenApiParameter.QUERY,
                description="외부 임포트 출처 필터 (''=자체생성, inpock, litly, linktree)",
                required=False,
            ),
            OpenApiParameter(
                "is_reference",
                bool,
                OpenApiParameter.QUERY,
                description="AI 레퍼런스 대상 여부 필터 (true/false)",
                required=False,
            ),
            OpenApiParameter(
                "owner",
                int,
                OpenApiParameter.QUERY,
                description="소유자 User PK 로 필터",
                required=False,
            ),
            OpenApiParameter(
                "search",
                str,
                OpenApiParameter.QUERY,
                description="title / slug / user__email 부분 검색",
                required=False,
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                description="정렬 (created_at, updated_at, title; `-` 접두로 내림차순)",
                required=False,
            ),
        ],
        responses={
            200: AdminPageListSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "slug": "brand-shop",
                            "title": "브랜드샵 공식",
                            "owner": {"id": 42, "email": "owner@example.com"},
                            "is_public": True,
                            "is_active": True,
                            "import_source": "",
                            "is_reference": False,
                            "views_30d": 1280,
                            "created_at": "2026-04-01T09:00:00+09:00",
                            "updated_at": "2026-05-20T18:30:00+09:00",
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────
# E-2. 페이지 상세 / 모더레이션
# ─────────────────────────────────────────────────────────────


class AdminPageDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAdminUser]
    lookup_field = "slug"

    def get_queryset(self):
        return Page.objects.select_related("user", "reference_category").prefetch_related("blocks")

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return AdminPageUpdateSerializer
        return AdminPageDetailSerializer

    @extend_schema(
        tags=["admin-pages"],
        summary="[관리자] 페이지 상세 조회",
        description="""
## 개요
slug 로 페이지 1건의 상세를 반환합니다. 목록 필드에 더해 블록 요약 목록, 누적/최근 통계
(`views_total`, `clicks_total`, `views_30d`), AI 레퍼런스 카테고리가 포함됩니다.

## 사용 시나리오
- 백오피스에서 특정 페이지를 열어 구성/트래픽을 점검하고 모더레이션 여부를 판단할 때.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 스코프 조회. `select_related`/`prefetch_related` 로 블록·소유자·카테고리를 함께 로드.
- `stats.views_total` = 누적 PageView, `stats.clicks_total` = 누적 BlockClick, `views_30d` = 최근 30일.

## 주의사항
- IG 토큰 등 비밀값은 포함되지 않습니다 (페이지 도메인 한정).
        """,
        responses={
            200: AdminPageDetailSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 slug 페이지 없음"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                value={
                    "slug": "brand-shop",
                    "title": "브랜드샵 공식",
                    "owner": {"id": 42, "email": "owner@example.com"},
                    "is_public": True,
                    "is_active": True,
                    "import_source": "",
                    "is_reference": False,
                    "views_30d": 1280,
                    "created_at": "2026-04-01T09:00:00+09:00",
                    "updated_at": "2026-05-20T18:30:00+09:00",
                    "blocks": [
                        {"id": 1, "type": "profile", "order": 0, "is_enabled": True},
                        {"id": 2, "type": "single_link", "order": 1, "is_enabled": True},
                    ],
                    "stats": {"views_total": 53120, "clicks_total": 9821, "views_30d": 1280},
                    "reference_category": {"slug": "profile-link", "name": "프로필 링크"},
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-pages"],
        summary="[관리자] 페이지 모더레이션 (차단/공개)",
        description="""
## 개요
페이지의 `is_active`(차단) / `is_public`(강제 비공개)만 부분 수정합니다. 정책 위반 페이지를
차단하거나 강제로 비공개 전환하는 모더레이션 액션입니다.

## 사용 시나리오
- 신고된 페이지를 검토 후 차단(`is_active=false`)하거나, 공개를 강제 해제(`is_public=false`)할 때.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 보낸 필드만 갱신(부분 수정). 그 외 페이지 필드는 이 엔드포인트로 변경 불가.
- 변경 성공 후 `AdminActionLog`(action=`page.update`)에 before/after 감사 로그를 적재합니다.
- 응답은 갱신된 페이지 상세(`AdminPageDetailSerializer`)로 반환합니다.

## 주의사항
- `PUT` 은 비활성화되어 있습니다 (부분 수정 PATCH 만 사용).
- `is_active=false` 차단 시 해당 페이지의 공개 URL 접근이 즉시 차단됩니다.
        """,
        request=AdminPageUpdateSerializer,
        responses={
            200: AdminPageDetailSerializer,
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 slug 페이지 없음"),
        },
        examples=[
            OpenApiExample(
                "차단 요청",
                value={"is_active": False},
                request_only=True,
            ),
            OpenApiExample(
                "강제 비공개 요청",
                value={"is_public": False},
                request_only=True,
            ),
            OpenApiExample(
                "응답 예시",
                value={
                    "slug": "brand-shop",
                    "title": "브랜드샵 공식",
                    "owner": {"id": 42, "email": "owner@example.com"},
                    "is_public": False,
                    "is_active": False,
                    "import_source": "",
                    "is_reference": False,
                    "views_30d": 1280,
                    "created_at": "2026-04-01T09:00:00+09:00",
                    "updated_at": "2026-06-02T11:00:00+09:00",
                    "blocks": [],
                    "stats": {"views_total": 53120, "clicks_total": 9821, "views_30d": 1280},
                    "reference_category": None,
                },
                response_only=True,
            ),
        ],
    )
    def patch(self, request, *args, **kwargs):
        page = self.get_object()
        before = {"is_active": page.is_active, "is_public": page.is_public}

        serializer = self.get_serializer(page, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        page.refresh_from_db()
        after = {"is_active": page.is_active, "is_public": page.is_public}

        changes = {
            field: {"before": before[field], "after": after[field]}
            for field in before
            if before[field] != after[field]
        }
        if changes:
            log_admin_action(
                request=request,
                action=AdminActionLog.Action.PAGE_UPDATE,
                target_type="page",
                target_id=page.slug,
                target_repr=page.title,
                changes=changes,
            )
            logger.info(
                "[admin-pages] req=%s page=%s 모더레이션 changes=%s",
                getattr(request, "id", ""),
                page.slug,
                changes,
            )

        # 상세 시리얼라이저로 응답 (목록 + 블록 + 통계 포함)
        detail = AdminPageDetailSerializer(page, context=self.get_serializer_context())
        return Response(detail.data)

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────
# E-3. 페이지 문의 목록
# ─────────────────────────────────────────────────────────────


class AdminPageInquiryListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminPageInquirySerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["category"]
    search_fields = ["name", "email", "phone", "subject", "content"]
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        # drf-spectacular 스키마 생성 시엔 kwargs(slug)가 없으므로 빈 쿼리셋 반환.
        if getattr(self, "swagger_fake_view", False):
            return ContactInquiry.objects.none()
        return ContactInquiry.objects.filter(page__slug=self.kwargs["slug"]).order_by("-created_at")

    @extend_schema(
        tags=["admin-pages"],
        summary="[관리자] 페이지 문의 목록",
        description="""
## 개요
지정한 페이지(slug)로 들어온 방문자 문의(ContactInquiry)를 최신순으로 반환합니다.

## 사용 시나리오
- CS/모더레이션 담당자가 특정 페이지의 문의 내역을 점검할 때.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 스코프: 경로의 `slug` 페이지에 속한 문의만 조회 (워크스페이스 무관).
- 정렬은 `created_at` 내림차순(최신 우선)이 기본입니다.
- 필터: `category`(general/business/support/other). 검색: `name`, `email`, `phone`, `subject`, `content`.

## 주의사항
- 응답은 `{count, next, previous, results}` 형태(PAGE_SIZE=20)입니다.
- 존재하지 않는 slug 면 빈 목록(count=0)이 반환됩니다 (404 아님).
        """,
        parameters=[
            OpenApiParameter(
                "slug",
                str,
                OpenApiParameter.PATH,
                description="대상 페이지 slug",
                required=True,
            ),
            OpenApiParameter(
                "category",
                str,
                OpenApiParameter.QUERY,
                description="문의 분류 필터 (general/business/support/other)",
                required=False,
            ),
            OpenApiParameter(
                "search",
                str,
                OpenApiParameter.QUERY,
                description="name/email/phone/subject/content 부분 검색",
                required=False,
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                description="정렬 (created_at, updated_at; `-` 접두로 내림차순)",
                required=False,
            ),
        ],
        responses={
            200: AdminPageInquirySerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="(slug 미존재 시 빈 목록 반환 — 일반적으로 404 아님)"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": 11,
                            "page": 5,
                            "name": "김방문",
                            "category": "business",
                            "email": "visitor@example.com",
                            "phone": "010-1234-5678",
                            "subject": "협업 제안",
                            "content": "광고 협업 문의드립니다.",
                            "agreed_to_terms": True,
                            "memo": "",
                            "created_at": "2026-05-30T14:00:00+09:00",
                            "updated_at": "2026-05-30T14:00:00+09:00",
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────
# E-4. 페이지 구독자 목록
# ─────────────────────────────────────────────────────────────


class AdminPageSubscriptionListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminPageSubscriptionSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["category"]
    search_fields = ["name", "email", "phone"]
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        # drf-spectacular 스키마 생성 시엔 kwargs(slug)가 없으므로 빈 쿼리셋 반환.
        if getattr(self, "swagger_fake_view", False):
            return PageSubscription.objects.none()
        return PageSubscription.objects.filter(page__slug=self.kwargs["slug"]).order_by(
            "-created_at"
        )

    @extend_schema(
        tags=["admin-pages"],
        summary="[관리자] 페이지 구독자 목록",
        description="""
## 개요
지정한 페이지(slug)의 구독자(PageSubscription)를 최신순으로 반환합니다.

## 사용 시나리오
- 백오피스에서 특정 페이지의 구독/뉴스레터 수집 현황을 점검할 때.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 전역 스코프: 경로의 `slug` 페이지에 속한 구독자만 조회 (워크스페이스 무관).
- 정렬은 `created_at` 내림차순(최신 우선)이 기본입니다.
- 필터: `category`(page_subscribe/newsletter/event/other). 검색: `name`, `email`, `phone`.

## 주의사항
- 응답은 `{count, next, previous, results}` 형태(PAGE_SIZE=20)입니다.
- 구독자 이메일은 개인정보이므로 화면 노출/내보내기 시 처리방침을 준수하세요.
        """,
        parameters=[
            OpenApiParameter(
                "slug",
                str,
                OpenApiParameter.PATH,
                description="대상 페이지 slug",
                required=True,
            ),
            OpenApiParameter(
                "category",
                str,
                OpenApiParameter.QUERY,
                description="구독 분류 필터 (page_subscribe/newsletter/event/other)",
                required=False,
            ),
            OpenApiParameter(
                "search",
                str,
                OpenApiParameter.QUERY,
                description="name/email/phone 부분 검색",
                required=False,
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                description="정렬 (created_at, updated_at; `-` 접두로 내림차순)",
                required=False,
            ),
        ],
        responses={
            200: AdminPageSubscriptionSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="(slug 미존재 시 빈 목록 반환 — 일반적으로 404 아님)"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": 7,
                            "page": 5,
                            "name": "이구독",
                            "category": "newsletter",
                            "email": "sub@example.com",
                            "phone": "",
                            "agreed_to_terms": True,
                            "memo": "",
                            "created_at": "2026-05-29T10:00:00+09:00",
                            "updated_at": "2026-05-29T10:00:00+09:00",
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
