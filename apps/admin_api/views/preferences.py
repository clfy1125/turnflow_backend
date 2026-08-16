"""apps/admin_api/views/preferences.py — 관리자별 백오피스 화면 설정 (UI-1).

``/api/v1/admin/me/preferences/`` — 본인 것만 읽고 쓴다.

## 설계 결정 두 가지

**스키마를 검증하지 않는다.** 프론트 화면 설정이라 키가 자주 바뀌는데, 서버가 스키마를
들고 있으면 프론트가 키 하나 추가할 때마다 백엔드 배포를 기다려야 한다. 값이 이상하면
프론트가 기본값으로 떨어지면 된다. 대신 **크기 상한 4KB** 만 지킨다 — 검증하지 않는 칸은
언젠가 쓰레기통이 되므로, 무한히 자라지 못하게 막는 선은 있어야 한다.

**PATCH 는 최상위 키 단위 병합이다.** 통째로 덮으면 두 화면이 서로의 설정을 지운다
(탭 편집 화면이 저장하는 순간 표 열 순서가 날아가는 식). 값 안쪽까지 재귀 병합하지는
않는다 — 배열을 부분 병합하려 들면 "탭 3개로 줄이기"가 불가능해진다.

GLOBAL scope 예외: 어드민 API 는 워크스페이스 교차지만 이 엔드포인트만은 ``request.user``
본인으로 한정된다(``/admin/me/`` 와 같은 규약).
"""

from __future__ import annotations

import json
import logging

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.models import AdminPreference

logger = logging.getLogger(__name__)

# 직렬화된 JSON 기준 상한. 프론트가 "4KB 면 충분하다"고 했고, 탭 구성(경로 5개)은 200바이트
# 남짓이라 20배 이상 여유가 있다.
MAX_PREFERENCES_BYTES = 4096


class AdminPreferencesSerializer(serializers.Serializer):
    """응답 스키마 (문서용). 내용은 자유 JSON 이라 필드를 못 박지 않는다."""

    preferences = serializers.DictField(
        help_text="관리자 본인의 화면 설정 전체 (병합 후 최종 상태). 스키마 없음"
    )


