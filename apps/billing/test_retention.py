"""리텐션(해지 방어) 기능 테스트 — 일시정지(Pause) / 다음 1회 할인 / 윈백 / 오퍼 트래킹."""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.analytics.models import CancellationEvent
from apps.billing.models import PaymentHistory, PaymentStatus, SubscriptionPlan, SubscriptionStatus
from apps.billing.subscription_utils import ensure_subscription, get_effective_plan
from apps.billing.tasks import (
    charge_subscription_renewal,
    handle_pause_expiry,
    notify_pause_resume_reminder,
    send_winback_emails,
)
from apps.billing.toss_service import TossBillingClient

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"ret-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


@pytest.fixture
def client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _paid_sub(
    user,
    plan_name="pro",
    status=SubscriptionStatus.ACTIVE,
    period_end_delta=timedelta(days=10),
    snapshot=9900,
    billing_key="bk_ret_1",
):
    plan = SubscriptionPlan.objects.get(name=plan_name)
    sub = ensure_subscription(user)
    sub.plan = plan
    sub.status = status
    sub.current_period_start = timezone.now() - timedelta(days=20)
    sub.current_period_end = timezone.now() + period_end_delta
    sub.monthly_amount_snapshot = snapshot
    sub.last_pause_at = None
    sub.retention_discount_pending = False
    sub.retention_discount_used_at = None
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
# ① 일시정지 (Pause)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestPause:
    def test_pause_sets_status_and_fields(self, client, user):
        sub = _paid_sub(user, period_end_delta=timedelta(days=10))
        res = client.post("/api/v1/billing/pause/", {"months": 2}, format="json")
        assert res.status_code == 200, res.data
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.PAUSED
        assert sub.paused_months == 2
        expected = sub.current_period_end + timedelta(days=60)
        assert abs((sub.pause_ends_at - expected).total_seconds()) < 5
        assert sub.last_pause_at is not None
        assert res.data["status"] == "paused"
        assert res.data["can_pause"] is False

    def test_gating_pro_within_period_free_after(self, user):
        sub = _paid_sub(user, period_end_delta=timedelta(days=5))
        sub.status = SubscriptionStatus.PAUSED
        sub.paused_months = 1
        sub.pause_ends_at = sub.current_period_end + timedelta(days=30)
        sub.save()
        # 잔여 유료기간 내 → 프로 유지
        assert get_effective_plan(user).name == "pro"
        # 정지 개시(기간 경과) 후 → 무료 수준
        sub.current_period_end = timezone.now() - timedelta(days=1)
        sub.save()
        assert get_effective_plan(user).name == "free"

    def test_cannot_pause_when_trialing(self, client, user):
        _paid_sub(user, status=SubscriptionStatus.TRIALING)
        res = client.post("/api/v1/billing/pause/", {"months": 1}, format="json")
        assert res.status_code == 400

    def test_cannot_pause_within_a_year(self, client, user):
        sub = _paid_sub(user)
        sub.last_pause_at = timezone.now() - timedelta(days=100)
        sub.save()
        res = client.post("/api/v1/billing/pause/", {"months": 1}, format="json")
        assert res.status_code == 400
        assert res.data.get("code") == "pause_limit_reached"

    def test_invalid_months_rejected(self, client, user):
        _paid_sub(user)
        res = client.post("/api/v1/billing/pause/", {"months": 6}, format="json")
        assert res.status_code == 400

    def test_resume_from_pause_within_period_no_charge(self, client, user, monkeypatch):
        mock = ChargeMock(monkeypatch)
        sub = _paid_sub(user, period_end_delta=timedelta(days=5))
        sub.status = SubscriptionStatus.PAUSED
        sub.paused_months = 1
        sub.pause_ends_at = sub.current_period_end + timedelta(days=30)
        sub.save()
        res = client.post("/api/v1/billing/resume/")
        assert res.status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.pause_ends_at is None
        assert sub.paused_months is None
        assert mock.calls == []  # 잔여 유료기간 내 → 무과금

    def test_handle_pause_expiry_auto_resumes_and_dispatches(self, user, monkeypatch):
        dispatched = []
        monkeypatch.setattr(
            "apps.billing.tasks.charge_subscription_renewal.delay",
            lambda sid: dispatched.append(sid),
        )
        sub = _paid_sub(user, period_end_delta=timedelta(days=-40))
        sub.status = SubscriptionStatus.PAUSED
        sub.paused_months = 1
        sub.pause_ends_at = timezone.now() - timedelta(minutes=5)  # 만료
        sub.save()
        resume_end = sub.pause_ends_at

        handle_pause_expiry()

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.pause_ends_at is None
        assert sub.paused_months is None
        assert sub.current_period_end == resume_end  # 과거 시각 → 즉시 갱신 대상
        assert str(sub.id) in dispatched

    def test_pause_resume_reminder_marks_once(self, user, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "apps.billing.tasks.pause_resume_reminder_email", lambda sub: sent.append(sub.pk)
        )
        sub = _paid_sub(user)
        sub.status = SubscriptionStatus.PAUSED
        sub.paused_months = 1
        sub.pause_ends_at = timezone.now() + timedelta(days=2)  # 3일 창 안
        sub.pause_resume_reminder_sent_at = None
        sub.save()

        notify_pause_resume_reminder()
        sub.refresh_from_db()
        assert sub.pause_resume_reminder_sent_at is not None
        assert sub.pk in sent

        sent.clear()
        notify_pause_resume_reminder()  # 멱등 — 재발송 없음
        assert sent == []


