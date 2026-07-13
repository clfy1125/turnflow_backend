"""apps/admin_api/views/referral.py — 어드민 레퍼럴 코드 관리 뷰.

라우팅: ``/api/v1/admin/referral-codes/`` 아래. 권한: ``IsAdminUser``(is_staff=True).
전역 스코프 — 특정 워크스페이스로 필터링하지 않는다(레퍼럴 코드는 서비스 전역 자원).

엔드포인트:
  - 목록/생성:      ``GET/POST  /api/v1/admin/referral-codes/``
  - 상세/수정/삭제: ``GET/PATCH/DELETE /api/v1/admin/referral-codes/<uuid:pk>/``
  - 사용 이력:      ``GET  /api/v1/admin/referral-codes/<uuid:pk>/redemptions/``

사용자용 레퍼럴 API(``apps/billing/referral_views.py`` — validate/redeem/my-status)와 완전히
분리된 백오피스 관리면이다. mutation(POST/PATCH/DELETE) 성공 후 ``log_admin_action`` 으로
감사 로그를 남긴다.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Count, ProtectedError, Q
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog
from apps.admin_api.serializers.referral import (
    AdminReferralCodeSerializer,
    AdminReferralCodeWriteSerializer,
    AdminReferralRedemptionSerializer,
)
from apps.billing.models import ReferralCode, ReferralRedemption

logger = logging.getLogger(__name__)

# 감사 changes 추적 대상 스칼라 필드 (target_plan 은 별도로 plan.name 비교).
_TRACKED_FIELDS = [
    "code",
    "description",
    "trial_days",
    "is_active",
    "max_uses",
    "valid_from",
    "valid_until",
]


def _annotated_qs():
    """redemptions_count / converted_count 집계를 얹은 기본 쿼리셋."""
    return (
        ReferralCode.objects.select_related("target_plan")
        .annotate(
            redemptions_count=Count("redemptions", distinct=True),
            converted_count=Count(
                "redemptions",
                filter=Q(redemptions__converted_to_paid=True),
                distinct=True,
            ),
        )
        .order_by("-created_at")
    )


class AdminReferralCodeListCreateView(generics.ListCreateAPIView):
    """레퍼럴 코드 목록 조회 + 신규 생성."""

    permission_classes = [IsAdminUser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active", "target_plan"]
    search_fields = ["code", "description"]
    ordering_fields = ["created_at", "current_uses", "valid_until", "trial_days"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return _annotated_qs()

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AdminReferralCodeWriteSerializer
        return AdminReferralCodeSerializer

    @extend_schema(
        tags=["admin-referral"],
        summary="[관리자] 레퍼럴 코드 목록 조회",
        description="""
## 개요
서비스 전역의 **레퍼럴 코드**를 페이지네이션하여 반환합니다. 각 항목은 부여 대상 플랜(중첩),
사용 제한/현황, 그리고 현재 사용 가능 여부(`is_redeemable`)를 포함합니다.

## 사용 시나리오
- 백오피스 "레퍼럴 코드 관리" 화면 첫 로딩
- 코드/설명 검색, 활성 여부·대상 플랜 필터, 사용횟수·만료일 정렬

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 응답 필드
| 필드 | 설명 |
|------|------|
| `code` | 대문자 정규화된 코드 |
| `target_plan` | 트라이얼로 부여될 플랜(중첩: id/name/display_name/…) |
| `trial_days` | 트라이얼 일수 |
| `is_active` | 활성 여부 |
| `max_uses` / `current_uses` | 최대(무제한이면 null) / 현재 사용 횟수 |
| `valid_from` / `valid_until` | 사용 가능 기간(null 이면 제한 없음) |
| `redemptions_count` | 이 코드로 시작된 총 트라이얼 수 |
| `converted_count` | 그중 유료 전환된 수 |
| `is_redeemable` / `redeemable_reason` | 현재 사용 가능 여부 + 불가 사유 |

