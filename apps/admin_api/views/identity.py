"""apps/admin_api/views/identity.py — 어드민 신원/게이팅 뷰.

``/api/v1/admin/me/`` — 로그인한 스태프 본인의 신원/권한 플래그를 반환한다.
프론트 백오피스가 진입 시 호출하여 접근 게이팅(메뉴/버튼 노출)에 사용한다.

GLOBAL scope: 이 앱의 모든 엔드포인트는 워크스페이스 교차 어드민 API 이므로
request.user 의 워크스페이스로 필터링하지 않는다. 본 엔드포인트는 조회(GET)만
제공하며 상태를 바꾸지 않으므로 감사 로그(AdminActionLog)를 남기지 않는다.

권한: IsAdminUser(is_staff=True). 비스태프는 403, 미인증은 401.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import generics
from rest_framework.permissions import IsAdminUser

from apps.admin_api.serializers.identity import AdminMeSerializer

logger = logging.getLogger(__name__)


class AdminMeView(generics.RetrieveAPIView):
    """현재 로그인한 어드민(스태프) 본인의 신원/권한 플래그 조회.

    ``get_object()`` 가 ``request.user`` 를 그대로 반환하므로, IsAdminUser 를 통과한
    스태프 본인의 정보만 노출된다(다른 사용자 조회 불가).
    """

    permission_classes = [IsAdminUser]
    serializer_class = AdminMeSerializer

    def get_object(self):
        return self.request.user

    @extend_schema(
        tags=["admin-identity"],
        summary="[관리자] 내 신원/권한 조회",
        description="""
## 개요
현재 로그인한 **스태프(관리자) 본인**의 신원과 권한 플래그
(`is_active` / `is_staff` / `is_superuser`)를 반환합니다.
비밀 정보(비밀번호 등)는 절대 포함되지 않습니다.

## 사용 시나리오
- 프론트 백오피스 진입 직후 호출하여 **접근 게이팅**(메뉴/버튼 노출)에 사용합니다.
- 토큰 보유 사용자가 실제로 관리자 권한이 있는지(`is_staff=True`) 확인할 때.
- `is_superuser` 여부로 권한 상승성 동작(예: 회원 `is_staff` 부여) 버튼 노출을 결정할 때.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만)
- 미인증(토큰 없음/만료) → **401**, 인증됐으나 비스태프(`is_staff=False`) → **403**.

## 비즈니스 로직
- **`admin_role` / `allowed_sections`(RBAC-1)**: 어드민 역할 기반 섹션 게이팅.
  `admin_role` 은 `"full"`(기존 스태프 — 전 권한) 또는 `"marketing_viewer"`
  (마케팅 대시보드 **조회 전용**, 외주/에이전시용). 역할은 **동명의 Django Group** 으로
  부여하며(모델 변경 없음), 회수는 다음 요청부터 즉시 반영됩니다(JWT claim 미사용).
  슈퍼유저는 안전 밸브로 항상 `full`.
- `allowed_sections` 는 접근 가능한 백오피스 섹션 키 배열(프론트 사이드바와 1:1):
  full = `["marketing","operations","users","pages","auto_dm","support","system"]`,
  marketing_viewer = `["marketing"]`. **프론트는 역할 문자열이 아니라 이 배열로 분기**하세요 —
  역할이 추가돼도 프론트 배포가 필요 없습니다.
- 이 배열은 UX 용이며 **실제 차단은 서버**가 합니다(RBAC-2) — 허용되지 않은
  `/api/v1/admin/**` 경로는 화이트리스트 방식으로 **403**
  (`error.details.code = "section_forbidden"`).
- `get_object()` 가 `request.user` 를 그대로 반환하므로 **본인 정보만** 조회됩니다.
- 워크스페이스 교차 글로벌 어드민 API 이지만, 본 엔드포인트는 사용자 본인만 노출하므로
  추가 필터가 필요 없습니다.
- 조회(GET) 전용 — 상태 변경이 없으므로 감사 로그(AdminActionLog)를 남기지 않습니다.

## 주의사항
- 다른 사용자의 정보는 이 엔드포인트로 조회할 수 없습니다(항상 토큰 주인 본인).
- `is_staff=False` 인 일반 사용자는 토큰이 유효해도 403 을 받습니다(IsAdminUser 차단).

```bash
curl -X GET http://localhost:8000/api/v1/admin/me/ \\
  -H "Authorization: Bearer YOUR_STAFF_ACCESS_TOKEN"
```
        """,
        request=None,
        responses={
            200: OpenApiResponse(
                response=AdminMeSerializer,
                description="조회 성공 — 본인 신원/권한 플래그 반환",
                examples=[
                    OpenApiExample(
                        "성공 응답",
                        value={
                            "id": 1,
                            "email": "admin@turnflow.ai.kr",
                            "full_name": "운영 관리자",
                            "is_active": True,
                            "is_staff": True,
                            "is_superuser": True,
                            "admin_role": "full",
                            "allowed_sections": [
                                "marketing",
                                "operations",
                                "users",
                                "pages",
                                "auto_dm",
                                "support",
                                "system",
                            ],
                        },
                        response_only=True,
                    ),
                    OpenApiExample(
                        "마케팅 조회 전용 계정 (외주)",
                        value={
                            "id": 42,
                            "email": "agency@partner.co.kr",
                            "full_name": "외주 마케팅",
                            "is_active": True,
                            "is_staff": True,
                            "is_superuser": False,
                            "admin_role": "marketing_viewer",
                            "allowed_sections": ["marketing"],
                        },
                        response_only=True,
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰이 없거나 유효하지 않음"),
            403: OpenApiResponse(description="권한 없음 — is_staff=False (관리자 아님)"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
