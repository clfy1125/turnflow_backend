"""apps/admin_api/dm_policy_rollup.py — 사람 단위 `policy` 집계 (DM-7 / DM-8 / DM-13).

`dm_error_catalog.classify()` 는 **발송 1건**을 분류한다. 캠페인 상세 화면이 필요한 것은
**사람 단위 인원**(🔴 34명 / ⚪ 91명)과 그 사유별 내역이라, 사람 → 대표 로그 → policy 로
접는 계층이 하나 더 필요하다. 그 규칙을 여기 한 곳에 둔다.

**어드민 전용 모듈인 이유** — `policy`(조사 필요/자동 처리)는 운영자용 어휘다. 공용
`campaign_stats.build_dm_stats()` 에 넣으면 유저 콘솔 응답에도 실려 나가고,
`apps.integrations` → `apps.admin_api` 역방향 의존이 생긴다.

──────────────────────────────────────────────────────────────────────────
두 축 (DM-13)
──────────────────────────────────────────────────────────────────────────
화면의 '발송 안 됨'은 축마다 모수와 판정 규칙이 다르다. 카드 숫자와 목록 행이 어긋나지
않으려면 **집계·목록 배지·서버 필터가 전부 같은 대표 로그**를 봐야 한다 →
:func:`rep_log_qs` 하나만 쓴다.

===========  ==================================  ==================================
축            '발송 안 됨' 모수                    대표(사유) 로그
===========  ==================================  ==================================
opening       루트 DM 이 발송/대기 어디에도 못     그 사람의 **가장 최근 실패·정체**
              간 사람 + 도착 미확인으로 끝난 사람   루트 DM
follow_up     **마지막** 후속 DM 이 실패인 사람     그 마지막 후속 DM 자체
===========  ==================================  ==================================

대표 사유 후보에서 성공 로그를 빼는 이유: 성공은 '발송 안 됨'의 사유가 될 수 없어서다.
그래야 사유별 인원의 합이 카드 인원과 정확히 맞는다.
"""

from __future__ import annotations

from collections import Counter

from django.db.models import Count, Q

from apps.admin_api.dm_error_catalog import INVESTIGATE, NORMAL, classify, policy_display
from apps.integrations.campaign_stats import (
    CONFIRMED_DELIVERED_STATUSES,
    FOLLOW_UP_KIND,
    QUEUE_WAITING_STATUSES,
    ROOT_DM_Q,
    SENT_FOR_QUOTA_STATUSES,
    followup_bucket,
    followup_failed_q,
    latest_followup_rows,
)
from apps.integrations.models import SentDMLog

# 축 키 — `?dm_axis=` 파라미터 값이기도 하다.
AXIS_OPENING = "opening"
AXIS_FOLLOW_UP = "follow_up"
AXES = (AXIS_OPENING, AXIS_FOLLOW_UP)

# 대표 사유 후보 = '발송 안 됨'을 설명할 수 있는 로그들.
# 성공(delivered/read/recovery_delivered)·진행 중(queued/accepted…)은 사유가 아니다.
NOT_SENT_LOG_STATUSES = [
    SentDMLog.Status.FAILED,  # legacy
    SentDMLog.Status.FAILED_API,  # legacy
    SentDMLog.Status.FAILED_TOKEN,
    SentDMLog.Status.FAILED_WINDOW,
    SentDMLog.Status.FAILED_PARAM,
    SentDMLog.Status.FAILED_NO_TRACE,
    SentDMLog.Status.RECOVERY_PENDING,
    SentDMLog.Status.RECOVERY_EXPIRED,
    SentDMLog.Status.SKIPPED,
]

_REP_FIELDS = ("status", "error_code", "error_subcode", "error_message")
_EMPTY_BLOCK = {"total": 0, "investigate": 0, "normal": 0, "breakdown": []}