## 주의사항
- 응답은 `{count,next,previous,results}` 형태(PAGE_SIZE=20)입니다.
        """,
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="활성 여부 필터 (true/false).",
            ),
            OpenApiParameter(
                name="target_plan",
                type=str,
                location=OpenApiParameter.QUERY,
                description="대상 플랜 UUID 필터 (SubscriptionPlan.id).",
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description="code / description 부분 일치 검색.",
            ),
            OpenApiParameter(
                name="ordering",
                type=str,
                location=OpenApiParameter.QUERY,
                description="정렬 (created_at/current_uses/valid_until/trial_days, '-' 내림차순).",
            ),
        ],
        responses={
            200: AdminReferralCodeSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                response_only=True,
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": "f1e2d3c4-0000-0000-0000-000000000001",
                            "code": "WELCOME2026",
                            "description": "신규 가입 웰컴 프로모션",
                            "target_plan": {
                                "id": "550e8400-e29b-41d4-a716-446655440002",
                                "name": "pro",
                                "display_name": "프로",
                                "monthly_price": 14900,
                                "features": {"max_pages": -1},
                                "sort_order": 1,
                                "is_active": True,
                            },
                            "trial_days": 30,
                            "is_active": True,
                            "max_uses": 100,
                            "current_uses": 12,
                            "valid_from": None,
                            "valid_until": "2026-12-31T14:59:59Z",
                            "redemptions_count": 12,
                            "converted_count": 5,
                            "is_redeemable": True,
                            "redeemable_reason": "",
                            "created_at": "2026-07-01T09:00:00Z",
                            "updated_at": "2026-07-10T09:00:00Z",
                        }
                    ],
                },
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-referral"],
        summary="[관리자] 레퍼럴 코드 생성",
        description="""
## 개요
신규 레퍼럴 코드를 생성합니다. 사용자는 이 코드를 입력해 **결제 없이** `target_plan` 트라이얼을
시작할 수 있습니다(1인 1회).

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `code` | ✅ | string | 코드. 대소문자 무시(대문자 저장), 영문·숫자·`-`·`_` 2~50자 |
| `target_plan` | ✅ | uuid | 부여할 플랜(SubscriptionPlan.id). 보통 pro |
| `trial_days` | ❌ | int | 트라이얼 일수 1~3650 (기본 30) |
| `description` | ❌ | string | 내부 메모 (최대 200자) |
| `is_active` | ❌ | bool | 활성 여부 (기본 true) |
| `max_uses` | ❌ | int/null | 최대 사용 횟수 (null=무제한, 기본 무제한) |
| `valid_from` | ❌ | datetime/null | 사용 시작 시각 (null=즉시) |
| `valid_until` | ❌ | datetime/null | 사용 종료 시각 (null=무기한) |

## 검증
- 코드는 정규화(대문자) 후 **대소문자 무시 중복 불가** → 중복 시 400.
- `valid_until` 은 `valid_from` 이후여야 함.

## 응답
- 생성된 코드를 목록/상세와 동일한 읽기 형태(중첩 plan + 집계 포함)로 반환합니다.
- 성공 시 `AdminActionLog(referral.create)` 으로 감사 기록합니다.
        """,
        request=AdminReferralCodeWriteSerializer,
        responses={
            201: AdminReferralCodeSerializer,
            400: OpenApiResponse(description="검증 실패 (코드 형식/중복, 기간 역전, 플랜 미존재 등)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
        },
        examples=[
            OpenApiExample(
                "요청 예시",
                request_only=True,
                value={
                    "code": "welcome2026",
                    "target_plan": "550e8400-e29b-41d4-a716-446655440002",
                    "trial_days": 30,
                    "description": "신규 가입 웰컴 프로모션",
                    "max_uses": 100,
                    "valid_until": "2026-12-31T23:59:59+09:00",
                },
            ),
        ],
    )
    def post(self, request, *args, **kwargs):
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        code = write.save()

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.REFERRAL_CREATE,
            target_type="referral_code",
            target_id=code.pk,
            target_repr=code.code,
            changes={
                "code": {"before": None, "after": code.code},
                "target_plan": {"before": None, "after": code.target_plan.name},
                "trial_days": {"before": None, "after": code.trial_days},
            },
        )
        logger.info(
            "[admin-referral] req=%s 코드 생성 code=%s plan=%s trial=%sd",
            getattr(request, "id", ""),
            code.code,
            code.target_plan.name,
            code.trial_days,
        )

        read = AdminReferralCodeSerializer(code, context=self.get_serializer_context())
        return Response(read.data, status=status.HTTP_201_CREATED)


class AdminReferralCodeDetailView(generics.RetrieveUpdateDestroyAPIView):
    """레퍼럴 코드 단건 상세 조회 + 부분 수정 + 삭제."""

    permission_classes = [IsAdminUser]
    lookup_field = "pk"

    def get_queryset(self):
        return _annotated_qs()

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return AdminReferralCodeWriteSerializer
        return AdminReferralCodeSerializer

    @extend_schema(
        tags=["admin-referral"],
        summary="[관리자] 레퍼럴 코드 상세 조회",
        description="""
