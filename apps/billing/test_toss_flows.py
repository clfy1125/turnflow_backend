"""토스 빌링 플로우 테스트 — confirm(트라이얼/즉시과금/카드변경) · 플랜변경 · 추가계정 · 환불.

토스 API 는 TossBillingClient 클래스 메서드를 monkeypatch 로 대체 (실 네트워크 없음).
더러운 테스트 DB 대응: 이메일/키는 uuid 로 유일화, 집계는 델타 단언.
"""

import math
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing import tasks, toss_flows
from apps.billing.models import (
    PaymentHistory,
    PaymentStatus,
    ReferralCode,
    ReferralRedemption,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)
from apps.billing.subscription_utils import ensure_subscription
from apps.billing.toss_flows import (
    BillingFlowError,
    ChargeDeclinedError,
    apply_refund,
    change_extra_accounts,
    change_plan,
    confirm_billing,
)
from apps.billing.toss_service import TossBillingClient, TossError, TossNetworkError

User = get_user_model()


# ──────────────────────────────────────────────
# 픽스처 / 헬퍼
# ──────────────────────────────────────────────


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"toss-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


@pytest.fixture
def pro_plan(db):
    return SubscriptionPlan.objects.get(name="pro")


@pytest.fixture
def basic_plan(db):
    return SubscriptionPlan.objects.get(name="basic")


@pytest.fixture
def free_plan(db):
    return SubscriptionPlan.objects.get(name="free")


class TossMock:
    """TossBillingClient 호출 기록 + 응답 제어."""

    def __init__(self):
        self.charges = []
        self.deleted_keys = []
        self.charge_error = None  # TossError/TossNetworkError 인스턴스면 raise

    def install(self, monkeypatch, billing_key="bk_test_1"):
        def fake_issue(auth_key, customer_key):
            return {
                "billingKey": billing_key,
                "customerKey": customer_key,
                "cardCompany": "현대",
                "cardNumber": "433012******123*",
            }

        def fake_charge(**kwargs):
            if self.charge_error is not None:
                raise self.charge_error
            self.charges.append(kwargs)
            return {
                "paymentKey": f"pk_{uuid.uuid4().hex[:12]}",
                "orderId": kwargs["order_id"],
                "status": "DONE",
                "receipt": {"url": "https://receipt.example/1"},
                "card": {"company": "현대", "number": "433012******123*"},
                "totalAmount": kwargs["amount"],
            }

        def fake_delete(billing_key_arg, customer_key_arg):
            self.deleted_keys.append(billing_key_arg)
            return {}

        monkeypatch.setattr(TossBillingClient, "issue_billing_key", fake_issue)
        monkeypatch.setattr(TossBillingClient, "charge", fake_charge)
        monkeypatch.setattr(TossBillingClient, "delete_billing_key", fake_delete)
        return self


@pytest.fixture
def toss(monkeypatch):
    return TossMock().install(monkeypatch)


