"""홈 알림 — **판정 단일 소스**.

설계 원칙 (2026-09-10 제품 결정)
--------------------------------
1. **이벤트를 쌓지 않는다.** "지금 이 상태인가"를 매번 계산한다. 해소되면 배너가 저절로
   사라지므로 "복구됐어요" 알림이 공짜로 따라오고, 해결된 알림을 지우는 배치가 필요 없다.
2. **인스타(Meta Graph)를 절대 부르지 않는다.** 홈은 로그인한 모든 사용자의 첫 화면이라
   여기서 Graph 를 부르면 앱 단위 쿼터를 태워 **다른 워크스페이스의 댓글 수집·DM 발송이 굶는다.**
   Graph 가 필요한 사실(최신 게시물·웹훅 구독 상태)은 주기 태스크가 미리 DB 에 적어 둔 값을 읽는다
   (`IGAccountConnection.latest_media_*` / `webhook_healthy`).
3. **서버는 문장을 만들지 않는다.** `code`(머신 키) + `data`(숫자) 만 내려보내고 문장은 프론트
   i18n 이 만든다. 서버가 완성된 한국어를 내리면 다국어가 막히고 문구 수정에 배포가 필요하다.
4. **판정을 복제하지 않는다.** 한도·활성계정·발송정지는 실제 동작이 쓰는 함수를 그대로 부른다.
   화면과 실제 동작이 갈리는 사고를 이미 여러 번 겪었다.

등급
----
``critical`` 자동화가 실제로 멈춤 · ``warning`` 새고 있음 · ``todo`` 권유(장애 아님)

``rank`` 는 작을수록 위. 원칙은 **돈이 새는 순 → 사용자가 지금 손쓸 수 있는 순**.
같은 등급 안에서 기다리는 것 말고 할 게 없는 항목(발송 일시제한)이 아래로 내려가는 이유다.

닫기
----
``critical``/``warning`` 은 닫을 수 없다(닫으면 알림의 의미가 없다 — 해소되면 자동으로 사라진다).
``todo`` 는 닫을 수 있고 그 사실을 서버에 남긴다(:class:`~apps.home.models.HomeAlertDismissal`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# ── 알림 코드 (머신 키). 프론트 i18n 키와 1:1 ────────────────────────────────
# 계정 연결
IG_DISCONNECTED = "ig_disconnected"
IG_ACCOUNT_DISABLED = "ig_account_disabled"
COMMENT_STREAM_DOWN = "comment_stream_down"
# 발송
DM_QUOTA_EXHAUSTED = "dm_quota_exhausted"
DM_QUOTA_WARNING = "dm_quota_warning"
CAMPAIGN_POST_RESTRICTED = "campaign_post_restricted"
DM_SEND_BLOCKED = "dm_send_blocked"
# 결제
PAYMENT_FAILED = "payment_failed"
SUBSCRIPTION_ENDING = "subscription_ending"
# 권유
RECENT_POST_NO_CAMPAIGN = "recent_post_no_campaign"
CREATE_CAMPAIGN = "create_campaign"
LINK_PAGE_EMPTY = "link_page_empty"
REPORT_READY = "report_ready"
REPORT_RUNNING = "report_running"
REPORT_FAILED = "report_failed"
MIGRATION_REVIEW_PENDING = "migration_review_pending"

# 강제 팝업 코드 — 배너가 아니라 **반드시 선택해야 넘어가는 모달**.
# 바이오링크 페이지가 플랜 축소 때 쓰는 것과 같은 성격이다.
BLOCKING_IG_ACCOUNT_SELECTION = "ig_account_selection_required"

LEVEL_CRITICAL = "critical"
LEVEL_WARNING = "warning"
LEVEL_TODO = "todo"

# 순위 — 작을수록 위. 「돈이 새는 순 → 손쓸 수 있는 순」
RANKS = {
    PAYMENT_FAILED: 10,
    IG_DISCONNECTED: 20,
    COMMENT_STREAM_DOWN: 40,
    DM_QUOTA_EXHAUSTED: 50,
    CAMPAIGN_POST_RESTRICTED: 60,
    DM_SEND_BLOCKED: 70,
    IG_ACCOUNT_DISABLED: 80,
    DM_QUOTA_WARNING: 100,
    SUBSCRIPTION_ENDING: 110,
    # 권유 — 사용자 지정 순서(라1 → 라2 → 라3 → 라4 → 라5)
    RECENT_POST_NO_CAMPAIGN: 200,
    CREATE_CAMPAIGN: 210,
    LINK_PAGE_EMPTY: 220,
    REPORT_READY: 230,
    REPORT_RUNNING: 231,
    REPORT_FAILED: 232,
    MIGRATION_REVIEW_PENDING: 240,
}

# 「기본 상태」 — 알릴 게 하나도 없을 때 홈이 비지 않도록 맨 뒤에 항상 두는 항목.
# 캠페인이 0건이면 정식 권유(rank 210)로, 이미 있으면 기본 상태(rank 999)로 나간다.
DEFAULT_RANK = 999

# 최근 게시물을 "새 게시물"로 볼 기간 (2026-09-10 제품 결정: 일주일)
RECENT_POST_DAYS = 7
# 월 한도 경고 임계 (80%)
QUOTA_WARNING_RATIO = 0.8


@dataclass
class Alert:
    code: str
    level: str
    scope: str = "workspace"  # workspace | ig_connection | campaign | page | report
    target_id: str = ""
    target_label: str = ""
    since: object = None
    rank_override: int | None = None
    data: dict = field(default_factory=dict)

    @property
    def rank(self) -> int:
        if self.rank_override is not None:
            return self.rank_override
        return RANKS.get(self.code, DEFAULT_RANK)

    @property
    def dismissible(self) -> bool:
        """권유만 닫을 수 있다. 기본 상태(create_campaign)는 닫으면 홈이 비므로 예외."""
        return self.level == LEVEL_TODO and self.code != CREATE_CAMPAIGN

    @property
    def dismiss_key(self) -> str:
        """닫음을 기록할 대상 키. 대상이 바뀌면 다시 떠야 하므로 대상 id 를 쓴다."""
        return self.target_id or ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "rank": self.rank,
            "scope": self.scope,
            "target_id": self.target_id or None,
            "target_label": self.target_label or "",
            "dismissible": self.dismissible,
            "dismiss_key": self.dismiss_key,
            "since": self.since,
            "data": self.data,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 판정
# ═══════════════════════════════════════════════════════════════════════════


def build_home_alerts(workspace, user) -> dict:
    """워크스페이스 1개의 현재 알림 상태를 계산한다.

    Graph 호출 0 · 쓰기 0(순수 읽기) · 예외는 항목 단위로 삼킨다 —
    한 판정이 깨져도 나머지 알림이 사라지면 안 된다(홈 첫 화면이다).
    """
    owner = workspace.owner
    alerts: list[Alert] = []
    blocking = None

    connections = _load_connections(workspace)

    for builder in (
        _check_payment,
        _check_connections,
        _check_webhook,
        _check_quota,
        _check_restricted_campaigns,
        _check_send_block,
        _check_subscription_ending,
        _check_recent_post,
        _check_campaigns,
        _check_link_pages,
        _check_report,
        _check_migration,
    ):
        try:
            alerts.extend(builder(workspace=workspace, owner=owner, connections=connections) or [])
        except Exception:  # noqa: BLE001 — 판정 하나가 홈 전체를 죽이지 않게
            logger.exception("home alert 판정 실패: %s ws=%s", builder.__name__, workspace.id)

    try:
        blocking = _check_blocking(owner)
    except Exception:  # noqa: BLE001
        logger.exception("home alert 강제팝업 판정 실패 ws=%s", workspace.id)

    alerts = _apply_dismissals(alerts, user=user, workspace=workspace)
    alerts.sort(key=lambda a: (a.rank, a.code, a.target_id))

    return {
        "generated_at": timezone.now(),
        "workspace_id": str(workspace.id),
        "blocking": blocking,
        "alerts": [a.to_dict() for a in alerts],
        "counts": {
            "critical": sum(1 for a in alerts if a.level == LEVEL_CRITICAL),
            "warning": sum(1 for a in alerts if a.level == LEVEL_WARNING),
            "todo": sum(1 for a in alerts if a.level == LEVEL_TODO),
        },
    }


def _load_connections(workspace):
    from apps.integrations.models import IGAccountConnection

    return list(
        IGAccountConnection.objects.filter(workspace=workspace)
        .exclude(status=IGAccountConnection.Status.REVOKED)
        .order_by("created_at")
    )


# ── 다1 결제 실패 ────────────────────────────────────────────────────────────
def _check_payment(*, workspace, owner, connections):
    from apps.billing.models import SubscriptionStatus
    from apps.billing.subscription_utils import ensure_subscription
    from apps.billing.tasks import GRACE_PERIOD_DAYS

    sub = ensure_subscription(owner)
    if sub.status != SubscriptionStatus.PAST_DUE:
        return []

    grace_ends_at = None
    if sub.current_period_end:
        grace_ends_at = sub.current_period_end + timedelta(days=GRACE_PERIOD_DAYS)

    a = Alert(
        code=PAYMENT_FAILED,
        level=LEVEL_CRITICAL,
        data={
            "plan": sub.plan.name,
            "amount": sub.renewal_amount,
            "grace_ends_at": grace_ends_at,
            "next_retry_at": sub.next_billing_retry_at,
            "card_masked": sub.card_number_masked or "",
        },
    )
    a.since = sub.current_period_end
    return [a]


# ── 가1 연결 끊김 / 가2 계정 꺼짐 ────────────────────────────────────────────
def _check_connections(*, workspace, owner, connections):
    from apps.billing.subscription_utils import get_ig_account_allowance
    from apps.integrations.models import IGAccountConnection

    out = []
    dead_statuses = (
        IGAccountConnection.Status.EXPIRED,
        IGAccountConnection.Status.ERROR,
    )
    for conn in connections:
        if conn.status in dead_statuses:
            a = Alert(
                code=IG_DISCONNECTED,
                level=LEVEL_CRITICAL,
                scope="ig_connection",
                target_id=str(conn.id),
                target_label=conn.username or conn.name or "",
                data={
                    "status": conn.status,
                    # 원인 머신 키 — 화면 문구가 원인마다 달라야 한다.
                    "reason": conn.reconnect_reason or "reconnect_required",
                    # 다시 연결하면 아직 시간이 남은 건은 저절로 다시 나간다.
                    "revives_on_reconnect": True,
                },
            )
            a.since = conn.token_dead_first_seen_at or conn.updated_at
            out.append(a)

    # 가2 — 슬롯이 남아 있는데 꺼져 있는 계정 (플랜 축소 잔재). 슬롯이 꽉 찼으면 사용자의
    # 의도적 선택이므로 알리지 않는다.
    allowance = get_ig_account_allowance(owner)
    active = [c for c in connections if c.is_active]
    inactive = [c for c in connections if not c.is_active]
    slots_free = allowance < 0 or len(active) < allowance
    if inactive and slots_free:
        out.append(
            Alert(
                code=IG_ACCOUNT_DISABLED,
                level=LEVEL_CRITICAL,
                data={
                    "count": len(inactive),
                    "allowance": allowance,
                    "active": len(active),
                    "accounts": [{"id": str(c.id), "username": c.username or ""} for c in inactive],
                },
            )
        )
    return out


# ── 가4 새 댓글이 안 들어옴 ──────────────────────────────────────────────────
def _check_webhook(*, workspace, owner, connections):
    """웹훅 구독이 꺼진 것으로 **기록된** 계정.

    판정에 Graph 를 부르지 않는다 — 1시간마다 도는 `resubscribe_all_webhooks` 가 점검하면서
    결과를 `webhook_healthy` 에 적어 둔다. 그 태스크는 꺼져 있으면 즉시 재구독까지 하므로,
    여기 걸린다는 것은 **재구독조차 실패했다**는 뜻이다.
    """
    from apps.integrations.models import IGAccountConnection

    out = []
    for conn in connections:
        if not conn.is_active or conn.status != IGAccountConnection.Status.ACTIVE:
            continue  # 연결 자체가 죽었으면 가1 이 이미 말한다
        if conn.webhook_healthy is not False:
            continue
        a = Alert(
            code=COMMENT_STREAM_DOWN,
            level=LEVEL_CRITICAL,
            scope="ig_connection",
            target_id=str(conn.id),
            target_label=conn.username or "",
            data={"checked_at": conn.webhook_checked_at},
        )
        a.since = conn.webhook_checked_at
        out.append(a)
    return out


# ── 나2 한도 소진 / 나4 80% ──────────────────────────────────────────────────
def _check_quota(*, workspace, owner, connections):
    from apps.billing.dm_limits import (
        check_dm_quota,
        count_quota_skipped_dms,
        count_revivable_quota_skipped_dms,
    )
    from apps.integrations.campaign_stats import _month_bounds

    allowed, used, limit = check_dm_quota(owner)
    if limit is None or limit < 0:
        return []  # 무제한

    period_start, period_end = _month_bounds()

    if not allowed:
        # 「지금 몇 건의 댓글에 DM 이 안 나가고 있는가」 — 결제를 유도하려면 이 숫자가 필요하다.
        blocked = count_quota_skipped_dms(owner)
        # 「결제하면 바로 나간다」 — 되살림 창(댓글 7일)이 남아 있는 건만 약속할 수 있다.
        resumable = count_revivable_quota_skipped_dms(owner)
        a = Alert(
            code=DM_QUOTA_EXHAUSTED,
            level=LEVEL_CRITICAL,
            data={
                "used": used,
                "limit": limit,
                "blocked_count": blocked,
                "resumable_count": resumable,
                "resumes_on_upgrade": True,
                "resets_at": period_end,
                "period_start": period_start,
            },
        )
        return [a]

    if limit and used >= limit * QUOTA_WARNING_RATIO:
        return [
            Alert(
                code=DM_QUOTA_WARNING,
                level=LEVEL_WARNING,
                data={
                    "used": used,
                    "limit": limit,
                    "remaining": max(0, limit - used),
                    "resets_at": period_end,
                },
            )
        ]
    return []


# ── 나3 게시물이 막혀 캠페인 정지 ────────────────────────────────────────────
def _check_restricted_campaigns(*, workspace, owner, connections):
    from apps.integrations.models import AutoDMCampaign

    qs = AutoDMCampaign.objects.filter(
        ig_connection__workspace=workspace,
        status=AutoDMCampaign.Status.PAUSED,
        auto_paused_reason="post_restricted",
    ).order_by("-auto_paused_at")[:20]

    out = []
    for c in qs:
        a = Alert(
            code=CAMPAIGN_POST_RESTRICTED,
            level=LEVEL_CRITICAL,
            scope="campaign",
            target_id=str(c.id),
            target_label=c.name or "",
            data={
                "campaign_id": str(c.id),
                "name": c.name or "",
                "media_id": c.media_id or "",
                "permalink": c.media_url or "",
                "paused_at": c.auto_paused_at,
                # 다른 게시물에서는 정상 발송된다 — 범위를 모르면 오해가 생기는 경우다.
                "other_posts_unaffected": True,
            },
        )
        a.since = c.auto_paused_at
        out.append(a)
    return out


# ── 나1 인스타가 발송을 막음 ─────────────────────────────────────────────────
def _check_send_block(*, workspace, owner, connections):
    from apps.integrations.models import SentDMLog
    from apps.integrations.rate_governor import action_block_cooldown_remaining

    out = []
    now = timezone.now()
    for conn in connections:
        if not conn.is_active:
            continue
        remaining = action_block_cooldown_remaining(conn.external_account_id)
        if remaining <= 0:
            continue
        waiting = SentDMLog.objects.filter(
            campaign__ig_connection=conn, status=SentDMLog.Status.QUEUED
        ).count()
        out.append(
            Alert(
                code=DM_SEND_BLOCKED,
                level=LEVEL_CRITICAL,
                scope="ig_connection",
                target_id=str(conn.id),
                target_label=conn.username or "",
                data={
                    "seconds_remaining": int(remaining),
                    "resumes_at": now + timedelta(seconds=int(remaining)),
                    "waiting_count": waiting,
                    # 기다리면 자동으로 재개된다 — 사용자가 할 일이 없다.
                    "auto_resumes": True,
                },
            )
        )
    return out


# ── 다6 해지 예약 ────────────────────────────────────────────────────────────
def _check_subscription_ending(*, workspace, owner, connections):
    from apps.billing.models import SubscriptionStatus
    from apps.billing.subscription_utils import ensure_subscription

    sub = ensure_subscription(owner)
    if sub.status != SubscriptionStatus.CANCELLED:
        return []
    if not sub.current_period_end or sub.current_period_end <= timezone.now():
        return []
    return [
        Alert(
            code=SUBSCRIPTION_ENDING,
            level=LEVEL_WARNING,
            data={
                "ends_at": sub.current_period_end,
                "plan": sub.plan.name,
                "days_left": max(0, (sub.current_period_end - timezone.now()).days),
            },
        )
    ]


# ── 라1 최근 게시물에 캠페인이 없음 ──────────────────────────────────────────
def _check_recent_post(*, workspace, owner, connections):
    """최근 7일 이내에 올린 게시물인데 자동 DM 캠페인이 없는 경우.

    최신 게시물은 `refresh_latest_media` 태스크가 미리 적어 둔 값을 읽는다(Graph 호출 0).
    """
    from apps.integrations.models import AutoDMCampaign

    cutoff = timezone.now() - timedelta(days=RECENT_POST_DAYS)
    out = []
    for conn in connections:
        if not conn.is_active or not conn.latest_media_id or not conn.latest_media_at:
            continue
        if conn.latest_media_at < cutoff:
            continue
        has_campaign = AutoDMCampaign.objects.filter(
            ig_connection=conn, media_id=conn.latest_media_id
        ).exists()
        if has_campaign:
            continue
        a = Alert(
            code=RECENT_POST_NO_CAMPAIGN,
            level=LEVEL_TODO,
            scope="ig_connection",
            target_id=conn.latest_media_id,
            target_label=conn.username or "",
            data={
                "ig_connection_id": str(conn.id),
                "media_id": conn.latest_media_id,
                "published_at": conn.latest_media_at,
                "permalink": conn.latest_media_permalink or "",
            },
        )
        a.since = conn.latest_media_at
        out.append(a)
    return out


# ── 라2 캠페인 만들기 (기본 상태) ────────────────────────────────────────────
def _check_campaigns(*, workspace, owner, connections):
    from apps.integrations.models import AutoDMCampaign

    total = AutoDMCampaign.objects.filter(ig_connection__workspace=workspace).count()
    is_default = total > 0
    return [
        Alert(
            code=CREATE_CAMPAIGN,
            level=LEVEL_TODO,
            # 이미 캠페인이 있으면 「기본 상태」 — 다른 알림이 하나도 없을 때만 보이도록 맨 뒤로.
            rank_override=DEFAULT_RANK if is_default else None,
            data={"campaign_total": total, "is_default": is_default},
        )
    ]


# ── 라3 링크 페이지가 비었거나 비공개 ────────────────────────────────────────
def _check_link_pages(*, workspace, owner, connections):
    from django.db.models import Count

    from apps.pages.models import Page

    pages = list(
        Page.objects.filter(user=owner, is_active=True)
        .annotate(nb=Count("blocks"))
        .order_by("created_at")
    )
    if not pages:
        return [
            Alert(
                code=LINK_PAGE_EMPTY,
                level=LEVEL_TODO,
                scope="page",
                target_id="none",
                data={"reason": "no_page"},
            )
        ]

    # 공개된 페이지가 하나라도 있고 내용이 있으면 알릴 게 없다.
    if any(p.is_public and p.nb > 0 for p in pages):
        return []

    target = pages[0]
    reason = "no_blocks" if target.nb == 0 else "private"
    return [
        Alert(
            code=LINK_PAGE_EMPTY,
            level=LEVEL_TODO,
            scope="page",
            target_id=str(target.id),
            target_label=target.title or target.slug,
            data={
                "reason": reason,
                "page_id": target.id,
                "slug": target.slug,
                "blocks_count": target.nb,
                "is_public": target.is_public,
            },
        )
    ]


# ── 라4 리포트 ───────────────────────────────────────────────────────────────
def _check_report(*, workspace, owner, connections):
    from apps.insta_reports.models import InstagramReport, ReportStatus

    latest = InstagramReport.objects.filter(workspace=workspace).order_by("-created_at").first()
    if latest is None:
        return []

    mapping = {
        ReportStatus.SUCCEEDED: (REPORT_READY, LEVEL_TODO),
        ReportStatus.RUNNING: (REPORT_RUNNING, LEVEL_TODO),
        ReportStatus.QUEUED: (REPORT_RUNNING, LEVEL_TODO),
        ReportStatus.FAILED: (REPORT_FAILED, LEVEL_TODO),
    }
    entry = mapping.get(latest.status)
    if entry is None:
        return []
    code, level = entry
    a = Alert(
        code=code,
        level=level,
        scope="report",
        target_id=str(latest.id),
        target_label=latest.ig_username or "",
        data={
            "report_id": str(latest.id),
            "status": latest.status,
            "progress": getattr(latest, "progress", None),
            "stage": getattr(latest, "stage", ""),
            "error_code": getattr(latest, "error_code", "") or "",
        },
    )
    a.since = latest.created_at
    return [a]


# ── 라5 불러온 캠페인 검수 대기 ──────────────────────────────────────────────
def _check_migration(*, workspace, owner, connections):
    from apps.integrations.models import DMCampaignCandidate

    qs = DMCampaignCandidate.objects.filter(
        ig_connection__workspace=workspace,
        status=DMCampaignCandidate.Status.DETECTED,
    ).exclude(band=DMCampaignCandidate.Band.EXCLUDED)
    count = qs.count()
    if not count:
        return []
    latest = qs.order_by("-created_at").first()
    job_id = str(latest.job_id) if latest and latest.job_id else ""
    return [
        Alert(
            code=MIGRATION_REVIEW_PENDING,
            level=LEVEL_TODO,
            target_id=job_id,
            data={"count": count, "job_id": job_id},
        )
    ]


# ── 강제 팝업: 쓸 계정을 골라야 함 (다3 + 가3) ───────────────────────────────
def _check_blocking(owner) -> dict | None:
    """반드시 선택해야 넘어가는 모달.

    바이오링크 페이지가 플랜 축소 때 쓰는 것과 같은 성격이라, 판정도 그쪽과 같은
    단일 소스(:func:`apps.billing.subscription_utils.ig_activation_state`)를 쓴다.
    "켜진 계정 0개"도 같은 조건에 이미 들어 있다.
    """
    from apps.billing.subscription_utils import ig_activation_state

    state = ig_activation_state(owner)
    if not state["needs_activation_adjustment"]:
        return None
    return {
        "code": BLOCKING_IG_ACCOUNT_SELECTION,
        "data": {
            "max_ig_accounts": state["max_ig_accounts"],
            "total_accounts": state["total_accounts"],
            "active_accounts": state["active_accounts"],
        },
    }


# ── 닫음 반영 ────────────────────────────────────────────────────────────────
def _apply_dismissals(alerts: list[Alert], *, user, workspace) -> list[Alert]:
    from .models import HomeAlertDismissal

    dismissible = [a for a in alerts if a.dismissible]
    if not dismissible:
        return alerts
    keys = {
        (d.code, d.target_key)
        for d in HomeAlertDismissal.objects.filter(user=user, workspace=workspace)
    }
    if not keys:
        return alerts
    return [a for a in alerts if not (a.dismissible and (a.code, a.dismiss_key) in keys)]
