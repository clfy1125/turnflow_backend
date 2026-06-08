"""apps/admin_api/serializers/identity.py — 어드민 신원/게이팅 시리얼라이저.

``/api/v1/admin/me/`` 에서 사용. 로그인한 스태프 본인의 신원/권한 플래그만
읽기 전용으로 노출한다. 비밀 정보는 직렬화하지 않는다.
일반 유저용 시리얼라이저는 ``apps.authentication.serializers`` 참고.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()


class AdminMeSerializer(serializers.ModelSerializer):
    """현재 로그인한 어드민(스태프) 본인의 신원/권한 플래그 (전부 읽기 전용).

    프론트 백오피스가 진입 시 호출하여 메뉴/버튼 노출을 게이팅하는 데 사용한다.
    - ``is_staff``: 백오피스 접근 가능 여부 (IsAdminUser 통과 조건).
    - ``is_superuser``: 권한 상승성 동작(예: 회원 is_staff 부여) 가능 여부.
    """

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "is_active",
            "is_staff",
            "is_superuser",
        ]
        read_only_fields = [
            "id",
            "email",
            "full_name",
            "is_active",
            "is_staff",
            "is_superuser",
        ]