# ──────────────────────────────────────────────
# confirm — 트라이얼
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestConfirmTrial:
    def test_pro_first_confirm_starts_trial_without_charge(self, user, toss, pro_plan):
        result = confirm_billing(user, auth_key="ak1", plan_name="pro")

        sub = result["subscription"]
        assert result["scenario"] == "trial"
        assert sub.status == SubscriptionStatus.TRIALING
        assert sub.plan.name == "pro"
        assert toss.charges == []  # 과금 없음
        assert sub.monthly_amount_snapshot == pro_plan.monthly_price
        assert sub.trial_used_at is not None
        assert sub.pro_activated_at is None  # 첫 실과금 전까지 null
        # 30일 트라이얼
        days = (sub.current_period_end - timezone.now()).days
        assert 29 <= days <= 30
        # 빌링키 암호화 저장 (평문이 그대로 저장되지 않음)
        assert sub._encrypted_toss_billing_key
        assert sub._encrypted_toss_billing_key != "bk_test_1"
        assert sub.toss_billing_key == "bk_test_1"  # 디스크립터 복호화
        assert sub.toss_billing_key_hash
        assert sub.card_company == "현대"

    def test_referral_code_extends_trial_to_60_days(self, user, toss, pro_plan):
        code = ReferralCode.objects.create(
            code=f"PARTNER{uuid.uuid4().hex[:6].upper()}",
            target_plan=pro_plan,
            trial_days=30,
        )
        result = confirm_billing(user, auth_key="ak1", plan_name="pro", referral_code=code.code)

        sub = result["subscription"]
        days = (sub.current_period_end - timezone.now()).days
        assert 59 <= days <= 60
        redemption = ReferralRedemption.objects.get(user=user)
        assert redemption.referral_code_id == code.pk
        code.refresh_from_db()
        assert code.current_uses == 1

    def test_referral_reuse_rejected(self, user, toss, pro_plan):
        code = ReferralCode.objects.create(
            code=f"ONCE{uuid.uuid4().hex[:6].upper()}", target_plan=pro_plan, trial_days=30
        )
        ReferralRedemption.objects.create(
            user=user,
            referral_code=code,
            trial_started_at=timezone.now(),
            trial_ends_at=timezone.now() + timedelta(days=30),
        )
        with pytest.raises(BillingFlowError) as e:
            confirm_billing(user, auth_key="ak1", plan_name="pro", referral_code=code.code)
        assert "이미" in e.value.detail

    def test_trial_is_once_per_user(self, user, toss, pro_plan):
        """트라이얼 소진 후 재구독은 즉시 과금 (등록→해지→재등록 무한 무료 루프 방어)."""
        sub = ensure_subscription(user)
        sub.trial_used_at = timezone.now() - timedelta(days=90)
        sub.save(update_fields=["trial_used_at"])

        result = confirm_billing(user, auth_key="ak1", plan_name="pro")

        assert result["scenario"] == "charge_now"
        assert len(toss.charges) == 1
        assert toss.charges[0]["amount"] == pro_plan.monthly_price
        assert result["subscription"].status == SubscriptionStatus.ACTIVE

    def test_no_card_referral_trial_attach_keeps_period(self, user, toss, pro_plan):
        """무카드 레퍼럴 트라이얼 중 카드 등록 — 기간 적층 금지."""
        sub = ensure_subscription(user)
        trial_end = timezone.now() + timedelta(days=11)
        sub.plan = pro_plan
        sub.status = SubscriptionStatus.TRIALING
        sub.current_period_end = trial_end
        sub.save()

        result = confirm_billing(user, auth_key="ak1", plan_name="pro")

        sub.refresh_from_db()
        assert result["scenario"] == "attach_only"
        assert sub.current_period_end == trial_end  # 불변
        assert sub.has_billing_key
        assert toss.charges == []


# ──────────────────────────────────────────────
# confirm — 즉시 과금 / 카드 변경 / 검증
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestConfirmChargeAndCardChange:
    def test_basic_confirm_charges_immediately(self, user, toss, basic_plan):
        result = confirm_billing(user, auth_key="ak1", plan_name="basic")

        sub = result["subscription"]
        assert result["scenario"] == "charge_now"
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.plan.name == "basic"
        assert sub.pro_activated_at is not None
        assert len(toss.charges) == 1
        assert toss.charges[0]["amount"] == basic_plan.monthly_price
        payment = result["payment"]
        assert payment.status == PaymentStatus.PAID
        assert payment.toss_payment_key
        assert payment.receipt_url

    def test_basic_charge_declined_keeps_billing_key(self, user, toss, free_plan):
        toss.charge_error = TossError("REJECT_CARD_PAYMENT", "한도 초과", 400)

        with pytest.raises(ChargeDeclinedError) as e:
            confirm_billing(user, auth_key="ak1", plan_name="basic")

        assert e.value.status_code == 402
        sub = ensure_subscription(user)
        sub.refresh_from_db()
        assert sub.plan.name == "free"  # 플랜 미전환
        assert sub.has_billing_key  # 빌링키는 유지 → 다른 카드/재시도 가능
        assert e.value.payment.status == PaymentStatus.FAILED
        assert e.value.payment.failure_code == "REJECT_CARD_PAYMENT"

    def test_ambiguous_network_error_leaves_pending(self, user, toss):
        toss.charge_error = TossNetworkError("timeout")

        with pytest.raises(toss_flows.ChargePendingError) as e:
            confirm_billing(user, auth_key="ak1", plan_name="basic")

        assert e.value.status_code == 202
        assert e.value.payment.status == PaymentStatus.PENDING  # reconcile 이 확정

    def test_card_change_keeps_plan_and_period(self, user, toss, pro_plan):
        sub = ensure_subscription(user)
        period_end = timezone.now() + timedelta(days=17)
        sub.plan = pro_plan
        sub.status = SubscriptionStatus.ACTIVE
        sub.current_period_end = period_end
        sub.monthly_amount_snapshot = 9900
        sub.set_billing_key("bk_old", card_company="국민", card_number="9409****")
        sub.save()

        result = confirm_billing(user, auth_key="ak2")  # plan_name 생략 = 카드 변경

        sub.refresh_from_db()
        assert result["scenario"] == "card_change"
        assert sub.plan.name == "pro"
        assert sub.current_period_end == period_end
        assert sub.monthly_amount_snapshot == 9900
        assert sub.toss_billing_key == "bk_test_1"  # 새 키
        assert "bk_old" in toss.deleted_keys  # 이전 키 정리
        assert toss.charges == []

    def test_customer_key_mismatch_rejected(self, user, monkeypatch):
        def bad_issue(auth_key, customer_key):
            return {"billingKey": "bk_x", "customerKey": "tf_someone_else"}

        monkeypatch.setattr(TossBillingClient, "issue_billing_key", bad_issue)
        with pytest.raises(BillingFlowError):
            confirm_billing(user, auth_key="ak1", plan_name="pro")

    def test_free_user_without_plan_name_rejected(self, user, toss):
        with pytest.raises(BillingFlowError) as e:
            confirm_billing(user, auth_key="ak1")
        assert "plan_name" in e.value.detail

    def test_already_paid_active_confirm_with_plan_rejected(self, user, toss, basic_plan):
        confirm_billing(user, auth_key="ak1", plan_name="basic")
        with pytest.raises(BillingFlowError) as e:
            confirm_billing(user, auth_key="ak2", plan_name="pro")
        assert "change-plan" in e.value.detail


