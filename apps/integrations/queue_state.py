"""DM 순차 발송 큐 현황(queue-state) 페이로드 빌더 — 유저 콘솔/어드민 공용.

``GET /integrations/dm-verification/queue-state/`` (워크스페이스 스코프)와
``GET /admin/auto-dm/campaigns/{id}/queue-state/`` (cross-workspace, DM-3)가 **같은 함수**를
호출한다. 두 화면이 다른 숫자를 보이지 않도록 집계를 여기 한 곳에 둔다 —
호출부는 각자의 권한 검사(멤버십 / IsAdminUser)만 하고 페이로드는 손대지 않는다.

``people`` 블록은 :func:`campaign_stats.people_rollup` 이라 캠페인 상세 통계의
``unique_*`` 와 정의가 같다.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from . import dm_pacer
from .campaign_stats import SENT_FOR_QUOTA_STATUSES as _SENT_FOR_QUOTA
from .campaign_stats import people_rollup
from .models import SentDMLog
from .rate_governor import PRIVATE_REPLY_HOURLY_CAP, action_block_cooldown_remaining

# gauge.failed 용 하드 실패 (복구 대기/만료는 진행 중이라 제외 — 게이지는 '큐 진행' 관점)
_HARD_FAILED = [
    SentDMLog.Status.FAILED_TOKEN,
    SentDMLog.Status.FAILED_WINDOW,
    SentDMLog.Status.FAILED_PARAM,
    SentDMLog.Status.FAILED,  # legacy
]


def waiting_window_risk(waiting_qs, horizon_seconds: float) -> dict:
    """``horizon_seconds`` 뒤에 재개했을 때 **이미 메시징 창이 닫혀 있을** 대기 건 수.

    왜 필요한가 (2026-08-26 CS #66027015):
      계정 정지 중 화면에 "제한이 풀리면 47명에게 순서대로 발송합니다" 라고 쓰려면 그 47이
      **실제로 나갈 수 있는 수**여야 한다. 그런데 게이트 리워드(자료가 담긴 2번째 DM)의 창은
      **24h** 이고 기본 쿨다운도 **24h** 라, 정지가 걸리는 순간 대기 중이던 리워드는 재개
      시각과 만료 시각이 **같아진다** — 실측(prod): 만료 ``18:08:15.536`` vs 재개
      ``18:08:16.131``, 0.6초 차이로 종결. 우연이 아니라 구조다.
      이 값을 빼고 숫자를 노출하면 화면이 "보내겠다"고 약속한 뒤 조용히 실패한다.

    판정은 ``send_dm_task`` 진입부 age 가드와 같은 규칙(``_messaging_window``)을 쓴다.
    ``created_at + window <= now + horizon`` 이면 위험.

    ★ ``people`` 은 ``people.waiting`` 과 **같은 모수**(루트 DM = 오프닝/단독,
    :data:`campaign_stats.ROOT_DM_Q`)로 센다. 그래야 ``people.waiting - risk.people`` 이
    성립한다. 후속 DM(리워드)은 애초에 ``people`` 블록에 들어가지 않으므로 섞으면 뺄셈이
    음수로 갈 수도 있는 엉터리가 된다 — 대신 ``followup_events`` 로 따로 낸다.

    한계(의도적): 게이트 재탭 복구분의 창 재개(``_window_anchor``)는 반영하지 않는다 —
    verification_log 를 행마다 파싱해야 해서 목록 응답에 넣기엔 비싸다. 그래서 이 값은
    **위험을 과대평가**할 수 있다(재탭으로 살아날 건까지 셈). 과소평가보다 안전한 방향이다.
    """
    from django.utils import timezone as _tz

    from .campaign_stats import ROOT_DM_Q
    from .tasks import COMMENT_MESSAGING_WINDOW, USER_ID_MESSAGING_WINDOW

    horizon = _tz.now() + timedelta(seconds=max(0.0, horizon_seconds))
    # created_at <= horizon - window  ⇔  created_at + window <= horizon
    expired_q = (Q(comment_id="") & Q(created_at__lte=horizon - USER_ID_MESSAGING_WINDOW)) | (
        ~Q(comment_id="") & Q(created_at__lte=horizon - COMMENT_MESSAGING_WINDOW)
    )
    risky = waiting_qs.filter(expired_q)
    root_risky = risky.filter(ROOT_DM_Q)
    return {
        # people.waiting 에서 그대로 뺄 수 있는 수 (같은 모수 = 루트 DM)
        "people": root_risky.values("recipient_user_id").distinct().count(),
        "events": risky.count(),
        # 자료가 담긴 2번째 DM — 창이 24h 라 정지 초기 건은 구조적으로 여기 잡힌다.
        "followup_events": risky.exclude(ROOT_DM_Q).count(),
        "horizon_s": int(max(0.0, horizon_seconds)),
    }


def build_queue_state_payload(ig_conn, campaign=None) -> dict:
    """큐 현황 페이로드 (게이지 + 사람 단위 + ETA + 차단 요인).

    ``campaign`` 을 주면 scope="campaign"(그 캠페인 범위), 없으면 scope="account".
    ETA 는 계정 공유 페이서 포인터를 쓰므로 대기 건 수는 **계정 단위**로 센다.

    권한 검사는 하지 않는다 — 호출부 책임.
    """
    ext = str(ig_conn.external_account_id)
    workspace = ig_conn.workspace
    account_qs = SentDMLog.objects.filter(campaign__ig_connection=ig_conn)
    scope_qs = account_qs.filter(campaign=campaign) if campaign else account_qs

    agg = scope_qs.aggregate(
        sent=Count("id", filter=Q(status__in=_SENT_FOR_QUOTA)),
        waiting=Count("id", filter=Q(status=SentDMLog.Status.QUEUED)),
        in_flight=Count("id", filter=Q(status=SentDMLog.Status.SUBMITTING)),
        failed=Count("id", filter=Q(status__in=_HARD_FAILED)),
    )
    gauge = {
        "sent": agg["sent"],
        "waiting": agg["waiting"],
        "in_flight": agg["in_flight"],
        "failed": agg["failed"],
        "total": agg["sent"] + agg["waiting"] + agg["in_flight"],
    }
    # v4.4 — 사람(수신자) 단위 게이지. gauge 는 발송 이벤트 단위라 follow-gate
    # 캠페인(1명 = 오프닝+리워드 2건 이상)에서 사람 수보다 크게 보인다.
    people = people_rollup(scope_qs)
    people["processed"] = people["sent"] + people["failed"]

    account_waiting_qs = account_qs.filter(status=SentDMLog.Status.QUEUED)
    account_waiting = account_waiting_qs.count()
    ahead = 0
    if campaign:
        my_oldest = scope_qs.filter(status=SentDMLog.Status.QUEUED).aggregate(m=Min("created_at"))[
            "m"
        ]
        if my_oldest:
            ahead = account_waiting_qs.filter(created_at__lt=my_oldest).count()

    # ── ETA (v4.3): 대기 건 대부분은 확정 슬롯(next_retry_at)을 보유. 버킷별로
    #    max(확정 슬롯) 과 (포인터 + 미클레임 × 평균 간격) 추정을 합성한다.
    #    미클레임은 계정 공유 포인터를 소비하므로 **계정 단위**로 센다.
    now_ts = _time.time()
    finish_ts = now_ts
    is_estimate = False

    bucket_filters = {
        dm_pacer.BUCKET_PRIVATE_REPLY: (
            dm_pacer.bucket_q(dm_pacer.BUCKET_PRIVATE_REPLY),
            dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_PRIVATE_REPLY),
        ),
        dm_pacer.BUCKET_SEND_API: (
            dm_pacer.bucket_q(dm_pacer.BUCKET_SEND_API),
            dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_SEND_API),
        ),
    }
    scope_waiting_qs = scope_qs.filter(status=SentDMLog.Status.QUEUED)
    for bucket, (bucket_q, avg_gap) in bucket_filters.items():
        scope_bucket = scope_waiting_qs.filter(bucket_q)
        if not scope_bucket.exists():
            continue
        claimed_max = scope_bucket.aggregate(m=Max("next_retry_at"))["m"]
        if claimed_max:
            finish_ts = max(finish_ts, claimed_max.timestamp())
        # 미클레임(슬롯 미예약) — 계정 전체가 같은 포인터를 소비하므로 계정 단위 추정
        unclaimed_account = account_waiting_qs.filter(bucket_q, next_retry_at__isnull=True).count()
        if unclaimed_account and scope_bucket.filter(next_retry_at__isnull=True).exists():
            pointer = dm_pacer.peek_next_slot(ext, bucket) or now_ts
            finish_ts = max(finish_ts, max(pointer, now_ts) + unclaimed_account * avg_gap)
            is_estimate = True

    # ── 차단 요인 ──
    ab_remaining = action_block_cooldown_remaining(ext)
    blocking_reason = None
    if ab_remaining > 0:
        blocking_reason = "action_block_cooldown"
        finish_ts = max(finish_ts, now_ts + ab_remaining)  # 쿨다운 후 재개
        is_estimate = True
    else:
        from apps.billing.dm_limits import check_dm_quota

        try:
            quota_ok, _, _ = check_dm_quota(workspace.owner)
        except Exception:  # noqa: BLE001 — 쿼터 조회 실패는 표시만 정상 취급
            quota_ok = True
        if not quota_ok and gauge["waiting"] > 0:
            blocking_reason = "monthly_quota_reached"
            is_estimate = True

    eta_seconds = max(0.0, round(finish_ts - now_ts, 1)) if gauge["waiting"] else 0.0
    eta_finish_at = (
        datetime.fromtimestamp(finish_ts, tz=timezone.get_current_timezone())
        if gauge["waiting"]
        else None
    )

    cap = getattr(settings, "IG_PRIVATE_REPLY_HOURLY_CAP", PRIVATE_REPLY_HOURLY_CAP)
    return {
        "scope": "campaign" if campaign else "account",
        "campaign_id": str(campaign.id) if campaign else None,
        "ig_connection_id": str(ig_conn.id),
        "external_account_id": ext,
        "ig_username": ig_conn.username or "",
        "gauge": gauge,
        "people": people,
        "pacing": {
            "private_reply_avg_gap_s": dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_PRIVATE_REPLY),
            "send_api_avg_gap_s": dm_pacer.avg_gap_seconds(dm_pacer.BUCKET_SEND_API),
            "hourly_backstop_cap": int(cap or 0),
        },
        "account_waiting": account_waiting,
        "ahead_of_this_campaign": ahead,
        "blocking_reason": blocking_reason,
        "action_block_cooldown_seconds": int(ab_remaining),
        # 재개 시점에 이미 창이 닫혀 있을 대기 건 — "N명에게 발송합니다" 문구의 안전장치.
        # people.waiting 에서 이만큼 빼야 지킬 수 있는 약속이 된다.
        "waiting_window_risk": waiting_window_risk(
            scope_qs.filter(status=SentDMLog.Status.QUEUED), ab_remaining
        ),
        "eta_seconds": eta_seconds,
        "eta_finish_at": eta_finish_at,
        "eta_is_estimate": bool(is_estimate),
        "generated_at": timezone.now(),
    }
