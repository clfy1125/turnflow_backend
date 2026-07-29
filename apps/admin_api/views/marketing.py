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
from apps.admin_api.dashboard_cache import bust_marketing_dashboard_cache
from apps.admin_api.models import AdminActionLog, MarketingChannelLink
from apps.admin_api.roles import (
    EXCLUDE_FORBIDDEN_CODE,
    EXCLUDE_FORBIDDEN_MESSAGE,
    NOT_LINK_OWNER_CODE,
    NOT_LINK_OWNER_MESSAGE,
    can_delete_channel_link,
    can_exclude_channel_link_from_stats,
    resolve_admin_role,
)
from apps.admin_api.serializers.marketing import (
    AdminChannelLinkSerializer,
    AdminChannelLinkUpdateSerializer,
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
    "excluded_from_stats": False,
    "created_by_email": "marketer@clfy.ai.kr",
    "can_delete": True,
    "can_exclude": True,
    "created_at": "2026-07-26T10:00:00+09:00",
    "updated_at": "2026-07-26T10:00:00+09:00",
}
# 외주(마케팅 조회 전용) 계정이 같은 링크를 봤을 때 — 내부 직원 이메일 비노출 + 남의 링크.
# can_exclude 는 소유자 여부와 무관하게 항상 false (MKT-12: full 전용).
_EXAMPLE_LINK_VIEWER = {
    **_EXAMPLE_LINK,
    "created_by_email": "",
    "can_delete": False,
    "can_exclude": False,
}


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
        # MKT-11: 새 링크는 방문 0이어도 채널별 성과에 행이 생겨야 하는데, 대시보드 응답이
        # 5분 캐시를 타서 최대 5분간 "저장했는데 안 나온다"로 보였다. 저장 직후 확인하는
        # 흐름이라 여기서만은 지연이 '동작 안 함'으로 읽힌다 → 즉시 무효화.
        bust_marketing_dashboard_cache(reason="channel_link.create")
        read = AdminChannelLinkSerializer(link, context=self.get_serializer_context())
        return Response(read.data, status=status.HTTP_201_CREATED)


