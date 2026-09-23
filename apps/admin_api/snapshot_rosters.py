"""apps/admin_api/snapshot_rosters.py — 전체 현황 타일의 **모수 쿼리 단일 소스** (SNAP-1/2).

마케팅 대시보드 상단 `전체 현황` 의 `실제 결제 인원` · `프로 체험 인원` 타일을 누르면 그
사람들의 명단으로 들어간다. 타일과 명단이 조용히 어긋나지 않으려면 **숫자를 만드는 그
쿼리가 명단도 만들어야** 한다 — 그래서 판정을 여기 한 곳에 두고
``views/dashboard_marketing.py`` 의 ``_snapshot``/``_trial_now`` 와 명단 뷰가 함께 import 한다.

지켜야 할 항등 (프론트 요청서 §공통 ①):
    SNAP-1 count               == snapshot.paying.total
    SNAP-1 ?plan=X count       == snapshot.paying.by_plan[X].count
    SNAP-2 count               == trial_now.total (= will_charge + cancelled + no_card)
    SNAP-2 ?bucket=will_charge == trial_now.will_charge
    SNAP-2 ?bucket=cancelled   == trial_now.cancelled
    SNAP-2 ?bucket=no_card     == trial_now.no_card

⭐ 2026-09-23 정책 변경: ``no_card``(카드 없이 프로를 쓰는 체험자)를 **명단에 포함**한다.
종전엔 "카드 등록 체험자만"이었는데, 카드 없는 프로 30일 자동 지급(``billing/auto_trial.py``)이
켜지면서 그쪽이 **체험 인원의 다수**가 됐다 — 빼두면 화면의 '프로 체험 인원' 이 실제 프로를
쓰는 사람보다 한참 작아지고, 그 인원을 어드민에서 찾을 방법이 아예 없었다.
"결제 예정액" 열이 거짓이 되는 문제는 **금액을 주지 않는 것**으로 막는다(``cancelled`` 과 동일
취급 — :func:`apps.admin_api.views.snapshot` 의 ``expected_amount``).
"""

from __future__ import annotations

from django.db.models import F, Q

from apps.billing.models import PaymentHistory, PaymentStatus, SubscriptionStatus, UserSubscription

# 마케팅 지표에서 제외하는 플랜 (dashboard_marketing._PAID_EXCLUDE 와 같은 값 —
# 순환 import 를 피하려 여기에 정본을 두고 그쪽이 이것을 쓴다).
PAID_EXCLUDE = ["free", "admin"]

# 체험 버킷 (SNAP-2 ?bucket=)
BUCKET_WILL_CHARGE = "will_charge"
BUCKET_CANCELLED = "cancelled"
BUCKET_NO_CARD = "no_card"
TRIAL_BUCKETS = (BUCKET_WILL_CHARGE, BUCKET_CANCELLED, BUCKET_NO_CARD)


def paying_subscriptions_qs():
    """`실제 결제 인원` 의 모수 — 실결제(PAID) 이력 보유 + 현재 유료 구독 ACTIVE.

    PAST_DUE(결제 실패 dunning 중)는 제외한다 — customer_actions.payment_failed 에 별도로
    잡히고, '실제 결제 인원' 의 의미를 흐린다(R-7 ②). 무료체험·어드민 수동 부여도 제외
    (전자는 PAID 이력이 없고 후자는 admin 플랜 제외로 걸러진다).
    """
    paid_user_ids = PaymentHistory.objects.filter(status=PaymentStatus.PAID).values("user_id")
    return UserSubscription.objects.filter(
        user_id__in=paid_user_ids, status=SubscriptionStatus.ACTIVE
    ).exclude(plan__name__in=PAID_EXCLUDE)


def trial_will_charge_qs(now):
    """체험 중 + 카드 있음 + 미취소 → 기간말에 **과금된다**."""
    return (
        UserSubscription.objects.exclude(plan__name__in=PAID_EXCLUDE)
        .filter(current_period_end__gt=now)
        .filter(status=SubscriptionStatus.TRIALING, billing_key_issued_at__isnull=False)
    )


def trial_cancelled_qs(now):
    """체험 중 취소(기간 남음) → 과금 없이 free 로 내려간다.

    ⚠️ ``cancelled_during_trial_at`` 은 재체험 시에도 지우지 않으므로(T-3-②) 과거 체험의
    취소 기록이 남아 있을 수 있다. 그 값으로 '지금 유료 기간을 취소한 사람' 을 체험 취소로
    오인하지 않도록 **현재 기간 포함**(>= current_period_start)을 함께 본다.
    """
    return (
        UserSubscription.objects.exclude(plan__name__in=PAID_EXCLUDE)
        .filter(current_period_end__gt=now)
        .filter(
            status=SubscriptionStatus.CANCELLED,
            cancelled_during_trial_at__isnull=False,
            current_period_start__isnull=False,
            cancelled_during_trial_at__gte=F("current_period_start"),
        )
    )


def trial_no_card_qs(now):
    """체험 중 + 카드 없음 + 미취소 → 과금 대상이 아니다.

    쿠폰 체험과 **카드 없는 프로 30일 자동 지급**(``billing/auto_trial.py``)이 여기 모인다.
    체험이 끝나면 빌링키가 없어 ``handle_trial_expiry`` 가 과금 없이 무료로 내린다 — 그래서
    금액 열(``expected_amount``)은 주지 않는다.
    """
    return (
        UserSubscription.objects.exclude(plan__name__in=PAID_EXCLUDE)
        .filter(current_period_end__gt=now)
        .filter(status=SubscriptionStatus.TRIALING, billing_key_issued_at__isnull=True)
    )


def trial_roster_qs(now, *, bucket: str | None = None):
    """`프로 체험 인원` 명단의 모수 — ``will_charge ∪ cancelled ∪ no_card``.

    세 버킷의 합집합이 곧 ``trial_now.total`` 이다(타일 값과 같아야 하므로 하나도 빼지 않는다).
    ``bucket`` 을 주면 그 버킷만. 값 검증은 호출 측(뷰)이 화이트리스트로 한다.
    """
    will = trial_will_charge_qs(now)
    cancelled = trial_cancelled_qs(now)
    no_card = trial_no_card_qs(now)
    if bucket == BUCKET_WILL_CHARGE:
        return will
    if bucket == BUCKET_CANCELLED:
        return cancelled
    if bucket == BUCKET_NO_CARD:
        return no_card
    return (
        UserSubscription.objects.exclude(plan__name__in=PAID_EXCLUDE)
        .filter(current_period_end__gt=now)
        .filter(
            Q(pk__in=will.values("pk"))
            | Q(pk__in=cancelled.values("pk"))
            | Q(pk__in=no_card.values("pk"))
        )
    )


def bucket_of(sub) -> str:
    """행 하나의 버킷 판정 — 명단 응답의 ``bucket`` 필드.

    프론트는 이 값을 그대로 신뢰한다(자체 재판정 금지). ``trial_roster_qs`` 의 세 하위
    쿼리와 **같은 조건**이어야 한다: TRIALING 이면 카드 유무로 will_charge / no_card 가
    갈리고, 그 밖(CANCELLED)이면 cancelled.
    """
    if sub.status == SubscriptionStatus.TRIALING:
        return BUCKET_WILL_CHARGE if sub.billing_key_issued_at else BUCKET_NO_CARD
    return BUCKET_CANCELLED
