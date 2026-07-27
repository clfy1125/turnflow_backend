"""apps/admin_api/views/marketing.py — 마케팅 채널 링크 서버 저장 CRUD (M-4).

라우팅: ``/api/v1/admin/marketing/channel-links/`` 아래. 권한: ``IsAdminUser``(is_staff=True).

채널 링크 생성기의 "저장한 링크"가 프론트 localStorage 전용이라 기기·관리자 간 공유가
안 되던 것을 서버 저장으로 이관한다. 조회는 **전 관리자 공용**(생성자 무관 전체 노출).

엔드포인트:
  - 목록/생성:  ``GET/POST  /api/v1/admin/marketing/channel-links/``
  - 이름수정/삭제: ``PATCH/DELETE /api/v1/admin/marketing/channel-links/<int:pk>/``

``url``·``channel`` 은 서버 계산(:mod:`apps.admin_api.serializers.marketing`) —
채널 키는 방문/가입 저장과 동일한 ``derive_channel`` 단일 소스라 마케팅 대시보드의
채널별 성과 행과 어휘가 일치한다. mutation 성공 시 ``AdminActionLog`` 감사 기록.

역할별 권한 (RBAC-4) — 경로 게이트는 미들웨어, **객체 소유자 검사는 이 뷰**:
  ================  ======  ==================================================
  메서드            full    marketing_viewer(외주)
  ================  ======  ==================================================
  GET   목록        200     200 (created_by_email 은 빈 문자열로 마스킹)
  POST  생성        201     201
  DELETE 상세       204     **자기가 만든 링크만** 204, 남의 것/생성자 null 은 403
  GET/PATCH 상세    200     403 (미들웨어 화이트리스트 밖 — Q1 기본안 유지)
  ================  ======  ==================================================
"""

from __future__ import annotations

import logging

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog, MarketingChannelLink
from apps.admin_api.roles import (
    NOT_LINK_OWNER_CODE,
    NOT_LINK_OWNER_MESSAGE,
    can_delete_channel_link,
    resolve_admin_role,
)
from apps.admin_api.serializers.marketing import (
    AdminChannelLinkRenameSerializer,
    AdminChannelLinkSerializer,
    AdminChannelLinkWriteSerializer,
)

logger = logging.getLogger(__name__)

_EXAMPLE_LINK = {
    "id": 3,
    "name": "7월 틱톡 리타겟팅",
    "base_url": "https://turnflow.link/",
    "utm_source": "tiktok",
    "utm_medium": "cpc",
    "utm_campaign": "2026-07-retargeting",
    "utm_content": "video_a",
    "url": (
        "https://turnflow.link/?utm_source=tiktok&utm_medium=cpc"
        "&utm_campaign=2026-07-retargeting&utm_content=video_a"
    ),
    "channel": "tiktok_ads",
    "created_by_email": "marketer@clfy.ai.kr",
    "can_delete": True,
    "created_at": "2026-07-26T10:00:00+09:00",
    "updated_at": "2026-07-26T10:00:00+09:00",
}
# 외주(마케팅 조회 전용) 계정이 같은 링크를 봤을 때 — 내부 직원 이메일 비노출 + 남의 링크
_EXAMPLE_LINK_VIEWER = {**_EXAMPLE_LINK, "created_by_email": "", "can_delete": False}