# ──────────────────────────────────────────────
# change-plan (유료 ↔ 유료)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestChangePlan:
    def _paid_sub(self, user, plan, toss):
        confirm_billing(user, auth_key="ak1", plan_name=plan.name)
        return ensure_subscription(user)

    def test_upgrade_prorates_and_preserves_period(self, user, toss, basic_plan, pro_plan):
        self._paid_sub(user, basic_plan, toss)
        sub = ensure_subscription(user)
        period_end_before = sub.current_period_end
        toss.charges.clear()

        result = change_plan(user, "pro")

        sub = result["subscription"]
        assert sub.plan.name == "pro"
        assert sub.monthly_amount_snapshot == pro_plan.monthly_price
        # ★주기(앵커) 유지 — 리셋되지 않는다★
        assert sub.current_period_end == period_end_before
        # 남은 기간분 차액만 비례 청구 (신규 전액보다 작다) — payment.amount 와 일치
        assert len(toss.charges) == 1
        charged = toss.charges[0]["amount"]
        assert charged == result["payment"].amount
        assert 0 < charged < pro_plan.monthly_price
        assert "-up-pro-0-" in toss.charges[0]["order_id"]
        # 다음 정기 갱신은 프로 전액
        assert sub.renewal_amount == pro_plan.monthly_price

    def test_downgrade_reserves_pending_plan(self, user, toss, basic_plan, pro_plan):
        # pro 즉시 과금 상태 만들기 (트라이얼 우회)
        sub = ensure_subscription(user)
        sub.trial_used_at = timezone.now()
        sub.save(update_fields=["trial_used_at"])
        confirm_billing(user, auth_key="ak1", plan_name="pro")
        toss.charges.clear()

        result = change_plan(user, "basic")

        sub = result["subscription"]
        assert sub.plan.name == "pro"  # 기간말까지 유지
        assert sub.pending_plan.name == "basic"
        assert sub.pending_amount_snapshot == basic_plan.monthly_price
        assert result["effective_at"] == sub.current_period_end
        assert toss.charges == []  # 다운그레이드는 무과금

    def test_same_plan_with_pending_cancels_reservation(self, user, toss, basic_plan):
        sub = ensure_subscription(user)
        sub.trial_used_at = timezone.now()
        sub.save(update_fields=["trial_used_at"])
        confirm_billing(user, auth_key="ak1", plan_name="pro")
        change_plan(user, "basic")

        result = change_plan(user, "pro")  # 현재 플랜 재선택 = 예약 취소

        sub = result["subscription"]
        assert sub.pending_plan is None
        assert "취소" in result["detail"]

    def test_free_user_rejected(self, user, toss):
        with pytest.raises(BillingFlowError):
            change_plan(user, "pro")


