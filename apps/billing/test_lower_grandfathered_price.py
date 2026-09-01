"""가격 인하분 소급(`lower_grandfathered_price`) 테스트.

핵심 성질 4개:
- 스냅샷 > 현재가 는 현재가까지 **내린다**
- 스냅샷 < 현재가(진짜 프로모 그랜드파더링)는 **올리지 않는다**
- ``--apply`` 없이는 **아무것도 쓰지 않는다**
- PENDING 주문이 떠 있는 구독은 **건너뛴다** (원장/청구 어긋남 방지)
"""

import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from apps.billing.models import (
    EXTRA_IG_ACCOUNT_PRICE,
    PaymentHistory,
    PaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from apps.billing.subscription_utils import ensure_subscription

User = get_user_model()


@pytest.fixture
def pro_plan(db):
    """현재 판매가 14,900 으로 고정 (테스트 DB 는 dev DB 와 공유 — 값 보장 필요)."""
    plan, _ = SubscriptionPlan.objects.get_or_create(
        name="pro", defaults={"display_name": "프로", "monthly_price": 14900, "sort_order": 2}
    )
    if plan.monthly_price != 14900:
        plan.monthly_price = 14900
        plan.save(update_fields=["monthly_price"])
    return plan


def _sub(plan, *, snapshot, extra=0, status=SubscriptionStatus.ACTIVE):
    user = User.objects.create_user(
        email=f"gfp-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )
    sub = ensure_subscription(user)
    sub.plan = plan
    sub.status = status
    sub.current_period_start = timezone.now() - timedelta(days=20)
    sub.current_period_end = timezone.now() + timedelta(days=10)
    sub.monthly_amount_snapshot = snapshot
    sub.extra_ig_accounts = extra
    sub.set_billing_key("bk_gfp_1", card_company="현대", card_number="4330****")
    sub.save()
    return sub


def _run(*args):
    out = StringIO()
    call_command("lower_grandfathered_price", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestLowerGrandfatheredPrice:
    def test_dry_run_writes_nothing(self, pro_plan):
        sub = _sub(pro_plan, snapshot=15900)
        out = _run("--email", sub.user.email)

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot == 15900, "DRY-RUN 이 DB 를 바꿨다"
        assert "WOULD" in out
        assert "DRY-RUN" in out

    def test_apply_lowers_to_current_price(self, pro_plan):
        sub = _sub(pro_plan, snapshot=15900)
        out = _run("--email", sub.user.email, "--apply")

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot == 14900
        assert sub.renewal_amount == 14900
        assert "DONE" in out

    def test_extra_accounts_still_added(self, pro_plan):
        """추가 IG 계정 가산은 유지 — 기본료만 내린다."""
        sub = _sub(pro_plan, snapshot=15900, extra=2)
        _run("--email", sub.user.email, "--apply")

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot == 14900
        assert sub.renewal_amount == 14900 + 2 * EXTRA_IG_ACCOUNT_PRICE

    def test_never_raises_promo_snapshot(self, pro_plan):
        """9,900 프로모 그랜드파더링은 절대 올리지 않는다."""
        sub = _sub(pro_plan, snapshot=9900)
        _run("--email", sub.user.email, "--apply")

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot == 9900

    def test_null_snapshot_untouched(self, pro_plan):
        """스냅샷 없음 = 이미 현재가 폴백 — 대상이 아니다."""
        sub = _sub(pro_plan, snapshot=None)
        _run("--email", sub.user.email, "--apply")

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot is None
        assert sub.renewal_amount == 14900

    def test_idempotent(self, pro_plan):
        sub = _sub(pro_plan, snapshot=15900)
        _run("--email", sub.user.email, "--apply")
        out2 = _run("--email", sub.user.email, "--apply")

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot == 14900
        assert "대상          : 0건" in out2
        assert "내릴 것이 없습니다" in out2

    def test_skips_pending_order(self, pro_plan):
        """승인 진행 중(PENDING) 주문이 있으면 손대지 않는다."""
        sub = _sub(pro_plan, snapshot=15900)
        PaymentHistory.objects.create(
            user=sub.user,
            subscription=sub,
            amount=15900,
            status=PaymentStatus.PENDING,
            description="테스트 진행 중 주문",
            toss_order_id=f"tf-{uuid.uuid4().hex[:16]}",
        )
        out = _run("--email", sub.user.email, "--apply")

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot == 15900
        assert "SKIP" in out

    def test_trialing_included(self, pro_plan):
        """체험 중이어도 첫 결제가 옛 가격이면 내린다."""
        sub = _sub(pro_plan, snapshot=15900, status=SubscriptionStatus.TRIALING)
        _run("--email", sub.user.email, "--apply")

        sub.refresh_from_db()
        assert sub.monthly_amount_snapshot == 14900