def _keys(group_by_campaign: bool) -> list[str]:
    return ["campaign_id", "recipient_user_id"] if group_by_campaign else ["recipient_user_id"]


def rep_log_qs(log_qs, *, axis: str | None = None, group_by_campaign: bool = False):
    """사람별 **대표 로그 1행** 쿼리셋 (Postgres DISTINCT ON) — 판정 규칙의 단일 소스.

    ``axis=None`` 이면 축을 가리지 않고 '가장 최근 실패·정체 로그'다(목록 배지 기본값 —
    어느 축인지 모른 채 한 사람을 볼 때).

    ``.values("id")`` 로 뽑아 서브쿼리로 쓰면 파이썬 분류 결과를 **쌍 OR 체인 없이**
    목록 필터에 그대로 얹을 수 있다(DM-15 의 500쌍 상한 제거).
    """
    keys = _keys(group_by_campaign)
    if axis == AXIS_FOLLOW_UP:
        # 후속 축의 대표는 '마지막 후속 DM' 그 자체다(실패 여부와 무관하게 뽑고,
        # '발송 안 됨' 판정은 그 1건의 버킷으로 한다) — followup_rollup 과 같은 규칙.
        base = log_qs.filter(dm_kind=FOLLOW_UP_KIND)
    elif axis == AXIS_OPENING:
        base = log_qs.filter(ROOT_DM_Q, status__in=NOT_SENT_LOG_STATUSES)
    else:
        base = log_qs.filter(status__in=NOT_SENT_LOG_STATUSES)
    return base.order_by(*keys, "-created_at").distinct(*keys)


def _rep_rows(log_qs, *, axis: str | None = None, group_by_campaign: bool = False) -> dict:
    """:func:`rep_log_qs` 를 dict 로 — 키는 ``recipient_user_id`` 또는 (campaign, recipient)."""
    keys = _keys(group_by_campaign)
    rows = rep_log_qs(log_qs, axis=axis, group_by_campaign=group_by_campaign).values(
        *keys, *_REP_FIELDS
    )
    if group_by_campaign:
        return {(r["campaign_id"], r["recipient_user_id"]): r for r in rows}
    return {r["recipient_user_id"]: r for r in rows}


def _classify_row(row: dict) -> dict:
    """대표 로그 1행 → 사전 판정 (건너뜀은 error_message 로 사유가 갈리므로 함께 넘긴다)."""
    return classify(
        row["error_code"] or "",
        row["error_subcode"] or "",
        row["status"],
        row.get("error_message") or "",
    )


def _block(reps: list[dict]) -> dict:
    """대표 로그 목록 → ``not_sent`` 응답 블록.

    계약 (프론트가 표·칩에 그대로 씀):
      - ``investigate + normal == total``
      - ``Σ breakdown[].people == total``
      - ``title`` 은 서버 사전 문구 그대로 (프론트에 사유 이름을 두지 않는다)
      - ``reason`` 은 문구가 바뀌어도 고정 (프론트가 그대로 ``?error_reason=`` 에 싣는다)
    """
    if not reps:
        return dict(_EMPTY_BLOCK, breakdown=[])

    buckets: Counter = Counter()
    for row in reps:
        described = _classify_row(row)
        buckets[(described["policy"], described["reason"], described["title"])] += 1

    breakdown = [
        {
            "reason": reason,
            "policy": policy,
            "policy_display": policy_display(policy),
            "title": title,
            "people": n,
        }
        # 🔴 먼저, 그 안에서 인원 많은 순 (화면 정렬을 프론트가 다시 하지 않도록)
        for (policy, reason, title), n in sorted(
            buckets.items(), key=lambda kv: (kv[0][0] != INVESTIGATE, -kv[1], kv[0][1])
        )
    ]
    return {
        "total": len(reps),
        "investigate": sum(b["people"] for b in breakdown if b["policy"] == INVESTIGATE),
        "normal": sum(b["people"] for b in breakdown if b["policy"] == NORMAL),
        "breakdown": breakdown,
    }


