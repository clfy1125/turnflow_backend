"""apps/admin_api/serializers/identity.py — 어드민 신원/게이팅 시리얼라이저.

``/api/v1/admin/me/`` 에서 사용. 로그인한 스태프 본인의 신원/권한 플래그만
읽기 전용으로 노출한다. 비밀 정보는 직렬화하지 않는다.
일반 유저용 시리얼라이저는 ``apps.authentication.serializers`` 참고.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.admin_api.roles import ALL_SECTIONS, ROLE_FULL, ROLE_MARKETING_VIEWER, admin_role
from apps.admin_api.roles import allowed_sections as _allowed_sections

User = get_user_model()


class AdminMeSerializer(serializers.ModelSerializer):
    """현재 로그인한 어드민(스태프) 본인의 신원/권한 플래그 (전부 읽기 전용).

    프론트 백오피스가 진입 시 호출하여 메뉴/버튼 노출을 게이팅하는 데 사용한다.
    - ``is_staff``: 백오피스 접근 가능 여부 (IsAdminUser 통과 조건).
    - ``is_superuser``: 권한 상승성 동작(예: 회원 is_staff 부여) 가능 여부.
    - ``admin_role`` / ``allowed_sections``: 역할 기반 섹션 게이팅 (RBAC-1).
      프론트는 **allowed_sections 배열만 보고** 사이드바를 필터해야 한다 — 역할 문자열을
      하드코딩하면 역할이 늘 때마다 프론트 배포가 필요해진다.
    """

    admin_role = serializers.SerializerMethodField(
        help_text=f'어드민 역할 — "{ROLE_FULL}"(기존 스태프 전 권한) | '
        f'"{ROLE_MARKETING_VIEWER}"(마케팅 대시보드 조회 전용). '
        "부여는 동명의 Django Group 으로 하며 회수는 다음 요청부터 즉시 반영된다"
    )
    allowed_sections = serializers.SerializerMethodField(
        help_text="접근 가능한 백오피스 섹션 키 배열 (프론트 사이드바와 1:1). "
        f"full = {ALL_SECTIONS}, marketing_viewer = ['marketing']. "
        "이 배열에 없는 섹션의 API 는 서버에서도 403 이다(RBAC-2)"
    )

    def get_admin_role(self, obj) -> str:
        return admin_role(obj)

    def get_allowed_sections(self, obj) -> list[str]:
        return _allowed_sections(admin_role(obj))

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "admin_role",
            "allowed_sections",
        ]
        read_only_fields = [
            "id",
            "email",
            "full_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "admin_role",
            "allowed_sections",
        ]
