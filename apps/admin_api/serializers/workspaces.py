"""apps/admin_api/serializers/workspaces.py — 어드민 워크스페이스 & 멤버십 시리얼라이저.

``/api/v1/admin/`` 아래에서 ``IsAdminUser``(is_staff=True) 권한으로만 접근하는
크로스-워크스페이스(전역) 백오피스 시리얼라이저 모음.

- 목록/상세 조회는 읽기 전용(ModelSerializer + SerializerMethodField).
- 수정(PATCH)은 별도 write 시리얼라이저(``AdminWorkspaceUpdateSerializer`` /
  ``AdminMembershipUpdateSerializer``)로 분리해 노출 필드를 최소화한다.
- 오너(OWNER) 보호: 멤버 역할 변경 시 오너를 대상으로 하거나 역할을 owner 로 올리는
  요청은 거부한다 (apps.workspace.serializers.MembershipUpdateSerializer 와 동일 정책).

일반 유저용 시리얼라이저는 ``apps.workspace.serializers`` 참고.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.integrations.models import IGAccountConnection
from apps.pages.models import Page
from apps.workspace.models import Membership, Workspace

User = get_user_model()


# ---------------------------------------------------------------------------
# 인라인(중첩) 읽기 시리얼라이저
# ---------------------------------------------------------------------------


class _OwnerSerializer(serializers.ModelSerializer):
    """워크스페이스 오너 요약 (목록/상세 공통)."""

    class Meta:
        model = User
        fields = ["id", "email"]
        read_only_fields = fields
        ref_name = "AdminWorkspaceOwner"


class _MemberUserSerializer(serializers.ModelSerializer):
    """멤버십에 연결된 사용자 요약."""

    class Meta:
        model = User
        fields = ["id", "email", "full_name"]
        read_only_fields = fields
        ref_name = "AdminWorkspaceMemberUser"


class _MembershipSerializer(serializers.ModelSerializer):
    """상세 응답의 ``members`` 항목 1건."""

    membership_id = serializers.UUIDField(source="id", read_only=True)
    user = _MemberUserSerializer(read_only=True)

    class Meta:
        model = Membership
        fields = ["membership_id", "user", "role", "created_at"]
        read_only_fields = fields
        ref_name = "AdminWorkspaceMembership"


class _IGConnectionSerializer(serializers.ModelSerializer):
    """상세 응답의 ``ig_connections`` 항목 1건.

    보안: access_token 등 비밀값은 절대 직렬화하지 않는다 (id/username/status 만 노출).
    """

    class Meta:
        model = IGAccountConnection
        fields = ["id", "username", "status"]
        read_only_fields = fields
        ref_name = "AdminWorkspaceIGConnection"


# ---------------------------------------------------------------------------
# 목록 / 상세 (읽기)
# ---------------------------------------------------------------------------


class AdminWorkspaceListSerializer(serializers.ModelSerializer):
    """워크스페이스 목록 1건 (전역 조회용).

    ``members_count`` 는 뷰의 ``annotate(members_count=Count("memberships"))`` 결과를
    그대로 노출한다 (N+1 방지).
    """

    owner = _OwnerSerializer(read_only=True)
    members_count = serializers.IntegerField(
        read_only=True, help_text="이 워크스페이스에 속한 멤버십 수 (annotate)."
    )

    class Meta:
        model = Workspace
        fields = [
            "id",
            "name",
            "slug",
            "owner",
            "plan",
            "members_count",
            "created_at",
        ]
        read_only_fields = fields


class AdminWorkspaceDetailSerializer(serializers.ModelSerializer):
    """워크스페이스 상세 — 목록 필드 + 설명 / 멤버 목록 / 페이지 수 / IG 연동."""

    owner = _OwnerSerializer(read_only=True)
    members_count = serializers.SerializerMethodField(help_text="이 워크스페이스에 속한 멤버십 수.")
    members = serializers.SerializerMethodField(
        help_text="멤버십 목록: [{membership_id, user, role, created_at}]."
    )
    pages_count = serializers.SerializerMethodField(
        help_text="오너가 소유한 Page 수 (Page.user == workspace.owner)."
    )
    ig_connections = serializers.SerializerMethodField(
        help_text="연결된 IG 계정 요약: [{id, username, status}] (토큰 비노출)."
    )

    class Meta:
        model = Workspace
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "owner",
            "plan",
            "members_count",
            "members",
            "pages_count",
            "ig_connections",
            "created_at",
        ]
        read_only_fields = fields

    def get_members_count(self, obj: Workspace) -> int:
        return obj.memberships.count()

    def get_members(self, obj: Workspace) -> list:
        memberships = obj.memberships.select_related("user").all()
        return _MembershipSerializer(memberships, many=True, context=self.context).data

    def get_pages_count(self, obj: Workspace) -> int:
        return Page.objects.filter(user=obj.owner).count()

    def get_ig_connections(self, obj: Workspace) -> list:
        connections = obj.ig_connections.all()
        return _IGConnectionSerializer(connections, many=True, context=self.context).data


# ---------------------------------------------------------------------------
# 쓰기 (PATCH)
# ---------------------------------------------------------------------------


class AdminWorkspaceUpdateSerializer(serializers.ModelSerializer):
    """``PATCH /api/v1/admin/workspaces/{id}/`` 요청 바디.

    플랜(요금제) 강제 조정 및 이름 정정 용도. 보낸 키만 적용(partial update).
    """

    class Meta:
        model = Workspace
        fields = ["plan", "name"]
        extra_kwargs = {
            "plan": {
                "required": False,
                "help_text": "요금제: starter / pro / enterprise.",
            },
            "name": {"required": False, "help_text": "워크스페이스 표시 이름."},
        }


class AdminMembershipUpdateSerializer(serializers.ModelSerializer):
    """``PATCH /api/v1/admin/workspaces/{ws}/members/{id}/`` 요청 바디 (역할 변경).

    오너 보호 정책:
        - 대상 멤버십이 이미 OWNER 역할이면 변경 거부.
        - 새 역할을 owner 로 올리는 요청도 거부 (오너 승격은 별도 소유권 이전 절차).
    """

    class Meta:
        model = Membership
        fields = ["role"]
        extra_kwargs = {
            "role": {"help_text": "변경할 역할: admin / member (owner 불가)."},
        }

    def validate_role(self, value):
        if self.instance and self.instance.role == Membership.Role.OWNER:
            raise serializers.ValidationError("오너 역할은 변경할 수 없습니다.")
        if value == Membership.Role.OWNER:
            raise serializers.ValidationError("멤버를 오너로 승격할 수 없습니다.")
        return value
