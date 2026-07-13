"""apps/admin_api/views/users.py — 어드민 회원(계정) 관리 뷰.

라우팅: ``/api/v1/admin/users/`` 아래. 권한: ``IsAdminUser``(is_staff=True).
전역(cross-workspace) 스코프 — request.user 의 워크스페이스로 절대 필터링하지 않는다.

엔드포인트:
  - 회원 목록:        ``GET   /api/v1/admin/users/``
  - 회원 상세/수정:   ``GET/PATCH /api/v1/admin/users/<int:pk>/``
  - 비밀번호 재설정:  ``POST  /api/v1/admin/users/<int:pk>/password-reset/``

mutation(PATCH/POST) 성공 후에는 ``log_admin_action`` 으로 감사 로그를 남긴다.
``is_staff`` 변경처럼 권한 상승성 동작은 슈퍼유저만 허용한다.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog
from apps.admin_api.serializers.users import (
    AdminUserDetailSerializer,
    AdminUserListSerializer,
    AdminUserSubscriptionSerializer,
    AdminUserSubscriptionUpdateSerializer,
    AdminUserUpdateSerializer,
)
from apps.emails.tasks import send_password_reset_email

logger = logging.getLogger(__name__)

User = get_user_model()


class AdminUserListView(generics.ListAPIView):
    """전체 회원 목록 (전역 스코프, 페이지네이션 유지)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminUserListSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["is_active", "is_email_verified", "is_staff"]
    search_fields = ["email", "full_name"]
    ordering_fields = ["date_joined", "last_login", "email"]
    ordering = ["-date_joined"]

    def get_queryset(self):
        # workspace_count(멤버십 수) / pages_count(소유 페이지 수)는 annotate 로 집계해
        # 시리얼라이저 N+1 을 피한다. subscription 블록은 select_related 로 prefetch.
        # plan(레거시) / ig_connections_count 는 SerializerMethodField.
        qs = (
            User.objects.all()
            .annotate(
                workspace_count=Count("memberships", distinct=True),
                pages_count=Count("pages", distinct=True),
            )
            .select_related("subscription__plan")
            .prefetch_related("owned_workspaces")
        )
        # ?plan= 은 **실제 구독**(UserSubscription.plan.name) 기준 필터.
        # (레거시 Workspace.plan 이 아님 — 콘솔 플랜 컬럼/필터 정합성 교정.)
        plan = self.request.query_params.get("plan")
        if plan:
            qs = qs.filter(subscription__plan__name=plan)
        return qs

    @extend_schema(
        tags=["admin-users"],
        summary="[관리자] 회원 목록 조회",
        description="""
## 개요
서비스에 가입한 **전체 회원(계정)** 을 페이지네이션하여 반환합니다. 특정 워크스페이스에
국한되지 않는 전역(cross-workspace) 관리용 목록입니다.

## 사용 시나리오
- 백오피스 회원 관리 화면 진입 시 첫 로딩
- 이메일/이름 검색, 활성/인증/스태프 여부 필터링, 요금제별 조회
- 가입일·마지막 로그인 기준 정렬

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- `workspace_count`(소속 멤버십 수), `pages_count`(소유 페이지 수)는 annotate 집계.
- `subscription` 은 회원의 **실제 구독**(UserSubscription→SubscriptionPlan) 요약:
  `{plan_name, plan_display_name, status, current_period_end}`. 구독 레코드가 없으면 `null`.
- `plan`(⚠️ **DEPRECATED**)은 레거시 Workspace.plan(starter/pro/enterprise) 중 최상위 —
  **실제 과금과 무관**하므로 신규 코드는 `subscription.plan_name` 을 쓰세요.
- `ig_connections_count` 는 회원 소유 워크스페이스에 연결된 IG 계정 수.
- `?plan=` 필터는 **실제 구독 plan_name**(free/pro/admin…) 기준으로 필터링합니다.

## 주의사항
- 응답은 `{count,next,previous,results}` 형태(PAGE_SIZE=20)입니다.
- IG access_token 등 비밀 값은 절대 포함되지 않습니다 (카운트만 노출).
        """,
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="활성 계정 여부 필터 (true/false).",
                required=False,
            ),
            OpenApiParameter(
                name="is_email_verified",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="이메일 인증 여부 필터 (true/false).",
                required=False,
            ),
            OpenApiParameter(
                name="is_staff",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="스태프(어드민) 계정 여부 필터 (true/false).",
                required=False,
            ),
            OpenApiParameter(
                name="plan",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "실제 구독 요금제 필터 — UserSubscription.plan.name 기준 "
                    "(free/pro/admin…). 레거시 Workspace.plan 이 아님."
                ),
                required=False,
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description="email / full_name 부분 일치 검색.",
                required=False,
            ),
            OpenApiParameter(
                name="ordering",
                type=str,
                location=OpenApiParameter.QUERY,
                description="정렬 필드 (date_joined/last_login/email, '-' 접두로 내림차순).",
                required=False,
            ),
        ],
        responses={
            200: AdminUserListSerializer(many=True),
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
                            "id": 42,
                            "email": "user@example.com",
                            "full_name": "홍길동",
                            "is_active": True,
                            "is_email_verified": True,
                            "is_staff": False,
                            "date_joined": "2026-01-10T09:00:00+09:00",
                            "last_login": "2026-06-01T18:30:00+09:00",
                            "workspace_count": 2,
                            "subscription": {
                                "plan_name": "pro",
                                "plan_display_name": "프로",
                                "status": "active",
                                "current_period_end": "2026-07-01T00:00:00+09:00",
                            },
                            "plan": "starter",
                            "pages_count": 3,
                            "ig_connections_count": 1,
                        }
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdminUserDetailView(generics.RetrieveUpdateAPIView):
    """회원 단건 상세 조회 + 부분 수정 (전역 스코프)."""

    permission_classes = [IsAdminUser]
    queryset = (
        User.objects.all()
        .select_related("subscription__plan")
        .prefetch_related("owned_workspaces", "memberships__workspace", "pages")
    )
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return AdminUserUpdateSerializer
        return AdminUserDetailSerializer

    @extend_schema(
        tags=["admin-users"],
        summary="[관리자] 회원 상세 조회",
        description="""
## 개요
단일 회원의 상세 정보를 반환합니다. 목록 필드에 더해 소유 워크스페이스, 소속 멤버십,
소유 페이지, IG 연동(비밀 토큰 제외), 캠페인 수를 중첩 포함합니다.

## 사용 시나리오
- 백오피스에서 특정 회원 카드를 펼쳐볼 때
- 계정 상태(활성/인증/스태프) 변경 직전 현황 확인

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- `subscription`: 회원의 **실제 구독** {plan_name, plan_display_name, status, current_period_end}.
  구독 레코드 없으면 null. 등급 변경은 `PATCH /admin/users/{id}/subscription/`.
- `plan`(⚠️ **DEPRECATED**): 레거시 Workspace.plan 기준 — 실제 과금과 무관.
- `owned_workspaces`: 회원이 owner 인 워크스페이스 [{id,name,plan,members_count}].
- `memberships`: 회원이 속한 멤버십 [{workspace_id,workspace_name,role}].
- `pages`: 소유 페이지 [{slug,title,is_public,is_active}].
- `ig_connections`: 소유 워크스페이스의 IG 연동 [{id,username,status,token_expires_at}].
- `campaigns_count`: 소유 워크스페이스의 자동 DM 캠페인 총 수.

## 주의사항
- IG access_token 등 비밀 값은 절대 직렬화되지 않습니다 (status/만료시각만).
        """,
        responses={
            200: AdminUserDetailSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 회원 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-users"],
        summary="[관리자] 회원 정보 수정",
        description="""
## 개요
회원의 계정 플래그를 부분 수정합니다. 보낸 필드만 갱신됩니다.

## 사용 시나리오
- 계정 비활성화/복구(`is_active`), 이메일 인증 강제 처리(`is_email_verified`)
- 표시 이름 정정(`full_name`)
- 스태프 권한 부여/회수(`is_staff`) — **슈퍼유저만**

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)
- `is_staff` 변경은 추가로 슈퍼유저(is_superuser=True) 여야 합니다.

## 비즈니스 로직
- `is_staff` 가 요청 본문에 포함됐는데 호출자가 슈퍼유저가 아니면 **403** 으로 거부합니다.
- 저장 성공 후 변경된 필드의 before/after 를 `AdminActionLog(user.update)` 로 감사 기록합니다.

## 주의사항
- PUT 은 비활성화되어 있습니다. 부분 수정은 PATCH 만 사용하세요.
- 자기 자신의 권한을 회수할 때 잠금 가능성에 유의하세요(서버는 막지 않습니다).
        """,
        request=AdminUserUpdateSerializer,
        responses={
            200: AdminUserDetailSerializer,
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(
                description="관리자 권한 없음, 또는 is_staff 변경을 슈퍼유저가 아닌 계정이 시도"
            ),
            404: OpenApiResponse(description="해당 회원 없음"),
        },
        examples=[
            OpenApiExample(
                "요청 예시 (계정 비활성화)",
                value={"is_active": False},
                request_only=True,
            ),
            OpenApiExample(
                "요청 예시 (스태프 권한 부여 — 슈퍼유저만)",
                value={"is_staff": True},
                request_only=True,
            ),
        ],
    )
    def patch(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        # is_staff 변경은 권한 상승성 동작 — 슈퍼유저만 허용.
        if "is_staff" in request.data and not request.user.is_superuser:
            return Response(
                {"detail": "is_staff 변경은 슈퍼유저만 가능합니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        user = self.get_object()
        tracked = ["is_active", "is_email_verified", "full_name", "is_staff"]
        before = {f: getattr(user, f) for f in tracked}

        serializer = self.get_serializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        user.refresh_from_db()

        # 실제로 바뀐 필드만 changes 에 기록.
        changes = {
            f: {"before": before[f], "after": getattr(user, f)}
            for f in tracked
            if before[f] != getattr(user, f)
        }
        if changes:
            log_admin_action(
                request=request,
                action=AdminActionLog.Action.USER_UPDATE,
                target_type="user",
                target_id=user.pk,
                target_repr=user.email,
                changes=changes,
            )
            logger.info(
                "[admin-users] req=%s user=%s 수정 fields=%s",
                getattr(request, "id", ""),
                user.email,
                list(changes.keys()),
            )

        return Response(AdminUserDetailSerializer(user, context=self.get_serializer_context()).data)


class AdminUserPasswordResetView(APIView):
    """회원에게 비밀번호 재설정 메일 발송 (어드민 트리거)."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin-users"],
        summary="[관리자] 비밀번호 재설정 발송",
        description="""
## 개요
대상 회원에게 비밀번호 재설정 메일을 발송하도록 트리거합니다. 실제 발송은 Celery
태스크(`emails.send_password_reset_email`)가 비동기로 처리합니다.

## 사용 시나리오
- 고객 문의 대응 시 운영자가 대신 재설정 메일을 보내야 할 때

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 비즈니스 로직
- 회원이 **활성(is_active)** 이고 **사용 가능한 비밀번호(has_usable_password)** 를 가진 경우에만
  재설정 메일 태스크를 큐잉합니다 (소셜 전용 계정 등은 큐잉하지 않음).
- 계정 상태와 무관하게 **항상 200** 으로 동일 응답하여 계정 존재/상태를 드러내지 않습니다.
- 발송 트리거 후 `AdminActionLog(user.password_reset)` 으로 감사 기록합니다.

## 주의사항
- 요청 본문은 없습니다 (대상은 URL 의 pk).
- 메일 실제 전달 성공 여부는 이 응답이 보장하지 않습니다 (비동기 처리).
        """,
        request=None,
        responses={
            200: OpenApiResponse(
                description="발송 트리거 완료 (항상 동일 응답)",
                response={
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                },
            ),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 회원 없음"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                value={"detail": "비밀번호 재설정 메일을 발송했습니다."},
                response_only=True,
            )
        ],
    )
    def post(self, request, pk: int):
        user = get_object_or_404(User, pk=pk)

        if user.is_active and user.has_usable_password():
            send_password_reset_email.delay(user.id)
            logger.info(
                "[admin-users] req=%s user=%s 비밀번호 재설정 메일 큐잉",
                getattr(request, "id", ""),
                user.email,
            )

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.USER_PASSWORD_RESET,
            target_type="user",
            target_id=user.pk,
            target_repr=user.email,
        )

        return Response(
            {"detail": "비밀번호 재설정 메일을 발송했습니다."},
            status=status.HTTP_200_OK,
        )


class AdminUserSubscriptionUpdateView(APIView):
    """회원의 실제 구독(요금제) 등급을 **결제 없이** 강제 조정 (CS 보상/수기 부여/강등)."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin-users"],
        summary="[관리자] 회원 구독(요금제) 강제 변경",
        description="""
## 개요
대상 회원의 **실제 구독**(`UserSubscription`)의 플랜을 **결제 없이** 즉시 교체합니다.
CS 보상, 수기 부여, 강등 등 운영 조정 용도입니다. 사용자 본인 흐름(`POST /billing/change-plan/`,
토스 결제 동반)과는 **완전히 분리**되어 있습니다.

## 사용 시나리오
- 무료 회원에게 프로를 수기 부여 / 잘못 결제된 회원 강등 / 내부 `admin` 플랜 부여

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 요청 (둘 중 **정확히 하나**)
| 필드 | 타입 | 설명 |
|------|------|------|
| `plan` | string | SubscriptionPlan.name (예: `free` / `basic` / `pro` / `admin`) |
| `plan_id` | uuid | SubscriptionPlan.id |

- 비활성(is_active=False) 플랜(예: 운영용 `admin`)도 어드민은 부여 가능 — 여기선 is_active 로 막지 않습니다.

## 비즈니스 로직 (강제 변경 정책)
- 구독 레코드가 **없으면 생성**, 있으면 플랜 교체.
- `status="active"`, `current_period_start=now`, `current_period_end=null`(무기한 — 결제 주기가 없으므로),
  `cancelled_at=null` 로 설정합니다.
- **토스 빌링키는 건드리지 않습니다.** 갱신 스케줄러는 `current_period_end`가 없는 구독을
  과금하지 않으므로 수기 부여 회원에게 자동 청구가 나가지 않습니다. 결제 중이던 회원을
  강등하면 빌링키가 남지만 주기가 제거돼 더 이상 과금되지 않습니다(서버가 로그를 남김).
- AI 토큰 잔액은 **자동 지급/회수하지 않습니다**(등급만 조정). 필요 시 토큰은 별도 조정하세요.
- 변경 성공 시 `AdminActionLog(user.subscription_update)` 로 감사 기록합니다.

## 주의사항
- PATCH 만 지원합니다. 본문은 `plan` 또는 `plan_id` 중 하나만 보내세요(둘 다/둘 다 없음 → 400).
- 응답은 변경 후 구독 요약 블록입니다(목록/상세의 `subscription` 필드와 동일 형태).
        """,
        request=AdminUserSubscriptionUpdateSerializer,
        responses={
            200: AdminUserSubscriptionSerializer,
            400: OpenApiResponse(description="plan/plan_id 누락·중복·미존재 등 검증 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 회원 없음"),
        },
        examples=[
            OpenApiExample("요청 예시 (name 으로)", value={"plan": "pro"}, request_only=True),
            OpenApiExample(
                "요청 예시 (id 로)",
                value={"plan_id": "550e8400-e29b-41d4-a716-446655440002"},
                request_only=True,
            ),
            OpenApiExample(
                "응답 예시",
                value={
                    "plan_name": "pro",
                    "plan_display_name": "프로",
                    "status": "active",
                    "current_period_end": None,
                },
                response_only=True,
            ),
        ],
    )
    def patch(self, request, pk: int):
        from apps.billing.models import SubscriptionStatus
        from apps.billing.subscription_utils import ensure_subscription

        user = get_object_or_404(User, pk=pk)

        serializer = AdminUserSubscriptionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_plan = serializer.validated_data["plan_obj"]

        sub = ensure_subscription(user)
        before = {"plan_name": sub.plan.name, "status": sub.status}

        # 강제 변경은 결제 주기를 제거하므로(period_end=null) 갱신 과금은 멈춘다.
        # 빌링키가 남아있으면 추후 사용자 본인 결제 흐름에서 재사용 가능 — 정보 로그만.
        if sub.has_billing_key:
            logger.info(
                "[admin-users] req=%s user=%s 구독 강제 변경 — 토스 빌링키 잔존 "
                "(주기 제거로 자동 과금 없음)",
                getattr(request, "id", ""),
                user.email,
            )

        sub.plan = new_plan
        sub.status = SubscriptionStatus.ACTIVE
        sub.current_period_start = timezone.now()
        sub.current_period_end = None  # 무기한 — 결제 주기 없는 어드민 수기 부여
        sub.cancelled_at = None
        # 수기 변경은 대기 중이던 예약(플랜/추가계정 축소)을 무효화한다.
        sub.pending_plan = None
        sub.pending_amount_snapshot = None
        sub.pending_extra_ig_accounts = None
        # pro 가 아니면 추가 IG 계정 슬롯 개념이 없으므로 0 으로 리셋 — 허용량 부풀림 방지
        # (갱신/무료강등 경로와 동일 관례). pro 로의 수기 부여면 기존 extra 는 유지.
        if new_plan.name != "pro":
            sub.extra_ig_accounts = 0
        sub.save(
            update_fields=[
                "plan",
                "status",
                "current_period_start",
                "current_period_end",
                "cancelled_at",
                "pending_plan",
                "pending_amount_snapshot",
                "pending_extra_ig_accounts",
                "extra_ig_accounts",
                "updated_at",
            ]
        )

        # 허용량이 줄었으면 활성 IG 계정 초과분 자동 비활성 + 재선택 유도 (갱신 경로와 동일).
        # 대상이 staff/superuser 면 무제한이라 no-op.
        try:
            from apps.billing.tasks import _enforce_ig_activation_after_renewal

            _enforce_ig_activation_after_renewal(sub)
        except Exception:
            logger.exception(
                "[admin-users] req=%s user=%s IG 활성 계정 자동 조정 실패(non-fatal)",
                getattr(request, "id", ""),
                user.email,
            )

        after = {"plan_name": sub.plan.name, "status": sub.status}
        log_admin_action(
            request=request,
            action=AdminActionLog.Action.USER_SUBSCRIPTION_UPDATE,
            target_type="user",
            target_id=user.pk,
            target_repr=user.email,
            changes={
                "plan_name": {"before": before["plan_name"], "after": after["plan_name"]},
                "status": {"before": before["status"], "after": after["status"]},
            },
        )
        logger.info(
            "[admin-users] req=%s user=%s 구독 변경 %s → %s",
            getattr(request, "id", ""),
            user.email,
            before["plan_name"],
            after["plan_name"],
        )

        block = {
            "plan_name": sub.plan.name,
            "plan_display_name": sub.plan.display_name,
            "status": sub.status,
            "current_period_end": sub.current_period_end,
        }
        return Response(AdminUserSubscriptionSerializer(block).data, status=status.HTTP_200_OK)
