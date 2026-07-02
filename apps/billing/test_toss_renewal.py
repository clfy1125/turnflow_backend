"""토스 갱신 파이프라인 테스트 — 과금/중복방지/dunning/reconcile/웹훅 처리."""

import hashlib
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.models import (
    PaymentHistory,
    PaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
    TossWebhookLog,
    UserSubscription,
)
from apps.billing.subscription_utils import ensure_subscription
from apps.billing.tasks import (
    charge_subscription_renewal,
    handle_cancelled_expiry,
    handle_grace_period_expiry,
    handle_trial_expiry,
    process_toss_webhook,
    reconcile_pending_payments,
)
from apps.billing.toss_service import TossBillingClient, TossError

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"renew-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _paid_sub(
    user,
    plan_name="pro",
    status=SubscriptionStatus.ACTIVE,
    period_end_delta=timedelta(hours=-1),
    snapshot=9900,
    extra=0,
    billing_key="bk_renew_1",
):
    plan = SubscriptionPlan.objects.get(name=plan_name)
    sub = ensure_subscription(user)
    sub.plan = plan
    sub.status = status
    sub.current_period_start = timezone.now() - timedelta(days=30)
    sub.current_period_end = timezone.now() + period_end_delta
    sub.monthly_amount_snapshot = snapshot
    sub.extra_ig_accounts = extra
    if billing_key:
        sub.set_billing_key(billing_key, card_company="현대", card_number="4330****")
    sub.save()
    return sub


class ChargeMock:
    def __init__(self, monkeypatch, error=None):
        self.calls = []
        self.error = error

        def fake_charge(**kwargs):
            self.calls.append(kwargs)
            if self.error is not None:
                raise self.error
            return {
                "paymentKey": f"pk_{uuid.uuid4().hex[:12]}",
                "orderId": kwargs["order_id"],
                "status": "DONE",
                "receipt": {"url": "https://receipt.example/r"},
                "card": {"company": "현대", "number": "4330****"},
            }

        monkeypatch.setattr(TossBillingClient, "charge", fake_charge)
        monkeypatch.setattr(
            TossBillingClient, "delete_billing_key", lambda billing_key, customer_key: {}
        )


# ──────────────────────────────────────────────
# 갱신 성공 / 금액 계산
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestRenewalCharge:
    def test_renewal_charges_snapshot_plus_extras_and_extends(self, user, monkeypatch):
        sub = _paid_sub(user, snapshot=9900, extra=2)
        old_end = sub.current_period_end
        mock = ChargeMock(monkeypatch)

        result = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert result["result"] == "paid"
        assert mock.calls[0]["amount"] == 9900 + 9900 * 2  # 스냅샷 + 추가계정
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        # 만료 직후 갱신 → 만료일부터 이어붙음 (사용자 손해 없음)
        assert sub.current_period_start == old_end
        assert sub.current_period_end == old_end + timedelta(days=30)
        assert sub.renewal_attempts == 0
        payment = PaymentHistory.objects.get(subscription=sub, status=PaymentStatus.PAID)
        assert payment.amount == 29700

    def test_trialing_first_charge_converts(self, user, monkeypatch):
        from apps.billing.models import ReferralCode, ReferralRedemption

        sub = _paid_sub(user, status=SubscriptionStatus.TRIALING, snapshot=9900)
        code = ReferralCode.objects.create(
            code=f"T{uuid.uuid4().hex[:8].upper()}",
            target_plan=sub.plan,
            trial_days=30,
        )
        redemption = ReferralRedemption.objects.create(
            user=user,
            referral_code=code,
            trial_started_at=timezone.now() - timedelta(days=30),
            trial_ends_at=sub.current_period_end,
        )
        ChargeMock(monkeypatch)

        result = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert result["result"] == "paid"
        sub.refresh_from_db()
        redemption.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.pro_activated_at is not None  # 첫 실과금 시점 기록
        assert redemption.converted_to_paid is True

    def test_pending_downgrade_applied_on_renewal(self, user, monkeypatch):
        basic = SubscriptionPlan.objects.get(name="basic")
        sub = _paid_sub(user, plan_name="pro", snapshot=9900, extra=1)
        sub.pending_plan = basic
        sub.pending_amount_snapshot = basic.monthly_price
        sub.save(update_fields=["pending_plan", "pending_amount_snapshot"])
        mock = ChargeMock(monkeypatch)

        charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        # basic 은 추가계정 미지원 → 청구액에 extras 미포함, 적용 시 슬롯 0으로
        assert mock.calls[0]["amount"] == basic.monthly_price
        sub.refresh_from_db()
        assert sub.plan.name == "basic"
        assert sub.monthly_amount_snapshot == basic.monthly_price
        assert sub.pending_plan is None
        assert sub.extra_ig_accounts == 0

    def test_cancelled_never_charged(self, user, monkeypatch):
        sub = _paid_sub(user, status=SubscriptionStatus.CANCELLED)
        mock = ChargeMock(monkeypatch)

        result = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert result["result"] == "not_due"
        assert mock.calls == []

    def test_not_yet_due_skipped(self, user, monkeypatch):
        sub = _paid_sub(user, period_end_delta=timedelta(days=10))
        mock = ChargeMock(monkeypatch)

        result = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert result["result"] == "not_due"
        assert mock.calls == []

    def test_double_run_charges_exactly_once(self, user, monkeypatch):
        """beat 중복 디스패치/재실행 — 결정적 orderId 소유권으로 과금 1회 보장."""
        sub = _paid_sub(user)
        mock = ChargeMock(monkeypatch)

        r1 = charge_subscription_renewal.apply(args=[str(sub.id)]).get()
        r2 = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert r1["result"] == "paid"
        assert r2["result"] in ("already_paid", "not_due")  # 기간 연장돼 not_due
        assert len(mock.calls) == 1


