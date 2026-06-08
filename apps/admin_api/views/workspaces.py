"""apps/admin_api/views/workspaces.py — 어드민 워크스페이스 & 멤버십 뷰.

``/api/v1/admin/workspaces/`` 아래 크로스-워크스페이스(전역) 백오피스 엔드포인트.

- 권한: 모든 뷰 ``IsAdminUser``(is_staff=True). request.user 의 소속 워크스페이스로
  절대 필터링하지 않는다 (전역 운영 화면).
- 조회(GET)는 감사 로그를 남기지 않는다. 상태를 바꾸는 PATCH/DELETE 만 ``log_admin_action`` 적재.
- 오너 보호: 멤버 역할 변경/삭제 시 워크스페이스 오너의 멤버십은 보호한다.
- 보안: IG access_token 등 비밀값은 응답에 절대 포함하지 않는다.
"""

from __future__ import annotations

import logging

from django.db.models import Count
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog
from apps.admin_api.serializers.workspaces import (
    AdminMembershipUpdateSerializer,
    AdminWorkspaceDetailSerializer,
    AdminWorkspaceListSerializer,
    AdminWorkspaceUpdateSerializer,
)
from apps.workspace.models import Membership, Workspace

logger = logging.getLogger(__name__)


class AdminWorkspaceListView(generics.ListAPIView):
    """전역 워크스페이스 목록 (필터/검색/정렬)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminWorkspaceListSerializer
    queryset = (
        Workspace.objects.select_related("owner").annotate(members_count=Count("memberships")).all()
    )
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["plan"]
    search_fields = ["name", "slug", "owner__email"]
    ordering_fields = ["created_at", "name", "plan"]
    ordering = ["-created_at"]

    @extend_schema(
        tags=["admin-workspaces"],
        summary="[관리자] 워크스페이스 목록",
        description="""
## 개요
전체 테넌트(워크스페이스)를 워크스페이스 경계 없이 전역으로 조회한다. 각 항목에
오너 요약과 멤버 수(`members_count`, annotate)가 포함된다.

## 사용 시나리오
- 백오피스 대시보드에서 가입 워크스페이스 현황/요금제 분포를 점검할 때.
- 특정 고객사(이름/슬러그/오너 이메일)를 검색해 상세로 진입하기 전 목록 확인.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만)

## 비즈니스 로직
- `select_related(owner)` + `annotate(members_count=Count("memberships"))` 로 N+1 방지.
- 필터 `plan`(starter/pro/enterprise), 검색 `name`/`slug`/`owner__email`,
  정렬 `created_at`/`name`/`plan` (기본 `-created_at`).
- 응답은 표준 페이지네이션 `{count, next, previous, results}`.

## 주의사항
- 전역 조회이므로 호출자의 소속 워크스페이스로 절대 필터링하지 않는다.
- 비밀값(IG 토큰 등)은 포함되지 않는다.
        """,
        parameters=[
            OpenApiParameter(
                "plan",
                str,
                OpenApiParameter.QUERY,
                description="요금제 필터: starter / pro / enterprise",
                required=False,
            ),
            OpenApiParameter(
                "search",
                str,
                OpenApiParameter.QUERY,
                description="name / slug / owner__email 부분 검색",
                required=False,
            ),
            OpenApiParameter(
                "ordering",
                str,
                OpenApiParameter.QUERY,
                description="정렬: created_at / name / plan (앞에 - 붙이면 내림차순)",
                required=False,
            ),
            OpenApiParameter(
                "page",
                int,
                OpenApiParameter.QUERY,
                description="페이지 번호 (PAGE_SIZE=20)",
                required=False,
            ),
        ],
        responses={
            200: AdminWorkspaceListSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
        },
        examples=[
            OpenApiExample(
                "Pro 요금제 목록 응답",
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": "8f2e1c4a-1234-4abc-9def-0123456789ab",
                            "name": "ACME 마케팅",
                            "slug": "acme-marketing",
                            "owner": {"id": 7, "email": "owner@acme.com"},
                            "plan": "pro",
                            "members_count": 4,
                            "created_at": "2026-05-01T10:00:00+09:00",
                        }
                    ],
                },
                response_only=True,
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdminWorkspaceDetailView(generics.RetrieveUpdateAPIView):
    """워크스페이스 상세 조회 + 요금제/이름 수정."""

    permission_classes = [IsAdminUser]
    queryset = Workspace.objects.select_related("owner").all()
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.request.method == "PATCH":
            return AdminWorkspaceUpdateSerializer
        return AdminWorkspaceDetailSerializer

    @extend_schema(
        tags=["admin-workspaces"],
        summary="[관리자] 워크스페이스 상세",
        description="""