## 개요
단일 레퍼럴 코드의 상세를 반환합니다. 목록 항목과 동일한 형태(중첩 plan + 집계 + 사용가능 여부)입니다.
코드별 **사용 이력**은 `GET /admin/referral-codes/{id}/redemptions/` 로 조회하세요.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)
        """,
        responses={
            200: AdminReferralCodeSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 코드 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-referral"],
        summary="[관리자] 레퍼럴 코드 수정",
        description="""
## 개요
레퍼럴 코드를 부분 수정합니다(PATCH). 보낸 필드만 갱신됩니다.

## 사용 시나리오
- 프로모션 조기 종료(`is_active=false`) / 기간·한도 조정 / 대상 플랜·설명 변경

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 요청 필드
생성과 동일(전부 선택). 자주 쓰는 값:
| 필드 | 설명 |
|------|------|
| `is_active` | 활성/비활성 토글 (프로모션 on/off) |
| `max_uses` | 최대 사용 횟수 — **이미 사용된 횟수 미만으로는 낮출 수 없음** |
| `valid_until` | 종료 시각 조정 |
| `target_plan` | 부여 플랜 변경 (SubscriptionPlan.id) |

## 검증
- 코드 변경 시 대소문자 무시 중복 불가.
- `max_uses < current_uses` 이면 400.
- `valid_until < valid_from` 이면 400.

## 응답
- 수정된 코드를 읽기 형태로 반환하고, 바뀐 필드의 before/after 를 `AdminActionLog(referral.update)`
  로 감사 기록합니다.

## 주의사항
- PUT 은 비활성화되어 있습니다 — 부분 수정은 PATCH 만 사용하세요.
        """,
        request=AdminReferralCodeWriteSerializer,
        responses={
            200: AdminReferralCodeSerializer,
            400: OpenApiResponse(description="검증 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 코드 없음"),
        },
        examples=[
            OpenApiExample("요청 예시 (비활성화)", request_only=True, value={"is_active": False}),
            OpenApiExample(
                "요청 예시 (한도 상향)", request_only=True, value={"max_uses": 500}
            ),
        ],
    )
    def patch(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        code = self.get_object()
        before = {f: getattr(code, f) for f in _TRACKED_FIELDS}
        before_plan = code.target_plan.name

        serializer = self.get_serializer(code, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        code.refresh_from_db()

        changes = {
            f: {"before": _json_safe(before[f]), "after": _json_safe(getattr(code, f))}
            for f in _TRACKED_FIELDS
            if before[f] != getattr(code, f)
        }
        if code.target_plan.name != before_plan:
            changes["target_plan"] = {"before": before_plan, "after": code.target_plan.name}

        if changes:
            log_admin_action(
                request=request,
                action=AdminActionLog.Action.REFERRAL_UPDATE,
                target_type="referral_code",
                target_id=code.pk,
                target_repr=code.code,
                changes=changes,
            )
            logger.info(
                "[admin-referral] req=%s 코드 수정 code=%s fields=%s",
                getattr(request, "id", ""),
                code.code,
                list(changes.keys()),
            )

        read = AdminReferralCodeSerializer(
            _annotated_qs().get(pk=code.pk), context=self.get_serializer_context()
        )
        return Response(read.data)

    @extend_schema(
        tags=["admin-referral"],
        summary="[관리자] 레퍼럴 코드 삭제",
        description="""
## 개요
레퍼럴 코드를 **영구 삭제**합니다.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직 (안전장치)
- **사용 이력(트라이얼)이 1건이라도 있으면 삭제할 수 없습니다** → **409**.
  사용 이력은 유료 전환/어트리뷰션 추적의 근거라 보존해야 하므로, 이런 코드는
  삭제 대신 `PATCH {"is_active": false}` 로 **비활성화**하세요.
