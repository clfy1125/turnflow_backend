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

from .dm_status_groups import HIDDEN_SPAM, status_group_q
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
def root_dm_q(prefix: str = "") -> Q:
    """루트 DM 판별 Q — ``prefix`` 를 주면 역참조 경로(예: ``dm_logs``)로 감싼다.

    캠페인 목록에서 SentDMLog 를 조인해 사람 단위로 집계할 때 같은 정의를 써야 하므로
    (annotate_campaign_people) 판정을 복제하지 말고 이 함수를 쓸 것.
    """
    p = f"{prefix}__" if prefix else ""
    return Q(**{f"{p}parent_log__isnull": True}) & ~Q(**{f"{p}dm_kind": SentDMLog.DMKind.REWARD})


ROOT_DM_Q = root_dm_q()


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


def build_dm_stats(log_qs) -> dict:
    """SentDMLog 쿼리셋 → DM 발송 통계 dict (``DMVerificationStatsSerializer`` 형태).

    **유저 콘솔(`/dm-verification/stats/`)과 어드민(`admin_api` 캠페인 상세 ·
    `/admin/dm-verification/stats/`)의 단일 소스** (DM-1-a). 어드민이 사본을 들고
    있던 시절에는 이벤트 단위 필드만 있고 `unique_*` 가 통째로 빠져 있어서, 같은
    캠페인의 숫자가 화면마다 달랐다 — 필드를 복사하지 말고 이 함수를 호출할 것.

    두 층위를 함께 낸다:
    - **이벤트 단위** (total/queued/delivered/... , delivery_rate/read_rate) — 배송
      신뢰성·디버깅용. follow-gate 캠페인은 1명 = DM 2건이라 사람 수보다 크다.
    - **사람 단위** (``unique_*``) — 마케팅 화면용. 모수는 루트 DM(:data:`ROOT_DM_Q`)
      기준 :func:`people_rollup` 과 동일해서 queue-state 의 ``people`` 과 정합한다.

    헤드라인 지표는 ``unique_sent_rate``(= unique_sent / unique_targets). ``delivery_rate``
    는 하드실패가 분모에서 빠져 100% 로 부풀 수 있으므로 헤드라인에 쓰지 말 것.
    """
    delivered_or_read = Q(status="delivered") | Q(status="read")
    agg = log_qs.aggregate(
        total=Count("id"),
        queued=Count("id", filter=Q(status="queued")),
        submitting=Count("id", filter=Q(status="submitting")),
        accepted=Count("id", filter=Q(status="accepted")),
        delivered=Count("id", filter=Q(status="delivered")),
        read=Count("id", filter=Q(status="read")),
        rate_limited=Count("id", filter=Q(status="rate_limited")),
        failed_token=Count("id", filter=Q(status="failed_token")),
        failed_window=Count("id", filter=Q(status="failed_window")),
        failed_param=Count("id", filter=Q(status="failed_param")),
        failed_no_trace=Count("id", filter=Q(status="failed_no_trace")),
        skipped=Count("id", filter=Q(status="skipped")),
        recovery_pending=Count("id", filter=Q(status="recovery_pending")),
        recovery_delivered=Count("id", filter=Q(status="recovery_delivered")),
        recovery_expired=Count("id", filter=Q(status="recovery_expired")),
        legacy_sent=Count("id", filter=Q(status="sent")),
        legacy_failed=Count("id", filter=Q(status="failed")),
        legacy_failed_api=Count("id", filter=Q(status="failed_api")),
        # v3.3 — DM 종류별
        standalone_total=Count("id", filter=Q(dm_kind="standalone")),
        opening_total=Count("id", filter=Q(dm_kind="opening")),
        opening_delivered=Count("id", filter=Q(dm_kind="opening") & delivered_or_read),
        reward_total=Count("id", filter=Q(dm_kind="reward")),
        reward_delivered=Count("id", filter=Q(dm_kind="reward") & delivered_or_read),
        # v3.3 — Follow-gate
        gate_pending=Count("id", filter=Q(gate_status="pending")),
        gate_passed=Count("id", filter=Q(gate_status="passed")),
        gate_expired=Count("id", filter=Q(gate_status="expired")),
        # 공개 답글
        public_replies_posted=Count("id", filter=~Q(public_reply_id="")),
    )

    # ACCEPTED 진입 건 = accepted + delivered + read + failed_no_trace
    # (DELIVERED/READ는 ACCEPTED를 거쳐 갔고, no_trace 도 ACCEPTED 후 종결)
    accepted_or_after = (
        agg["accepted"]
        + agg["delivered"]
        + agg["read"]
        + agg["failed_no_trace"]
        + agg["recovery_delivered"]  # 복구 재전송 성공 = 실제 도착
    )
    confirmed_delivered = agg["delivered"] + agg["read"] + agg["recovery_delivered"]

    delivery_rate = confirmed_delivered / accepted_or_after if accepted_or_after else 0.0
    read_rate = agg["read"] / confirmed_delivered if confirmed_delivered else 0.0
    # Gate 통과율 = gate_passed / opening_delivered
    # (opening DELIVERED 중 사용자가 응답해서 통과한 비율)
    gate_passthrough_rate = (
        agg["gate_passed"] / agg["opening_delivered"] if agg["opening_delivered"] else 0.0
    )

    agg["delivery_rate"] = round(delivery_rate, 4)
    agg["read_rate"] = round(read_rate, 4)
    agg["gate_passthrough_rate"] = round(gate_passthrough_rate, 4)

    # ─────────────────────────────────────────────────────────────
    # v4.2 — 사람(수신자 Instagram ID) 단위 지표 + CTR (마케팅 API)
    # ─────────────────────────────────────────────────────────────
    def _uniq(**flt) -> int:
        base = log_qs.filter(**flt) if flt else log_qs
        return base.values("recipient_user_id").distinct().count()

    unique_recipients = _uniq()
    unique_sent = (
        log_qs.filter(status__in=SENT_FOR_QUOTA_STATUSES)
        .values("recipient_user_id")
        .distinct()
        .count()
    )
    unique_delivered = (
        log_qs.filter(status__in=["delivered", "read"])
        .values("recipient_user_id")
        .distinct()
        .count()
    )
    unique_read = _uniq(status="read")
    unique_followers = _uniq(gate_status="passed")

    # CTR = 상호작용한 고유 수신자 / 발송된 고유 수신자.
    #  - 게이트형 캠페인(follow_gate_enabled=True): 버튼 1회라도 클릭 = child 로그 존재
    #    (reward=통과 / retry=클릭했으나 미통과 둘 다 parent_log 로 opening 에 묶인다).
    #  - 비게이트형 캠페인: 상호작용 단계가 없으므로 "읽음(READ)" 을 참여로 본다.
    # 두 조건은 캠페인 타입으로 자연히 배타적이라 OR 하나로 집계된다.
    ctr_interacted = (
        log_qs.filter(
            Q(campaign__follow_gate_enabled=True, parent_log__isnull=False)
            | Q(campaign__follow_gate_enabled=False, status="read")
        )
        .values("recipient_user_id")
        .distinct()
        .count()
    )
    ctr = ctr_interacted / unique_sent if unique_sent else 0.0

    gate_flags = set(log_qs.values_list("campaign__follow_gate_enabled", flat=True).distinct())
    if gate_flags == {True}:
        ctr_basis = "click"
    elif gate_flags == {False} or not gate_flags:
        ctr_basis = "read"
    else:
        ctr_basis = "mixed"

    unique_delivery_rate = unique_delivered / unique_sent if unique_sent else 0.0

    # ── v4.4 — 사람 단위 처리 현황 (루트 DM 기준 rollup — queue-state.people 과 동일 정의)
    # unique_failed: 아무것도 받지 못한 사람 (하드실패·복구 대기/만료·한도 스킵).
    # "확인 필요" 카드가 이 값을 쓴다 — delivery_rate(Meta 접수건 기준)에는 하드실패가
    # 분모에서 빠져 100% 로 보이므로, 실패 인원은 이 필드로만 노출된다.
    people = people_rollup(log_qs)
    # 도착 미확인(사람): no_trace 만 있고 확정 도착이 한 번도 없는 수신자.
    # 발송은 됐으므로(쿼터 소진) people.failed 와는 서로소 — 합산해도 중복 없음.
    unique_unconfirmed = (
        log_qs.filter(status=SentDMLog.Status.FAILED_NO_TRACE)
        .exclude(
            recipient_user_id__in=log_qs.filter(status__in=CONFIRMED_DELIVERED_STATUSES).values(
                "recipient_user_id"
            )
        )
        .values("recipient_user_id")
        .distinct()
        .count()
    )
    # unique_delivered(전체 qs)는 리워드/재안내 child 도 세는 반면 people.total 은 루트 DM
    # 기준이라, 부모 오프닝이 시간창(기본 30일) 밖이고 child 만 안에 든 드문 경우 분자>분모가
    # 될 수 있다 → [0,1] 로 클램프해 100% 초과 표기를 막는다.
    unique_reach_rate = min(unique_delivered / people["total"], 1.0) if people["total"] else 0.0
    # 헤드라인 "N% 메시지가 성공적으로 전송됐어요" = 전체 대상 대비 전송된 비율.
    unique_sent_rate = min(unique_sent / people["total"], 1.0) if people["total"] else 0.0

    # ── v4.5 — '숨겨진 요청 · 스팸' 분리 (아무것도 못 받은 사람 = people.failed 의 부분집합)
    # 비팔로워 채널 미개설(2534025)로 숨김함으로 간 케이스: 복구 대기/만료(recovery_*) +
    # 복구 OFF 시 failed_param@2534025. failed 버킷(발송·대기 없음)에 든 사람만 센다
    # (같은 사람이 다른 댓글로 발송/대기 중이면 sent/waiting 버킷이므로 여기서 제외).
    root = log_qs.filter(ROOT_DM_Q)
    sent_or_waiting_ids = root.filter(
        status__in=SENT_FOR_QUOTA_STATUSES + QUEUE_WAITING_STATUSES
    ).values("recipient_user_id")
    unique_hidden_spam = (
        root.filter(status_group_q(HIDDEN_SPAM))
        .exclude(recipient_user_id__in=sent_or_waiting_ids)
        .values("recipient_user_id")
        .distinct()
        .count()
    )
    # 기존 '확인 필요' 총합(= failed + 도착미확인) 과, 숨김함을 뺀 새 '확인 필요'.
    # hidden_spam ⊆ failed 이므로 excl 은 항상 ≥ 0.
    unique_needs_attention = people["failed"] + unique_unconfirmed
    unique_needs_attention_excl_hidden = max(unique_needs_attention - unique_hidden_spam, 0)

    agg.update(
        {
            "unique_recipients": unique_recipients,
            "unique_sent": unique_sent,
            "unique_delivered": unique_delivered,
            "unique_read": unique_read,
            "unique_followers": unique_followers,
            "unique_delivery_rate": round(unique_delivery_rate, 4),
            "unique_targets": people["total"],
            "unique_waiting": people["waiting"],
            "unique_failed": people["failed"],
            "unique_unconfirmed": unique_unconfirmed,
            "unique_reach_rate": round(unique_reach_rate, 4),
            "unique_sent_rate": round(unique_sent_rate, 4),
            "unique_hidden_spam": unique_hidden_spam,
            "unique_needs_attention": unique_needs_attention,
            "unique_needs_attention_excl_hidden": unique_needs_attention_excl_hidden,
            "ctr": round(ctr, 4),
            "ctr_basis": ctr_basis,
            "ctr_interacted": ctr_interacted,
            "ctr_denominator": unique_sent,
        }
    )
    return agg


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


