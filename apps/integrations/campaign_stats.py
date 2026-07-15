"""Auto DM 캠페인 목록/요약용 통계 집계 헬퍼 (조회 고도화 v4.1).

프론트엔드 조회 고도화 요청(docs/backend-auto-dm-list-enhancements.md)을 위한 공유 로직.
목록 항목 enrichment, 요약 엔드포인트, 월간 사용량을 한 곳에서 계산해
N+1 통계 호출을 제거하고 정의를 단일화한다.

정의 출처:
  - delivery_rate: verification_views.stats / admin_api `_build_stats` 와 동일
    (확정도착 = delivered+read, 모수 = accepted+delivered+read+failed_no_trace)
  - needs_attention: dm_frontend_actions 의 severity=error 상태 + failed_no_trace
  - 월간 사용량: SentDMLog 에서 캘린더월(Asia/Seoul) 직접 집계 (UsageCounter 는 발송 시
    증가되지 않아 stale → 정확도를 위해 로그를 직접 센다).
    한도는 owner 구독 플랜 features.dm_monthly_limit (billing.dm_limits 와 동일 정의).
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from .models import AutoDMCampaign, SentDMLog

# ── 상태 집합 (delivery_rate / delivered_count 계산용) ──────────────────────────
# 확정 도착(사용자에게 "도착함"이라 보고 가능 + 읽음). legacy "sent" 는 모수와 분자
# 양쪽에서 빠지므로 delivery_rate 정의(_build_stats)와 일치시키기 위해 제외한다.
CONFIRMED_DELIVERED_STATUSES = [
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.RECOVERY_DELIVERED,  # 복구 재전송 성공 = 확정 도착
]
# delivery_rate 모수: ACCEPTED 진입 이후 종결된 건 (도착/읽음/도착미확인 포함)
ACCEPTED_OR_AFTER_STATUSES = [
    SentDMLog.Status.ACCEPTED,
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.FAILED_NO_TRACE,
    SentDMLog.Status.RECOVERY_DELIVERED,  # 실제 발송+도착했으므로 분자·분모 양쪽 포함
]
# 사용자 조치가 필요한 상태 (severity=error + 도착미확인 자가점검).
# error: 토큰만료(재연동) / 24h윈도우만료 / 파라미터오류.  warning: 도착미확인.
NEEDS_ATTENTION_STATUSES = [
    SentDMLog.Status.FAILED_TOKEN,
    SentDMLog.Status.FAILED_WINDOW,
    SentDMLog.Status.FAILED_PARAM,
    SentDMLog.Status.FAILED_NO_TRACE,
]
# 월간 사용량(quota) 으로 카운트할 상태: 실제로 Meta 에 발송 요청이 접수된 건.
# accepted 이후 + legacy sent. queued/submitting/skipped/rate_limited/거부성 실패는 제외
# (발송 전 단계이거나 발송 자체가 안 일어났으므로 quota 미소진).
SENT_FOR_QUOTA_STATUSES = [
    SentDMLog.Status.ACCEPTED,
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.FAILED_NO_TRACE,
    SentDMLog.Status.SENT,  # legacy
    SentDMLog.Status.RECOVERY_DELIVERED,  # 실제 Meta 발송 소비 → 쿼터 집계
]
# "발송 성공" 표시용: v3 정상 흐름(accepted 이후) + legacy sent.
# ⚠️ v3 상태머신에서 성공 DM 은 accepted→delivered→read 로 가고 legacy 'sent' 가 되지
# 않는다 — status='sent' 단독 카운트는 성공할수록 0 이 되는 함정 (2026-07-07 prod 실측).
SENT_OK_STATUSES = [
    SentDMLog.Status.ACCEPTED,
    SentDMLog.Status.DELIVERED,
    SentDMLog.Status.READ,
    SentDMLog.Status.SENT,  # legacy
    SentDMLog.Status.RECOVERY_DELIVERED,  # 복구 재전송 성공 (종결·성공)
]
# "발송 실패" 표시용: 분류 실패(v3.2) + legacy. FAILED_NO_TRACE 는 '도착 미확인'이지
# 실패가 아니므로 제외 (total_unconfirmed / unconfirmed 로 별도 노출).
FAILED_STATUSES = [
    SentDMLog.Status.FAILED,  # legacy
    SentDMLog.Status.FAILED_TOKEN,
    SentDMLog.Status.FAILED_WINDOW,
    SentDMLog.Status.FAILED_PARAM,
    SentDMLog.Status.FAILED_API,  # legacy alias
    SentDMLog.Status.RECOVERY_EXPIRED,  # 복구 대기 만료 (종결·실패)
]
# "진행 중" 표시용: 아직 종결되지 않은 발송 전/중 상태 + legacy pending.
IN_FLIGHT_STATUSES = [
    SentDMLog.Status.QUEUED,
    SentDMLog.Status.SUBMITTING,
    SentDMLog.Status.RATE_LIMITED,
    SentDMLog.Status.PENDING,  # legacy
    SentDMLog.Status.RECOVERY_PENDING,  # 복구 대기 (미종결 — 사용자 DM/TTL 이 전이)
]
# 발송 큐에서 차례를 기다리는 상태 (사람 단위 게이지의 "대기" 판정용).
# IN_FLIGHT 와 달리 RECOVERY_PENDING 제외 — 복구 대기는 큐 진행이 아니라 사용자의
# 재댓글을 기다리는 상태라, 대기로 세면 게이지가 영원히 100% 에 못 닿는다.
QUEUE_WAITING_STATUSES = [
    SentDMLog.Status.QUEUED,
    SentDMLog.Status.SUBMITTING,
    SentDMLog.Status.RATE_LIMITED,
    SentDMLog.Status.PENDING,  # legacy
]

# 루트 DM(오프닝/단독) 판별 — 사람 단위 집계의 모수.
# reward(게이트 통과 보상)·child(재안내 등 parent 가 있는 후속 DM)는 같은 사람에게 가는
# 부가 발송이라 제외한다. "한 사람 = 루트 DM 1개" 가 유저 콘솔 합의 단위.
# ⚠️ parent_log 는 on_delete=SET_NULL 이라, 부모 오프닝이 삭제되면 재안내 child(dm_kind=OPENING)
#    가 parent NULL 이 되어 루트로 오인될 수 있다. 현재 로그 아카이브(retention=0)는 비활성이라
#    무해하지만, archive_old_sentdmlogs 활성화 전 이 가정(부모 생존)을 함께 점검할 것.
ROOT_DM_Q = Q(parent_log__isnull=True) & ~Q(dm_kind=SentDMLog.DMKind.REWARD)


def people_rollup(log_qs) -> dict:
    """사람(수신자) 단위 처리 현황 롤업 — 루트 DM(오프닝/단독) 기준.

    한 사람이 루트 DM 을 여러 건 받아도(댓글 2회 등) 1명으로 센다.
    버킷 우선순위: sent > waiting > failed(잔여).
      - sent    : 루트 DM 이 1건이라도 실제 발송됨 (SENT_FOR_QUOTA — Meta 접수 이상)
      - waiting : 발송된 건 없고, 큐에서 차례 대기/발송 중인 루트 DM 이 있음
      - failed  : 나머지 = 아무것도 못 받고 종결·정체된 사람
                  (하드실패 failed_* / 복구 대기·만료 recovery_* / 한도 skipped 포함)
    total = sent + waiting + failed 항등이 항상 성립한다.

    성능: 폴링 엔드포인트(queue-state 5~10초)에서 호출되므로 단일 aggregate 로 계산한다.
    SENT_FOR_QUOTA 와 QUEUE_WAITING 은 서로소 상태집합이므로 포함-배제로
    waiting = |sent∪waiting 사람| − |sent 사람| (= sent 없이 waiting 만 있는 사람) 이 성립한다.

    ⚠️ 근사: recipient_user_id 값 공간이 경로마다 다르다(웹훅=IGSID, 폴링=username 폴백,
    _recipient_match_q 참조). 한 사람이 두 키로 로그를 가지면(폴 보정 pending + 웹훅 재댓글
    성공 등) 2명으로 셀 수 있다 — 재발송이 정상화되는 recovery 크로스키 케이스에 한정된
    드문 오차이며 발송에는 영향 없다. 정확 매칭이 필요한 recovery flip 은 _recipient_match_q 사용.
    """
    root = log_qs.filter(ROOT_DM_Q)
    agg = root.aggregate(
        total=Count("recipient_user_id", distinct=True),
        sent=Count(
            "recipient_user_id",
            filter=Q(status__in=SENT_FOR_QUOTA_STATUSES),
            distinct=True,
        ),
        sent_or_waiting=Count(
            "recipient_user_id",
            filter=Q(status__in=SENT_FOR_QUOTA_STATUSES + QUEUE_WAITING_STATUSES),
            distinct=True,
        ),
    )
    total = agg["total"] or 0
    sent = agg["sent"] or 0
    waiting = max((agg["sent_or_waiting"] or 0) - sent, 0)
    return {
        "total": total,
        "sent": sent,
        "waiting": waiting,
        "failed": max(total - sent - waiting, 0),
    }


# annotate 결과를 담는 임시 속성명 (모델 필드와 충돌 안 나게 언더스코어 프리픽스)
_ANNO_CONFIRMED = "_confirmed_delivered"
_ANNO_ACCEPTED = "_accepted_or_after"
_ANNO_NEEDS = "_needs_attention"
_ANNO_LAST = "_last_sent_at"


def annotate_campaign_stats(qs):
    """campaign queryset 에 per-campaign dm_logs 집계를 annotate (목록 N+1 제거).

    한 번의 LEFT JOIN + 조건부 집계로 모든 캠페인의 통계를 계산한다.
    부모/자식 로그를 모두 포함한다(전체 발송 그림 = canonical _build_stats 와 동일 범위).
    """
    return qs.annotate(
        **{
            _ANNO_CONFIRMED: Count(
                "dm_logs", filter=Q(dm_logs__status__in=CONFIRMED_DELIVERED_STATUSES)
            ),
            _ANNO_ACCEPTED: Count(
                "dm_logs", filter=Q(dm_logs__status__in=ACCEPTED_OR_AFTER_STATUSES)
            ),
            _ANNO_NEEDS: Count("dm_logs", filter=Q(dm_logs__status__in=NEEDS_ATTENTION_STATUSES)),
            _ANNO_LAST: Max("dm_logs__created_at"),
        }
    )


def compute_campaign_enrichment(obj: AutoDMCampaign) -> dict:
    """캠페인 1건의 enrichment dict 계산.

    annotate_campaign_stats 로 annotate 된 인스턴스면 그 값을 쓰고(추가 쿼리 0),
    아니면 그 캠페인 로그를 즉석 집계한다(단건 경로 — pause/resume 등에서 안전 fallback).
    """
    confirmed = getattr(obj, _ANNO_CONFIRMED, None)
    if confirmed is None:
        agg = obj.dm_logs.aggregate(
            confirmed=Count("id", filter=Q(status__in=CONFIRMED_DELIVERED_STATUSES)),
            accepted=Count("id", filter=Q(status__in=ACCEPTED_OR_AFTER_STATUSES)),
            needs=Count("id", filter=Q(status__in=NEEDS_ATTENTION_STATUSES)),
            last=Max("created_at"),
        )
        confirmed = agg["confirmed"]
        accepted = agg["accepted"]
        needs = agg["needs"]
        last = agg["last"]
    else:
        accepted = getattr(obj, _ANNO_ACCEPTED, 0) or 0
        needs = getattr(obj, _ANNO_NEEDS, 0) or 0
        last = getattr(obj, _ANNO_LAST, None)

    delivery_rate = round(confirmed / accepted, 4) if accepted else 0.0
    return {
        "delivered_count": confirmed,
        "delivery_rate": delivery_rate,
        "needs_attention_count": needs,
        "last_sent_at": last,
        # 게시물 썸네일 = 캠페인 media_url (목록 응답에서 Graph API 로 best-effort 보강됨)
        "thumbnail_url": obj.media_url or None,
    }


def build_counts(campaign_qs) -> dict:
    """상태별 캠페인 개수 + total. (단일 group-by 쿼리)"""
    rows = campaign_qs.values("status").annotate(n=Count("id"))
    by_status = {row["status"]: row["n"] for row in rows}
    counts = {s: by_status.get(s, 0) for s in AutoDMCampaign.Status.values}
    counts["total"] = sum(by_status.values())
    return counts


def _month_bounds(now=None):
    """현재 시각이 속한 캘린더월의 [start, next_month_start) 경계 (서버 타임존 기준, aware)."""
    local = timezone.localtime(now or timezone.now())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def is_admin_user(user) -> bool:
    """관리자 모드 여부 (DRF IsAdminUser 와 동일 기준: is_staff). superuser 도 포함."""
    return bool(user and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)))


def compute_monthly_usage(workspace, now=None, *, user=None) -> dict:
    """워크스페이스의 이번 달 DM 사용량 + 한도.

    한도는 workspace.owner 의 구독 플랜 features.dm_monthly_limit (-1=무제한) —
    발송 게이트(billing.dm_limits.check_dm_quota)와 동일 정의.
    요청자가 **관리자(is_staff/superuser)** 면 플랜과 무관하게 무제한(-1)으로 본다.
    사용량은 SentDMLog 에서 캘린더월 범위를 직접 집계(quota 소진 상태만).
    주의: 표시 수치는 이 워크스페이스 범위이고, enforcement 는 owner 전체 범위다
    (플랜이 유저 단위이므로 멀티 워크스페이스 분산 우회를 막기 위함).
    """
    from apps.billing.dm_limits import get_dm_monthly_limit

    start, end = _month_bounds(now)
    # v4.2 — 과금 정의(billing.dm_limits)와 동일하게 (캠페인 × 수신자) 고유쌍으로 집계한다.
    sent_this_month = (
        SentDMLog.objects.filter(
            campaign__ig_connection__workspace=workspace,
            created_at__gte=start,
            created_at__lt=end,
            status__in=SENT_FOR_QUOTA_STATUSES,
        )
        .values("campaign_id", "recipient_user_id")
        .distinct()
        .count()
    )

    if is_admin_user(user):
        limit = -1  # 관리자 모드 → 무제한
    else:
        limit = get_dm_monthly_limit(workspace.owner)
    is_unlimited = limit == -1
    return {
        "sent_this_month": sent_this_month,
        "monthly_free_limit": limit,  # -1 = 무제한
        "remaining_this_month": (None if is_unlimited else max(limit - sent_this_month, 0)),
        "is_over_limit": (False if is_unlimited else sent_this_month >= limit),
        "period_start": start,
        "period_end": end,
    }


def build_delivery_summary(campaign_qs) -> dict:
    """목록 범위 전체의 발송 요약 (delivery_rate / needs_attention 합).

    campaign_qs 에 연결된 모든 dm_logs 를 가로질러 집계한다.
    """
    agg = SentDMLog.objects.filter(campaign__in=campaign_qs).aggregate(
        confirmed=Count("id", filter=Q(status__in=CONFIRMED_DELIVERED_STATUSES)),
        accepted=Count("id", filter=Q(status__in=ACCEPTED_OR_AFTER_STATUSES)),
        needs=Count("id", filter=Q(status__in=NEEDS_ATTENTION_STATUSES)),
        delivered_or_sent=Count("id", filter=Q(status__in=SentDMLog.DELIVERED_STATUSES)),
        last=Max("created_at"),
    )
    confirmed = agg["confirmed"]
    accepted = agg["accepted"]
    total_attempt = SentDMLog.objects.filter(campaign__in=campaign_qs).count()
    delivery_rate = round(confirmed / accepted, 4) if accepted else 0.0
    success_rate = round(agg["delivered_or_sent"] / total_attempt, 4) if total_attempt else 0.0
    return {
        "total_sent": agg["delivered_or_sent"],
        "delivery_rate": delivery_rate,
        "success_rate": success_rate,
        "needs_attention_total": agg["needs"],
        "_last_activity_at": agg["last"],
    }


# ── 신규 요청자 시계열 (캠페인 진행 추이) ─────────────────────────────────────
# range → 버킷 단위. 고정 매핑(적응형 없음)이라 프론트 렌더가 예측 가능하다.
TIMESERIES_RANGES = {"all": "day", "24h": "hour", "7d": "day"}


def new_requester_timeseries(campaign, range_key: str = "all", now=None) -> dict:
    """캠페인 '신규 요청자' 시계열 — x=시간 버킷, y=그 버킷에 처음 요청한 사람 수.

    사람 단위: 한 사람의 **최초 트리거(루트 DM) 시각**을 그 사람의 요청 시점으로 본다
    (ROOT_DM_Q 기준 = people_rollup 과 동일한 사람 키공간). 같은 사람이 여러 번 댓글을
    달아도(재요청·복구 재댓글 포함) 최초 1회만 집계한다.

    핵심 정확성 규칙: first_at 은 캠페인 **전 생애** 루트 로그에서 사람별 MIN(created_at)
    으로 구한 뒤에야 윈도우로 거른다. 3일 전 첫 요청 + 1시간 전 재요청한 사람은 24h 뷰에서
    '신규'가 아니어야 하기 때문이다.

    시각 근사: created_at 은 웹훅 수신 시각(댓글 작성 후 수 초). 폴링 보정 댓글은 최대
    ~1시간 늦을 수 있다. IG 댓글 원본 작성시각은 저장하지 않으므로 created_at 이 프록시다.

    버킷·윈도우 정렬: 윈도우 = 버킷 그리드. 24h=현재(진행 중) 시각으로 끝나는 24개 시간
    버킷, 7d=오늘로 끝나는 7개 일 버킷, all=최초 요청일~오늘 일 버킷. 따라서 항상
    ``sum(series[].new_requesters) == totals.window_new_requesters`` 이고, all 이면
    ``== totals.lifetime_unique_requesters`` (stats people.total 과 동일 정의).

    KST(Asia/Seoul, DST 없음) 기준 벽시계 절단. 반환 datetime 은 전부 KST-aware.
    """
    if range_key not in TIMESERIES_RANGES:
        range_key = "all"
    tz = timezone.get_current_timezone()  # Asia/Seoul
    now_local = timezone.localtime(now or timezone.now(), tz)

    root_qs = campaign.dm_logs.filter(ROOT_DM_Q)
    # 사람별 전 생애 최초 요청 시각. "" recipient_user_id 는 people_rollup 과 동일하게 한
    # 행으로 collapse 되어 총계가 stats people.total 과 일치한다. 수천 규모라 Python 버킷팅으로 충분.
    first_ats = [
        row["first_at"]
        for row in root_qs.values("recipient_user_id").annotate(first_at=Min("created_at"))
        if row["first_at"] is not None
    ]
    last_request_at = root_qs.aggregate(m=Max("created_at"))["m"]
    lifetime_total = len(first_ats)
    first_request_at = min(first_ats) if first_ats else None

    granularity = TIMESERIES_RANGES[range_key]

    def _trunc(d):
        if granularity == "hour":
            return d.replace(minute=0, second=0, microsecond=0)
        return d.replace(hour=0, minute=0, second=0, microsecond=0)

    # KST 정렬 그리드(양끝 포함). 가장 오래된 버킷 시작 == 윈도우 시작.
    if range_key == "24h":
        end_b = _trunc(now_local)
        grid = [end_b - timedelta(hours=i) for i in range(23, -1, -1)]
        window_start = grid[0]
    elif range_key == "7d":
        end_b = _trunc(now_local)
        grid = [end_b - timedelta(days=i) for i in range(6, -1, -1)]
        window_start = grid[0]
    else:  # all — 최초 요청일부터 오늘까지
        grid = []
        if first_request_at is not None:
            start_b = _trunc(timezone.localtime(first_request_at, tz))
            end_b = _trunc(now_local)
            grid = [start_b + timedelta(days=i) for i in range((end_b - start_b).days + 1)]
        window_start = None  # 전 기간 = 윈도우 필터 없음

    counter: Counter = Counter()
    window_new = 0
    for dt in first_ats:
        b = _trunc(timezone.localtime(dt, tz))
        if window_start is None or b >= window_start:
            counter[b] += 1
            window_new += 1

    return {
        "range": range_key,
        "granularity": granularity,
        "timezone": "Asia/Seoul",
        "series": [{"bucket": b, "new_requesters": counter.get(b, 0)} for b in grid],
        "totals": {
            "lifetime_unique_requesters": lifetime_total,
            "window_new_requesters": window_new,
            "first_request_at": (
                timezone.localtime(first_request_at, tz) if first_request_at else None
            ),
            # 반복 댓글 포함 최신 루트 로그 시각 = '아직 움직이나' 신호(series 의 최초요청과 구분).
            "last_request_at": (
                timezone.localtime(last_request_at, tz) if last_request_at else None
            ),
        },
        # 로그 보존정책(SENTDMLOG_ARCHIVE_RETENTION_DAYS>0)이 켜지면 과거 first_at 이 왜곡되므로
        # false. 활성화 전 필수 절차는 config/settings/base.py 의 SENTDMLOG_ARCHIVE_* 주석 참조.
        "history_complete": not getattr(settings, "SENTDMLOG_ARCHIVE_RETENTION_DAYS", 0),
    }