- 사용 이력이 없는 코드만 하드 삭제되며, 성공 시 `AdminActionLog(referral.delete)` 로 기록합니다.

## 응답
- 204 No Content (삭제 완료)
        """,
        request=None,
        responses={
            204: OpenApiResponse(description="삭제 완료"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 코드 없음"),
            409: OpenApiResponse(description="사용 이력이 있어 삭제 불가 (비활성화 권장)"),
        },
        examples=[
            OpenApiExample(
                "409 예시",
                response_only=True,
                value={
                    "detail": "이미 12회 사용된 코드입니다. 삭제 대신 비활성화(is_active=false)하세요.",
                    "code": "referral_has_redemptions",
                },
            )
        ],
    )
    def delete(self, request, *args, **kwargs):
        return self.destroy(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        obj_pk = self.get_object().pk

        # 코드 행을 잠근 채로 사용 여부 확인 + 삭제를 한 트랜잭션에서 수행한다.
        # select_for_update 가 사용자용 redeem 흐름(referral_views: 같은 코드 select_for_update)과
        # 직렬화되어 count()==0 검사와 delete() 사이의 동시 사용(TOCTOU)을 차단한다.
        # 그래도 남는 경합은 ProtectedError 로 잡아 문서화된 409 로 되돌린다(500 방지).
        with transaction.atomic():
            code = ReferralCode.objects.select_for_update().get(pk=obj_pk)
            used = code.redemptions.count()
            if used > 0:
                return Response(
                    {
                        "detail": (
                            f"이미 {used}회 사용된 코드입니다. 삭제 대신 "
                            "비활성화(is_active=false)하세요."
                        ),
                        "code": "referral_has_redemptions",
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            code_value = code.code
            code_pk = code.pk
            try:
                code.delete()
            except ProtectedError:
                return Response(
                    {
                        "detail": (
                            "사용 이력이 생겨 삭제할 수 없습니다. "
                            "비활성화(is_active=false)하세요."
                        ),
                        "code": "referral_has_redemptions",
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.REFERRAL_DELETE,
            target_type="referral_code",
            target_id=code_pk,
            target_repr=code_value,
        )
        logger.info(
            "[admin-referral] req=%s 코드 삭제 code=%s", getattr(request, "id", ""), code_value
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminReferralCodeRedemptionsView(generics.ListAPIView):
    """특정 레퍼럴 코드의 사용 이력 목록."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminReferralRedemptionSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["converted_to_paid"]
    search_fields = ["user__email", "user__full_name"]
    ordering_fields = ["created_at", "trial_ends_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        # drf-spectacular 스키마 생성 시(swagger_fake_view)엔 URL kwargs 가 없다.
        if getattr(self, "swagger_fake_view", False):
            return ReferralRedemption.objects.none()
        code = get_object_or_404(ReferralCode, pk=self.kwargs["pk"])
        return ReferralRedemption.objects.filter(referral_code=code).select_related("user")

    @extend_schema(
        tags=["admin-referral"],
        summary="[관리자] 레퍼럴 코드 사용 이력 조회",
        description="""
## 개요
특정 레퍼럴 코드로 시작된 **트라이얼 사용 이력**을 페이지네이션하여 반환합니다.
누가·언제 트라이얼을 시작했고, 유료로 전환됐는지 추적합니다.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 필터/검색
- `?converted_to_paid=true|false` — 유료 전환 여부
- `?search=` — 사용자 email / 이름 부분 일치
- `?ordering=` — created_at / trial_ends_at

## 응답 필드
| 필드 | 설명 |
|------|------|
| `user_id` / `user_email` / `user_full_name` | 사용 회원 |
| `trial_started_at` / `trial_ends_at` | 트라이얼 기간 |
| `converted_to_paid` / `converted_at` | 유료 전환 여부·시각 |
| `is_trial_active` | 현재 트라이얼 유효(종료 전 + 미전환) 여부 |
        """,
        parameters=[
            OpenApiParameter(
                name="converted_to_paid",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="유료 전환 여부 필터 (true/false).",
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description="사용자 email / full_name 부분 일치 검색.",
            ),
        ],
        responses={
            200: AdminReferralRedemptionSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 코드 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


def _json_safe(value):
    """datetime 등 비-JSON 값을 감사 로그(JSONField)에 안전한 형태로 변환."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