class AdminPreferencesRequestSerializer(serializers.Serializer):
    """PATCH 본문 — 임의의 최상위 키를 그대로 받는다."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({"detail": "객체(JSON object)를 보내주세요."})
        return data


class AdminPreferencesView(APIView):
    """관리자 본인의 화면 설정 조회/병합 저장."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminPreferencesSerializer

    @extend_schema(
        tags=["admin-identity"],
        summary="[관리자] 내 화면 설정 조회",
        description="""
## 개요
관리자 **본인**의 백오피스 화면 설정을 반환합니다. 모바일 하단 탭 구성처럼 기기가 아니라
**계정을 따라다녀야 하는** 프론트 상태를 서버에 보관하기 위한 저장소입니다.

## 사용 시나리오
- 콘솔 진입 시 1회 호출해 저장된 설정을 적용합니다. 값이 없으면 `{}` 이므로 프론트 기본값을
  그대로 쓰면 됩니다.
- 기기를 바꾸거나 안드로이드 셸이 WebView 데이터를 잃어도 설정이 살아남습니다.

## 인증
- `Authorization: Bearer <staff_access_token>` — **본인 것만** 조회됩니다.
- 마케팅 전용 계정(`marketing_viewer`)도 사용할 수 있습니다(자기 탭은 자기가 고쳐야 하므로
  RBAC 화이트리스트에 포함).

## 응답 데이터
- `preferences`: 저장된 JSON 전체. **스키마가 없습니다** — 서버는 보관만 하고 해석하지 않습니다.

## 주의사항
- 권한·요금제 같은 **판정 값을 넣지 마세요.** 사용자가 자기 값을 PATCH 로 바꿀 수 있어
  신뢰 경계 밖입니다. 순수 표시 설정만 담습니다.
- 값이 예상과 다르면 프론트가 기본값으로 떨어지도록 짜 두세요(서버는 검증하지 않습니다).

```bash
curl -X GET https://api.example.com/api/v1/admin/me/preferences/ \\
  -H "Authorization: Bearer YOUR_STAFF_ACCESS_TOKEN"
```
        """,
        request=None,
        responses={
            200: OpenApiResponse(
                response=AdminPreferencesSerializer,
                description="조회 성공 (저장된 적 없으면 빈 객체)",
                examples=[
                    OpenApiExample(
                        "저장된 설정",
                        value={
                            "preferences": {
                                "mobile_nav": [
                                    "/dashboard",
                                    "/pages",
                                    "/dashboard/marketing",
                                    "/auto-dm/campaigns",
                                    "/users",
                                ]
                            }
                        },
                        response_only=True,
                    ),
                    OpenApiExample("저장 전", value={"preferences": {}}, response_only=True),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="권한 없음 — is_staff=False"),
        },
    )
    def get(self, request):
        row = AdminPreference.objects.filter(user=request.user).first()
        return Response({"preferences": row.data if row else {}}, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["admin-identity"],
        summary="[관리자] 내 화면 설정 저장 (키 단위 병합)",
        description="""
## 개요
보낸 **최상위 키만** 덮어쓰고 나머지는 보존합니다. 통째로 교체하지 않습니다 —
두 화면이 각자 저장할 때 서로의 설정을 지우는 것을 막기 위해서입니다.

## 인증
- `Authorization: Bearer <staff_access_token>` — 본인 것만 수정됩니다.

## 요청 필드
- 임의의 최상위 키/값 (JSON object). **스키마 검증 없음.**
- 예: `{"mobile_nav": ["/dashboard", "/users"]}` → `mobile_nav` 만 교체, 다른 키는 유지.

## 비즈니스 로직
- **병합 깊이는 1단계입니다.** 값 안쪽까지 재귀 병합하지 않습니다 — 배열을 부분 병합하면
  "탭을 3개로 줄이기"가 불가능해집니다. 배열·객체 값은 통째로 교체됩니다.
- 키를 **삭제**하려면 `null` 을 보내세요 (`{"mobile_nav": null}` → 그 키가 사라집니다).
- 응답은 병합 후 **전체 상태**입니다 — 프론트가 다시 GET 할 필요가 없습니다.

## 주의사항
- 전체 크기 상한 **4KB**(직렬화 기준). 초과 시 400 `preferences_too_large` 이며,
  **기존 값은 변경되지 않습니다.**
- 상태를 바꾸지만 다른 관리자에게 영향이 없는 개인 설정이라 감사 로그(AdminActionLog)를
  남기지 않습니다.

```bash
curl -X PATCH https://api.example.com/api/v1/admin/me/preferences/ \\
  -H "Authorization: Bearer YOUR_STAFF_ACCESS_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"mobile_nav": ["/dashboard", "/pages", "/users"]}'
```
        """,
        request=AdminPreferencesRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=AdminPreferencesSerializer,
                description="병합 후 전체 설정",
                examples=[
                    OpenApiExample(
                        "병합 결과",
                        value={
                            "preferences": {
                                "mobile_nav": ["/dashboard", "/pages", "/users"],
                                "table_columns": {"users": ["email", "plan"]},
                            }
                        },
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(
                description="`details.code = preferences_too_large` (4KB 초과) 또는 "
                "본문이 JSON 객체가 아님"
            ),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음 — is_staff=False"),
        },
    )
    def patch(self, request):
        payload = request.data
        if not isinstance(payload, dict):
            return self._error("객체(JSON object)를 보내주세요.", "invalid_body")

        row, _ = AdminPreference.objects.get_or_create(user=request.user)
        merged = dict(row.data or {})
        for key, value in payload.items():
            if value is None:
                merged.pop(key, None)  # null = 키 삭제
            else:
                merged[key] = value

        # 상한 검사는 **저장 전에** 한다 — 넘으면 기존 값이 그대로 남아야 한다.
        try:
            size = len(json.dumps(merged, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return self._error("JSON 으로 직렬화할 수 없는 값이 있습니다.", "invalid_body")
        if size > MAX_PREFERENCES_BYTES:
            return self._error(
                f"화면 설정이 너무 큽니다 ({size}바이트 / 상한 {MAX_PREFERENCES_BYTES}).",
                "preferences_too_large",
                size=size,
                limit=MAX_PREFERENCES_BYTES,
            )

        row.data = merged
        row.save(update_fields=["data", "updated_at"])
        return Response({"preferences": merged}, status=status.HTTP_200_OK)

    @staticmethod
    def _error(message: str, code: str, **extra):
        """프로젝트 표준 에러 봉투 — 사유 코드는 `error.details.code`."""
        return Response(
            {
                "success": False,
                "error": {
                    "code": 400,
                    "message": message,
                    "details": {"code": code, **extra},
                },
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
