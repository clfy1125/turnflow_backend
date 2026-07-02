"""토스 빌링 플로우 테스트 — confirm(트라이얼/즉시과금/카드변경) · 플랜변경 · 추가계정 · 환불.

토스 API 는 TossBillingClient 클래스 메서드를 monkeypatch 로 대체 (실 네트워크 없음).
더러운 테스트 DB 대응: 이메일/키는 uuid 로 유일화, 집계는 델타 단언.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing import toss_flows
from apps.billing.models import (
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

    def test_upgrade_charges_and_resets_period(self, user, toss, basic_plan, pro_plan):
        self._paid_sub(user, basic_plan, toss)
        toss.charges.clear()

        result = change_plan(user, "pro")

        sub = result["subscription"]
        assert sub.plan.name == "pro"
        assert sub.monthly_amount_snapshot == pro_plan.monthly_price
        assert len(toss.charges) == 1
        assert toss.charges[0]["amount"] == pro_plan.monthly_price
        days = (sub.current_period_end - timezone.now()).days
        assert 29 <= days <= 30

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
        confirm_billing(user, auth_key="ak1", plan_name="pro")  # 트라이얼로 pro 활성
        return ensure_subscription(user)

    def test_increase_charges_immediately(self, user, toss):
        self._pro_user(user, toss)
        result = change_extra_accounts(user, 2)

        sub = result["subscription"]
        assert sub.extra_ig_accounts == 2
        assert len(toss.charges) == 1
        assert toss.charges[0]["amount"] == 9900 * 2  # 증가분 즉시 결제
        assert "-extra-" in toss.charges[0]["order_id"]
        assert sub.renewal_amount == sub.monthly_amount_snapshot + 9900 * 2

    def test_increase_declined_does_not_change_count(self, user, toss):
        self._pro_user(user, toss)
        toss.charge_error = TossError("REJECT_CARD_PAYMENT", "한도 초과", 400)

        with pytest.raises(ChargeDeclinedError):
            change_extra_accounts(user, 1)

        sub = ensure_subscription(user)
        sub.refresh_from_db()
        assert sub.extra_ig_accounts == 0

    def test_decrease_requires_disconnecting_first(self, user, toss):
        from apps.integrations.models import IGAccountConnection
        from apps.workspace.models import Workspace

        sub = self._pro_user(user, toss)
        sub.extra_ig_accounts = 2
        sub.save(update_fields=["extra_ig_accounts"])

        ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=user)
        for i in range(3):  # 1(기본)+2(추가) = 3개 연동 중
            IGAccountConnection.objects.create(
                workspace=ws,
                external_account_id=f"ig_{uuid.uuid4().hex[:8]}",
                username=f"u{i}",
                account_type="BUSINESS",
                status=IGAccountConnection.Status.ACTIVE,
            )

        with pytest.raises(BillingFlowError) as e:
            change_extra_accounts(user, 0)  # 허용 1개인데 3개 연동 중
        assert "해제" in e.value.detail

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