# ── 오프닝 축 ─────────────────────────────────────────────────────────
def opening_not_sent_annotations() -> dict:
    """'발송 안 됨' 판정을 그룹 쿼리(values().annotate())에서 쓸 수 있게 한 판.

    :func:`opening_not_sent` 과 **같은 산술**이며 (`campaign_stats._derive_people` 의
    failed + unconfirmed 정의), 목록 필터 `?dm_axis=opening` 이 이걸 HAVING 으로 건다 →
    카드 인원과 목록 행 수가 구조적으로 같아진다.
    """
    sent_or_waiting = Q(status__in=SENT_FOR_QUOTA_STATUSES + QUEUE_WAITING_STATUSES)
    return {
        "root_n": Count("id", filter=ROOT_DM_Q),
        "root_sent_or_waiting_n": Count("id", filter=ROOT_DM_Q & sent_or_waiting),
        "root_confirmed_n": Count(
            "id", filter=ROOT_DM_Q & Q(status__in=CONFIRMED_DELIVERED_STATUSES)
        ),
        "root_no_trace_n": Count(
            "id", filter=ROOT_DM_Q & Q(status=SentDMLog.Status.FAILED_NO_TRACE)
        ),
    }


OPENING_NOT_SENT_HAVING = Q(root_n__gt=0) & (
    Q(root_sent_or_waiting_n=0) | (Q(root_no_trace_n__gt=0) & Q(root_confirmed_n=0))
)


def opening_not_sent(log_qs) -> dict:
    """오프닝 축 '발송 안 됨' 인원 분해 (= ``unique_failed + unique_unconfirmed``).

    두 집합의 합이며 서로소다:
      - failed      : 루트 DM 이 발송/대기 어디에도 못 간 사람
      - unconfirmed : 발송은 됐으나 도착 미확인으로 끝난 사람 (confirmed 없음)
    (`campaign_stats._derive_people` 의 정의와 같은 산술 — 값이 갈라지면 회귀 테스트가 잡는다.)
    """
    flags = (
        log_qs.values("recipient_user_id")
        .annotate(**opening_not_sent_annotations())
        .filter(OPENING_NOT_SENT_HAVING)
        # Meta.ordering 이 GROUP BY 로 새면 사람이 여러 행으로 쪼개진다 — 명시적으로 끈다.
        .order_by()
    )
    not_sent_ids = [f["recipient_user_id"] for f in flags]
    if not not_sent_ids:
        return dict(_EMPTY_BLOCK, breakdown=[])

    reps = _rep_rows(log_qs, axis=AXIS_OPENING)
    return _block([reps[rid] for rid in not_sent_ids if rid in reps])


# ── 후속 DM 축 ────────────────────────────────────────────────────────
def followup_not_sent_annotations(log_qs, *, group_by_campaign: bool = True) -> dict:
    """'마지막 후속 DM 이 실패' 판정을 그룹 쿼리에서 쓸 수 있게 한 판 (DM-13).

    ``rep_log_qs`` 서브쿼리를 그대로 참조하므로 :func:`followup_not_sent` 와 **같은 행**을
    본다 — Max(created_at) 비교 같은 재구현을 쓰면 동시각 타이 때 둘이 갈린다.
    """
    rep_ids = rep_log_qs(log_qs, axis=AXIS_FOLLOW_UP, group_by_campaign=group_by_campaign).values(
        "id"
    )
    return {"followup_failed_n": Count("id", filter=Q(id__in=rep_ids) & followup_failed_q())}


FOLLOWUP_NOT_SENT_HAVING = Q(followup_failed_n__gt=0)