# ──────────────────────────────────────────────
# 추가 IG 계정
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestExtraAccounts:
    def _pro_user(self, user, toss):
        """ACTIVE 프로 (트라이얼 우회 즉시 과금) — 추가계정 변경은 TRIALING 차단이므로."""
        sub = ensure_subscription(user)
        sub.trial_used_at = timezone.now()
        sub.save(update_fields=["trial_used_at"])
        confirm_billing(user, auth_key="ak1", plan_name="pro")  # charge_now → ACTIVE
        return ensure_subscription(user)

    def test_increase_prorates_immediately(self, user, toss):
        self._pro_user(user, toss)
        toss.charges.clear()
        result = change_extra_accounts(user, 2)

        sub = result["subscription"]
        assert sub.extra_ig_accounts == 2
        # 잔여일 일할 청구 (전액 이하) — payment.amount 와 일치
        assert len(toss.charges) == 1
        charged = toss.charges[0]["amount"]
        assert charged == result["payment"].amount
        assert 0 < charged <= 9900 * 2
        assert "-ex-0-2-" in toss.charges[0]["order_id"]
        # 다음 갱신은 계정 전체가 합산된 전액
        assert sub.renewal_amount == sub.monthly_amount_snapshot + 9900 * 2

    def test_trialing_blocks_extra_accounts(self, user, toss):
        confirm_billing(user, auth_key="ak1", plan_name="pro")  # TRIALING
        with pytest.raises(BillingFlowError) as e:
            change_extra_accounts(user, 1)
        assert "체험" in e.value.detail

    def test_increase_declined_does_not_change_count(self, user, toss):
        self._pro_user(user, toss)
        toss.charges.clear()
        toss.charge_error = TossError("REJECT_CARD_PAYMENT", "한도 초과", 400)

        with pytest.raises(ChargeDeclinedError):
            change_extra_accounts(user, 1)

        sub = ensure_subscription(user)
        sub.refresh_from_db()
        assert sub.extra_ig_accounts == 0

    def test_decrease_defers_to_next_renewal(self, user, toss):
        """축소는 즉시 반영/거부하지 않고 다음 갱신으로 예약만 한다(초과 연동이어도)."""
        from apps.integrations.models import IGAccountConnection
        from apps.workspace.models import Workspace

        sub = self._pro_user(user, toss)
        sub.extra_ig_accounts = 2
        sub.save(update_fields=["extra_ig_accounts"])
        toss.charges.clear()

        # 허용량(3)을 초과해 연동돼 있어도 거부하지 않는다
        ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=user)
        for i in range(3):
            IGAccountConnection.objects.create(
                workspace=ws,
                external_account_id=f"ig_{uuid.uuid4().hex[:8]}",
                username=f"u{i}",
                account_type="BUSINESS",
                status=IGAccountConnection.Status.ACTIVE,
            )

        result = change_extra_accounts(user, 0)

        sub = result["subscription"]
        assert result["payment"] is None
        assert toss.charges == []
        assert sub.extra_ig_accounts == 2  # 이번 주기는 그대로
        assert sub.pending_extra_ig_accounts == 0  # 다음 갱신에 0으로 예약
        assert result["effective_at"] == sub.current_period_end
        assert sub.renewal_amount == sub.monthly_amount_snapshot  # 축소 반영된 갱신액

    def test_decrease_reservation_can_be_cancelled(self, user, toss):
        sub = self._pro_user(user, toss)
        sub.extra_ig_accounts = 2
        sub.save(update_fields=["extra_ig_accounts"])
        change_extra_accounts(user, 1)  # 축소 예약 (2 → 1)
        assert ensure_subscription(user).pending_extra_ig_accounts == 1

        result = change_extra_accounts(user, 2)  # 현재값 재요청 = 예약 취소

        assert result["subscription"].pending_extra_ig_accounts is None
        assert "취소" in result["detail"]

    def test_increase_clears_pending_decrease(self, user, toss):
        sub = self._pro_user(user, toss)
        sub.extra_ig_accounts = 2
        sub.save(update_fields=["extra_ig_accounts"])
        change_extra_accounts(user, 0)  # 축소 예약 (pending=0)
        assert ensure_subscription(user).pending_extra_ig_accounts == 0
        toss.charges.clear()

        result = change_extra_accounts(user, 3)  # 증가 → 즉시 반영 + 예약 해제

        sub = result["subscription"]
        assert sub.extra_ig_accounts == 3
        assert sub.pending_extra_ig_accounts is None
        assert len(toss.charges) == 1

    def test_same_count_no_pending_rejected(self, user, toss):
        self._pro_user(user, toss)
        with pytest.raises(BillingFlowError) as e:
            change_extra_accounts(user, 0)  # 현재 0, 예약 없음 → 동일
        assert "동일" in e.value.detail

    def test_basic_user_rejected(self, user, toss):
        confirm_billing(user, auth_key="ak1", plan_name="basic")
        with pytest.raises(BillingFlowError):
            change_extra_accounts(user, 1)


