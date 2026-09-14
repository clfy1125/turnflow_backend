"""홈 알림의 **이메일 판** — 배너를 못 본 사용자에게만 나간다.

정책 (2026-09-10 제품 결정)
---------------------------
- 대상은 **결제 관련 + 캠페인에 심각한 이상**뿐이다. 체험 종료 예고는 **보내지 않는다**
  (결제 화면에서 이미 사전 동의를 받았다 — 다시 알리면 해지 유도만 된다).
- 결제 실패는 이미 `billing.payment_failed_email` 이 즉시 보낸다. 여기서는 중복해서 보내지
  않고, **연결 끊김**과 **월 한도 소진** 두 가지만 담당한다.
- **인앱이 먼저, 메일은 이탈했을 때.** 배너가 뜬 상태로 24시간 동안 콘솔에 들어오지 않은
  사용자에게만 1통. 콘솔에 있는 사람에게 메일까지 보내면 피로만 쌓인다.
- 같은 사건에 대해 **7일에 1통**을 넘지 않는다. 판정은 EmailLog 를 직접 보므로 별도 테이블이
  필요 없고, 캐시가 날아가도 중복 발송이 생기지 않는다.

⚠️ 기본 dormant (`HOME_ALERT_EMAILS_ENABLED=False`). 켜는 순간 실사용자에게 메일이 나가므로
   운영에서 사람이 명시적으로 켠다 — 윈백·2차 동의와 같은 방식이다.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# 배너를 보지 않았다고 판단하는 기준
INACTIVE_HOURS = 24
# 같은 사건 재발송 간격
RESEND_COOLDOWN_DAYS = 7


def _emails_enabled() -> bool:
    return bool(getattr(settings, "HOME_ALERT_EMAILS_ENABLED", False))


def _recently_emailed(user, template_key) -> bool:
    from apps.emails.models import EmailLog

    cutoff = timezone.now() - timedelta(days=RESEND_COOLDOWN_DAYS)
    return EmailLog.objects.filter(
        user=user, template_key=template_key, created_at__gte=cutoff
    ).exists()


def _is_away(user) -> bool:
    """24시간 넘게 콘솔에 안 들어왔는가.

    `last_login` 은 JWT 로그인 시 갱신된다(SIMPLE_JWT.UPDATE_LAST_LOGIN=True).
    로그인 기록이 아예 없으면 '들어온 적 없음'이므로 보낸다.
    """
    if not user.last_login:
        return True
    return (timezone.now() - user.last_login) >= timedelta(hours=INACTIVE_HOURS)


@shared_task(name="home.send_alert_emails")
def send_alert_emails(limit: int = 500) -> dict:
    """멈춤 상태가 24시간 넘게 방치된 사용자에게 안내 메일을 보낸다."""
    from apps.emails.constants import TEMPLATE_DM_QUOTA_REACHED, TEMPLATE_IG_CONNECTION_LOST
    from apps.emails.tasks import send_dm_quota_reached_email, send_ig_connection_lost_email
    from apps.workspace.models import Workspace

    from .alerts import DM_QUOTA_EXHAUSTED, IG_DISCONNECTED, build_home_alerts

    summary = {"scanned": 0, "connection_lost": 0, "quota_reached": 0, "skipped": 0}
    if not _emails_enabled():
        return {"skipped": "disabled", **summary}

    from apps.core.site_control import is_active_site

    if not is_active_site():
        return {"skipped": "passive_site", **summary}

    workspaces = (
        Workspace.objects.filter(ig_connections__isnull=False)
        .select_related("owner")
        .distinct()[:limit]
    )
    for ws in workspaces:
        owner = ws.owner
        if owner is None or not owner.is_active:
            continue
        summary["scanned"] += 1
        if not _is_away(owner):
            summary["skipped"] += 1
            continue
        try:
            payload = build_home_alerts(ws, owner)
        except Exception:  # noqa: BLE001 - 워크스페이스 하나가 배치를 깨지 않게
            logger.exception("send_alert_emails: 알림 계산 실패 ws=%s", ws.id)
            continue

        by_code = {a["code"]: a for a in payload["alerts"]}

        alert = by_code.get(IG_DISCONNECTED)
        if alert and not _recently_emailed(owner, TEMPLATE_IG_CONNECTION_LOST):
            since = alert.get("since")
            send_ig_connection_lost_email.delay(
                owner.id,
                {
                    "ig_username": alert.get("target_label") or "",
                    "since_date": timezone.localtime(since).strftime("%Y-%m-%d") if since else "",
                },
            )
            summary["connection_lost"] += 1

        alert = by_code.get(DM_QUOTA_EXHAUSTED)
        if alert and not _recently_emailed(owner, TEMPLATE_DM_QUOTA_REACHED):
            d = alert["data"]
            resets = d.get("resets_at")
            send_dm_quota_reached_email.delay(
                owner.id,
                {
                    "used_str": f"{d.get('used', 0):,}",
                    "limit_str": f"{d.get('limit', 0):,}",
                    "blocked_str": f"{d.get('blocked_count', 0):,}",
                    "resumable_str": f"{d.get('resumable_count', 0):,}",
                    "reset_date": timezone.localtime(resets).strftime("%Y-%m-%d") if resets else "",
                },
            )
            summary["quota_reached"] += 1

    logger.info("send_alert_emails: %s", summary)
    return summary
