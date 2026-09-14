"""홈 알림 API — `/api/v1/home/`.

- `GET  /api/v1/home/alerts/`            현재 알림 상태 (Graph 호출 0, 30초 캐시)
- `POST /api/v1/home/alerts/dismiss/`    권유 알림 닫기 (기기 간 유지)
"""

import logging

from django.core.cache import cache
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.workspace.models import Workspace

from .alerts import RANKS, build_home_alerts
from .models import HomeAlertDismissal
from .serializers import (
    HomeAlertDismissRequestSerializer,
    HomeAlertDismissResponseSerializer,
    HomeAlertsResponseSerializer,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30


def _cache_key(user_id, workspace_id) -> str:
    return f"home:alerts:{user_id}:{workspace_id}"


def _resolve_workspace(request, workspace_id=None):
    """워크스페이스 결정 — 캠페인 요약 엔드포인트와 같은 규약.

    지정하면 그것(멤버십 확인), 없으면 사용자의 워크스페이스가 하나일 때 자동 결정.
    여러 개인데 미지정이면 400.
    """
    qs = Workspace.objects.filter(memberships__user=request.user).distinct()
    if workspace_id:
        ws = qs.filter(id=workspace_id).first()
        if ws is None:
            return None, "워크스페이스를 찾을 수 없거나 권한이 없습니다."
        return ws, None
    found = list(qs[:2])
    if not found:
        return None, "소속된 워크스페이스가 없습니다."
    if len(found) > 1:
        return None, "워크스페이스가 여러 개입니다. workspace_id 를 지정하세요."
    return found[0], None


def _error(message, code=status.HTTP_400_BAD_REQUEST):
    """프로젝트 공통 에러 포맷(apps/core/exceptions.py)과 같은 모양으로 응답."""
    return Response(
        {"success": False, "error": {"code": code, "message": message, "details": {}}},
        status=code,
    )


class HomeAlertsView(APIView):
    """홈 화면 알림 목록."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="홈 알림 목록 조회",
        description="""
## 목적
홈 첫 화면에 띄울 **「지금 이 상태인가」** 를 한 번에 계산해 돌려줍니다.
이벤트를 쌓는 알림함이 아니라 **현재 상태 계산**이라, 문제가 해소되면 다음 호출에서 그 항목이
그냥 사라집니다(별도의 「복구됐어요」 알림이 필요 없습니다).

## 언제 호출하나
홈 진입 시 1회 + 이후 **60초 간격 폴링**을 권장합니다.
서버가 30초 캐시를 두므로 그보다 자주 불러도 값이 바뀌지 않습니다.

## 비용
**인스타(Meta Graph) 를 부르지 않습니다.** 전부 우리 DB 조회입니다.
(최신 게시물·웹훅 상태는 주기 태스크가 미리 적어 둔 값을 읽습니다.)
따라서 홈에서 마음 놓고 폴링해도 Graph 쿼터에 영향이 없습니다.

## 응답 구조
| 필드 | 의미 |
|---|---|
| `blocking` | **null 이 아니면 반드시 선택해야 넘어가는 모달**을 띄웁니다. 배너가 아닙니다. |
| `alerts[]` | 배너·카드로 뿌릴 항목들. `rank` 오름차순으로 이미 정렬돼 있습니다. |
| `counts` | 등급별 개수 (`critical` / `warning` / `todo`) |

### 등급(`level`)
- `critical` — 자동화가 실제로 멈춰 있음. **닫을 수 없습니다**(해소되면 자동으로 사라짐)
- `warning` — 완전히 멈추진 않았지만 새고 있음. 닫을 수 없습니다
- `todo` — 장애가 아니라 권유. **닫을 수 있습니다**(`dismissible: true`)

### 순서(`rank`)
작을수록 위. 원칙은 **돈이 새는 순 → 사용자가 지금 손쓸 수 있는 순**.
서버가 정해 내려보내므로 프론트에서 다시 정렬하지 마세요
(웹·앱·다른 화면이 같은 판단을 하도록 서버를 단일 소스로 둡니다).

### 문장은 프론트가 만듭니다
서버는 `code`(머신 키)와 `data`(숫자·시각)만 줍니다. 한국어/영어 문장은 프론트 i18n 에서
만드세요. 시각은 전부 ISO8601 입니다.

## 알림 코드 전체 목록
| code | level | data 주요 키 |
|---|---|---|
| `payment_failed` | critical | `plan` `amount` `grace_ends_at` `next_retry_at` `card_masked` |
| `ig_disconnected` | critical | `status` `reason` `revives_on_reconnect` |
| `comment_stream_down` | critical | `checked_at` |
| `dm_quota_exhausted` | critical | `used` `limit` **`blocked_count`** **`resumable_count`** `resumes_on_upgrade` `resets_at` |
| `campaign_post_restricted` | critical | `campaign_id` `name` `media_id` `permalink` `paused_at` `other_posts_unaffected` |
| `dm_send_blocked` | critical | `seconds_remaining` `resumes_at` `waiting_count` `auto_resumes` |
| `ig_account_disabled` | critical | `count` `allowance` `active` `accounts[]` |
| `dm_quota_warning` | warning | `used` `limit` `remaining` `resets_at` |
| `subscription_ending` | warning | `ends_at` `plan` `days_left` |
| `recent_post_no_campaign` | todo | `ig_connection_id` `media_id` `published_at` `permalink` |
| `create_campaign` | todo | `campaign_total` `is_default` |
| `link_page_empty` | todo | `reason`(`no_page`/`no_blocks`/`private`) `page_id` `slug` `blocks_count` |
| `report_ready` / `report_running` / `report_failed` | todo | `report_id` `status` `progress` `stage` `error_code` |
| `migration_review_pending` | todo | `count` `job_id` |

`blocking.code` 는 현재 `ig_account_selection_required` 하나입니다
(`max_ig_accounts` / `total_accounts` / `active_accounts`).

## 알아두실 점
- `create_campaign` 은 **항상 내려갑니다.** 캠페인이 0건이면 정상 권유(rank 210),
  이미 있으면 `data.is_default=true` + rank 999 로 맨 뒤에 옵니다 — 다른 알림이 하나도 없을 때
  홈이 비지 않게 하는 **기본 상태**입니다. 그래서 이 항목만 닫을 수 없습니다.
- `dm_quota_exhausted` 의 `blocked_count` 는 「지금 몇 건의 댓글에 DM 이 못 나가고 있는지」,
  `resumable_count` 는 「플랜을 올리면 바로 다시 나갈 수 있는 건수」입니다
  (되살림 창이 댓글 작성 후 7일이라 두 숫자가 다를 수 있습니다).

## 인증
`Authorization: Bearer <access_token>` 필수. 본인이 멤버인 워크스페이스만.
        """,
        parameters=[
            OpenApiParameter(
                name="workspace_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="워크스페이스 UUID. 사용자의 워크스페이스가 하나면 생략 가능.",
            ),
        ],
        responses={
            200: HomeAlertsResponseSerializer,
            400: OpenApiResponse(description="워크스페이스를 결정할 수 없음 / 잘못된 id"),
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="해당 워크스페이스 멤버가 아님"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "요청 (fetch)",
                value=(
                    "const res = await fetch('/api/v1/home/alerts/', {\n"
                    "  headers: { Authorization: `Bearer ${accessToken}` },\n"
                    "});\n"
                    "const { blocking, alerts } = await res.json();"
                ),
                request_only=True,
            ),
            OpenApiExample(
                "응답 200 — 한도 소진 + 새 게시물",
                value={
                    "generated_at": "2026-09-10T14:12:03+09:00",
                    "workspace_id": "0f2c…",
                    "blocking": None,
                    "alerts": [
                        {
                            "code": "dm_quota_exhausted",
                            "level": "critical",
                            "rank": 50,
                            "scope": "workspace",
                            "target_id": None,
                            "target_label": "",
                            "dismissible": False,
                            "dismiss_key": "",
                            "since": None,
                            "data": {
                                "used": 200,
                                "limit": 200,
                                "blocked_count": 37,
                                "resumable_count": 31,
                                "resumes_on_upgrade": True,
                                "resets_at": "2026-10-01T00:00:00+09:00",
                            },
                        },
                        {
                            "code": "recent_post_no_campaign",
                            "level": "todo",
                            "rank": 200,
                            "scope": "ig_connection",
                            "target_id": "17912…",
                            "target_label": "turnflow.official",
                            "dismissible": True,
                            "dismiss_key": "17912…",
                            "since": "2026-09-09T20:42:00+09:00",
                            "data": {
                                "ig_connection_id": "8b3a…",
                                "media_id": "17912…",
                                "published_at": "2026-09-09T20:42:00+09:00",
                                "permalink": "https://www.instagram.com/p/…",
                            },
                        },
                    ],
                    "counts": {"critical": 1, "warning": 0, "todo": 1},
                },
                response_only=True,
            ),
            OpenApiExample(
                "응답 400",
                value={
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "워크스페이스가 여러 개입니다. workspace_id 를 지정하세요.",
                        "details": {},
                    },
                },
                response_only=True,
                status_codes=["400"],
            ),
        ],
        tags=["Home"],
    )
    def get(self, request):
        workspace, err = _resolve_workspace(request, request.query_params.get("workspace_id"))
        if workspace is None:
            return _error(err)

        key = _cache_key(request.user.id, workspace.id)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)

        payload = build_home_alerts(workspace, request.user)
        try:
            cache.set(key, payload, timeout=CACHE_TTL_SECONDS)
        except Exception:  # noqa: BLE001 - 캐시 장애가 홈을 막지 않게
            logger.warning("home alerts 캐시 저장 실패 ws=%s", workspace.id)
        return Response(payload)


class HomeAlertDismissView(APIView):
    """권유 알림 닫기."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="홈 알림 닫기",
        description="""
## 목적
`level: "todo"` 인 권유 알림을 **더 이상 보지 않게** 합니다.
브라우저가 아니라 **서버에 저장**하므로, PC 에서 건너뛴 게시물이 폰·앱에서 다시 뜨지 않습니다.

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|---|---|---|---|
| `code` | ✅ | string | 닫을 알림의 `code`. `todo` 등급만 허용. |
| `target_key` | 선택 | string | 알림의 `dismiss_key` 를 **그대로** 넣으세요. 대상이 없으면 생략(빈 문자열). |
| `workspace_id` | 조건부 | uuid | 사용자의 워크스페이스가 여러 개일 때만 필수. |

## 다시 뜨는 규칙
`target_key` 단위로 기억합니다. 대상이 바뀌면 다시 뜹니다 —
게시물 A 를 건너뛰어도 **새로 올린 게시물 B 는 다시 뜹니다.**

## 닫을 수 없는 것
- `critical` / `warning` 등급 (자동화가 멈춘 상태를 닫으면 알림의 의미가 없습니다.
  **문제가 해소되면 저절로 사라집니다.**)
- `create_campaign` (다른 알림이 하나도 없을 때 홈을 채우는 기본 상태)
둘 다 **400** 을 돌려줍니다.

## 멱등성
같은 것을 두 번 닫아도 200 입니다(처음 닫은 시각을 그대로 돌려줍니다).

## 인증
`Authorization: Bearer <access_token>` 필수.
        """,
        request=HomeAlertDismissRequestSerializer,
        responses={
            200: HomeAlertDismissResponseSerializer,
            400: OpenApiResponse(
                description="닫을 수 없는 알림 / 알 수 없는 code / 워크스페이스 미결정"
            ),
            401: OpenApiResponse(description="인증 필요"),
            403: OpenApiResponse(description="해당 워크스페이스 멤버가 아님"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "요청 — 이 게시물 건너뛰기",
                value={"code": "recent_post_no_campaign", "target_key": "17912…"},
                request_only=True,
            ),
            OpenApiExample(
                "응답 200",
                value={
                    "code": "recent_post_no_campaign",
                    "target_key": "17912…",
                    "dismissed_at": "2026-09-10T14:20:00+09:00",
                },
                response_only=True,
            ),
            OpenApiExample(
                "응답 400 — 닫을 수 없는 알림",
                value={
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "이 알림은 닫을 수 없습니다. 문제가 해소되면 자동으로 사라집니다.",
                        "details": {},
                    },
                },
                response_only=True,
                status_codes=["400"],
            ),
        ],
        tags=["Home"],
    )
    def post(self, request):
        serializer = HomeAlertDismissRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["code"]
        target_key = serializer.validated_data.get("target_key") or ""

        workspace, err = _resolve_workspace(request, serializer.validated_data.get("workspace_id"))
        if workspace is None:
            return _error(err)

        if code not in RANKS:
            return _error(f"알 수 없는 알림 코드입니다: {code}")

        # 닫기 허용 여부는 alerts.Alert 와 같은 규칙을 쓴다 — 여기서 조건을 복제하면
        # 화면에는 닫기 버튼이 있는데 서버가 거절하는(또는 그 반대) 상태가 생긴다.
        from .alerts import LEVEL_TODO, Alert

        probe = Alert(code=code, level=LEVEL_TODO, target_id=target_key)
        if not probe.dismissible:
            return _error("이 알림은 닫을 수 없습니다. 문제가 해소되면 자동으로 사라집니다.")
        if RANKS[code] < RANKS["recent_post_no_campaign"]:
            return _error("이 알림은 닫을 수 없습니다. 문제가 해소되면 자동으로 사라집니다.")

        obj, _created = HomeAlertDismissal.objects.get_or_create(
            user=request.user, workspace=workspace, code=code, target_key=target_key
        )
        cache.delete(_cache_key(request.user.id, workspace.id))
        return Response({"code": code, "target_key": target_key, "dismissed_at": obj.dismissed_at})