# ──────────────────────────────────────────────
# 환불
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestApplyRefund:
    def test_refund_downgrades_and_is_idempotent(self, user, toss, basic_plan):
        result = confirm_billing(user, auth_key="ak1", plan_name="basic")
        payment = result["payment"]

        assert apply_refund(payment, downgrade=True, reason="test") is True
        payment.refresh_from_db()
        sub = ensure_subscription(user)
        sub.refresh_from_db()
        assert payment.status == PaymentStatus.REFUNDED
        assert sub.plan.name == "free"
        assert not sub.has_billing_key

        # 멱등 — 두 번째 호출은 no-op
        assert apply_refund(payment, downgrade=True) is False

    def test_downgrade_preserves_custom_css_and_trial_flag(self, user, toss):
        """다운그레이드가 custom_css(free 허용)와 trial_used_at(어뷰징 방어)을 보존."""
        from apps.pages.models import Page

        result = confirm_billing(user, auth_key="ak1", plan_name="basic")
        page = Page.objects.create(
            user=user, slug=f"p-{uuid.uuid4().hex[:8]}", title="t", custom_css=".a{color:red}"
        )
        sub = ensure_subscription(user)
        UserSubscription.objects.filter(pk=sub.pk).update(trial_used_at=timezone.now())

        apply_refund(result["payment"], downgrade=True)

        page.refresh_from_db()
        sub.refresh_from_db()
        assert page.custom_css == ".a{color:red}"  # CSS 는 지우지 않는다
        assert sub.trial_used_at is not None  # 트라이얼 사용 이력 유지


# ──────────────────────────────────────────────
# 비례배분 (proration) 계산 헬퍼 — 순수 함수, now 주입으로 정확값 단언
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestProrationHelpers:
    def _sub_with_period(self, user, base, days_total=30):
        sub = ensure_subscription(user)
        sub.current_period_start = base
        sub.current_period_end = base + timedelta(days=days_total)
        return sub

    def test_ratio_half_and_clamps(self, user):
        base = timezone.now()
        sub = self._sub_with_period(user, base)
        # 잔여 15/30 = 정확히 0.5
        assert toss_flows.proration_ratio(sub, now=base + timedelta(days=15)) == 0.5
        # 이미 지남 → 0.0
        assert toss_flows.proration_ratio(sub, now=base + timedelta(days=40)) == 0.0
        # 미래 과다 → 1.0 상한
        assert toss_flows.proration_ratio(sub, now=base - timedelta(days=5)) == 1.0
        # current_period_end None(free) → 0.0
        sub.current_period_end = None
        assert toss_flows.proration_ratio(sub, now=base) == 0.0

    def test_extra_accounts_charge_prorated(self, user):
        base = timezone.now()
        sub = self._sub_with_period(user, base)
        # 잔여 0.5, delta=2 → floor(9900*2*0.5) = 9900
        amt = toss_flows.compute_extra_accounts_charge(sub, 2, now=base + timedelta(days=15))
        assert amt == 9900

    def test_upgrade_charge_is_new_minus_credit(self, user, basic_plan, pro_plan):
        base = timezone.now()
        sub = self._sub_with_period(user, base)
        sub.plan = basic_plan
        sub.monthly_amount_snapshot = basic_plan.monthly_price
        sub.extra_ig_accounts = 0
        now = base + timedelta(days=15)  # 잔여 0.5
        amt = toss_flows.compute_upgrade_charge(sub, pro_plan, 0, now=now)
        expected = max(
            0,
            math.floor(pro_plan.monthly_price * 0.5) - math.ceil(basic_plan.monthly_price * 0.5),
        )
        assert amt == expected
        # 신규 전액보다 작아야 한다 (크레딧 차감)
        assert amt < pro_plan.monthly_price

    def test_zero_remaining_gives_zero_charge(self, user, pro_plan):
        base = timezone.now()
        sub = self._sub_with_period(user, base)
        # 주기 말 (잔여 0) → 0원
        assert toss_flows.compute_extra_accounts_charge(sub, 3, now=base + timedelta(days=30)) == 0