## 개요
단일 워크스페이스의 전체 운영 정보를 반환한다: 오너, 요금제, 설명, 멤버 목록,
오너 소유 페이지 수(`pages_count`), 연결된 IG 계정 요약(`ig_connections`).

## 사용 시나리오
- 목록에서 특정 워크스페이스 진입 시 전체 상태를 한 화면에서 확인.
- 멤버 구성/IG 연동/페이지 보유 여부를 점검하며 요금제 조정 판단.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만)

## 비즈니스 로직
- `members`: 각 멤버십의 `{membership_id, user, role, created_at}`.
- `pages_count`: `Page.objects.filter(user=workspace.owner).count()`.
- `ig_connections`: `{id, username, status}` 만 (access_token 등 비밀값 비노출).

## 주의사항
- 전역 조회 — 호출자 소속으로 필터링하지 않는다.
- 존재하지 않는 id 는 404.
        """,
        responses={
            200: AdminWorkspaceDetailSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="워크스페이스 없음"),
        },
        examples=[
            OpenApiExample(
                "상세 응답",
                value={
                    "id": "8f2e1c4a-1234-4abc-9def-0123456789ab",
                    "name": "ACME 마케팅",
                    "slug": "acme-marketing",
                    "description": "ACME 공식 인스타 운영",
                    "owner": {"id": 7, "email": "owner@acme.com"},
                    "plan": "pro",
                    "members_count": 2,
                    "members": [
                        {
                            "membership_id": "11111111-1111-1111-1111-111111111111",
                            "user": {
                                "id": 7,
                                "email": "owner@acme.com",
                                "full_name": "김오너",
                            },
                            "role": "owner",
                            "created_at": "2026-05-01T10:00:00+09:00",
                        }
                    ],
                    "pages_count": 3,
                    "ig_connections": [
                        {
                            "id": "22222222-2222-2222-2222-222222222222",
                            "username": "acme_official",
                            "status": "active",
                        }
                    ],
                    "created_at": "2026-05-01T10:00:00+09:00",
                },
                response_only=True,
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-workspaces"],
        summary="[관리자] 워크스페이스 수정",
        description="""
## 개요
워크스페이스의 요금제(`plan`)와 이름(`name`)을 강제 조정한다. partial update —
보낸 키만 적용된다.

## 사용 시나리오
- 영업/정산 결과에 따라 요금제를 수동으로 승급/강등.
- 잘못 입력된 워크스페이스 이름 정정.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만)

## 비즈니스 로직
- 변경 성공 후 `AdminActionLog`(action=workspace.update)에 before/after 감사 로그 적재.
- 응답은 갱신된 워크스페이스 상세(`AdminWorkspaceDetailSerializer`).

## 주의사항
- `slug`/`owner` 등은 이 엔드포인트로 변경할 수 없다.
- 존재하지 않는 id 는 404, 잘못된 plan 값은 400.
        """,
        request=AdminWorkspaceUpdateSerializer,
        responses={
            200: AdminWorkspaceDetailSerializer,
            400: OpenApiResponse(description="유효성 검증 실패 (예: 잘못된 plan)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="워크스페이스 없음"),
        },
        examples=[
            OpenApiExample(
                "요금제 승급 요청",
                value={"plan": "enterprise"},
                request_only=True,
            ),
            OpenApiExample(
                "수정 후 응답(요약)",
                value={
                    "id": "8f2e1c4a-1234-4abc-9def-0123456789ab",
                    "name": "ACME 마케팅",
                    "plan": "enterprise",
                },
                response_only=True,
            ),
        ],
    )
    def patch(self, request, *args, **kwargs):
        ws = self.get_object()
        before = {"plan": ws.plan, "name": ws.name}

        serializer = self.get_serializer(ws, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        ws.refresh_from_db()

        changes = {}
        for field in ("plan", "name"):
            after = getattr(ws, field)
            if before[field] != after:
                changes[field] = {"before": before[field], "after": after}

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.WORKSPACE_UPDATE,
            target_type="workspace",
            target_id=ws.id,
            target_repr=ws.name,
            changes=changes,
        )
        logger.info(
            "[admin-workspace-update] req=%s ws=%s changes=%s",
            getattr(request, "id", ""),
            ws.id,
            list(changes.keys()),
        )

        out = AdminWorkspaceDetailSerializer(ws, context=self.get_serializer_context())
        return Response(out.data, status=status.HTTP_200_OK)

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)


class AdminWorkspaceMemberDetailView(APIView):
    """워크스페이스 멤버십 역할 변경 / 제거."""

    permission_classes = [IsAdminUser]

    def _get_membership(self, workspace_id, membership_id) -> Membership:
        return get_object_or_404(
            Membership.objects.select_related("user", "workspace", "workspace__owner"),
            id=membership_id,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _is_workspace_owner(membership: Membership) -> bool:
        """이 멤버십이 해당 워크스페이스의 오너(소유자) 멤버십인지."""
        return (
            membership.role == Membership.Role.OWNER
            or membership.user_id == membership.workspace.owner_id
        )

    @extend_schema(
        tags=["admin-workspaces"],
        summary="[관리자] 멤버 역할 변경",
        description="""
