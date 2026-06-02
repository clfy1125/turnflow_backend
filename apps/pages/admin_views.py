"""apps/pages/admin_views.py — 어드민 전용 AI 레퍼런스 관리 뷰.

라우팅: ``/api/v1/admin/`` 아래.  권한: ``IsAdminUser`` (is_staff=True).

엔드포인트:
  - 카테고리 CRUD: ``/api/v1/admin/reference-categories/``
  - 레퍼런스 후보 목록: ``/api/v1/admin/reference-pages/``
  - 페이지 → 레퍼런스 토글: ``PATCH /api/v1/admin/pages/<slug>/reference/``
  - 스냅샷 캡쳐 트리거: ``POST   /api/v1/admin/pages/<slug>/reference/snapshot/``
  - 스냅샷 상태 폴링:   ``GET    /api/v1/admin/pages/<slug>/reference/snapshot/status/``
"""
from __future__ import annotations

import logging

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import generics, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_serializers import (
    AdminPageReferenceUpdateSerializer,
    AdminReferenceCategorySerializer,
    AdminReferencePageSerializer,
    AdminSnapshotStatusSerializer,
    AdminSnapshotTriggerResponseSerializer,
)
from .models import Page, ReferenceCategory

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 카테고리 CRUD
# ─────────────────────────────────────────────────────────────

class AdminReferenceCategoryListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminReferenceCategorySerializer
    queryset = ReferenceCategory.objects.all().order_by("sort_order", "id")
    pagination_class = None  # 카테고리는 수십 개 이하 — 전체 반환

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 카테고리 목록 조회",
        description="""
## 개요
AI 페이지 생성 시 노출되는 카테고리를 모두 반환합니다 (is_active 무관, sort_order ASC).
공개 API(`GET /api/v1/ai/categories/`) 는 is_active=True 만 노출하지만, 어드민 화면은 비활성 항목도 관리합니다.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 응답 필드
- `reference_page_count`: 해당 카테고리에 매핑된 Page (is_reference=True) 수
        """,
        responses={
            200: AdminReferenceCategorySerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 카테고리 생성",
        description="새 카테고리를 추가합니다. slug 는 유일해야 하며 소문자/하이픈만 허용.",
        request=AdminReferenceCategorySerializer,
        responses={
            201: AdminReferenceCategorySerializer,
            400: OpenApiResponse(description="검증 실패 (slug 중복 등)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "request",
                value={
                    "slug": "podcast",
                    "name": "팟캐스트",
                    "description": "오디오 콘텐츠 크리에이터용",
                    "icon_emoji": "🎙️",
                    "sort_order": 12,
                    "is_active": True,
                },
                request_only=True,
            )
        ],
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class AdminReferenceCategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminReferenceCategorySerializer
    queryset = ReferenceCategory.objects.all()

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 카테고리 단건 조회",
        description="ID 로 카테고리 1건을 조회. reference_page_count 포함.",
        responses={
            200: AdminReferenceCategorySerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="카테고리 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 카테고리 전체 수정",
        description="모든 필드를 다시 보냅니다. 부분 수정은 PATCH 사용.",
        request=AdminReferenceCategorySerializer,
        responses={
            200: AdminReferenceCategorySerializer,
            400: OpenApiResponse(description="검증 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="카테고리 없음"),
        },
    )
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 카테고리 부분 수정",
        description="보낸 필드만 갱신합니다 (예: `sort_order`, `is_active` 토글).",
        request=AdminReferenceCategorySerializer,
        responses={
            200: AdminReferenceCategorySerializer,
            400: OpenApiResponse(description="검증 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="카테고리 없음"),
        },
    )
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 카테고리 삭제",
        description="""
카테고리 삭제 시 매핑된 Page 의 ``reference_category`` 는 NULL 로 설정되며
페이지 자체는 보존됩니다. 매핑 정리가 필요한 경우 응답 본문의
``affected_pages`` 를 참고하세요. 영구 삭제 대신 ``is_active=False`` 비활성화를 권장합니다.
        """,
        responses={
            204: OpenApiResponse(description="삭제 성공"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="카테고리 없음"),
        },
    )
    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        affected = Page.objects.filter(reference_category=instance).count()
        logger.warning(
            "ReferenceCategory 삭제 — slug=%s, affected_pages=%d",
            instance.slug,
            affected,
        )
        return super().delete(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────
# 레퍼런스 후보 페이지 목록
# ─────────────────────────────────────────────────────────────

class AdminReferencePageListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminReferencePageSerializer

    def get_queryset(self):
        qs = Page.objects.select_related("user", "reference_category")
        params = self.request.query_params
        cat = params.get("category")
        if cat:
            qs = qs.filter(reference_category__slug=cat)
        is_ref = params.get("is_reference")
        if is_ref is not None:
            qs = qs.filter(is_reference=is_ref.lower() == "true")
        only_public = params.get("only_public", "true").lower() == "true"
        if only_public:
            qs = qs.filter(is_public=True)
        return qs.order_by(
            "reference_category__sort_order",
            "reference_order",
            "-updated_at",
        )

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 후보 페이지 목록",
        description="""
어드민이 레퍼런스로 큐레이션할 수 있는 페이지 목록.
기본은 `is_public=True` 만 노출 (비공개 페이지는 캡쳐 불가).

## 쿼리 파라미터
- `category` (선택): 카테고리 slug (예: `profile-link`).  주어지면 해당 카테고리 페이지만.
- `is_reference` (선택): `true` / `false`. 현재 레퍼런스 토글 상태로 필터.
- `only_public` (선택, 기본 true): `false` 면 비공개 페이지도 포함.
        """,
        parameters=[
            OpenApiParameter("category", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("is_reference", bool, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("only_public", bool, OpenApiParameter.QUERY, required=False),
        ],
        responses={
            200: AdminReferencePageSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────
# 페이지 ↔ 레퍼런스 토글 / 스냅샷
# ─────────────────────────────────────────────────────────────

class AdminPageReferenceUpdateView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 페이지를 AI 레퍼런스로 토글/수정",
        description="""
페이지를 AI 레퍼런스로 지정하거나 카테고리/순서/표시명을 수정합니다.

## 제약
- `is_reference=True` 로 지정하려면 페이지가 `is_public=True` 여야 함 (400 차단).
- `reference_category_id` 에 `null` 을 보내면 카테고리 해제.

## 어떤 필드를 보내는가
모든 필드 optional — 보낸 키만 적용됩니다.
        """,
        request=AdminPageReferenceUpdateSerializer,
        responses={
            200: AdminReferencePageSerializer,
            400: OpenApiResponse(description="비공개 페이지에 is_reference=True 지정 등 검증 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="페이지 없음"),
        },
        examples=[
            OpenApiExample(
                "지정",
                value={
                    "is_reference": True,
                    "reference_category_id": 1,
                    "reference_order": 1,
                    "reference_title": "감성 카페 브랜드 페이지",
                    "reference_description": "파스텔 톤 + 그리드 갤러리",
                },
                request_only=True,
            ),
            OpenApiExample(
                "해제",
                value={"is_reference": False},
                request_only=True,
            ),
        ],
    )
    def patch(self, request, slug):
        page = get_object_or_404(Page, slug=slug)
        ser = AdminPageReferenceUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        if vd.get("is_reference") and not page.is_public:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "비공개 페이지는 레퍼런스로 지정할 수 없습니다. 먼저 공개로 전환하세요.",
                        "details": {"slug": page.slug, "is_public": False},
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if "reference_category_id" in vd:
            page.reference_category_id = vd["reference_category_id"]
        for f in (
            "is_reference",
            "reference_order",
            "reference_title",
            "reference_description",
        ):
            if f in vd:
                setattr(page, f, vd[f])
        page.save()
        logger.info(
            "AI 레퍼런스 메타 갱신 — slug=%s, is_reference=%s, category_id=%s",
            page.slug,
            page.is_reference,
            page.reference_category_id,
        )
        return Response(
            AdminReferencePageSerializer(page, context={"request": request}).data
        )


class AdminPageReferenceSnapshotTriggerView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 페이지 스냅샷 캡쳐 트리거",
        description="""
Playwright Headless Chromium 으로 모바일 뷰포트(390×844)의 최초 화면을 캡쳐합니다.
Celery 비동기 — 응답은 즉시 202 와 함께 job_id 반환.  상태는 `GET .../snapshot/status/` 로 폴링.

## 제약
- 페이지가 `is_public=True` 여야 합니다.
- 직전 캡쳐가 `pending`/`running` 상태면 409 — 완료/실패 후 재시도.

## 캡쳐 대상 URL
환경변수 `SNAPSHOT_BASE_URL` + `/@{slug}` (예: `https://turnflow.link/@brand-shop`).
        """,
        request=None,
        responses={
            202: AdminSnapshotTriggerResponseSerializer,
            400: OpenApiResponse(description="비공개 페이지"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="페이지 없음"),
            409: OpenApiResponse(description="이미 진행 중인 캡쳐 작업이 있음"),
        },
    )
    def post(self, request, slug):
        page = get_object_or_404(Page, slug=slug)

        if not page.is_public:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "비공개 페이지는 스냅샷을 캡쳐할 수 없습니다.",
                        "details": {"slug": page.slug},
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if page.reference_snapshot_status in (
            Page.SnapshotStatus.PENDING,
            Page.SnapshotStatus.RUNNING,
        ):
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 409,
                        "message": "이미 진행 중인 스냅샷 작업이 있습니다.",
                        "details": {
                            "job_id": page.reference_snapshot_job_id,
                            "status": page.reference_snapshot_status,
                        },
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )

        page.reference_snapshot_status = Page.SnapshotStatus.PENDING
        page.reference_snapshot_error = ""
        page.save(
            update_fields=[
                "reference_snapshot_status",
                "reference_snapshot_error",
                "updated_at",
            ]
        )

        # tasks.py import 는 함수 안에서 — circular import 회피 + Celery 미가용 환경 보호
        from .tasks import capture_reference_snapshot

        async_result = capture_reference_snapshot.delay(page.id)
        page.reference_snapshot_job_id = async_result.id
        page.save(update_fields=["reference_snapshot_job_id", "updated_at"])

        logger.info(
            "스냅샷 캡쳐 트리거 — slug=%s, job_id=%s",
            page.slug,
            async_result.id,
        )
        return Response(
            {"job_id": async_result.id, "status": Page.SnapshotStatus.PENDING},
            status=status.HTTP_202_ACCEPTED,
        )


class AdminPageReferenceSnapshotStatusView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin-references"],
        summary="[관리자] 레퍼런스 페이지 스냅샷 상태 폴링",
        description="""
어드민이 캡쳐 트리거 후 폴링하는 엔드포인트. 클라이언트는 2~5초 간격으로
`status` 가 `succeeded`/`failed` 가 될 때까지 호출하면 됩니다.

## 응답 의미
- `status`: `""` (없음) / `pending` / `running` / `succeeded` / `failed`
- `snapshot_url`: status=succeeded 일 때 절대 URL. 그 외엔 null.
- `error`: status=failed 일 때 사유 메시지.
        """,
        responses={
            200: AdminSnapshotStatusSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="페이지 없음"),
        },
    )
    def get(self, request, slug):
        page = get_object_or_404(Page, slug=slug)
        snapshot_url = None
        if page.reference_snapshot:
            url = page.reference_snapshot.url
            snapshot_url = (
                request.build_absolute_uri(url) if url.startswith("/") else url
            )
        data = {
            "job_id": page.reference_snapshot_job_id,
            "status": page.reference_snapshot_status,
            "error": page.reference_snapshot_error,
            "snapshot_url": snapshot_url,
            "updated_at": page.reference_snapshot_updated_at,
        }
        return Response(AdminSnapshotStatusSerializer(data).data)
