"""기간이 끝난 **무카드** 체험(= 만료 배치가 아직 안 돈 창)의 결제 시나리오.

만료 배치(``handle_trial_expiry``)는 1시간 주기라, 기간이 끝난 뒤에도 최대 1시간 동안
``status`` 가 ``TRIALING`` 으로 남아 있다. 그 창에서 카드를 등록하면 종전에는
``attach_only`` 로 가서

  · ``first_charge_at`` 이 **과거 시각**으로 고지되고(= PaymentConsent 에 허위 고지)
  · 화면은 "체험 종료 시 첫 결제가 진행됩니다" 라고 안내하는데
  · ``process_due_renewals``(10분 주기)가 **즉시** 긁는다

프론트 화면이 ``trial_last_day`` 가 지나면 곧바로 "체험이 끝났어요 / 카드 등록" 을 띄우므로
이 창은 실제로 밟힌다(2026-10-13 제보). 기간이 끝났으면 **배치가 돈 뒤와 같은 결론**
(``charge_now``)을 내는 것이 맞고, 그러면 배치와 사용자 중 누가 먼저든 결과가 같다.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.models import SubscriptionPlan, SubscriptionStatus, TrialKind
from apps.billing.subscription_utils import ensure_subscription
from apps.billing.tasks import handle_trial_expiry
from apps.billing.toss_flows import preview_subscription, trial_window_open

User = get_user_model()


def _trial(*, ends_in, card=False):
    """무카드/카드 자동 체험을 만든다. ``ends_in`` 이 음수면 이미 끝난 상태."""
    user = User.objects.create_user(
        email=f"lapse-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )
    pro = SubscriptionPlan.objects.get(name="pro")
    now = timezone.now()
    sub = ensure_subscription(user)
    sub.plan = pro
    sub.trial_plan = pro
    sub.status = SubscriptionStatus.TRIALING
    sub.trial_kind = TrialKind.AUTO
    sub.current_period_start = now - timedelta(days=30)
    sub.current_period_end = now + ends_in
    sub.monthly_amount_snapshot = pro.monthly_price
    sub.trial_used_at = sub.current_period_start
    if card:
        sub.set_billing_key("bk_lapse", card_company="현대", card_number="4330****")
    sub.save()
    return user, sub


@pytest.mark.django_db
class TestTrialWindowOpen:
    def test_open_while_time_remains(self):
        _, sub = _trial(ends_in=timedelta(days=3))
        assert trial_window_open(sub) is True

    def test_closed_once_period_passed(self):
        _, sub = _trial(ends_in=timedelta(minutes=-10))
        assert trial_window_open(sub) is False

    def test_card_trial_stays_open_renewals_owns_it(self):
        """카드 체험의 첫 과금은 갱신 파이프라인 소유 — 여기서 charge_now 로 바꾸면 이중 과금."""
        _, sub = _trial(ends_in=timedelta(minutes=-10), card=True)
        assert trial_window_open(sub) is True

    def test_missing_period_end_is_treated_as_open(self):
        _, sub = _trial(ends_in=timedelta(days=1))
        sub.current_period_end = None
        sub.save(update_fields=["current_period_end"])
        assert trial_window_open(sub) is True

    def test_non_trialing_is_not_open(self):
        _, sub = _trial(ends_in=timedelta(days=3))
        sub.status = SubscriptionStatus.ACTIVE
        sub.save(update_fields=["status"])
        assert trial_window_open(sub) is False


@pytest.mark.django_db
class TestLapsedTrialPreview:
    def test_running_trial_is_attach_only_with_future_charge(self):
        user, _ = _trial(ends_in=timedelta(days=3))
        d = preview_subscription(user, plan_name="pro")
        assert d["scenario"] == "attach_only"
        assert d["first_charge_at"] > timezone.now()

    def test_lapsed_trial_is_charge_now_not_attach_only(self):
        """핵심 회귀 — 과거 시각을 first_charge_at 으로 고지하면 안 된다."""
        user, _ = _trial(ends_in=timedelta(minutes=-10))
        d = preview_subscription(user, plan_name="pro")
        assert d["scenario"] == "charge_now"
        assert d["first_charge_at"] >= timezone.now() - timedelta(seconds=30)

    def test_long_lapsed_trial_also_charges_now(self):
        user, _ = _trial(ends_in=timedelta(days=-2))
        assert preview_subscription(user, plan_name="pro")["scenario"] == "charge_now"

    def test_lapsed_trial_may_switch_plan(self):
        """체험이 끝났으면 플랜을 바꿔 결제할 수 있어야 한다(체험 중엔 금지)."""
        user, _ = _trial(ends_in=timedelta(minutes=-10))
        assert preview_subscription(user, plan_name="basic")["scenario"] == "charge_now"

    def test_running_trial_cannot_switch_plan(self):
        from apps.billing.toss_flows import BillingFlowError

        user, _ = _trial(ends_in=timedelta(days=3))
        with pytest.raises(BillingFlowError):
            preview_subscription(user, plan_name="basic")

    def test_lapsed_trial_is_not_blocked_as_already_paid(self):
        """``is_paid_plan`` 가드에 걸리면 '이미 유료 구독 중' 으로 결제가 통째로 막힌다."""
        user, _ = _trial(ends_in=timedelta(minutes=-10))
        d = preview_subscription(user, plan_name="pro")  # 예외가 나면 실패
        assert d["first_charge_amount"] > 0


@pytest.mark.django_db
class TestBatchAndUserAgreeEitherOrder:
    """배치가 먼저 돌든 사용자가 먼저 누르든 **결론이 같아야** 한다."""

    def test_batch_downgrades_cardless_lapsed_trial(self):
        user, sub = _trial(ends_in=timedelta(minutes=-10))
        handle_trial_expiry()
        sub.refresh_from_db()
        assert sub.plan.name == "free"
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_scenario_is_charge_now_before_and_after_batch(self):
        user, sub = _trial(ends_in=timedelta(minutes=-10))
        before = preview_subscription(user, plan_name="pro")["scenario"]
        handle_trial_expiry()
        sub.refresh_from_db()
        after = preview_subscription(user, plan_name="pro")["scenario"]
        assert before == after == "charge_now"

    def test_batch_leaves_card_trials_alone(self):
        """카드 체험을 배치가 내리면 과금 없이 유료 기간이 사라진다 — 절대 금지."""
        _, sub = _trial(ends_in=timedelta(minutes=-10), card=True)
        handle_trial_expiry()
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.TRIALING
        assert sub.plan.name == "pro"

    def test_batch_leaves_running_trials_alone(self):
        _, sub = _trial(ends_in=timedelta(days=3))
        handle_trial_expiry()
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.TRIALING
        assert sub.plan.name == "pro"