class AdminChannelLinkListCreateView(generics.ListCreateAPIView):
    """채널 링크 목록 조회 + 신규 생성 (전 관리자 공용)."""

    permission_classes = [IsAdminUser]
    queryset = MarketingChannelLink.objects.select_related("created_by").order_by("-created_at")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["channel"]
    search_fields = ["name", "url", "utm_campaign"]
    ordering_fields = ["created_at", "name"]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AdminChannelLinkWriteSerializer
        return AdminChannelLinkSerializer

    @extend_schema(
        tags=["admin-marketing"],
        summary="[관리자] 채널 링크 목록 조회",
        description="""
## 개요
마케팅 채널 링크 생성기에서 저장한 **UTM 링크**를 페이지네이션하여 반환합니다.
저장소는 서버 DB — **전 관리자 공용**이라 누가 만들었든 모든 관리자에게 보입니다
(생성자는 `created_by_email` 로 표기).

## 사용 시나리오
- 백오피스 마케팅 대시보드 "채널 링크 생성기 > 저장한 링크" 목록 로딩
- 기존 localStorage 저장분의 서버 이관 후 기기·관리자 간 공유

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만 접근)
- 미인증 401, 일반 사용자(비스태프) 403.

## 필터/검색/정렬
- `?channel=` — 파생 채널 키 필터 (예: tiktok_ads)
- `?search=` — name / url / utm_campaign 부분 일치
- `?ordering=` — created_at / name ('-' 내림차순, 기본 -created_at)

## 응답 필드
| 필드 | 설명 |
|------|------|
| `name` | 링크 이름 |
| `base_url` / `utm_*` | 생성 시 입력값 |
| `url` | 서버가 조합한 최종 URL (기존 쿼리 보존, 동일 utm 키 교체) |
| `channel` | `derive_channel(utm_source, utm_medium)` 파생 키 — 대시보드 채널 키와 동일 어휘 |
| `created_by_email` | 생성 관리자 (탈퇴 시 빈 문자열). **marketing_viewer 는 항상 `""`** |
| `can_delete` | 이 요청자가 삭제 가능한지 (서버 판정, RBAC-4-c) |

## 역할별 차이 (RBAC-4)
- `full`: 기존 그대로 — `created_by_email` 노출, `can_delete` 항상 `true`.
- `marketing_viewer`(외주): 목록은 **전 관리자 공용 그대로** 보이지만(채널 리포트 해석에
  내부 팀 링크도 필요), `created_by_email` 은 **빈 문자열**로 마스킹되고
  `can_delete` 는 **자기가 만든 링크만** `true` 입니다.
  `can_delete=false` 인 행은 삭제 버튼을 렌더하지 마세요 — 누르면 403
  (`error.details.code="not_link_owner"`).

## 주의사항
- 응답은 `{count,next,previous,results}` 형태(PAGE_SIZE=20)입니다.
        """,
        parameters=[
            OpenApiParameter(
                name="channel",
                type=str,
                location=OpenApiParameter.QUERY,
                description="파생 채널 키 필터 (예: tiktok_ads / meta_ads / paid_other).",
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description="name / url / utm_campaign 부분 일치 검색.",
            ),
            OpenApiParameter(
                name="ordering",
                type=str,
                location=OpenApiParameter.QUERY,
                description="정렬 (created_at/name, '-' 내림차순, 기본 -created_at).",
            ),
        ],
        responses={
            200: AdminChannelLinkSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "응답 예시 (full 역할)",
                response_only=True,
                value={"count": 1, "next": None, "previous": None, "results": [_EXAMPLE_LINK]},
            ),
            OpenApiExample(
                "응답 예시 (marketing_viewer — 남이 만든 링크)",
                response_only=True,
                value={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [_EXAMPLE_LINK_VIEWER],
                },
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-marketing"],
        summary="[관리자] 채널 링크 생성",
        description="""
## 개요
UTM 채널 링크를 서버에 저장합니다. `url`(최종 URL)과 `channel`(채널 키)은 입력받지 않고
**서버가 계산**합니다 — 채널 파생은 방문/가입 어트리뷰션과 동일한 `derive_channel`
단일 소스를 사용해, 이 링크로 유입된 방문이 대시보드에서 같은 채널로 집계됩니다.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `name` | ✅ | string | 링크 이름 (최대 100자) |
| `base_url` | ✅ | string | http/https URL (최대 500자). 기존 쿼리스트링 보존 |
| `utm_source` | ❌ | string | 최대 100자. 채널 파생의 1차 입력 |
| `utm_medium` | ❌ | string | 최대 100자. 채널 파생의 2차 입력 |
| `utm_campaign` | ❌ | string | 최대 100자 |
| `utm_content` | ❌ | string | 최대 100자 |

## 비즈니스 로직
- `url` = base_url 에 비어있지 않은 utm 파라미터를 병합 (base_url 에 이미 같은 utm 키가
  있으면 새 값으로 교체, 프래그먼트 유지).
- `channel` = `derive_channel(utm_source, utm_medium, "")` — 예:
  `utm_source=tiktok` → `tiktok_ads`, 미매핑 source + `utm_medium=cpc` → `paid_other`.
- 성공 시 `AdminActionLog(channel_link.create)` 감사 기록.

## 검증
- `base_url` 은 http/https 스킴 + 호스트 필수 → 아니면 400.

### 요청 예시
```bash
curl -X POST -H "Authorization: Bearer <staff_token>" -H "Content-Type: application/json" \\
  -d '{"name":"7월 틱톡 리타겟팅","base_url":"https://turnflow.link/","utm_source":"tiktok","utm_medium":"cpc","utm_campaign":"2026-07-retargeting"}' \\
  "https://api.example.com/api/v1/admin/marketing/channel-links/"
```
        """,
        request=AdminChannelLinkWriteSerializer,
        responses={
            201: AdminChannelLinkSerializer,
            400: OpenApiResponse(description="검증 실패 (base_url 스킴/호스트, 길이 초과 등)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "요청 예시",
                request_only=True,
                value={
                    "name": "7월 틱톡 리타겟팅",
                    "base_url": "https://turnflow.link/",
                    "utm_source": "tiktok",
                    "utm_medium": "cpc",
                    "utm_campaign": "2026-07-retargeting",
                    "utm_content": "video_a",
                },
            ),
            OpenApiExample("응답 예시", response_only=True, value=_EXAMPLE_LINK),
        ],
    )
    def post(self, request, *args, **kwargs):
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        link = write.save()

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.CHANNEL_LINK_CREATE,
            target_type="channel_link",
            target_id=link.pk,
            target_repr=link.name,
            changes={
                "name": {"before": None, "after": link.name},
                "url": {"before": None, "after": link.url},
                "channel": {"before": None, "after": link.channel},
            },
        )
        logger.info(
            "[admin-marketing] req=%s 채널 링크 생성 id=%s channel=%s",
            getattr(request, "id", ""),
            link.pk,
            link.channel,
        )
        read = AdminChannelLinkSerializer(link, context=self.get_serializer_context())
        return Response(read.data, status=status.HTTP_201_CREATED)