# ──────────────────────────────────────────────
# Dunning (거절 재시도)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestDunning:
    def test_decline_schedules_d1_retry(self, user, monkeypatch):
        sub = _paid_sub(user)
        old_end = sub.current_period_end
        ChargeMock(monkeypatch, error=TossError("REJECT_CARD_PAYMENT", "잔액 부족", 400))

        result = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert result["result"] == "declined"
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.PAST_DUE
        assert sub.renewal_attempts == 1
        assert sub.next_billing_retry_at is not None
        assert "REJECT_CARD_PAYMENT" in sub.last_billing_error
        failed = PaymentHistory.objects.get(subscription=sub, status=PaymentStatus.FAILED)
        assert failed.toss_order_id.endswith("-a0")
        # D+1 (기간 만료가 이미 지났으면 now+6h 폴백)
        assert sub.next_billing_retry_at >= min(
            old_end + timedelta(days=1), timezone.now() + timedelta(hours=5)
        )

    def test_second_decline_uses_new_order_and_d3(self, user, monkeypatch):
        sub = _paid_sub(user, period_end_delta=timedelta(days=-1))
        ChargeMock(monkeypatch, error=TossError("REJECT", "x", 400))
        charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        # 재시도 시각 도래로 조작
        UserSubscription.objects.filter(pk=sub.pk).update(
            next_billing_retry_at=timezone.now() - timedelta(minutes=1)
        )
        result = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert result["result"] == "declined"
        sub.refresh_from_db()
        assert sub.renewal_attempts == 2
        orders = set(
            PaymentHistory.objects.filter(subscription=sub).values_list("toss_order_id", flat=True)
        )
        assert any(o.endswith("-a0") for o in orders)
        assert any(o.endswith("-a1") for o in orders)  # 거절 주문 재사용 금지

    def test_retry_success_recovers_to_active(self, user, monkeypatch):
        sub = _paid_sub(user)
        mock = ChargeMock(monkeypatch, error=TossError("REJECT", "x", 400))
        charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        mock.error = None  # 카드사 사정 회복
        UserSubscription.objects.filter(pk=sub.pk).update(
            next_billing_retry_at=timezone.now() - timedelta(minutes=1)
        )
        result = charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert result["result"] == "paid"
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.renewal_attempts == 0
        assert sub.last_billing_error == ""


