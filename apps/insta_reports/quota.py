"""리포트 생성 권한·이용 횟수 판정.

정책 (2026-07-29 확정):
  · **프로 전용** — 플랜 features `insta_report` (fail-closed)
  · **연동된 IG 계정당 캘린더월 1회** — features `insta_report_monthly_per_account`
    → 추가 IG 계정(9,900원)을 붙이면 그 계정 몫 1회가 그대로 늘어난다.
      (연동 2개 = 각 계정 1회 = 이번 달 총 2회)
  · 워크스페이스당 동시 생성 1건 (15분짜리 잡이라 큐가 밀리는 걸 막는다)
  · 실패는 이용 횟수 차감 안 함 (`InstagramReport.quota_consumed=False`)
  · 관리자(is_staff/superuser)는 무제한

월 경계는 서비스 표준 시간대(Asia/Seoul) 캘린더월이다.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException

from apps.billing.subscription_utils import get_user_plan, owner_has_feature
from apps.core.exceptions import PlanLimitExceededError
from apps.integrations.campaign_stats import is_admin_user

from .models import InstagramReport, ReportStatus

FEATURE_KEY = "insta_report"
LIMIT_KEY = "insta_report_monthly_per_account"
PLAN_REQUIRED = "pro"

# can_generate=false 사유 코드 (프론트 문구 분기용)
REASON_PLAN_REQUIRED = "PLAN_REQUIRED"
REASON_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
REASON_ALREADY_RUNNING = "ALREADY_RUNNING"
REASON_CONNECTION_INACTIVE = "CONNECTION_INACTIVE"
REASON_TOKEN_EXPIRED = "TOKEN_EXPIRED"

REASON_MESSAGES = {
    REASON_PLAN_REQUIRED: "인스타 성장 리포트는 프로 플랜에서 이용할 수 있어요.",
    REASON_QUOTA_EXCEEDED: (
        "이번 달 이 계정의 리포트를 이미 사용했어요. 다음 달 1일에 다시 만들 수 있어요."
    ),
    REASON_ALREADY_RUNNING: "리포트를 만들고 있어요. 완료된 뒤에 다시 시도해 주세요.",
    REASON_CONNECTION_INACTIVE: "비활성 상태인 계정이에요. 계정을 활성화한 뒤 시도해 주세요.",
    REASON_TOKEN_EXPIRED: "인스타그램 연결이 만료됐어요. 계정을 다시 연결한 뒤 시도해 주세요.",
}


class ReportGateError(APIException):
    """생성 거부 — status_code 는 사유별로 갈아끼운다.

    detail 을 dict 로 주므로 표준 핸들러가 ``error.details.code`` 로 사유를 실어 준다.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "리포트를 생성할 수 없습니다."
    default_code = "report_gate"

    def __init__(self, detail: dict, http_status: int):
        self.status_code = http_status
        super().__init__(detail)

    @classmethod
    def plan_required(cls) -> ReportGateError:
        return cls(
            {
                "message": REASON_MESSAGES[REASON_PLAN_REQUIRED],
                "code": REASON_PLAN_REQUIRED,
                "plan_required": PLAN_REQUIRED,
            },
            status.HTTP_403_FORBIDDEN,
        )

    @classmethod
    def already_running(cls, report_id) -> ReportGateError:
        return cls(
            {
                "message": REASON_MESSAGES[REASON_ALREADY_RUNNING],
                "code": REASON_ALREADY_RUNNING,
                "running_report_id": str(report_id),
            },
            status.HTTP_409_CONFLICT,
        )

    @classmethod
    def connection_unusable(cls, reason: str) -> ReportGateError:
        return cls(
            {"message": REASON_MESSAGES[reason], "code": reason},
            status.HTTP_400_BAD_REQUEST,
        )


