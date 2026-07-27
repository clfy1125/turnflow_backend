"""apps/admin_api/views/spam.py — 어드민 스팸 차단 댓글 로그 조회 (OPS-3).

라우팅: ``GET /api/v1/admin/spam/logs/`` (``IsAdminUser``, is_staff=True).

운영 대시보드 `스팸 방어` 카드의 `자세히 보기` — "무엇이 차단됐는가"를 개별 로그로 본다.
사용자용 ``/integrations/spam-filters/ig-connections/{id}/logs/`` 는 워크스페이스 멤버십
스코프 + 연결 1개 단위 + 원본 payload 포함이라 대시보드 용도로 쓸 수 없어 신설했다.

정합성 계약(중요): ``status`` 미지정 시 ``total`` 은 같은 기간
``dashboard/operations`` 의 ``spam.detected`` 와 **정확히 일치**한다 — 양쪽 모두
``SPAM_DETECTED_STATUSES``(detected/hidden/failed, clean 제외) + ``[since, until)`` 범위.
기간 규약(``window`` 프리셋 / 커스텀 ``start``·``end`` / 최대 92일)도 운영 대시보드의
헬퍼를 그대로 재사용한다(복제 금지).

권한: ``IsAdminUser``. 제한 역할(marketing_viewer)은 미들웨어에서 403 (RBAC-2) —
댓글 원문·소유자 이메일이 담기므로 외주 계정에 열리면 안 된다.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime

from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.serializers.spam import COMMENT_TEXT_MAX, AdminSpamLogListSerializer

# 기간 규약은 운영 대시보드와 **완전히 동일**해야 한다 → 헬퍼 재사용 (복제 금지)
from apps.admin_api.views.dashboard_ops import (
    ALLOWED_WINDOWS,
    MAX_CUSTOM_SPAN_DAYS,
    SPAM_DETECTED_STATUSES,
    _custom_bounds,
    _parse_custom_range,
    _window_bounds,
)
from apps.integrations.models import AutoDMCampaign, SpamCommentLog
from apps.integrations.services import is_instagram_permalink

logger = logging.getLogger(__name__)

TAG = "admin-spam"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ALLOWED_STATUSES = (
    SpamCommentLog.Status.DETECTED,
    SpamCommentLog.Status.HIDDEN,
    SpamCommentLog.Status.FAILED,
)


def _encode_cursor(created_at, log_id) -> str:
    """(created_at, id) 키셋 커서 → base64(JSON). 동시각 행에서도 경계가 안정적."""
    raw = json.dumps({"c": created_at.isoformat(), "i": str(log_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """커서 → (created_at, id). 형식이 깨졌으면 ValueError."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        return datetime.fromisoformat(payload["c"]), str(payload["i"])
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("cursor 형식이 올바르지 않습니다") from exc


def _bad_request(message: str, details: dict):
    return Response(
        {"success": False, "error": {"code": 400, "message": message, "details": details}},
        status=http_status.HTTP_400_BAD_REQUEST,
    )


class AdminSpamLogListView(APIView):
    """전역 스팸 차단 댓글 로그 목록 (커서 페이지네이션)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminSpamLogListSerializer

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 스팸 차단 댓글 로그",
        description="""
## 개요
스팸 필터가 **어떤 댓글을 차단했는지** 개별 로그로 조회합니다(전 워크스페이스).
운영 대시보드 `스팸 방어` 카드의 `자세히 보기` 드릴다운용입니다.

## 사용 시나리오
- "감지 수는 늘었는데 무엇이 잡혔나?" — 상태/카테고리 필터 + 무한 스크롤로 확인
- 오탐 신고 대응 — `q` 로 작성자·본문 검색 후 판정 이유(`spam_reasons`)·신뢰도 확인
- 숨김 실패(`status=failed`) 목록에서 `error_message` 로 원인 파악

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True)
- 미인증 **401**, 비스태프 **403**.
- **마케팅 조회 전용 역할(`marketing_viewer`)은 403** — 댓글 원문·소유자 이메일이 담깁니다.

## 비즈니스 로직
- **전역 조회** — request.user 워크스페이스로 필터링하지 않습니다.
- 기간 규약은 운영 대시보드와 **동일**합니다: `window` = `1h`|`24h`(기본)|`today`|`7d`|`30d`,
  또는 커스텀 `start`&`end`(YYYY-MM-DD, Asia/Seoul 로컬 날짜, 최대 92일). 둘을 함께 주면
  커스텀이 우선하고 `window` 는 무시됩니다. 범위는 `[since, until)`.
- `status` 미지정 시 **detected + hidden + failed**(=`clean` 제외)를 모두 포함하며,
  이때 `total` 은 같은 기간 `dashboard/operations` 의 **`spam.detected` 와 일치**합니다.
  (`spam.checked` 는 `clean` 을 포함한 전체 검사 수라 값이 다릅니다 — 정상입니다.)
