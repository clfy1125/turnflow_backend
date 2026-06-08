"""apps/admin_api/views/dashboard.py — 어드민 대시보드 단일 집계 엔드포인트.

라우팅: ``GET /api/v1/admin/metrics/overview/`` (``IsAdminUser``, is_staff=True).

백오피스 메인 화면 1장에 필요한 전사(GLOBAL) 지표를 한 번의 호출로 반환한다.
회원/워크스페이스/페이지/캠페인/DM/IG 연동 카운트와 "즉시 조치 필요(attention)"
항목을 중첩 구조로 묶는다. 모든 카운트는 request.user 소속과 무관한 전수 집계다
(워크스페이스 필터 없음).

DM 도착률(delivery_rate) 계산은 ``/api/v1/integrations/dm-verification/stats/``
(apps/integrations/verification_views.py) 의 공식과 **정확히 동일**하다:
    accepted_or_after = accepted + delivered + read + failed_no_trace
    confirmed_delivered = delivered + read
    delivery_rate = confirmed_delivered / accepted_or_after
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.serializers.dashboard import AdminMetricsOverviewSerializer
from apps.billing.models import SubscriptionPlan, UserSubscription
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.pages.models import Page
from apps.workspace.models import Workspace

logger = logging.getLogger(__name__)

User = get_user_model()

# 임계값 (manifest assumptions 참고)
LOW_DELIVERY_THRESHOLD = 0.9  # 최근 24h 도착률이 이 값 미만이면 "주의" 노출
STUCK_SUBMITTING_MINUTES = 10  # SUBMITTING 이 이 시간(분) 넘게 정체되면 stuck 카운트
ATTENTION_CAP = 50  # attention 리스트 최대 길이


def _accepted_or_after(agg: dict) -> int:
    """ACCEPTED 진입 건 수 — stats 엔드포인트와 동일 정의."""
    return agg["accepted"] + agg["delivered"] + agg["read"] + agg["failed_no_trace"]


def _delivery_rate(agg: dict) -> float:
    """도착 확정 비율 (0~1) — stats 엔드포인트와 동일 공식."""
    accepted_or_after = _accepted_or_after(agg)
    confirmed_delivered = agg["delivered"] + agg["read"]
    rate = confirmed_delivered / accepted_or_after if accepted_or_after else 0.0
    return round(rate, 4)


class AdminMetricsOverviewView(APIView):
    """어드민 대시보드 전역 지표 집계 (단일 GET)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminMetricsOverviewSerializer

    @extend_schema(
        tags=["admin-dashboard"],
        summary="[관리자] 대시보드 지표 집계",
        description="""
## 개요
백오피스 대시보드 메인 화면에 필요한 **전사(GLOBAL) 지표**를 단일 호출로 반환합니다.
회원/워크스페이스/페이지/자동 DM 캠페인/DM 발송/IG 연동 카운트와, 운영자가 즉시
조치해야 할 항목(`attention`)을 하나의 중첩 JSON 으로 묶어 내려줍니다.

## 사용 시나리오
- 관리자 로그인 직후 대시보드 첫 진입 시 1회 호출하여 상단 카드/차트를 채움
- 5~10분 간격 폴링으로 수치를 갱신 (실시간성이 크게 필요치 않은 요약 지표)
- `attention` 블록으로 만료 토큰/저조도착 계정/정체 발송을 즉시 식별

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만 접근)
- 일반 사용자/비스태프는 403.

## 비즈니스 로직
- **전수 집계**: request.user 의 소속 워크스페이스로 필터하지 않습니다 (백오피스 전역).
- `users`:
  - `new_today` 는 Asia/Seoul 기준 오늘 0시 이후 `date_joined`,
  - `new_7d` 는 최근 7일, `verified` 는 `is_email_verified=True`.
- `workspaces.by_plan`(⚠️ **DEPRECATED**): 레거시 `Workspace.plan` 별 카운트
  (starter/pro/enterprise). 실제 과금과 무관 — `subscriptions.by_plan` 사용 권장.
- `subscriptions.by_plan`: **실제 구독**(UserSubscription) 플랜별 회원 수.
  플랜이 DB-driven 이라 고정 키 대신 동적 리스트 `[{name, display_name, count}]`
  (비활성 플랜 포함, sort_order 오름차순).
- `campaigns`: `AutoDMCampaign.Status` 별 (active/paused/completed) + total.
- `dm`: `SentDMLog` 중 `created_at >= since` 만 집계.
  `delivery_rate` 는 `/integrations/dm-verification/stats/` 와 동일 공식
  (`(delivered+read) / (accepted+delivered+read+failed_no_trace)`).
- `ig_connections`: `IGAccountConnection.Status` 별 (active/expired/revoked/error).
- `attention`:
  - `expired_tokens`: status=expired 인 IG 계정 (소유자 이메일 포함, 최대 50건),
  - `low_delivery_accounts`: status=active 이면서 최근 24h 도착률 < 0.9 이고
    accepted-or-after 발송이 1건 이상인 계정 (최대 50건),
  - `stuck_submitting`: status=submitting 으로 10분 넘게 정체된 DM 로그 수.

## 주의사항
- IG access_token 등 비밀값은 **절대 직렬화하지 않습니다** — 상태/카운트만 노출.
- `since` 미지정 시 기본값은 `now - 30일` 입니다.
- `since` 가 ISO 8601 로 파싱되지 않으면 기본값(30일 전)으로 폴백합니다 (400 미발생).
        """,
        parameters=[
            OpenApiParameter(
                name="since",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "DM 집계 시작 시각 (ISO 8601, 예: 2026-05-01T00:00:00+09:00). "
                    "미지정 시 now-30일. 파싱 실패 시에도 now-30일로 폴백."
                ),
            ),
        ],
        responses={
            200: AdminMetricsOverviewSerializer,
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자(is_staff) 권한 없음"),
        },
        examples=[
            OpenApiExample(
                "응답 예시",
                response_only=True,
                value={
                    "users": {
                        "total": 1280,
                        "active": 1190,
                        "new_today": 7,
                        "new_7d": 53,
                        "verified": 980,
                    },
                    "workspaces": {"by_plan": {"starter": 900, "pro": 110, "enterprise": 12}},
                    "subscriptions": {
                        "by_plan": [
                            {"name": "free", "display_name": "무료", "count": 1100},
                            {"name": "pro", "display_name": "프로", "count": 175},
                            {"name": "admin", "display_name": "관리자", "count": 5},
                        ]
                    },
                    "pages": {"total": 2100, "public": 1450, "active": 1980},
                    "campaigns": {
                        "active": 340,
                        "paused": 58,
                        "completed": 120,
                        "total": 518,
                    },
                    "dm": {
                        "accepted": 50,
                        "delivered": 11200,
                        "delivery_rate": 0.9991,
                        "failed_token": 12,
                        "failed_window": 30,
                        "failed_param": 4,
                        "failed_no_trace": 9,
                        "since": "2026-05-03T00:00:00+09:00",
                    },
                    "ig_connections": {
                        "active": 410,
                        "expired": 23,
                        "revoked": 15,
                        "error": 3,
                    },
                    "attention": {
                        "expired_tokens": [
                            {
                                "id": "5b1f0c2e-0000-4a00-9c00-000000000001",
                                "username": "brand_official",
                                "owner_email": "owner@example.com",
                            }
                        ],
                        "low_delivery_accounts": [
                            {
                                "id": "5b1f0c2e-0000-4a00-9c00-000000000002",
                                "username": "shop_kr",
                                "delivery_rate": 0.83,
                            }
                        ],
                        "stuck_submitting": 2,
                    },
                },
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        request_id = getattr(request, "id", "") or ""
        now = timezone.now()

        # ── since 파싱 (ISO; 실패/미지정 → now-30d) ─────────────────────
        since_raw = request.query_params.get("since")
        since = None
        if since_raw:
            since = parse_datetime(since_raw)
            if since is not None and timezone.is_naive(since):
                since = timezone.make_aware(since, timezone.get_current_timezone())
        if since is None:
            since = now - timedelta(days=30)

        # ── 회원 지표 (Asia/Seoul 오늘 경계) ───────────────────────────
        today_local = timezone.localdate()  # TIME_ZONE=Asia/Seoul
        today_start = timezone.make_aware(
            timezone.datetime.combine(today_local, timezone.datetime.min.time()),
            timezone.get_current_timezone(),
        )
        seven_days_ago = now - timedelta(days=7)

        users_agg = User.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            new_today=Count("id", filter=Q(date_joined__gte=today_start)),
            new_7d=Count("id", filter=Q(date_joined__gte=seven_days_ago)),
            verified=Count("id", filter=Q(is_email_verified=True)),
        )

        # ── 워크스페이스 플랜 분포 (⚠️ DEPRECATED — 레거시 Workspace.plan) ───
        plan_counts = {"starter": 0, "pro": 0, "enterprise": 0}
        for row in Workspace.objects.values("plan").annotate(c=Count("id")).order_by():
            if row["plan"] in plan_counts:
                plan_counts[row["plan"]] = row["c"]

        # ── 실제 구독(UserSubscription) 플랜 분포 — DB-driven 동적 리스트 ───
        # 모든 플랜(비활성 포함)을 sort_order 순으로 노출. count 는 UserSubscription 기준.
        sub_count_map = {
            r["plan_id"]: r["c"]
            for r in UserSubscription.objects.values("plan_id").annotate(c=Count("id"))
        }
        subscriptions_by_plan = [
            {
                "name": p.name,
                "display_name": p.display_name,
                "count": sub_count_map.get(p.id, 0),
            }
            for p in SubscriptionPlan.objects.all().order_by("sort_order", "name")
        ]

        # ── 페이지 지표 ────────────────────────────────────────────────
        pages_agg = Page.objects.aggregate(
            total=Count("id"),
            public=Count("id", filter=Q(is_public=True)),
            active=Count("id", filter=Q(is_active=True)),
        )

        # ── 캠페인 상태별 ──────────────────────────────────────────────
        camp_agg = AutoDMCampaign.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status=AutoDMCampaign.Status.ACTIVE)),
            paused=Count("id", filter=Q(status=AutoDMCampaign.Status.PAUSED)),
            completed=Count("id", filter=Q(status=AutoDMCampaign.Status.COMPLETED)),
        )

        # ── DM 발송 지표 (since 이후) ──────────────────────────────────
        dm_agg = SentDMLog.objects.filter(created_at__gte=since).aggregate(
            accepted=Count("id", filter=Q(status=SentDMLog.Status.ACCEPTED)),
            delivered=Count("id", filter=Q(status=SentDMLog.Status.DELIVERED)),
            read=Count("id", filter=Q(status=SentDMLog.Status.READ)),
            failed_token=Count("id", filter=Q(status=SentDMLog.Status.FAILED_TOKEN)),
            failed_window=Count("id", filter=Q(status=SentDMLog.Status.FAILED_WINDOW)),
            failed_param=Count("id", filter=Q(status=SentDMLog.Status.FAILED_PARAM)),
            failed_no_trace=Count("id", filter=Q(status=SentDMLog.Status.FAILED_NO_TRACE)),
        )
        dm_delivery_rate = _delivery_rate(dm_agg)

        # ── IG 연동 상태별 ─────────────────────────────────────────────
        ig_agg = IGAccountConnection.objects.aggregate(
            active=Count("id", filter=Q(status=IGAccountConnection.Status.ACTIVE)),
            expired=Count("id", filter=Q(status=IGAccountConnection.Status.EXPIRED)),
            revoked=Count("id", filter=Q(status=IGAccountConnection.Status.REVOKED)),
            error=Count("id", filter=Q(status=IGAccountConnection.Status.ERROR)),
        )

        # ── attention: 만료 토큰 계정 ──────────────────────────────────
        expired_tokens = []
        expired_qs = (
            IGAccountConnection.objects.filter(status=IGAccountConnection.Status.EXPIRED)
            .select_related("workspace__owner")
            .order_by("-updated_at")[:ATTENTION_CAP]
        )
        for conn in expired_qs:
            owner = getattr(getattr(conn, "workspace", None), "owner", None)
            expired_tokens.append(
                {
                    "id": str(conn.id),
                    "username": conn.username,
                    "owner_email": getattr(owner, "email", "") or "",
                }
            )

        # ── attention: 최근 24h 저조도착 활성 계정 ─────────────────────
        last_24h = now - timedelta(hours=24)
        per_conn = (
            SentDMLog.objects.filter(
                created_at__gte=last_24h,
                campaign__ig_connection__status=IGAccountConnection.Status.ACTIVE,
            )
            .values(
                "campaign__ig_connection_id",
                "campaign__ig_connection__username",
            )
            .annotate(
                accepted=Count("id", filter=Q(status=SentDMLog.Status.ACCEPTED)),
                delivered=Count("id", filter=Q(status=SentDMLog.Status.DELIVERED)),
                read=Count("id", filter=Q(status=SentDMLog.Status.READ)),
                failed_no_trace=Count("id", filter=Q(status=SentDMLog.Status.FAILED_NO_TRACE)),
            )
        )
        low_delivery_accounts = []
        for row in per_conn:
            denom = _accepted_or_after(row)
            if denom < 1:
                continue  # accepted-or-after 발송 1건 이상만 평가
            rate = _delivery_rate(row)
            if rate < LOW_DELIVERY_THRESHOLD:
                low_delivery_accounts.append(
                    {
                        "id": str(row["campaign__ig_connection_id"]),
                        "username": row["campaign__ig_connection__username"] or "",
                        "delivery_rate": rate,
                    }
                )
        # 도착률 낮은 순으로 정렬 후 cap
        low_delivery_accounts.sort(key=lambda r: r["delivery_rate"])
        low_delivery_accounts = low_delivery_accounts[:ATTENTION_CAP]

        # ── attention: SUBMITTING 정체 ─────────────────────────────────
        stuck_cutoff = now - timedelta(minutes=STUCK_SUBMITTING_MINUTES)
        stuck_submitting = SentDMLog.objects.filter(
            status=SentDMLog.Status.SUBMITTING,
            created_at__lt=stuck_cutoff,
        ).count()

        logger.info(
            "[admin-metrics] req=%s users=%s ws=%s pages=%s campaigns=%s "
            "expired_tokens=%s low_delivery=%s stuck=%s",
            request_id,
            users_agg["total"],
            sum(plan_counts.values()),
            pages_agg["total"],
            camp_agg["total"],
            len(expired_tokens),
            len(low_delivery_accounts),
            stuck_submitting,
        )

        payload = {
            "users": {
                "total": users_agg["total"],
                "active": users_agg["active"],
                "new_today": users_agg["new_today"],
                "new_7d": users_agg["new_7d"],
                "verified": users_agg["verified"],
            },
            "workspaces": {"by_plan": plan_counts},
            "subscriptions": {"by_plan": subscriptions_by_plan},
            "pages": {
                "total": pages_agg["total"],
                "public": pages_agg["public"],
                "active": pages_agg["active"],
            },
            "campaigns": {
                "active": camp_agg["active"],
                "paused": camp_agg["paused"],
                "completed": camp_agg["completed"],
                "total": camp_agg["total"],
            },
            "dm": {
                "accepted": dm_agg["accepted"],
                "delivered": dm_agg["delivered"],
                "delivery_rate": dm_delivery_rate,
                "failed_token": dm_agg["failed_token"],
                "failed_window": dm_agg["failed_window"],
                "failed_param": dm_agg["failed_param"],
                "failed_no_trace": dm_agg["failed_no_trace"],
                "since": since.isoformat(),
            },
            "ig_connections": {
                "active": ig_agg["active"],
                "expired": ig_agg["expired"],
                "revoked": ig_agg["revoked"],
                "error": ig_agg["error"],
            },
            "attention": {
                "expired_tokens": expired_tokens,
                "low_delivery_accounts": low_delivery_accounts,
                "stuck_submitting": stuck_submitting,
            },
        }

        serializer = AdminMetricsOverviewSerializer(payload)
        return Response(serializer.data)