# ──────────────────────────────────────────────
# ② 리텐션 할인 (다음 1회 50%)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestRetentionDiscount:
    def test_apply_marks_pending_and_returns_half(self, client, user):
        _paid_sub(user, snapshot=15900)
        res = client.post(
            "/api/v1/billing/retention-offer/apply/", {"offer": "discount_50"}, format="json"
        )
        assert res.status_code == 200, res.data
        assert res.data["applied"] is True
        assert res.data["next_charge_amount"] == 7950
        sub = ensure_subscription(user)
        sub.refresh_from_db()
        assert sub.retention_discount_pending is True
        assert sub.retention_discount_used_at is not None

    def test_one_per_user(self, client, user):
        sub = _paid_sub(user)
        sub.retention_discount_used_at = timezone.now()
        sub.save()
        res = client.post("/api/v1/billing/retention-offer/apply/", {}, format="json")
        assert res.status_code == 400
        assert res.data.get("code") == "retention_offer_already_used"

    def test_charge_applies_half_and_consumes(self, user, monkeypatch):
        mock = ChargeMock(monkeypatch)
        sub = _paid_sub(user, snapshot=15900, period_end_delta=timedelta(hours=-1))
        sub.retention_discount_pending = True
        sub.retention_discount_used_at = timezone.now()
        sub.save()

        charge_subscription_renewal.apply(args=[str(sub.id)]).get()

        assert mock.calls[0]["amount"] == 7950  # 15900 * 50%
        sub.refresh_from_db()
        assert sub.retention_discount_pending is False  # 소멸
        assert sub.retention_discount_used_at is not None  # 1인1회 보존


# ──────────────────────────────────────────────
# ④ 윈백 이메일
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestWinback:
    def test_disabled_by_default(self, user, settings, monkeypatch):
        sent = []
        monkeypatch.setattr("apps.billing.tasks.winback_email", lambda u: sent.append(u.id))
        settings.WINBACK_ENABLED = False
        assert send_winback_emails() == {"skipped": "disabled"}
        assert sent == []

    def test_sends_to_consented_churned_payer(self, user, settings, monkeypatch):
        sent = []
        monkeypatch.setattr("apps.billing.tasks.winback_email", lambda u: sent.append(u.id))
        settings.WINBACK_ENABLED = True
        settings.WINBACK_AFTER_DAYS = 30

        free = SubscriptionPlan.objects.get(name="free")
        sub = ensure_subscription(user)
        sub.plan = free
        sub.status = SubscriptionStatus.ACTIVE
        sub.cancelled_at = timezone.now() - timedelta(days=30, hours=2)
        sub.save()
        user.marketing_opt_in = True
        user.save()
        PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=9900,
            status=PaymentStatus.PAID,
            description="past",
            toss_order_id=f"wb-{uuid.uuid4().hex[:10]}",
        )

        send_winback_emails()
        assert user.id in sent

    def test_no_consent_no_send(self, user, settings, monkeypatch):
        sent = []
        monkeypatch.setattr("apps.billing.tasks.winback_email", lambda u: sent.append(u.id))
        settings.WINBACK_ENABLED = True
        settings.WINBACK_AFTER_DAYS = 30

        free = SubscriptionPlan.objects.get(name="free")
        sub = ensure_subscription(user)
        sub.plan = free
        sub.cancelled_at = timezone.now() - timedelta(days=30, hours=2)
        sub.save()
        # marketing_opt_in 미동의(기본 False)
        PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=9900,
            status=PaymentStatus.PAID,
            description="past",
            toss_order_id=f"wb-{uuid.uuid4().hex[:10]}",
        )
        send_winback_emails()
        assert sent == []


# ──────────────────────────────────────────────
# ③ 오퍼 트래킹 (analytics)
# ──────────────────────────────────────────────


@pytest.mark.django_db
def test_cancellation_event_offer_saved(client, user):
    res = client.post(
        "/api/v1/track/cancellation-event/",
        {"event": "offer_accepted", "offer": "pause", "from_plan": "pro"},
        format="json",
    )
    assert res.status_code == 201
    ev = CancellationEvent.objects.filter(user=user, event="offer_accepted").first()
    assert ev is not None
    assert ev.offer == "pause"
