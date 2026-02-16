"""
Workspace and Membership views
"""

from rest_framework import status, generics, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from django.shortcuts import get_object_or_404

from .models import Workspace, Membership, WorkspaceInvitation
from .serializers import (
    WorkspaceSerializer,
    WorkspaceCreateSerializer,
    MembershipSerializer,
    MembershipUpdateSerializer,
    WorkspaceInvitationSerializer,
    WorkspaceInvitationCreateSerializer,
)
from .permissions import IsWorkspaceMember, IsWorkspaceAdmin, IsWorkspaceOwner


class WorkspaceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Workspace CRUD operations
    """

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Get workspaces where user is a member"""
        return Workspace.objects.filter(memberships__user=self.request.user).distinct()

    def get_serializer_class(self):
        """Return appropriate serializer class"""
        if self.action == "create":
            return WorkspaceCreateSerializer
        return WorkspaceSerializer

    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ["update", "partial_update"]:
            return [IsAuthenticated(), IsWorkspaceAdmin()]
        elif self.action == "destroy":
            return [IsAuthenticated(), IsWorkspaceOwner()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="워크스페이스 목록 조회",
        description="""
        ## 목적
        현재 로그인한 사용자가 속한 모든 워크스페이스 목록을 조회합니다.
        
        ## 사용 시나리오
        - 사용자가 속한 워크스페이스 목록을 표시할 때
        - 워크스페이스 선택 드롭다운을 구성할 때
        - 대시보드 초기 화면 구성 시
        
        ## 인증
        - **Bearer 토큰 필수**
        - 로그인한 사용자만 접근 가능
        
        ## 응답 데이터
        - 사용자가 멤버로 속한 모든 워크스페이스 목록 (owner, admin, member 역할 포함)
        - 각 워크스페이스의 멤버 수 포함
        
        ## 주의사항
        - 본인이 속하지 않은 워크스페이스는 조회되지 않습니다
        - 역할과 관계없이 모든 워크스페이스가 조회됩니다
        
        ## 사용 예시
        ```javascript
        const accessToken = localStorage.getItem('access_token');
        
        const response = await fetch('http://localhost:8000/api/v1/workspaces/', {
            headers: {
                'Authorization': `Bearer ${accessToken}`
            }
        });
        
        const workspaces = await response.json();
        // [{ id: '...', name: 'My Workspace', member_count: 5, ... }, ...]
        ```
        
        ```bash
        curl -X GET http://localhost:8000/api/v1/workspaces/ \\
          -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
        ```
        """,
        responses={
            200: WorkspaceSerializer(many=True),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="워크스페이스 생성",
        description="""
        ## 목적
        새로운 워크스페이스를 생성하고 생성자를 자동으로 Owner 역할로 등록합니다.
        
        ## 사용 시나리오
        - 신규 조직/팀 생성 시
        - 새 프로젝트 시작 시
        - 고객사별 워크스페이스 설정 시
        
        ## 인증
        - **Bearer 토큰 필수**
        - 로그인한 사용자만 생성 가능
        
        ## 요청 필드
        - `name` (필수): 워크스페이스 이름
        - `description` (선택): 워크스페이스 설명
        
        ## 자동 처리
        - `slug`: name으로부터 자동 생성 (중복 시 숫자 추가)
        - `owner`: 현재 로그인한 사용자로 자동 설정
        - `Membership`: 생성자를 owner 역할로 자동 등록
        
        ## 응답 데이터
        - 생성된 워크스페이스 전체 정보
        - UUID 기반 고유 ID
        
        ## 주의사항
        - 워크스페이스 생성 시 자동으로 owner 멤버십이 생성됩니다
        - slug는 자동 생성되며 수정할 수 없습니다
        
        ## 사용 예시
        ```javascript
        const response = await fetch('http://localhost:8000/api/v1/workspaces/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${accessToken}`
            },
            body: JSON.stringify({
                name: 'Acme Corporation',
                description: '고객사 Acme의 워크스페이스'
            })
        });
        
        const workspace = await response.json();
        // { id: '...', name: 'Acme Corporation', slug: 'acme-corporation', owner: ..., ... }
        ```
        
        ```bash
        curl -X POST http://localhost:8000/api/v1/workspaces/ \\
          -H "Content-Type: application/json" \\
          -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \\
          -d '{
            "name": "Acme Corporation",
            "description": "고객사 Acme의 워크스페이스"
          }'
        ```
        """,
        request=WorkspaceCreateSerializer,
        responses={
            201: WorkspaceSerializer,
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        summary="워크스페이스 상세 조회",
        description="""
        ## 목적
        특정 워크스페이스의 상세 정보를 조회합니다.
        
        ## 사용 시나리오
        - 워크스페이스 상세 페이지 진입 시
        - 워크스페이스 정보 확인 시
        
        ## 인증
        - **Bearer 토큰 필수**
        - 해당 워크스페이스의 멤버만 조회 가능
        
        ## 권한
        - 워크스페이스 멤버 (owner, admin, member 모두 가능)
        
        ## 주의사항
        - 멤버가 아닌 워크스페이스는 404 에러
        - 존재하지 않는 워크스페이스도 404 에러
        
        ## 사용 예시
        ```javascript
        const workspaceId = 'workspace-uuid-here';
        
        const response = await fetch(`http://localhost:8000/api/v1/workspaces/${workspaceId}/`, {
            headers: {
                'Authorization': `Bearer ${accessToken}`
            }
        });
        
        const workspace = await response.json();
        ```
        """,
        responses={
            200: WorkspaceSerializer,
            401: OpenApiResponse(description="인증 실패"),
            404: OpenApiResponse(description="워크스페이스를 찾을 수 없거나 접근 권한 없음"),
        },
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        summary="워크스페이스 수정",
        description="""
        ## 목적
        워크스페이스 정보를 수정합니다.
        
        ## 사용 시나리오
        - 워크스페이스 이름 변경
        - 설명 업데이트
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 권한
        - **Admin 또는 Owner만 가능**
        - Member 역할은 수정 불가 (403 에러)
        
        ## 수정 가능 필드
        - `name`: 워크스페이스 이름
        - `description`: 설명
        
        ## 수정 불가 필드
        - `slug`: 자동 생성되며 변경 불가
        - `owner`: 소유자 변경 불가
        
        ## 주의사항
        - Admin 이상 권한 필요
        - name 변경 시 slug는 자동 업데이트되지 않습니다
        
        ## 사용 예시
        ```javascript
        const response = await fetch(`http://localhost:8000/api/v1/workspaces/${workspaceId}/`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${accessToken}`
            },
            body: JSON.stringify({
                description: '업데이트된 설명'
            })
        });
        ```
        """,
        request=WorkspaceSerializer,
        responses={
            200: WorkspaceSerializer,
            400: OpenApiResponse(description="유효성 검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음 - Admin 또는 Owner 권한 필요"),
            404: OpenApiResponse(description="워크스페이스를 찾을 수 없음"),
        },
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        summary="워크스페이스 삭제",
        description="""
        ## 목적
        워크스페이스를 영구적으로 삭제합니다.
        
        ## 사용 시나리오
        - 프로젝트 종료
        - 워크스페이스 정리
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 권한
        - **Owner만 가능**
        - Admin 및 Member는 삭제 불가 (403 에러)
        
        ## 주의사항
        - ⚠️ **영구 삭제되며 복구할 수 없습니다**
        - 관련된 모든 멤버십도 함께 삭제됩니다
        - 워크스페이스와 연관된 다른 데이터도 삭제될 수 있습니다
        
        ## 사용 예시
        ```javascript
        const response = await fetch(`http://localhost:8000/api/v1/workspaces/${workspaceId}/`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${accessToken}`
            }
        });
        
        if (response.status === 204) {
            console.log('워크스페이스가 삭제되었습니다');
        }
        ```
        """,
        responses={
            204: OpenApiResponse(description="삭제 성공"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음 - Owner 권한 필요"),
            404: OpenApiResponse(description="워크스페이스를 찾을 수 없음"),
        },
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        summary="워크스페이스 멤버 목록 조회",
        description="""
        ## 목적
        워크스페이스의 모든 멤버 목록을 조회합니다.
        
        ## 사용 시나리오
        - 팀 멤버 목록 표시
        - 멤버 관리 페이지
        - 권한 설정 화면
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 권한
        - 워크스페이스 멤버 (owner, admin, member 모두 가능)
        
        ## 응답 데이터
        - 모든 멤버의 사용자 정보 및 역할
        - 가입 일시 정보
        
        ## 사용 예시
        ```javascript
        const response = await fetch(`http://localhost:8000/api/v1/workspaces/${workspaceId}/members/`, {
            headers: {
                'Authorization': `Bearer ${accessToken}`
            }
        });
        
        const members = await response.json();
        // [{ id: '...', user: {...}, role: 'owner', ... }, ...]
        ```
        """,
        responses={
            200: MembershipSerializer(many=True),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="워크스페이스 멤버가 아님"),
        },
    )
    @action(
        detail=True,
        methods=["get", "post"],
        permission_classes=[IsAuthenticated, IsWorkspaceMember],
    )
    def members(self, request, pk=None):
        """Get all members of the workspace or add a new member"""
        workspace = self.get_object()

        if request.method == "GET":
            memberships = workspace.memberships.select_related("user").all()
            serializer = MembershipSerializer(memberships, many=True)
            return Response(serializer.data)

        elif request.method == "POST":
            # Check if user is admin or owner
            membership = workspace.memberships.filter(user=request.user).first()
            if membership.role not in [Membership.Role.ADMIN, Membership.Role.OWNER]:
                return Response(
                    {"error": "Only admin or owner can add members"},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Add new member by user_id
            user_id = request.data.get("user_id")
            role = request.data.get("role", Membership.Role.MEMBER)

            if not user_id:
                return Response(
                    {"error": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST
                )

            # Check if user exists
            from apps.authentication.models import User

            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

            # Check if already a member
            if workspace.memberships.filter(user=user).exists():
                return Response(
                    {"error": "User is already a member"}, status=status.HTTP_400_BAD_REQUEST
                )

            # Create membership
            new_membership = Membership.objects.create(user=user, workspace=workspace, role=role)

            return Response(
                MembershipSerializer(new_membership).data, status=status.HTTP_201_CREATED
            )

    @extend_schema(
        summary="멤버 역할 변경",
        description="""
        ## 목적
        워크스페이스 멤버의 역할을 변경합니다.
        
        ## 사용 시나리오
        - 멤버를 Admin으로 승격
        - Admin을 Member로 강등
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 권한
        - **Owner만 가능**
        - Admin 및 Member는 역할 변경 불가
        
        ## 요청 필드
        - `role`: 변경할 역할 (owner, admin, member 중 하나)
        
        ## 제약사항
        - Owner 역할은 변경할 수 없습니다
        - 자기 자신의 역할은 변경할 수 없습니다 (권한 상실 방지)
        
        ## 주의사항
        - Owner 권한 필요
        - Owner 역할 변경 시도 시 400 에러
        
        ## 사용 예시
        ```javascript
        const membershipId = 'membership-uuid-here';
        
        const response = await fetch(
            `http://localhost:8000/api/v1/workspaces/${workspaceId}/members/${membershipId}/update_role/`,
            {
                method: 'PATCH',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${accessToken}`
                },
                body: JSON.stringify({
                    role: 'admin'
                })
            }
        );
        ```
        """,
        request=MembershipUpdateSerializer,
        responses={
            200: MembershipSerializer,
            400: OpenApiResponse(description="유효성 검증 실패 또는 Owner 역할 변경 시도"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음 - Owner 권한 필요"),
            404: OpenApiResponse(description="멤버십을 찾을 수 없음"),
        },
    )
    @action(
        detail=True,
        methods=["patch"],
        url_path="members/(?P<membership_id>[^/.]+)/update_role",
        permission_classes=[IsAuthenticated, IsWorkspaceOwner],
    )
    def update_member_role(self, request, pk=None, membership_id=None):
        """Update member role (Owner only)"""
        workspace = self.get_object()
        membership = get_object_or_404(Membership, id=membership_id, workspace=workspace)

        # Prevent self-demotion
        if membership.user == request.user:
            return Response(
                {"error": "Cannot change your own role"}, status=status.HTTP_400_BAD_REQUEST
            )

        serializer = MembershipUpdateSerializer(membership, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(MembershipSerializer(membership).data)

    @extend_schema(
        summary="멤버 제거",
        description="""
        ## 목적
        워크스페이스에서 멤버를 제거합니다.
        
        ## 사용 시나리오
        - 팀원 퇴사
        - 프로젝트에서 제외
        - 권한 회수
        
        ## 인증
        - **Bearer 토큰 필수**
        
        ## 권한
        - **Admin 또는 Owner만 가능**
        
        ## 제약사항
        - Owner는 제거할 수 없습니다
        - 자기 자신은 제거할 수 없습니다
        
        ## 주의사항
        - 멤버 제거 시 해당 사용자는 워크스페이스에 더 이상 접근할 수 없습니다
        
        ## 사용 예시
        ```javascript
        const response = await fetch(
            `http://localhost:8000/api/v1/workspaces/${workspaceId}/members/${membershipId}/remove/`,
            {
                method: 'DELETE',
                headers: {
                    'Authorization': `Bearer ${accessToken}`
                }
            }
        );
        ```
        """,
        responses={
            204: OpenApiResponse(description="멤버 제거 성공"),
            400: OpenApiResponse(description="Owner 제거 시도 또는 자기 자신 제거 시도"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음 - Admin 또는 Owner 권한 필요"),
            404: OpenApiResponse(description="멤버십을 찾을 수 없음"),
        },
    )
    @action(
        detail=True,
        methods=["delete"],
        url_path="members/(?P<membership_id>[^/.]+)/remove",
        permission_classes=[IsAuthenticated, IsWorkspaceAdmin],
    )
    def remove_member(self, request, pk=None, membership_id=None):
        """Remove member from workspace (Admin/Owner only)"""
        workspace = self.get_object()
        membership = get_object_or_404(Membership, id=membership_id, workspace=workspace)

        # Prevent removing owner
        if membership.role == Membership.Role.OWNER:
            return Response(
                {"error": "Cannot remove workspace owner"}, status=status.HTTP_400_BAD_REQUEST
            )

        # Prevent self-removal
        if membership.user == request.user:
            return Response({"error": "Cannot remove yourself"}, status=status.HTTP_400_BAD_REQUEST)

        membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
