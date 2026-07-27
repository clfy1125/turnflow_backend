"""billing.snapshot_daily_metrics (일별 구독 스냅샷, 어드민 마케팅 P-4) 테스트.

주의: 테스트 DB 가 더러울 수 있어 카운트 계열 단언 전에 기존 UserSubscription 을
전부 CANCELLED(기간 없음)로 눕혀 집계 모집단을 중립화한다 (트랜잭션 내 — 롤백됨).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.models import (
    EXTRA_IG_ACCOUNT_PRICE,
    DailyPaidCohortSnapshot,
    DailySubscriptionSnapshot,
    PaymentHistory,
    PaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)
from apps.billing.tasks import snapshot_daily_metrics

User = get_user_model()


@pytest.fixture
def clean_subs(db):
    """더러운 테스트 DB 방어 — 기존 구독을 집계 밖(CANCELLED·기간 없음)으로 중립화."""
    UserSubscription.objects.all().update(
        status=SubscriptionStatus.CANCELLED,
        current_period_end=None,
        extra_ig_accounts=0,
        trial_used_at=None,
    )
    PaymentHistory.objects.filter(paid_at__isnull=False).update(
        paid_at=timezone.now() - timedelta(days=800)
    )
    DailySubscriptionSnapshot.objects.all().delete()
    DailyPaidCohortSnapshot.objects.all().delete()


@pytest.fixture
def pro_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="pro", defaults={"display_name": "프로", "monthly_price": 14900, "sort_order": 2}
    )
    if obj.monthly_price != 14900:
        obj.monthly_price = 14900
        obj.save(update_fields=["monthly_price"])
    return obj


def _mk_user():
    return User.objects.create_user(
        email=f"snap-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )


def _mk_paying(pro_plan, amount=14900, extra=0, first_paid_days_ago=40):
    user = _mk_user()
    UserSubscription.objects.create(
        user=user,
        plan=pro_plan,
        status=SubscriptionStatus.ACTIVE,
        monthly_amount_snapshot=amount,
        extra_ig_accounts=extra,
    )
    PaymentHistory.objects.create(
        user=user,
        amount=amount,
        status=PaymentStatus.PAID,
        paid_at=timezone.now() - timedelta(days=first_paid_days_ago),
    )
    return user


class TestSnapshotDailyMetrics:
    def test_creates_state_and_cohort_rows(self, clean_subs, pro_plan):
        now = timezone.now()
        _mk_paying(pro_plan, amount=14900, extra=1, first_paid_days_ago=40)
        _mk_paying(pro_plan, amount=14900, first_paid_days_ago=40)
        # 체험 중 1명 (MRR/코호트 제외)
        trial_user = _mk_user()
        UserSubscription.objects.create(
            user=trial_user,
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            trial_used_at=now - timedelta(days=1),
        )
        # 취소 예약 1명 (주기 남음)
        cancel_user = _mk_user()
        UserSubscription.objects.create(
            user=cancel_user,
            plan=pro_plan,
            status=SubscriptionStatus.CANCELLED,
            current_period_end=now + timedelta(days=10),
        )

        result = snapshot_daily_metrics()
        today = timezone.localdate()
        assert result["date"] == str(today)

        snap = DailySubscriptionSnapshot.objects.get(snapshot_date=today)
        assert snap.paying_count == 2
        assert snap.trialing_count == 1
        assert snap.cancel_scheduled_count == 1
        assert snap.extra_ig_count == 1
        assert snap.mrr_total == 14900 * 2 + EXTRA_IG_ACCOUNT_PRICE
        assert snap.plan_status_counts["pro"]["active"] == 2

        # 코호트: 두 유료 회원 모두 첫 결제가 40일 전 → 같은 코호트 월
        cohort_month = timezone.localtime(now - timedelta(days=40)).date().replace(day=1)
        cohort = DailyPaidCohortSnapshot.objects.get(snapshot_date=today, cohort_month=cohort_month)
        assert cohort.paying_users == 2
        assert cohort.mrr == 14900 * 2 + EXTRA_IG_ACCOUNT_PRICE

    def test_idempotent_rerun_updates_in_place(self, clean_subs, pro_plan):
        _mk_paying(pro_plan)
        snapshot_daily_metrics()
        _mk_paying(pro_plan)  # 상태 변화 후 재실행 — 같은 날 upsert
        snapshot_daily_metrics()

        today = timezone.localdate()
        assert DailySubscriptionSnapshot.objects.filter(snapshot_date=today).count() == 1
        assert DailySubscriptionSnapshot.objects.get(snapshot_date=today).paying_count == 2
        # 코호트 행도 그날 것을 통째로 재작성 (중복 없음)
        assert DailyPaidCohortSnapshot.objects.filter(snapshot_date=today).count() == 1

    def test_paying_without_payment_history_excluded_from_cohort(self, clean_subs, pro_plan):
        # 어드민 수동 부여 등 PAID 이력 없는 유료 ACTIVE — 상태 카운트엔 포함, 코호트 제외
        user = _mk_user()
        UserSubscription.objects.create(
            user=user,
            plan=pro_plan,
            status=SubscriptionStatus.ACTIVE,
            monthly_amount_snapshot=14900,
        )
        snapshot_daily_metrics()
        today = timezone.localdate()
        assert DailySubscriptionSnapshot.objects.get(snapshot_date=today).paying_count == 1
        assert DailyPaidCohortSnapshot.objects.filter(snapshot_date=today).count() == 0