# ── 사람(인원) 단위 목록 요약 (DM-1-b) ────────────────────────────────────────
# annotate 필드명 = 응답 people.* 키 + **정렬 키**(OrderingFilter). 언더스코어 프리픽스를
# 붙이지 않는 이유 — `?ordering=-people_sent` 로 그대로 정렬할 수 있어야 하기 때문.
PEOPLE_ORDERING_FIELDS = ("people_targets", "people_sent")


def annotate_campaign_people(qs):
    """캠페인 목록에 **사람(인원) 단위** 집계를 annotate (목록 N+1 제거, DM-1-b).

    상세(:func:`build_dm_stats`)의 ``unique_*`` 와 **같은 정의**를 단일 LEFT JOIN 으로
    계산한다. 목록의 ``people.sent`` 와 상세의 ``unique_sent`` 가 어긋나면 안 되므로
    각 값의 유도식을 여기 명시한다.

    ``exclude(...)`` 를 못 쓰는 자리(unconfirmed/hidden_spam)는 **포함-배제**로 푼다:
      |A \\ B| = |A ∪ B| − |B|
    그래서 조건부 ``COUNT(DISTINCT recipient_user_id)`` 6개만으로 전부 나온다.
    파생값(waiting/failed/unconfirmed/hidden_spam/needs_attention)은 SQL 이 아니라
    :func:`people_from_annotations` 에서 뺄셈으로 만든다.
    """
    root = root_dm_q("dm_logs")
    sent_or_waiting = Q(dm_logs__status__in=SENT_FOR_QUOTA_STATUSES + QUEUE_WAITING_STATUSES)
    confirmed = Q(dm_logs__status__in=CONFIRMED_DELIVERED_STATUSES)
    no_trace = Q(dm_logs__status=SentDMLog.Status.FAILED_NO_TRACE)
    hidden = status_group_q(HIDDEN_SPAM, prefix="dm_logs")

    def _uniq(cond):
        return Count("dm_logs__recipient_user_id", filter=cond, distinct=True)

    return qs.annotate(
        # 전체 대상 인원 (루트 DM 기준 = people_rollup.total = unique_targets)
        people_targets=_uniq(root),
        # 실제 발송된 인원 (Meta 접수 이상) = unique_sent
        people_sent=_uniq(root & Q(dm_logs__status__in=SENT_FOR_QUOTA_STATUSES)),
        # ↓ 파생용 중간값 (응답에 그대로 나가지 않음)
        _people_sent_or_waiting=_uniq(root & sent_or_waiting),
        _people_confirmed=_uniq(confirmed),
        _people_no_trace_or_confirmed=_uniq(no_trace | confirmed),
        _people_hidden_or_sw=_uniq(root & (hidden | sent_or_waiting)),
    )


