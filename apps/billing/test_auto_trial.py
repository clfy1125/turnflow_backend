"""카드 없는 프로 30일 자동 지급 — 자격 판정·멱등·만료 경로.

지키려는 계약 셋:
  1. 한 사람에게 **한 번만** (기간이 60일이 되지 않는다)
  2. 만료 시 **과금 없이** 무료 복귀 (빌링키가 없으므로 process_due_renewals 대상이 아니다)
  3. 체험 중 카드 등록은 ``attach_only`` — 기간 불변, 만료일에 첫 결제

더러운 테스트 DB 대응(memory: test-db-not-clean): 이메일은 uuid 로 유일화하고,
집계는 전역 count 가 아니라 이 사용자의 행만 본다.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing import auto_trial
from apps.billing.models import PaymentHistory, SubscriptionPlan, SubscriptionStatus, TrialKind
from apps.billing.subscription_utils import ensure_subscription


def _user():
    return get_user_model().objects.create_user(
        email=f"autotrial-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


@pytest.mark.django_db
class TestAutoTrialGrant:
    @pytest.fixture
    def user(self, db):
        return _user()

    @pytest.fixture
    def client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_grants_pro_trial_without_card(self, client, user):
        res = client.post(
            reverse("billing:trial-auto-grant"),
            {"source": "signup", "provider": "email"},
            format="json",
        )
        assert res.status_code == 200, res.json()
        body = res.json()
        assert body["granted"] is True
        assert body["reason"] is None

        sub = user.subscription
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.TRIALING
        assert sub.plan.name == "pro"
        assert sub.has_billing_key is False
        assert sub.trial_kind == TrialKind.AUTO
        assert sub.trial_used_at is not None
        assert sub.trial_plan is not None and sub.trial_plan.name == "pro"
        # 그랜드파더링 — 지금 판매가를 얼려 둔다
        assert sub.monthly_amount_snapshot == SubscriptionPlan.objects.get(name="pro").monthly_price
        # 결제는 일어나지 않는다
        assert not PaymentHistory.objects.filter(user=user).exists()

    def test_trial_last_day_is_day_before_charge(self, client, user):
        """표기값 — `current_period_end` 를 날짜로 찍으면 하루 더 써도 되는 것처럼 보인다."""
        res = client.post(reverse("billing:trial-auto-grant"), {}, format="json")
        sub = user.subscription
        sub.refresh_from_db()

        expected = timezone.localdate(sub.current_period_end) - timedelta(days=1)
        assert sub.trial_last_day == expected
        assert res.json()["subscription"]["trial_last_day"] == str(expected)

    def test_second_call_is_idempotent(self, client, user):
        first = client.post(reverse("billing:trial-auto-grant"), {}, format="json").json()
        assert first["granted"] is True
        end_after_first = first["subscription"]["current_period_end"]

        second = client.post(reverse("billing:trial-auto-grant"), {}, format="json").json()
        assert second["granted"] is False
        assert second["reason"] == "trial_used"
        # ⚠️ 여기가 핵심 — 두 번째 호출로 30일이 더해지면 안 된다
        assert second["subscription"]["current_period_end"] == end_after_first

    def test_refuses_when_card_already_registered(self, user):
        sub = ensure_subscription(user)
        sub.set_billing_key("bkey_test", card_company="테스트", card_number="1234")
        sub.save()

        granted, reason = auto_trial.grant(user)
        assert granted is None
        assert reason == auto_trial.REASON_HAS_BILLING_KEY

    def test_refuses_when_already_paid_plan(self, user):
        sub = ensure_subscription(user)
        sub.plan = SubscriptionPlan.objects.get(name="pro")
        sub.status = SubscriptionStatus.ACTIVE
        sub.save()

        granted, reason = auto_trial.grant(user)
        assert granted is None
        assert reason == auto_trial.REASON_ALREADY_PRO

    def test_killswitch_off_blocks_grant(self, client, settings):
        settings.AUTO_PRO_TRIAL_ENABLED = False
        body = client.post(reverse("billing:trial-auto-grant"), {}, format="json").json()
        assert body["granted"] is False
        assert body["reason"] == "disabled"

    def test_signup_window_blocks_old_accounts_when_configured(self, user, settings):
        """창은 기본 0(제한 없음)이지만, 설정하면 가입 시점으로 자른다."""
        settings.AUTO_PRO_TRIAL_SIGNUP_WINDOW_DAYS = 7
        user.date_joined = timezone.now() - timedelta(days=30)
        user.save(update_fields=["date_joined"])

        granted, reason = auto_trial.grant(user)
        assert granted is None
        assert reason == auto_trial.REASON_NOT_NEW_USER

    def test_default_window_allows_old_accounts(self, user, settings):
        """기본값(0)에서는 오래된 가입자도 받는다 — 프로 체험 팝업의 대상이 그들이다."""
        settings.AUTO_PRO_TRIAL_SIGNUP_WINDOW_DAYS = 0
        user.date_joined = timezone.now() - timedelta(days=90)
        user.save(update_fields=["date_joined"])

        granted, reason = auto_trial.grant(user)
        assert granted is not None, reason


@pytest.mark.django_db
class TestAutoTrialExpiry:
    def test_expires_to_free_without_charging(self):
        """빌링키가 없으므로 handle_trial_expiry 가 **과금 없이** 무료로 내린다."""
        from apps.billing.tasks import handle_trial_expiry

        user = _user()
        sub, reason = auto_trial.grant(user)
        assert sub is not None, reason

        sub.current_period_end = timezone.now() - timedelta(hours=1)
        sub.save(update_fields=["current_period_end"])

        handle_trial_expiry()

        sub.refresh_from_db()
        assert sub.plan.name == "free"
        assert sub.status == SubscriptionStatus.ACTIVE
        assert not PaymentHistory.objects.filter(user=user).exists()
        # 내구 기록은 남는다 — 남아 있어야 재체험이 막히고, 팝업이 '체험 사용함'으로 판정한다
        assert sub.trial_used_at is not None
        assert sub.trial_kind == TrialKind.AUTO

    def test_expired_user_cannot_get_a_second_trial(self):
        from apps.billing.tasks import handle_trial_expiry

        user = _user()
        sub, _ = auto_trial.grant(user)
        sub.current_period_end = timezone.now() - timedelta(hours=1)
        sub.save(update_fields=["current_period_end"])
        handle_trial_expiry()

        granted, reason = auto_trial.grant(user)
        assert granted is None
        assert reason == auto_trial.REASON_TRIAL_USED


@pytest.mark.django_db
class TestAutoTrialPreviewScenario:
    def test_preview_returns_attach_only_during_auto_trial(self):
        """프론트 확인 요청 — 카드 없는 체험 중 견적이 `attach_only` 로 나와야 한다."""
        from apps.billing.toss_flows import preview_subscription

        user = _user()
        sub, reason = auto_trial.grant(user)
        assert sub is not None, reason

        quote = preview_subscription(user, plan_name="pro")
        assert quote["scenario"] == "attach_only"
        assert quote["is_trial"] is True
        # 기간은 불변 — 카드를 붙여도 체험이 짧아지거나 길어지지 않는다
        assert quote["trial_ends_at"] == sub.current_period_end
        assert quote["first_charge_at"] == sub.current_period_end
        assert quote["trial_last_day"] == sub.trial_last_day


@pytest.mark.django_db
class TestTrialKindSurvivesCardAttach:
    """자동 체험 중 카드를 등록해도 ``trial_kind`` 는 ``auto`` 로 남는다 (프론트 확인 요청).

    ⭐ 의도된 동작이다. ``trial_kind`` 는 "체험이 **어떻게 시작됐나**"의 내구 기록이고,
       "지금 카드가 있나"는 ``has_billing_key`` 가 따로 말해 준다. 카드를 붙였다고 값을
       ``card`` 로 덮으면 **"자동 지급받은 사람 중 몇 %가 카드를 붙였나"** 라는 코호트
       질문에 영영 답할 수 없다 — 그게 이 정책의 핵심 지표다.
    """

    def test_attach_only_does_not_rewrite_trial_kind(self, monkeypatch):
        from apps.billing.toss_flows import confirm_billing

        user = _user()
        sub, reason = auto_trial.grant(user)
        assert sub is not None, reason
        period_end_before = sub.current_period_end

        # 토스 외부 호출만 대체 — 나머지 분기는 실제 코드가 돈다.
        monkeypatch.setattr(
            "apps.billing.toss_flows.TossBillingClient.issue_billing_key",
            lambda auth_key, customer_key: {
                "billingKey": "bkey_test_attach",
                "customerKey": customer_key,
                "card": {"company": "테스트", "number": "1234"},
            },
        )
        fired = []
        monkeypatch.setattr(
            "apps.analytics.conversions.track_trial_started",
            lambda subscription, request=None: fired.append(subscription.id),
        )

        result = confirm_billing(user, auth_key="auth_test", plan_name="pro")

        assert result["scenario"] == "attach_only"
        sub.refresh_from_db()
        assert sub.trial_kind == TrialKind.AUTO  # ← card 로 바뀌지 않는다
        assert sub.has_billing_key is True
        # 기간도 불변 — 카드를 붙여도 체험이 짧아지거나 길어지지 않는다
        assert sub.current_period_end == period_end_before
        # StartTrial 은 체험 **시작** 때 한 번만 발사된다. 카드 부착은 재발사하지 않는다
        # (재발사하면 Meta 에서 같은 사람의 체험이 두 번 집계된다).
        assert fired == []
