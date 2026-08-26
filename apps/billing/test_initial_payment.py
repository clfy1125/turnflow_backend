"""첫 유료 결제 판별 — Meta Purchase 발사 기준 (2026-08-26).

⭐ 이 테스트가 지키는 것: **"첫 결제"를 주문번호 패턴이나 호출 지점으로 판별하면 틀린다.**

초판 구현이 그 실수를 했다. ``charge_now``(즉시 과금) 경로에서만 Purchase 를 발사하고
갱신 태스크에서는 안 불렀는데, **체험으로 시작한 사용자의 첫 유료 결제는 체험 종료 후
'갱신' 주문으로 들어온다**(카드 등록 시엔 0원). prod 실측에서 user 54·70 은 ``-init-``
주문이 아예 없고 갱신 패턴 1건이 그들의 첫 결제였다. 그래서 가장 중요한 전환인
**체험→유료**가 서버에서 통째로 빠져 있었다.

파일명이 ``test_`` 로 시작하므로 pytest 가 자동 수집한다 (``tests_`` 접두는 수집 안 됨).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.models import PaymentHistory, PaymentStatus

User = get_user_model()


def _user():
    return User.objects.create_user(
        email=f"initpay-{uuid.uuid4().hex[:10]}@test.com", password="pw12345!"
    )


def _pay(user, amount, *, status=PaymentStatus.PAID, paid_offset_min=0, order_id=""):
    now = timezone.now()
    return PaymentHistory.objects.create(
        user=user,
        amount=amount,
        status=status,
        description="테스트 결제",
        toss_order_id=order_id or f"tfsub-{uuid.uuid4().hex[:10]}",
        paid_at=(
            (now + timedelta(minutes=paid_offset_min)) if status == PaymentStatus.PAID else None
        ),
    )


@pytest.mark.django_db
class TestFirstPaidDetection:
    def test_single_payment_is_initial(self):
        u = _user()
        p = _pay(u, 14900)
        assert p.is_initial_payment is True

    def test_second_payment_is_not_initial(self):
        u = _user()
        first = _pay(u, 14900, paid_offset_min=0)
        second = _pay(u, 14900, paid_offset_min=60)
        assert first.is_initial_payment is True
        assert second.is_initial_payment is False

    def test_trial_user_first_charge_is_a_renewal_order(self):
        """★ prod 재현 — 체험자는 init 주문 없이 갱신 주문이 첫 결제다.

        주문번호로 판별했다면 이 케이스가 '첫 결제 아님'으로 빠져 체험→유료 전환이
        영구 누락된다.
        """
        u = _user()
        renewal = _pay(u, 14900, order_id="tfsub-67142901e1-20260809-a0")
        assert "init" not in renewal.toss_order_id
        assert renewal.is_initial_payment is True

    def test_upgrade_proration_after_first_is_not_initial(self):
        """업그레이드 비례배분은 이미 첫 결제가 있는 사용자에게만 생긴다 → 발사 대상 아님."""
        u = _user()
        init = _pay(u, 5900, paid_offset_min=0, order_id="tfsub-cf9559d1bf-init-1ce20228")
        upgrade = _pay(u, 9994, paid_offset_min=30, order_id="tfsub-cf9559d1bf-up-pro-0-20260819")
        assert init.is_initial_payment is True
        assert upgrade.is_initial_payment is False

    def test_zero_amount_never_counts(self):
        """체험 중 추가 IG 계정은 0원 — 유료 전환이 아니다."""
        u = _user()
        free = _pay(u, 0)
        assert free.is_initial_payment is False
        # 0원이 '첫 결제' 자리를 차지해서 뒤의 실결제를 가리면 안 된다
        real = _pay(u, 14900, paid_offset_min=10)
        assert real.is_initial_payment is True

    @pytest.mark.parametrize("status", [PaymentStatus.PENDING, PaymentStatus.FAILED])
    def test_unpaid_never_counts(self, status):
        u = _user()
        p = _pay(u, 14900, status=status)
        assert p.is_initial_payment is False

    def test_refunded_payment_not_initial(self):
        u = _user()
        p = _pay(u, 14900, status=PaymentStatus.REFUNDED)
        assert p.is_initial_payment is False

    def test_other_users_payments_do_not_interfere(self):
        a, b = _user(), _user()
        _pay(a, 14900, paid_offset_min=0)
        b_first = _pay(b, 14900, paid_offset_min=60)
        assert b_first.is_initial_payment is True

    def test_first_paid_id_for_returns_none_without_payments(self):
        assert PaymentHistory.first_paid_id_for(_user().id) is None


@pytest.mark.django_db
class TestCapiPurchaseGate:
    """track_purchase 가 첫 결제만 통과시키는가 (CAPI 비활성 상태에서도 게이트는 돈다)."""

    def test_renewal_is_skipped(self, monkeypatch):
        from apps.analytics import conversions

        sent = []
        monkeypatch.setattr(conversions, "dispatch_meta_capi", lambda **kw: sent.append(kw))

        u = _user()
        first = _pay(u, 14900, paid_offset_min=0)
        renewal = _pay(u, 14900, paid_offset_min=60)

        conversions.track_purchase(first)
        conversions.track_purchase(renewal)

        assert len(sent) == 1
        assert sent[0]["event_id"] == str(first.id)
        assert sent[0]["value"] == 14900

    def test_event_id_is_payment_uuid(self, monkeypatch):
        """★ 프론트 픽셀과 같은 값이어야 중복 제거된다 (배포본: {eventID: e.id})."""
        from apps.analytics import conversions

        sent = []
        monkeypatch.setattr(conversions, "dispatch_meta_capi", lambda **kw: sent.append(kw))
        p = _pay(_user(), 9900)
        conversions.track_purchase(p)
        assert sent[0]["event_id"] == str(p.id)
        assert sent[0]["event_name"] == "Purchase"

    def test_never_raises_on_broken_payment(self, monkeypatch):
        """계측 실패가 결제를 깨뜨리면 안 된다."""
        from apps.analytics import conversions

        class _Broken:
            id = "x"

            @property
            def is_initial_payment(self):
                raise RuntimeError("db down")

        conversions.track_purchase(_Broken())  # 예외가 밖으로 나오지 않아야 한다