def people_from_annotations(obj: AutoDMCampaign) -> dict | None:
    """:func:`annotate_campaign_people` 결과 → 응답 ``people`` 블록 (추가 쿼리 0).

    annotate 되지 않은 인스턴스면 None (호출부가 필드를 생략하도록).
    total = sent + waiting + failed 항등은 people_rollup 과 동일하게 성립한다.
    """
    targets = getattr(obj, "people_targets", None)
    if targets is None:
        return None
    sent = getattr(obj, "people_sent", 0) or 0
    sent_or_waiting = getattr(obj, "_people_sent_or_waiting", 0) or 0
    waiting = max(sent_or_waiting - sent, 0)
    failed = max(targets - sent - waiting, 0)
    # unconfirmed = |no_trace \ confirmed| = |no_trace ∪ confirmed| − |confirmed|
    unconfirmed = max(
        (getattr(obj, "_people_no_trace_or_confirmed", 0) or 0)
        - (getattr(obj, "_people_confirmed", 0) or 0),
        0,
    )
    # hidden_spam = |hidden \ (발송·대기)| = |hidden ∪ 발송·대기| − |발송·대기|
    hidden_spam = max((getattr(obj, "_people_hidden_or_sw", 0) or 0) - sent_or_waiting, 0)
    needs_attention = max(failed + unconfirmed - hidden_spam, 0)
    return {
        "targets": targets,
        "sent": sent,
        "waiting": waiting,
        "failed": failed,
        "unconfirmed": unconfirmed,
        "hidden_spam": hidden_spam,
        "needs_attention": needs_attention,
        "sent_rate": round(sent / targets, 4) if targets else 0.0,
    }


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