def followup_not_sent(log_qs) -> dict:
    """후속 DM 축 '발송 안 됨' 인원 분해 — **마지막 후속 DM** 기준(DM-6 과 같은 규칙).

    오프닝 축과 판정 규칙이 다르므로 대표 로그도 '최근 실패'가 아니라 **마지막 후속 그 자체**다
    (마지막이 실패인 사람만 이 블록에 들어오므로 둘은 같은 행을 가리킨다).
    """
    reps = [
        row
        for row in latest_followup_rows(log_qs, *_REP_FIELDS[1:])
        if followup_bucket(row["status"]) == "failed"
    ]
    return _block(reps)


# ── 축 없는 사람 단위 롤업 (운영 대시보드) ────────────────────────────
def not_sent_people_rollup(log_qs) -> dict:
    """축을 가리지 않은 '발송 안 됨' **사람 수** 롤업 (DM-17).

    운영 대시보드 팝업이 **건수**만 갖고 있어, 사유별 `보러가기` 로 착지한 수신자 목록
    (사람 단위)과 숫자가 달라 보이던 문제를 없앤다("3건" ↔ 2행 = 목록이 잘린 것처럼 읽힘).

    성립하는 항등 — 같은 기간·같은 조건으로 부른
    ``GET /admin/auto-dm/recipients/`` 의 ``count`` 와 같다:

    - ``investigate`` == ``?error_policy=investigate`` 의 count
    - ``normal``      == ``?error_policy=normal`` 의 count
    - ``by_reason[].people`` == ``?error_reason=<그 reason>`` 의 count

    구조적으로 같아지는 이유는 **같은 대표 로그**(:func:`rep_log_qs`, axis=None,
    (campaign, recipient) 그룹)를 보고 **같은 사전**(:func:`_classify_row`)으로 접기
    때문이다 — 목록 쪽 필터는 이 대표 로그 id 를 서브쿼리로 걸어 HAVING 한다.

    ``by_reason`` 은 사유 1종 = 1행이라 **서로소**이며 ``Σ people == total`` 이다.
    (``failure_breakdown`` 은 ``(code, subcode, status)`` 단위라 한 사유가 여러 행으로
     쪼개진다 — 그쪽 행의 ``people`` 을 더하면 중복 계산된다. 아래 뷰 주석 참고.)
    """
    rows = list(
        rep_log_qs(log_qs, group_by_campaign=True).values(
            "campaign_id", "recipient_user_id", *_REP_FIELDS
        )
    )
    block = _block(rows)
    return {
        "total": block["total"],
        "investigate": block["investigate"],
        "normal": block["normal"],
        "by_reason": block["breakdown"],
    }


# ── 목록 배지 ─────────────────────────────────────────────────────────
def person_rep_map(log_qs, *, axis: str | None = None, group_by_campaign: bool = True) -> dict:
    """사람 → ``{policy, reason, title}`` (DM-8 배지 · DM-16 사유 열 공용).

    실패·정체 로그가 하나도 없는 사람은 키가 없다(= 분류할 사유 자체가 없음 → 빈 문자열).
    `not_sent` 집계와 **같은 대표 로그**를 보므로 카드 인원과 목록 배지가 어긋나지 않는다.

    DM-16 — 사유 문구(title)도 여기서 나온다. 예전에는 '가장 최근 로그'에서 뽑아, 과거에
    실패했다가 결국 성공한 사람의 행이 `policy` 는 🔴 인데 사유 열만 비어 보였다.
    """
    out = {}
    for key, row in _rep_rows(log_qs, axis=axis, group_by_campaign=group_by_campaign).items():
        described = _classify_row(row)
        out[key] = {
            "policy": described["policy"],
            "reason": described["reason"],
            "title": described["title"],
        }
    return out


def person_policy_map(log_qs, *, axis: str | None = None, group_by_campaign: bool = True) -> dict:
    """사람 → ``policy`` (:func:`person_rep_map` 의 축약)."""
    return {
        key: rep["policy"]
        for key, rep in person_rep_map(
            log_qs, axis=axis, group_by_campaign=group_by_campaign
        ).items()
    }