## 개요
특정 워크스페이스의 멤버십 역할을 변경한다 (admin <-> member).

## 사용 시나리오
- 고객 요청/운영 정책에 따라 멤버 권한을 조정.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만)

## 비즈니스 로직
- 멤버십은 `(workspace_id, membership_id)` 조합으로 조회 — 불일치/없음은 404.
- 오너 보호: 대상이 워크스페이스 오너이면 400. 새 역할을 `owner` 로 올리는 것도 400.
- 변경 성공 후 `AdminActionLog`(action=membership.update)에 before/after 적재.

## 주의사항
- 오너 권한 이전은 이 엔드포인트로 처리하지 않는다 (별도 소유권 이전 절차 필요).
        """,
        request=AdminMembershipUpdateSerializer,
        responses={
            200: AdminMembershipUpdateSerializer,
            400: OpenApiResponse(description="오너 역할 변경 시도 또는 유효성 실패"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="멤버십 없음 (워크스페이스/멤버십 불일치)"),
        },
        examples=[
            OpenApiExample(
                "역할 변경 요청",
                value={"role": "admin"},
                request_only=True,
            ),
            OpenApiExample(
                "변경 후 응답",
                value={"role": "admin"},
                response_only=True,
            ),
        ],
    )
    def patch(self, request, workspace_id, membership_id):
        membership = self._get_membership(workspace_id, membership_id)

        if self._is_workspace_owner(membership):
            return Response(
                {"detail": "오너 역할은 변경할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AdminMembershipUpdateSerializer(membership, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        before_role = membership.role
        serializer.save()
        membership.refresh_from_db()

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.MEMBERSHIP_UPDATE,
            target_type="membership",
            target_id=membership.id,
            target_repr=f"{membership.user.email} @ {membership.workspace.name}",
            changes={"role": {"before": before_role, "after": membership.role}},
        )
        logger.info(
            "[admin-membership-update] req=%s membership=%s %s->%s",
            getattr(request, "id", ""),
            membership.id,
            before_role,
            membership.role,
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["admin-workspaces"],
        summary="[관리자] 멤버 제거",
        description="""
## 개요
특정 워크스페이스에서 멤버십을 제거한다.

## 사용 시나리오
- 퇴사/계약 종료 등으로 멤버를 워크스페이스에서 강제 제외할 때.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만)

## 비즈니스 로직
- 멤버십은 `(workspace_id, membership_id)` 조합으로 조회 — 불일치/없음은 404.
- 오너 보호: 대상이 워크스페이스 오너이면 400 (제거 불가).
- 삭제 성공 후 `AdminActionLog`(action=membership.delete) 적재, 204 반환.

## 주의사항
- 오너를 제거하려면 먼저 소유권을 다른 멤버에게 이전해야 한다.
        """,
        request=None,
        responses={
            204: OpenApiResponse(description="삭제 완료 (본문 없음)"),
            400: OpenApiResponse(description="오너 멤버십 삭제 시도"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="멤버십 없음"),
        },
    )
    def delete(self, request, workspace_id, membership_id):
        membership = self._get_membership(workspace_id, membership_id)

        if self._is_workspace_owner(membership):
            return Response(
                {"detail": "오너는 워크스페이스에서 제거할 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_id = membership.id
        target_repr = f"{membership.user.email} @ {membership.workspace.name}"
        removed_role = membership.role
        membership.delete()

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.MEMBERSHIP_DELETE,
            target_type="membership",
            target_id=target_id,
            target_repr=target_repr,
            changes={"role": {"before": removed_role, "after": None}},
        )
        logger.info(
            "[admin-membership-delete] req=%s membership=%s",
            getattr(request, "id", ""),
            target_id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