class AdminChannelLinkDetailView(generics.RetrieveUpdateDestroyAPIView):
    """채널 링크 단건 조회 + 이름 수정(PATCH) + 삭제."""

    permission_classes = [IsAdminUser]
    queryset = MarketingChannelLink.objects.select_related("created_by")
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return AdminChannelLinkRenameSerializer
        return AdminChannelLinkSerializer

    @extend_schema(
        tags=["admin-marketing"],
        summary="[관리자] 채널 링크 상세 조회",
        description="""
## 개요
저장된 채널 링크 단건을 반환합니다. 목록 항목과 동일한 형태입니다.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)
        """,
        responses={
            200: AdminChannelLinkSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 링크 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[OpenApiExample("응답 예시", response_only=True, value=_EXAMPLE_LINK)],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-marketing"],
        summary="[관리자] 채널 링크 이름 수정",
        description="""
## 개요
저장된 채널 링크의 **이름만** 수정합니다(PATCH).

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `name` | ✅ | string | 변경할 이름 (최대 100자) |

## 주의사항
- `base_url`/`utm_*` 는 수정할 수 없습니다 — `url`·`channel` 파생값과의 일관성을 위해
  UTM 조합을 바꾸려면 **삭제 후 재생성**하세요 (보내도 무시됩니다).
- 성공 시 `AdminActionLog(channel_link.update)` 감사 기록. PUT 은 비활성화.
        """,
        request=AdminChannelLinkRenameSerializer,
        responses={
            200: AdminChannelLinkSerializer,
            400: OpenApiResponse(description="검증 실패 (name 누락/길이 초과)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 링크 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample("요청 예시", request_only=True, value={"name": "7월 틱톡 리타겟팅 v2"}),
        ],
    )
    def patch(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        link = self.get_object()
        before_name = link.name

        serializer = self.get_serializer(link, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        if link.name != before_name:
            log_admin_action(
                request=request,
                action=AdminActionLog.Action.CHANNEL_LINK_UPDATE,
                target_type="channel_link",
                target_id=link.pk,
                target_repr=link.name,
                changes={"name": {"before": before_name, "after": link.name}},
            )
            logger.info(
                "[admin-marketing] req=%s 채널 링크 이름 수정 id=%s",
                getattr(request, "id", ""),
                link.pk,
            )
        read = AdminChannelLinkSerializer(link, context=self.get_serializer_context())
        return Response(read.data)

    @extend_schema(
        tags=["admin-marketing"],
        summary="[관리자] 채널 링크 삭제",
        description="""
## 개요
저장된 채널 링크를 **영구 삭제**합니다. 링크는 생성기 편의 데이터일 뿐이라
방문/가입 어트리뷰션 집계에는 영향이 없습니다(집계는 실제 방문의 utm 저장값 기준).

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 소유자 스코프 (RBAC-4-b)
- `full` 역할: 모든 링크 삭제 가능 (기존 동작 그대로).
- `marketing_viewer`(외주): **자기가 만든 링크만** 삭제할 수 있습니다. 남의 링크나
  생성자가 없는 링크(`created_by=null` — 생성 계정 삭제됨)는 **403**:
  `{"success": false, "error": {"code": 403, "message": "다른 관리자가 만든 링크는 삭제할 수 없습니다.",
  "details": {"code": "not_link_owner", "admin_role": "marketing_viewer"}}}`
  → 목록 응답의 `can_delete` 와 **같은 판정 함수**를 쓰므로, `can_delete=true` 인 행은
  반드시 삭제에 성공합니다. 차단된 시도는 감사 로그(`admin.access_denied`)에 남습니다.

## 응답
- 204 No Content (삭제 완료). 성공 시 `AdminActionLog(channel_link.delete)` 감사 기록.
        """,
        request=None,
        responses={
            204: OpenApiResponse(description="삭제 완료"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(
                description="관리자 권한 없음(is_staff=False) 또는 "
                '남이 만든 링크(details.code="not_link_owner")'
            ),
            404: OpenApiResponse(description="해당 링크 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def delete(self, request, *args, **kwargs):
        return self.destroy(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        link = self.get_object()

        # RBAC-4-b: 제한 역할(외주)은 **자기가 만든 링크만** 삭제. 응답의 can_delete 와
        # 반드시 같은 함수를 쓴다 — 갈라지면 버튼과 실제 동작이 어긋난다.
        role = resolve_admin_role(request)
        if not can_delete_channel_link(role, request.user, link):
            log_admin_action(
                request=request,
                action=AdminActionLog.Action.ADMIN_ACCESS_DENIED,
                target_type="channel_link",
                target_id=link.pk,
                target_repr=f"DELETE {link.name}"[:255],
                changes={"admin_role": role, "reason": NOT_LINK_OWNER_CODE},
            )
            logger.warning(
                "[admin-marketing] req=%s role=%s 남의 링크 삭제 시도 차단 id=%s",
                getattr(request, "id", ""),
                role,
                link.pk,
            )
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 403,
                        "message": NOT_LINK_OWNER_MESSAGE,
                        "details": {"code": NOT_LINK_OWNER_CODE, "admin_role": role},
                    },
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        link_pk, link_name = link.pk, link.name
        link.delete()

        log_admin_action(
            request=request,
            action=AdminActionLog.Action.CHANNEL_LINK_DELETE,
            target_type="channel_link",
            target_id=link_pk,
            target_repr=link_name,
        )
        logger.info(
            "[admin-marketing] req=%s 채널 링크 삭제 id=%s", getattr(request, "id", ""), link_pk
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