class AdminChannelLinkDetailView(generics.RetrieveUpdateDestroyAPIView):
    """채널 링크 단건 조회 + 이름 수정(PATCH) + 삭제."""

    permission_classes = [IsAdminUser]
    queryset = MarketingChannelLink.objects.select_related("created_by")
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return AdminChannelLinkUpdateSerializer
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
        summary="[관리자] 채널 링크 수정 (이름 / 집계 제외)",
        description="""
## 개요
저장된 채널 링크의 **이름**과 **집계 제외 여부**를 수정합니다(PATCH). 둘 다 선택 필드이고
보낸 것만 바뀝니다(둘 다 없으면 400).

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `name` | ❌ | string | 변경할 이름 (최대 **255자**) |
| `excluded_from_stats` | ❌ | boolean | `true` 면 마케팅 대시보드 집계에서 제외 (아래) |

## `excluded_from_stats` 동작 (MKT-12)
테스트/오생성/종료된 캠페인 링크가 채널별 성과 표에 계속 쌓이는 것을 정리하는 용도입니다.
`true` 로 켜면 **세 카드에서 함께** 빠집니다 — 한 곳만 빠지면 같은 키가 카드마다 다른
인원을 뜻하게 됩니다:

1. `channels.rows` — 이 링크의 행이 사라집니다
2. `trends.by_channel` — 이 키가 사라집니다
3. `funnel.available_channels` / `funnel.variants` — 드롭다운에서 사라집니다

⚠️ **인원은 사라지지 않습니다.** 이 링크로 들어온 방문·가입은 `other` 행으로 흡수되고
펼침에서 `sources[key="excluded_link"]`("집계에서 뺀 링크") 줄로 보입니다. 따라서
`Σrows[].signups + attribution_gap.signups_unattributed == 기간 가입자 수` 항등이
**그대로 유지**됩니다. (숨기는 것과 없애는 것은 다른 얘기라 흡수를 택했습니다.)

- **목록 API 에는 제외한 링크도 계속 나옵니다** — 되돌릴 경로를 유지합니다.
- `false` 로 되돌리면 행과 수치가 즉시 복구됩니다(과거 유입 소급 반영).
- 저장 유도 목록(`unsaved_utm.combos`)에는 **실리지 않습니다** — 이미 저장된 링크라
  그 조합으로 새로 만들면 중복 400 이 납니다.

## 권한
- `name`: `full` 은 전체, 제한 역할은 소유자 판정(`can_delete`)과 동일.
- `excluded_from_stats`: **`full` 전용**입니다. 집계 제외는 다른 사람이 보는 숫자를 바꾸는
  행위라 소유자 여부와 무관하게 제한 역할에 허용하지 않습니다(삭제와 판정이 다릅니다).
  응답의 **`can_exclude`** 로 판정을 내려주니 그 값으로 토글을 렌더하세요 — `false` 인데
  보내면 **403** `{"details": {"code": "exclude_not_allowed"}}` 입니다.
- 현재 PATCH 경로 자체가 제한 역할 화이트리스트에 없어 미들웨어에서 먼저 403
  (`section_forbidden`) 입니다. 위 판정은 경로를 열더라도 구멍이 생기지 않게 미리 닫아둔 것입니다.

## 주의사항
- `base_url`/`utm_*` 는 수정할 수 없습니다 — `url`·`channel` 파생값과 방문 매칭 4-튜플의
  일관성을 위해 UTM 조합을 바꾸려면 **삭제 후 재생성**하세요 (보내도 무시됩니다).
- 성공 시 `AdminActionLog(channel_link.update)` 감사 기록(바뀐 필드만). PUT 은 비활성화.
- 대시보드 캐시(`admin:dash:mkt:*`)를 즉시 무효화하므로 다음 조회에 바로 반영됩니다.

### 요청 예시
```bash
# 집계에서 빼기
curl -X PATCH -H "Authorization: Bearer <staff_token>" -H "Content-Type: application/json" \\
  -d '{"excluded_from_stats": true}' \\
  "https://api.example.com/api/v1/admin/marketing/channel-links/3/"
```
        """,
        request=AdminChannelLinkUpdateSerializer,
        responses={
            200: AdminChannelLinkSerializer,
            400: OpenApiResponse(description="검증 실패 (필드 없음/길이 초과)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(
                description="관리자 권한 없음(is_staff=False) / 남이 만든 링크"
                '(details.code="not_link_owner") / 집계 제외 권한 없음'
                '(details.code="exclude_not_allowed")'
            ),
            404: OpenApiResponse(description="해당 링크 없음"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "요청 예시 (이름 수정)",
                request_only=True,
                value={"name": "7월 틱톡 리타겟팅 v2"},
            ),
            OpenApiExample(
                "요청 예시 (집계에서 빼기)",
                request_only=True,
                value={"excluded_from_stats": True},
            ),
        ],
    )
    def patch(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def _owner_guard(self, request, link, verb: str):
        """제한 역할(외주)의 **남의 링크** 변경 차단 → 403 Response, 통과면 None.

        수정과 삭제가 같은 판정 함수를 쓴다 — 화이트리스트에 새 메서드를 열 때
        소유자 검사를 빠뜨리는 사고를 막는다(경로 게이트는 미들웨어, 객체 권한은 뷰).
        """
        role = resolve_admin_role(request)
        if can_delete_channel_link(role, request.user, link):
            return None
        log_admin_action(
            request=request,
            action=AdminActionLog.Action.ADMIN_ACCESS_DENIED,
            target_type="channel_link",
            target_id=link.pk,
            target_repr=f"{verb} {link.name}",  # 절단은 log_admin_action 이 담당
            changes={"admin_role": role, "reason": NOT_LINK_OWNER_CODE},
        )
        logger.warning(
            "[admin-marketing] req=%s role=%s 남의 링크 %s 시도 차단 id=%s",
            getattr(request, "id", ""),
            role,
            verb,
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

    def _exclude_guard(self, request, link):
        """MKT-12 — 집계 제외 토글은 full 전용. 통과면 None, 아니면 403 Response.

        소유자 스코프(_owner_guard)와 **별도 게이트**다: 삭제는 자기가 만든 것을 치우는
        일이지만, 집계 제외는 다른 사람이 보는 숫자를 바꾼다. 응답의 ``can_exclude`` 와
        같은 함수를 쓴다 — 갈라지면 화면 토글과 실제 동작이 어긋난다.
        """
        role = resolve_admin_role(request)
        if can_exclude_channel_link_from_stats(role):
            return None
        log_admin_action(
            request=request,
            action=AdminActionLog.Action.ADMIN_ACCESS_DENIED,
            target_type="channel_link",
            target_id=link.pk,
            target_repr=f"EXCLUDE {link.name}",  # 절단은 log_admin_action 이 담당
            changes={"admin_role": role, "reason": EXCLUDE_FORBIDDEN_CODE},
        )
        logger.warning(
            "[admin-marketing] req=%s role=%s 집계 제외 토글 차단 id=%s",
            getattr(request, "id", ""),
            role,
            link.pk,
        )
        return Response(
            {
                "success": False,
                "error": {
                    "code": 403,
                    "message": EXCLUDE_FORBIDDEN_MESSAGE,
                    "details": {"code": EXCLUDE_FORBIDDEN_CODE, "admin_role": role},
                },
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    def update(self, request, *args, **kwargs):
        link = self.get_object()
        # 현재 PATCH 는 제한 역할 화이트리스트에 없어 미들웨어에서 먼저 막힌다. 그래도 여기서
        # 한 번 더 검사한다 — 나중에 경로만 열면 남의 링크 이름을 바꿀 수 있는 구멍이 되므로
        # (DELETE 와 달리 게이트가 없었다). full 역할은 항상 통과라 회귀 없음.
        denied = self._owner_guard(request, link, "PATCH")
        if denied is not None:
            return denied
        # MKT-12: 집계 제외를 **건드리려 할 때만** 추가 게이트. 이름만 바꾸는 PATCH 는
        # 기존 소유자 판정 그대로 통과한다(경로가 열릴 때를 대비한 선반영).
        if "excluded_from_stats" in request.data:
            denied = self._exclude_guard(request, link)
            if denied is not None:
                return denied
        before_name, before_excluded = link.name, link.excluded_from_stats

        serializer = self.get_serializer(link, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        changes: dict = {}
        if link.name != before_name:
            changes["name"] = {"before": before_name, "after": link.name}
        if link.excluded_from_stats != before_excluded:
            changes["excluded_from_stats"] = {
                "before": before_excluded,
                "after": link.excluded_from_stats,
            }
        if changes:
            log_admin_action(
                request=request,
                action=AdminActionLog.Action.CHANNEL_LINK_UPDATE,
                target_type="channel_link",
                target_id=link.pk,
                target_repr=link.name,
                changes=changes,
            )
            logger.info(
                "[admin-marketing] req=%s 채널 링크 수정 id=%s fields=%s",
                getattr(request, "id", ""),
                link.pk,
                ",".join(changes),
            )
        # MKT-11: 이름은 채널별 성과 행의 label, excluded_from_stats 는 행 자체의 유무를
        # 바꾸므로 둘 다 캐시를 지워야 즉시 반영된다 (안 지우면 최대 5분간 안 사라진다).
        bust_marketing_dashboard_cache(reason="channel_link.update")
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
        denied = self._owner_guard(request, link, "DELETE")
        if denied is not None:
            return denied

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
        # MKT-11: 지운 링크의 행이 최대 5분간 남아 보이지 않게
        bust_marketing_dashboard_cache(reason="channel_link.delete")
        return Response(status=status.HTTP_204_NO_CONTENT)