# ──────────────────────────────────────────────
# 만료 배치 (grace / trial / cancelled)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestExpiryBatches:
    def test_grace_expiry_downgrades_after_7_days(self, user, monkeypatch):
        sub = _paid_sub(
            user,
            status=SubscriptionStatus.PAST_DUE,
            period_end_delta=timedelta(days=-8),
        )
        ChargeMock(monkeypatch)

        handle_grace_period_expiry.apply().get()

        sub.refresh_from_db()
        assert sub.plan.name == "free"
        assert not sub.has_billing_key

    def test_trial_expiry_skips_card_trials(self, user, monkeypatch):
        """빌링키 보유 트라이얼은 과금 대상 — trial_expiry 가 다운그레이드하면 안 됨."""
        sub = _paid_sub(
            user,
            status=SubscriptionStatus.TRIALING,
            period_end_delta=timedelta(hours=-2),
            billing_key="bk_trial",
        )

        handle_trial_expiry.apply().get()

        sub.refresh_from_db()
        assert sub.plan.name == "pro"  # 건드리지 않음 (갱신 태스크가 과금)
        assert sub.status == SubscriptionStatus.TRIALING

    def test_trial_expiry_downgrades_cardless_trials(self, user):
        sub = _paid_sub(
            user,
            status=SubscriptionStatus.TRIALING,
            period_end_delta=timedelta(hours=-2),
            billing_key=None,
        )

        handle_trial_expiry.apply().get()

        sub.refresh_from_db()
        assert sub.plan.name == "free"

    def test_cancelled_expiry_includes_basic_excludes_admin(self, user, monkeypatch):
        sub = _paid_sub(
            user,
            plan_name="basic",
            status=SubscriptionStatus.CANCELLED,
            period_end_delta=timedelta(hours=-2),
        )
        ChargeMock(monkeypatch)

        handle_cancelled_expiry.apply().get()

        sub.refresh_from_db()
        assert sub.plan.name == "free"

        # admin 플랜(0원)은 대상 아님
        admin_user = User.objects.create_user(
            email=f"adm-{uuid.uuid4().hex[:8]}@example.com", password="Pass1234!"
        )
        admin_sub = _paid_sub(
            admin_user,
            plan_name="admin",
            status=SubscriptionStatus.CANCELLED,
            period_end_delta=timedelta(hours=-2),
        )
        handle_cancelled_expiry.apply().get()
        admin_sub.refresh_from_db()
        assert admin_sub.plan.name == "admin"


# ──────────────────────────────────────────────
# Reconcile (모호 결제 확정)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestReconcile:
    def _stale_pending(self, sub, order_suffix="-a0"):
        order_id = f"tfsub-{sub.id.hex[:10]}-{sub.current_period_end:%Y%m%d}{order_suffix}"
        payment = PaymentHistory.objects.create(
            user=sub.user,
            subscription=sub,
            amount=9900,
            status=PaymentStatus.PENDING,
            description="갱신",
            toss_order_id=order_id,
            toss_idempotency_key=str(uuid.uuid4()),
        )
        PaymentHistory.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        payment.refresh_from_db()
        return payment

    def test_reconcile_confirms_done_renewal(self, user, monkeypatch):
        sub = _paid_sub(user)
        payment = self._stale_pending(sub)
        old_end = sub.current_period_end

        monkeypatch.setattr(
            TossBillingClient,
            "get_payment_by_order_id",
            lambda order_id: {
                "paymentKey": "pk_reconciled",
                "orderId": order_id,
                "status": "DONE",
                "receipt": {"url": "https://r"},
            },
        )
        reconcile_pending_payments.apply().get()

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == PaymentStatus.PAID
        assert payment.toss_payment_key == "pk_reconciled"
        assert sub.current_period_end == old_end + timedelta(days=30)  # 갱신 확정

    def test_reconcile_404_registers_dunning(self, user, monkeypatch):
        sub = _paid_sub(user)
        payment = self._stale_pending(sub)

        def raise_404(order_id):
            raise TossError("NOT_FOUND", "존재하지 않는 결제", 404)

        monkeypatch.setattr(TossBillingClient, "get_payment_by_order_id", raise_404)
        reconcile_pending_payments.apply().get()

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == PaymentStatus.FAILED
        assert sub.status == SubscriptionStatus.PAST_DUE
        assert sub.renewal_attempts == 1  # dunning 진입


# ──────────────────────────────────────────────
# 웹훅 (수신 뷰 + 처리 태스크)
# ──────────────────────────────────────────────

WEBHOOK_URL = "/api/v1/billing/toss/webhook/"