# ──────────────────────────────────────────────
# 비례배분 하드닝 — 이중과금 방어(결정적 orderId) · reconcile 자동 반영
# ──────────────────────────────────────────────


def _make_active_pro(user, toss):
    sub = ensure_subscription(user)
    sub.trial_used_at = timezone.now()
    sub.save(update_fields=["trial_used_at"])
    confirm_billing(user, auth_key="ak1", plan_name="pro")  # charge_now → ACTIVE
    toss.charges.clear()
    return ensure_subscription(user)


def _done_payload(amount):
    return {
        "status": "DONE",
        "paymentKey": f"pk_{uuid.uuid4().hex[:12]}",
        "receipt": {"url": "https://receipt.example/x"},
        "card": {"company": "현대", "number": "433012******123*"},
        "totalAmount": amount,
    }


@pytest.mark.django_db
class TestProrationHardening:
    def test_deterministic_order_dedupes_paid(self, user, toss):
        sub = _make_active_pro(user, toss)
        order_id = toss_flows.proration_extra_order_id(sub, 1)

        p1 = toss_flows.charge_prorated(sub, 5000, "테스트 비례", order_id)
        n = len(toss.charges)
        # 같은 주문 재요청(더블클릭) → 재청구 없이 기존 PAID 반환
        p2 = toss_flows.charge_prorated(sub, 5000, "테스트 비례", order_id)

        assert p2.pk == p1.pk
        assert p2.status == PaymentStatus.PAID
        assert len(toss.charges) == n  # 두 번째는 토스 재승인 안 함

    def test_pending_order_blocks_duplicate(self, user, toss):
        sub = _make_active_pro(user, toss)
        order_id = toss_flows.proration_extra_order_id(sub, 1)

        toss.charge_error = TossNetworkError("timeout")  # 모호 → PENDING
        with pytest.raises(toss_flows.ChargePendingError):
            toss_flows.charge_prorated(sub, 5000, "테스트", order_id)

        toss.charge_error = None  # 네트워크 복구돼도
        with pytest.raises(toss_flows.ChargePendingError):
            toss_flows.charge_prorated(sub, 5000, "테스트", order_id)  # PENDING이라 재승인 금지
        assert toss.charges == []

    def test_inflight_pending_blocks_different_target(self, user, toss):
        """이전 비례 청구가 미확정(PENDING)이면 다른 목표로의 청구도 차단 — 이중 청구 방지."""
        sub = _make_active_pro(user, toss)

        toss.charge_error = TossNetworkError("timeout")  # 첫 요청 모호 → PENDING(ex-0-1)
        with pytest.raises(toss_flows.ChargePendingError):
            toss_flows.charge_prorated(sub, 5000, "t1", toss_flows.proration_extra_order_id(sub, 1))

        toss.charge_error = None  # 네트워크 정상이어도
        with pytest.raises(toss_flows.ChargePendingError):
            # 다른 목표(ex-0-3) — 이전 미확정 때문에 차단
            toss_flows.charge_prorated(
                sub, 15000, "t2", toss_flows.proration_extra_order_id(sub, 3)
            )
        assert toss.charges == []  # 두 번째는 승인 시도조차 안 함

    def test_reconcile_finalizes_pending_upgrade(self, user, toss, basic_plan, pro_plan):
        confirm_billing(user, auth_key="ak1", plan_name="basic")  # ACTIVE basic
        toss.charges.clear()
        sub = ensure_subscription(user)
        order_id = toss_flows.proration_upgrade_order_id(sub, pro_plan, 0)
        payment = PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=3000,
            status=PaymentStatus.PENDING,
            description="업그레이드 비례",
            toss_order_id=order_id,
        )

        tasks._reconcile_confirm_paid(payment, _done_payload(3000))

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == PaymentStatus.PAID
        assert sub.plan.name == "pro"  # 구독 상태 자동 반영
        assert sub.monthly_amount_snapshot == pro_plan.monthly_price

    def test_reconcile_finalizes_pending_extra(self, user, toss):
        sub = _make_active_pro(user, toss)
        order_id = toss_flows.proration_extra_order_id(sub, 3)  # 0 → 3
        payment = PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=9000,
            status=PaymentStatus.PENDING,
            description="추가계정 비례",
            toss_order_id=order_id,
        )

        tasks._reconcile_confirm_paid(payment, _done_payload(9000))

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == PaymentStatus.PAID
        assert sub.extra_ig_accounts == 3