- 정렬은 **`created_at desc` 고정**(동시각은 id desc 로 tie-break).
- 페이지네이션은 **커서 방식** — `next_cursor` 를 그대로 다음 요청의 `cursor` 로 넘기세요.
  마지막 페이지면 `next_cursor=null`. `total` 은 커서와 무관한 전체 건수입니다.
- `comment_text` 는 **500자 상한**(초과 시 `comment_text_truncated=true`).
  `webhook_payload` / `api_response` 원본은 **제외**합니다(용량 + 불필요한 원본 PII).
- `media_permalink` 는 같은 `media_id` 의 DM 캠페인이 보유한 permalink 를 best-effort 로
  조인한 값입니다(스팸 로그 자체는 permalink 를 저장하지 않음). 없으면 빈 문자열.
- `link` 는 기존 link_hint 규약 `{page, params}` 그대로입니다.
- 읽기 전용 — 감사 로그(AdminActionLog)를 남기지 않습니다.

## 주의사항
- 잘못된 `window`/`status`/`cursor`/커스텀 범위는 **400** (`error.details.reason`).
- `limit` 은 1~200, 기본 50. 범위를 벗어나면 상·하한으로 잘립니다(400 아님).

### 요청 예시
```bash
# 최근 24시간 숨김 처리된 스팸만
curl -H "Authorization: Bearer <staff_token>" \\
  "https://api.example.com/api/v1/admin/spam/logs/?window=24h&status=hidden&limit=50"

# 다음 페이지
curl -H "Authorization: Bearer <staff_token>" \\
  "https://api.example.com/api/v1/admin/spam/logs/?window=24h&status=hidden&cursor=eyJjIjoi..."
```
        """,
        parameters=[
            OpenApiParameter(
                name="window",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=list(ALLOWED_WINDOWS),
                description="기간 프리셋 (기본 24h). start&end 를 함께 주면 무시됩니다.",
            ),
            OpenApiParameter(
                name="start",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=f"커스텀 시작일 YYYY-MM-DD (end 와 함께, 최대 {MAX_CUSTOM_SPAN_DAYS}일)",
            ),
            OpenApiParameter(
                name="end",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="커스텀 종료일 YYYY-MM-DD (포함)",
            ),
            OpenApiParameter(
                name="status",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=[s.value for s in ALLOWED_STATUSES],
                description="미지정 시 3종 전체 (clean 은 항상 제외)",
            ),
            OpenApiParameter(
                name="category",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="spam_category 정확 일치 필터",
            ),
            OpenApiParameter(
                name="ig_connection_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="특정 IG 연결(UUID)만",
            ),
            OpenApiParameter(
                name="q",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="commenter_username / comment_text 부분일치(대소문자 무시)",
            ),
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="이전 응답의 next_cursor",
            ),
            OpenApiParameter(
                name="limit",
                type=int,
                location=OpenApiParameter.QUERY,
                required=False,
                description=f"페이지 크기 (기본 {DEFAULT_LIMIT}, 최대 {MAX_LIMIT})",
            ),
        ],
        responses={
            200: AdminSpamLogListSerializer,
            400: OpenApiResponse(
                description="잘못된 window/status/cursor/커스텀 범위 — "
                '{"success": false, "error": {"code": 400, "message": "...", '
                '"details": {"reason": "..."}}}'
            ),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(
                description="관리자 권한 없음(is_staff=False) 또는 마케팅 조회 전용 역할 "
                '(error.details.code = "section_forbidden")'
            ),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                response_only=True,
                value={
                    "total": 1284,
                    "next_cursor": "eyJjIjoiMjAyNi0wNy0yN1QwMToxMjowMyswOTowMCIsImkiOiI4ZjFjIn0=",
                    "results": [
                        {
                            "id": "8f1c2b90-0f7a-4b1e-9a51-2b4c7d0e1a33",
                            "created_at": "2026-07-27T01:12:03+09:00",
                            "hidden_at": "2026-07-27T01:12:05+09:00",
                            "status": "hidden",
                            "ig_username": "brand_official",
                            "ig_connection_id": "3f2a1c88-1111-2222-3333-444455556666",
                            "owner_email": "owner@example.com",
                            "workspace_name": "브랜드 A",
                            "commenter_username": "spam_acc_01",
                            "comment_id": "17900000000000000",
                            "comment_text": "지금 디엠주세요 수익인증 …",
                            "comment_text_truncated": False,
                            "media_id": "17841000000000000",
                            "media_permalink": "https://www.instagram.com/reel/DaTFB8sS9zY/",
                            "spam_reasons": ["contains_url", "keyword:수익인증"],
                            "spam_category": "scam",
                            "confidence": 0.94,
                            "engine": "llm",
                            "error_message": "",
                            "link": {
                                "page": "/auto-dm/ig-connections",
                                "params": {"id": "3f2a1c88-1111-2222-3333-444455556666"},
                            },
                        }
                    ],
                },
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        now = timezone.now()
        params = request.query_params

        # ── 기간 (운영 대시보드와 동일 규약) ──
        start_raw, end_raw = params.get("start"), params.get("end")
        if start_raw or end_raw:
            if not (start_raw and end_raw):
                return _bad_request(
                    "커스텀 범위는 start 와 end 를 모두 지정해야 합니다",
                    {"reason": "start 와 end 를 함께 제공하세요"},
                )
            try:
                start_d, end_d = _parse_custom_range(start_raw, end_raw, now)
            except ValueError as exc:
                return _bad_request("잘못된 커스텀 범위입니다", {"reason": str(exc)})
            since, until, _granularity = _custom_bounds(start_d, end_d, now)
        else:
            window = params.get("window", "24h")
            if window not in ALLOWED_WINDOWS:
                return _bad_request(
                    f"잘못된 window 값입니다: {window!r}", {"allowed": list(ALLOWED_WINDOWS)}
                )
            since, _granularity = _window_bounds(window, now)
            until = now

        # ── 필터 ──
        qs = SpamCommentLog.objects.filter(created_at__gte=since, created_at__lt=until)
        status_param = params.get("status")
        if status_param:
            if status_param not in [s.value for s in ALLOWED_STATUSES]:
                return _bad_request(
                    f"잘못된 status 값입니다: {status_param!r}",
                    {"allowed": [s.value for s in ALLOWED_STATUSES]},
                )
            qs = qs.filter(status=status_param)
        else:
            # 기본 모집단 = 운영 대시보드 spam.detected 와 동일 (clean 제외)
            qs = qs.filter(status__in=SPAM_DETECTED_STATUSES)

        if category := params.get("category"):
            qs = qs.filter(spam_category=category)
        if conn_id := params.get("ig_connection_id"):
            qs = qs.filter(spam_filter__ig_connection_id=conn_id)
        if q := params.get("q"):
            qs = qs.filter(Q(commenter_username__icontains=q) | Q(comment_text__icontains=q))

        total = qs.count()

        # ── 커서 페이지네이션 (created_at desc, id desc tie-break) ──
        try:
            limit = int(params.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, MAX_LIMIT))

        page_qs = qs
        if cursor := params.get("cursor"):
            try:
                c_at, c_id = _decode_cursor(cursor)
            except ValueError as exc:
                return _bad_request("잘못된 cursor 입니다", {"reason": str(exc)})
            page_qs = page_qs.filter(Q(created_at__lt=c_at) | Q(created_at=c_at, id__lt=c_id))

        rows = list(
            page_qs.select_related("spam_filter__ig_connection__workspace__owner").order_by(
                "-created_at", "-id"
            )[: limit + 1]
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        )

        # media_id → permalink (같은 게시물의 DM 캠페인이 이미 갖고 있으면 재사용, best-effort).
        # 판정은 캠페인 시리얼라이저와 동일한 is_instagram_permalink (media_url 에는
        # 이미지 CDN URL 이 들어 있을 수 있어 그대로 쓰면 안 됨).
        media_ids = {r.media_id for r in rows if r.media_id}
        permalinks: dict = {}
        if media_ids:
            permalinks = {
                m: url
                for m, url in AutoDMCampaign.objects.filter(media_id__in=media_ids)
                .exclude(media_url="")
                .values_list("media_id", "media_url")
                if is_instagram_permalink(url)
            }

        results = [self._row(r, permalinks) for r in rows]
        return Response({"total": total, "next_cursor": next_cursor, "results": results})

    @staticmethod
    def _row(log, permalinks: dict) -> dict:
        conn = getattr(log.spam_filter, "ig_connection", None)
        workspace = getattr(conn, "workspace", None)
        owner = getattr(workspace, "owner", None)
        text = log.comment_text or ""
        truncated = len(text) > COMMENT_TEXT_MAX
        conn_id = str(conn.id) if conn else ""
        return {
            "id": str(log.id),
            "created_at": timezone.localtime(log.created_at).isoformat(),
            "hidden_at": timezone.localtime(log.hidden_at).isoformat() if log.hidden_at else None,
            "status": log.status,
            "ig_username": getattr(conn, "username", "") or "",
            "ig_connection_id": conn_id,
            "owner_email": getattr(owner, "email", "") or "",
            "workspace_name": getattr(workspace, "name", "") or "",
            "commenter_username": log.commenter_username or "",
            "comment_id": log.comment_id or "",
            "comment_text": text[:COMMENT_TEXT_MAX],
            "comment_text_truncated": truncated,
            "media_id": log.media_id or "",
            "media_permalink": permalinks.get(log.media_id, ""),
            "spam_reasons": log.spam_reasons if isinstance(log.spam_reasons, list) else [],
            "spam_category": log.spam_category or "",
            "confidence": log.confidence,
            "engine": log.engine or "",
            "error_message": log.error_message or "",
            "link": {
                "page": "/auto-dm/ig-connections" if conn_id else None,
                "params": {"id": conn_id} if conn_id else {},
            },
        }