@pytest.mark.django_db
class TestWebhook:
    def test_view_dedups_and_hashes_billing_key(self, client, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "apps.billing.tasks.process_toss_webhook.delay", lambda log_id: sent.append(log_id)
        )
        raw_key = f"bk_hook_{uuid.uuid4().hex[:8]}"
        body = {
            "eventType": "BILLING_DELETED",
            "createdAt": "2026-07-02T00:00:00.000000",
            "data": {"billingKey": raw_key},
        }

        before = TossWebhookLog.objects.count()
        r1 = client.post(WEBHOOK_URL, body, content_type="application/json")
        r2 = client.post(WEBHOOK_URL, body, content_type="application/json")

        assert r1.status_code == 200 and r2.status_code == 200
        assert TossWebhookLog.objects.count() == before + 1  # dedup
        assert len(sent) == 1
        log = TossWebhookLog.objects.order_by("-created_at").first()
        assert raw_key not in str(log.raw_data)  # 평문 저장 금지
        assert log.raw_data["data"]["billingKey"].startswith("sha256:")

    def test_billing_deleted_clears_key_and_cancels(self, user, client, monkeypatch):
        sub = _paid_sub(user, billing_key="bk_gone")
        monkeypatch.setattr("apps.billing.tasks.process_toss_webhook.delay", lambda log_id: None)
        body = {
            "eventType": "BILLING_DELETED",
            "createdAt": "2026-07-02T00:00:00.000000",
            "data": {"billingKey": "bk_gone"},
        }
        client.post(WEBHOOK_URL, body, content_type="application/json")
        log = TossWebhookLog.objects.get(
            dedup_key=f"billdel:{hashlib.sha256(b'bk_gone').hexdigest()[:32]}"
        )

        process_toss_webhook.apply(args=[str(log.id)]).get()

        sub.refresh_from_db()
        assert not sub.has_billing_key
        assert sub.status == SubscriptionStatus.CANCELLED  # 기간말까지 이용, 갱신 중단
        log.refresh_from_db()
        assert log.processed is True

    def test_cancel_webhook_applies_refund_after_requery(self, user, monkeypatch):
        sub = _paid_sub(user)
        payment = PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=9900,
            status=PaymentStatus.PAID,
            description="구독",
            toss_order_id=f"tfsub-{sub.id.hex[:10]}-init-{uuid.uuid4().hex[:8]}",
            toss_payment_key=f"pk_{uuid.uuid4().hex[:10]}",
            paid_at=timezone.now(),
        )
        monkeypatch.setattr(
            TossBillingClient,
            "get_payment",
            lambda payment_key: {
                "paymentKey": payment.toss_payment_key,
                "orderId": payment.toss_order_id,
                "status": "CANCELED",
            },
        )
        monkeypatch.setattr(
            TossBillingClient, "delete_billing_key", lambda billing_key, customer_key: {}
        )
        log = TossWebhookLog.objects.create(
            event_type="CANCEL_STATUS_CHANGED",
            dedup_key=f"cancel:{payment.toss_payment_key}:DONE:{uuid.uuid4().hex[:6]}",
            payment_key=payment.toss_payment_key,
            raw_data={},
        )

        process_toss_webhook.apply(args=[str(log.id)]).get()

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == PaymentStatus.REFUNDED
        assert sub.plan.name == "free"  # 대시보드 취소여도 다운그레이드 수렴

    def test_forged_webhook_ignored_by_requery(self, user, monkeypatch):
        """재조회가 실패하는 위조 본문은 아무것도 바꾸지 못한다."""
        sub = _paid_sub(user)
        payment = PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=9900,
            status=PaymentStatus.PAID,
            description="구독",
            toss_order_id=f"tfsub-{sub.id.hex[:10]}-init-{uuid.uuid4().hex[:8]}",
            toss_payment_key=f"pk_{uuid.uuid4().hex[:10]}",
        )

        def not_found(payment_key):
            raise TossError("NOT_FOUND_PAYMENT", "없는 결제", 404)

        monkeypatch.setattr(TossBillingClient, "get_payment", not_found)
        log = TossWebhookLog.objects.create(
            event_type="CANCEL_STATUS_CHANGED",
            dedup_key=f"forged:{uuid.uuid4().hex}",
            payment_key=payment.toss_payment_key,
            raw_data={},
        )

        process_toss_webhook.apply(args=[str(log.id)]).get()

        payment.refresh_from_db()
        assert payment.status == PaymentStatus.PAID  # 불변
        log.refresh_from_db()
        assert log.processed is True