# ──────────────────────────────────────────────
# 견적(preview) — 부작용 없음 · 실행과 동일 계산 · HTTP 배선
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestProrationPreview:
    def _controlled_period(self, user, base):
        sub = ensure_subscription(user)
        UserSubscription.objects.filter(pk=sub.pk).update(
            current_period_start=base, current_period_end=base + timedelta(days=30)
        )
        return ensure_subscription(user)

    def test_preview_upgrade_matches_helper_and_no_side_effects(
        self, user, toss, basic_plan, pro_plan
    ):
        confirm_billing(user, auth_key="ak1", plan_name="basic")  # ACTIVE basic
        toss.charges.clear()
        base = timezone.now()
        self._controlled_period(user, base)
        now = base + timedelta(days=15)  # ratio 0.5
        before = PaymentHistory.objects.filter(user=user).count()

        q = toss_flows.preview_change_plan(user, "pro", now=now)

        expected = toss_flows._upgrade_charge_breakdown(
            ensure_subscription(user), pro_plan, 0, now
        )[2]
        assert q["direction"] == "upgrade"
        assert q["immediate_charge"]["amount"] == expected
        assert q["immediate_charge"]["proration"]["net"] == expected
        assert q["effective_at"] is None
        assert q["next_renewal_amount"] == pro_plan.monthly_price
        # 부작용 없음: 플랜 불변, 결제행 미생성, 토스 미호출
        s = ensure_subscription(user)
        assert s.plan.name == "basic"
        assert PaymentHistory.objects.filter(user=user).count() == before
        assert toss.charges == []

    def test_preview_downgrade_is_zero_and_scheduled(self, user, toss, basic_plan):
        sub = _make_active_pro(user, toss)
        q = toss_flows.preview_change_plan(user, "basic")
        assert q["direction"] == "downgrade"
        assert q["immediate_charge"]["amount"] == 0
        assert q["effective_at"] == sub.current_period_end
        assert q["next_renewal_amount"] == basic_plan.monthly_price

    def test_preview_extra_increase_matches_helper(self, user, toss):
        _make_active_pro(user, toss)
        base = timezone.now()
        self._controlled_period(user, base)
        now = base + timedelta(days=15)  # ratio 0.5

        q = toss_flows.preview_change_extra_accounts(user, 2, now=now)

        # 견적 == 실행 계산(동일 헬퍼·동일 now·동일 sub) — DB 정밀도 무관하게 일치해야 함
        expected = toss_flows.compute_extra_accounts_charge(ensure_subscription(user), 2, now=now)
        assert q["direction"] == "increase"
        assert q["delta"] == 2
        assert q["immediate_charge"]["amount"] == expected
        assert 9800 <= expected <= 9900  # ~15/30 잔여 → 절반 근사
        assert q["unit_price"] == 9900
        assert q["next_renewal_amount"] == ensure_subscription(user).renewal_amount + 9900 * 2

    def test_preview_free_user_rejected(self, user, toss):
        with pytest.raises(BillingFlowError):
            toss_flows.preview_change_plan(user, "pro")

    def test_preview_endpoints_http_wiring(self, user, toss):
        _make_active_pro(user, toss)
        client = APIClient()
        client.force_authenticate(user=user)

        r1 = client.post(reverse("billing:extra-accounts-preview"), {"count": 2}, format="json")
        assert r1.status_code == 200
        assert r1.data["direction"] == "increase"
        assert r1.data["unit_price"] == 9900

        r2 = client.post(
            reverse("billing:change-plan-preview"), {"plan_name": "basic"}, format="json"
        )
        assert r2.status_code == 200
        assert r2.data["direction"] == "downgrade"

        # 견적은 부작용 없음 — PENDING 결제 미생성
        assert PaymentHistory.objects.filter(user=user, status=PaymentStatus.PENDING).count() == 0