def month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """이번 캘린더월(KST) 의 [시작, 다음 달 시작) aware datetime."""
    now = timezone.localtime(now or timezone.now())
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def monthly_allowance(user) -> int:
    """IG 계정 1개당 이번 달 생성 가능 횟수. -1 = 무제한, 0 = 불가."""
    if is_admin_user(user):
        return -1
    plan = get_user_plan(user)
    try:
        return int(plan.features.get(LIMIT_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0


def has_feature(workspace, user) -> bool:
    """프로 전용 게이트 (fail-closed). 관리자는 항상 통과."""
    return is_admin_user(user) or owner_has_feature(workspace, FEATURE_KEY)


def used_this_month(connection, now: datetime | None = None) -> int:
    """이 연동 계정이 이번 달 소모한 횟수.

    차감 대상 = ``quota_consumed=True`` 인 진행중/완료 리포트. 실패·취소는 세지 않는다.
    """
    start, end = month_window(now)
    return (
        InstagramReport.objects.filter(
            ig_connection=connection,
            quota_consumed=True,
            created_at__gte=start,
            created_at__lt=end,
        )
        .exclude(status__in=[ReportStatus.FAILED, ReportStatus.CANCELLED])
        .count()
    )


def running_report(workspace):
    """이 워크스페이스에서 아직 끝나지 않은 리포트(있으면 그 행)."""
    return (
        InstagramReport.objects.filter(
            workspace=workspace,
            status__in=[ReportStatus.QUEUED, ReportStatus.RUNNING],
        )
        .order_by("-created_at")
        .first()
    )


def evaluate(connection, user, *, running=None, now=None) -> dict:
    """분석 팝업/생성 직전 판정 결과.

    Returns dict:
        can_generate, reason, reason_message, plan_required,
        limit, used, remaining, period_end(ISO), running_report_id
    """
    workspace = connection.workspace
    limit = monthly_allowance(user)
    unlimited = limit == -1
    _, period_end = month_window(now)

    used = used_this_month(connection, now) if not unlimited else 0
    remaining = -1 if unlimited else max(limit - used, 0)

    if running is None:
        running = running_report(workspace)

    reason = None
    if not has_feature(workspace, user) or limit == 0:
        reason = REASON_PLAN_REQUIRED
    elif not connection.is_active:
        reason = REASON_CONNECTION_INACTIVE
    elif connection.is_token_expired():
        reason = REASON_TOKEN_EXPIRED
    elif running is not None:
        reason = REASON_ALREADY_RUNNING
    elif not unlimited and remaining <= 0:
        reason = REASON_QUOTA_EXCEEDED

    return {
        "can_generate": reason is None,
        "reason": reason,
        "reason_message": REASON_MESSAGES.get(reason, "") if reason else "",
        "plan_required": PLAN_REQUIRED,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "period_end": period_end.isoformat(),
        "running_report_id": str(running.id) if running else None,
    }


def ensure_can_generate(connection, user) -> dict:
    """생성 직전 강제 판정. 통과하면 판정 dict 반환, 아니면 예외를 던진다.

    - 프로 미보유 → 403 PLAN_REQUIRED
    - 비활성/토큰만료 → 400
    - 동시 생성 → 409 ALREADY_RUNNING
    - 월 한도 초과 → 429 PLAN_LIMIT_EXCEEDED (PlanLimitExceededError)
    """
    verdict = evaluate(connection, user)
    reason = verdict["reason"]
    if reason is None:
        return verdict
    if reason == REASON_PLAN_REQUIRED:
        raise ReportGateError.plan_required()
    if reason in (REASON_CONNECTION_INACTIVE, REASON_TOKEN_EXPIRED):
        raise ReportGateError.connection_unusable(reason)
    if reason == REASON_ALREADY_RUNNING:
        raise ReportGateError.already_running(verdict["running_report_id"])
    # QUOTA_EXCEEDED → 표준 429(PLAN_LIMIT_EXCEEDED) 로 통일
    plan = get_user_plan(user)
    raise PlanLimitExceededError(
        metric="insta_report_monthly_per_account",
        limit=verdict["limit"],
        current=verdict["used"],
        plan=getattr(plan, "name", "unknown"),
    )


def next_available_at(now: datetime | None = None) -> str:
    """다음 이용 가능 시점(다음 달 1일 00:00 KST) ISO 문자열."""
    _, end = month_window(now)
    return end.isoformat()


def quota_summary(user, connections, now=None) -> dict:
    """팝업 헤더용 합계 — 연동 계정 수 × 계정당 한도."""
    limit = monthly_allowance(user)
    _, period_end = month_window(now)
    if limit == -1:
        return {
            "per_account_limit": -1,
            "total_limit": -1,
            "total_used": 0,
            "total_remaining": -1,
            "period_end": period_end.isoformat(),
        }
    used = sum(used_this_month(c, now) for c in connections)
    total = limit * len(connections)
    return {
        "per_account_limit": limit,
        "total_limit": total,
        "total_used": used,
        "total_remaining": max(total - used, 0),
        "period_end": period_end.isoformat(),
    }
