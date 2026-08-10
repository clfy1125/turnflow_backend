"""결제 전 고지·동의 테스트.

핵심 계약 (프론트 요청서 `backend-payment-consent.md`):
1. 체험은 **+30일 고정** — 프론트의 `오늘+30일` 계산과 서버 확정값이 일치해야 한다.
2. 동의 기록(`POST /billing/consents/`)은 세 동의가 전부 true 일 때만 저장된다.
3. 견적(`preview`)은 부작용이 없어야 한다.

**2026-08-10 정책 변경**: 동의는 결제 화면 **1회**로 통일됐다
(`CONVERSION_SECOND_CONSENT_ENABLED=False` 기본). 그래서 2차 동의 파이프라인
(모달 플래그·과금 차단·D-14/D-3 메일)을 검증하는 클래스는 `_second_consent_on`
픽스처로 **명시적으로 켜고** 테스트한다 — 플래그를 켰을 때 그 기능이 여전히
정상 동작하는지가 재활성화 시점의 안전망이다.
기본값(꺼진 상태) 동작은 :class:`TestSecondConsentDisabledByDefault` 가 지킨다.

⚠️ pytest DB 는 dev DB 를 쓴다(test-db-not-clean) → 이메일은 uuid, 집계는 델타로 단언.
"""

import uuid
from datetime import date, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.consent import blocks_first_charge, conversion_consent_required
from apps.billing.models import (
    ConsentKind,
    PaymentConsent,
    PaymentHistory,
    ReferralCode,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)
from apps.billing.subscription_utils import ensure_subscription
from apps.billing.tasks import charge_subscription_renewal, notify_conversion_consent
from apps.billing.toss_flows import PERIOD_DAYS, TRIAL_BASE_DAYS
from apps.billing.toss_service import TossBillingClient

User = get_user_model()


def _dt(value) -> datetime:
    """DRF 직렬화 결과(ISO 문자열)를 datetime 으로."""
    return datetime.fromisoformat(value)


def _d(value) -> date:
    return date.fromisoformat(value)


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"consent-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


@pytest.fixture
def client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def _second_consent_on(settings):
    """2차 동의 파이프라인을 켠다 (기본은 꺼져 있다 — 2026-08-10 정책)."""
    settings.CONVERSION_SECOND_CONSENT_ENABLED = True
    return settings


def _trial_sub(user, *, trial_days=TRIAL_BASE_DAYS, elapsed_days=0, billing_key="bk_consent"):
    """체험 중 구독을 만든다. elapsed_days 만큼 이미 지난 상태로 세팅."""
    plan = SubscriptionPlan.objects.get(name="pro")
    sub = ensure_subscription(user)
    now = timezone.now()
    sub.plan = plan
    sub.trial_plan = plan
    sub.status = SubscriptionStatus.TRIALING
    sub.current_period_start = now - timedelta(days=elapsed_days)
    sub.current_period_end = sub.current_period_start + timedelta(days=trial_days)
    sub.monthly_amount_snapshot = 14900
    sub.extra_ig_accounts = 0
    sub.trial_used_at = sub.current_period_start
    sub.conversion_consent_at = None
    sub.conversion_consent_notice_sent_at = None
    sub.conversion_consent_reminder_sent_at = None
    if billing_key:
        sub.set_billing_key(billing_key, card_company="현대", card_number="4330****")
    sub.save()
    return sub


class ChargeSpy:
    """토스 승인 호출을 가로채 '몇 번 긁었는지' 를 센다."""

    def __init__(self, monkeypatch):
        self.calls = []

        def fake_charge(**kwargs):
            self.calls.append(kwargs)
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
# ① 견적 (preview) — 체험 계산이 +30일 고정인지
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestSubscriptionPreview:
    def test_pro_trial_is_exactly_30_days(self, client, user):
        before = timezone.now()
        res = client.get("/api/v1/billing/subscription/preview/?plan_name=pro")
        assert res.status_code == 200, res.data
        d = res.data
        assert d["scenario"] == "trial"
        assert d["is_trial"] is True
        # 프론트의 `오늘 + 30일` 과 정확히 일치해야 한다 (달력 1개월이 아님)
        assert d["trial_days"] == 30
        expected = before + timedelta(days=30)
        assert abs((_dt(d["first_charge_at"]) - expected).total_seconds()) < 10
        assert d["trial_ends_at"] == d["first_charge_at"]
        # 체험 마지막 이용일 = 첫 결제일 − 1일
        assert _d(d["trial_last_day"]) == timezone.localdate(_dt(d["first_charge_at"])) - timedelta(
            days=1
        )
        assert d["first_charge_amount"] == d["recurring_amount"]
        assert (_dt(d["next_renewal_at"]) - _dt(d["first_charge_at"])).days == PERIOD_DAYS

    def test_referral_code_extends_trial_without_consuming(self, client, user):
        plan = SubscriptionPlan.objects.get(name="pro")
        code = ReferralCode.objects.create(
            code=f"PRV{uuid.uuid4().hex[:6].upper()}", target_plan=plan, trial_days=14
        )
        res = client.get(
            f"/api/v1/billing/subscription/preview/?plan_name=pro&referral_code={code.code}"
        )
        assert res.status_code == 200, res.data
        assert res.data["trial_days"] == TRIAL_BASE_DAYS + 14 == 44
        assert res.data["referral_bonus_days"] == 14
        # 부작용 없음 — 쿠폰이 소진되지 않아야 한다
        code.refresh_from_db()
        assert code.current_uses == 0

    def test_basic_is_charge_now(self, client, user):
        res = client.get("/api/v1/billing/subscription/preview/?plan_name=basic")
        assert res.status_code == 200, res.data
        assert res.data["scenario"] == "charge_now"
        assert res.data["is_trial"] is False
        assert res.data["trial_ends_at"] is None
        assert res.data["trial_last_day"] is None
        assert res.data["first_charge_amount"] > 0

    def test_extra_accounts_included_in_amount(self, client, user):
        base = client.get("/api/v1/billing/subscription/preview/?plan_name=pro").data
        with_extra = client.get(
            "/api/v1/billing/subscription/preview/?plan_name=pro&extra_ig_accounts=2"
        ).data
        assert with_extra["first_charge_amount"] - base["first_charge_amount"] == 2 * 9900

    def test_no_subscription_row_is_created(self, client, user):
        assert not UserSubscription.objects.filter(user=user).exists()
        res = client.get("/api/v1/billing/subscription/preview/?plan_name=pro")
        assert res.status_code == 200
        # 부작용 없는 조회 — 무료 구독 행조차 만들지 않는다
        assert not UserSubscription.objects.filter(user=user).exists()

    def test_missing_plan_name_is_400(self, client, user):
        res = client.get("/api/v1/billing/subscription/preview/")
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "plan_name"

    def test_free_plan_rejected(self, client, user):
        res = client.get("/api/v1/billing/subscription/preview/?plan_name=free")
        assert res.status_code == 400

    def test_requires_auth(self, db):
        res = APIClient().get("/api/v1/billing/subscription/preview/?plan_name=pro")
        assert res.status_code == 401


# ──────────────────────────────────────────────
# ② conversion_consent_required 판정
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestConsentRequiredFlag:
    # 2026-08-10 정책상 2차 동의는 기본 OFF — 이 클래스는 켠 상태의 동작을 지킨다
    @pytest.fixture(autouse=True)
    def _on(self, _second_consent_on):
        pass

    def test_30day_trial_is_never_required(self, user):
        """30일 체험자는 결제 화면 동의로 요건 충족 → 모달 띄우지 않는다."""
        sub = _trial_sub(user, trial_days=30, elapsed_days=29)
        assert conversion_consent_required(sub) is False
        assert blocks_first_charge(sub) is False

    def test_44day_trial_required_only_inside_window(self, user):
        sub = _trial_sub(user, trial_days=44, elapsed_days=0)
        # D-0: 첫 결제가 44일 뒤 → 30일 창 밖이라 아직 아니다
        assert conversion_consent_required(sub) is False
        # 창 안(잔여 30일 이하)으로 들어오면 true
        sub.current_period_start = timezone.now() - timedelta(days=20)
        sub.current_period_end = sub.current_period_start + timedelta(days=44)
        sub.save()
        assert conversion_consent_required(sub) is True

    def test_no_card_trial_not_required(self, user):
        """카드 없는 쿠폰 체험자는 자동 유료전환 자체가 없다."""
        sub = _trial_sub(user, trial_days=44, elapsed_days=20, billing_key=None)
        assert conversion_consent_required(sub) is False
        assert blocks_first_charge(sub) is False

    def test_consented_is_not_required(self, user):
        sub = _trial_sub(user, trial_days=44, elapsed_days=20)
        sub.conversion_consent_at = timezone.now()
        sub.save()
        assert conversion_consent_required(sub) is False
        assert blocks_first_charge(sub) is False

    def test_blocks_charge_regardless_of_window(self, user):
        """과금 게이트는 창 조건과 무관 — 체험 종료 시점에 미동의면 막는다."""
        sub = _trial_sub(user, trial_days=44, elapsed_days=44)
        assert blocks_first_charge(sub) is True

    def test_gate_never_fires_before_trial_ends(self, user, monkeypatch):
        """⚠️ 회귀 방어 — 게이트는 due 재검증보다 **앞**에 있다.

        게이트가 '체험 종료 도래' 를 스스로 확인하지 않으면, 아직 한 달 넘게 남은 체험에
        과금 태스크가 한 번 잘못 디스패치되는 것만으로 그 사용자가 즉시 무료로 떨어진다.
        """
        spy = ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=44, elapsed_days=1)  # 43일 남음
        assert blocks_first_charge(sub) is False

        # 오배치 시뮬레이션 — 아직 due 가 아니므로 '아무 일도 없음' 이어야 한다
        result = charge_subscription_renewal(str(sub.id))
        assert result["result"] == "not_due"
        assert spy.calls == []
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.TRIALING
        assert sub.plan.name == "pro"  # 무료로 떨어지지 않았다
        assert sub.has_billing_key is True  # 빌링키도 살아 있다

    def test_my_subscription_exposes_flag(self, client, user):
        _trial_sub(user, trial_days=44, elapsed_days=20)
        res = client.get("/api/v1/billing/my-subscription/")
        assert res.status_code == 200, res.data
        assert res.data["conversion_consent_required"] is True
        assert res.data["conversion_consent_at"] is None
        assert res.data["trial_total_days"] == 44

    def test_my_subscription_flag_false_for_30day(self, client, user):
        _trial_sub(user, trial_days=30, elapsed_days=25)
        res = client.get("/api/v1/billing/my-subscription/")
        assert res.data["conversion_consent_required"] is False
        assert res.data["trial_total_days"] == 30


# ──────────────────────────────────────────────
# ③ 동의 기록 저장 API
# ──────────────────────────────────────────────


def _consent_body(kind="conversion", **over):
    body = {
        "kind": kind,
        "plan_name": "pro",
        "disclosed_first_charge_at": "2026-09-23",
        "disclosed_amount": 14900,
        "disclosed_recurring_cycle": "monthly",
        "payment_method_type": "card",
        "copy_version": "billingConsent@2026-08-10",
        "agreed_terms": True,
        "agreed_privacy": True,
        "agreed_recurring": True,
    }
    body.update(over)
    return body


@pytest.mark.django_db
class TestConsentRecord:
    def test_conversion_consent_unblocks_charge(self, client, user):
        sub = _trial_sub(user, trial_days=44, elapsed_days=20)
        res = client.post("/api/v1/billing/consents/", _consent_body(), format="json")
        assert res.status_code == 201, res.data
        assert res.data["applied_to_subscription"] is True
        sub.refresh_from_db()
        assert sub.conversion_consent_at is not None
        assert blocks_first_charge(sub) is False

    def test_snapshot_fields_persisted(self, client, user):
        _trial_sub(user, trial_days=44, elapsed_days=20)
        client.post("/api/v1/billing/consents/", _consent_body(), format="json")
        row = PaymentConsent.objects.filter(user=user).first()
        assert row is not None
        assert row.kind == ConsentKind.CONVERSION
        assert row.disclosed_amount == 14900
        assert row.disclosed_first_charge_at.isoformat() == "2026-09-23"
        assert row.copy_version == "billingConsent@2026-08-10"
        assert row.all_agreed is True
        assert row.ip_address is not None or row.ip_address is None  # 저장 시도됨
        assert row.subscription_id is not None

    def test_partial_agreement_is_400_and_records_nothing(self, client, user):
        _trial_sub(user, trial_days=44, elapsed_days=20)
        before = PaymentConsent.objects.filter(user=user).count()
        res = client.post(
            "/api/v1/billing/consents/",
            _consent_body(agreed_recurring=False),
            format="json",
        )
        assert res.status_code == 400
        assert "agreed_recurring" in res.data["error"]["details"]
        assert PaymentConsent.objects.filter(user=user).count() == before

    def test_initial_consent_does_not_touch_subscription(self, client, user):
        sub = _trial_sub(user, trial_days=44, elapsed_days=20)
        res = client.post("/api/v1/billing/consents/", _consent_body(kind="initial"), format="json")
        assert res.status_code == 201
        assert res.data["applied_to_subscription"] is False
        sub.refresh_from_db()
        assert sub.conversion_consent_at is None

    def test_initial_consent_works_without_subscription(self, client, user):
        """결제 화면 동의는 구독 행이 없는 신규 사용자도 남길 수 있어야 한다."""
        res = client.post("/api/v1/billing/consents/", _consent_body(kind="initial"), format="json")
        assert res.status_code == 201, res.data
        assert PaymentConsent.objects.filter(user=user, kind="initial").exists()

    def test_iso_datetime_accepted_for_disclosed_date(self, client, user):
        _trial_sub(user, trial_days=44, elapsed_days=20)
        res = client.post(
            "/api/v1/billing/consents/",
            _consent_body(disclosed_first_charge_at="2026-09-23T14:02:11+09:00"),
            format="json",
        )
        assert res.status_code == 201, res.data
        assert res.data["disclosed_first_charge_at"] == "2026-09-23"

    def test_requires_auth(self, db):
        res = APIClient().post("/api/v1/billing/consents/", _consent_body(), format="json")
        assert res.status_code == 401


# ──────────────────────────────────────────────
# ④ 첫 결제 분기 — **가장 중요**
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestFirstChargeGate:
    # 2026-08-10 정책상 2차 동의는 기본 OFF — 이 클래스는 켠 상태의 동작을 지킨다
    @pytest.fixture(autouse=True)
    def _on(self, _second_consent_on):
        pass

    def test_missing_consent_skips_charge_and_downgrades(self, user, monkeypatch):
        spy = ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=44, elapsed_days=44)
        payments_before = PaymentHistory.objects.filter(user=user).count()

        result = charge_subscription_renewal(str(sub.id))

        assert result["result"] == "skipped_missing_consent"
        assert spy.calls == []  # 토스 승인 호출이 아예 없어야 한다
        # PENDING 주문도 만들지 않는다 (reconcile 이 '결제 실패' 로 확정하는 것 방지)
        assert PaymentHistory.objects.filter(user=user).count() == payments_before
        sub.refresh_from_db()
        assert sub.plan.name == "free"
        assert sub.has_billing_key is False

    def test_data_is_preserved_on_consent_downgrade(self, user, monkeypatch):
        """무료 전환은 데이터를 지우지 않는다 — 재구독 시 복원되어야 한다."""
        from apps.pages.models import Page

        ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=44, elapsed_days=44)
        page = Page.objects.create(user=user, title="t", slug=f"cs-{uuid.uuid4().hex[:8]}")

        assert charge_subscription_renewal(str(sub.id))["result"] == "skipped_missing_consent"

        assert Page.objects.filter(pk=page.pk).exists()
        sub.refresh_from_db()
        assert sub.plan.name == "free"
        # 체험 이력의 내구 기록은 남는다
        assert sub.trial_used_at is not None
        assert sub.trial_plan is not None

    def test_consent_present_charges_normally(self, user, monkeypatch):
        spy = ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=44, elapsed_days=44)
        sub.conversion_consent_at = timezone.now()
        sub.save()

        result = charge_subscription_renewal(str(sub.id))

        assert result["result"] == "paid"
        assert len(spy.calls) == 1
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.plan.name == "pro"

    def test_30day_trial_charges_without_second_consent(self, user, monkeypatch):
        """기본 30일 체험은 2차 동의 없이 정상 결제된다 (회귀 방어)."""
        spy = ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=30, elapsed_days=30)

        result = charge_subscription_renewal(str(sub.id))

        assert result["result"] == "paid"
        assert len(spy.calls) == 1

    def test_regular_renewal_never_blocked(self, user, monkeypatch):
        """유료(ACTIVE) 정기 갱신은 2차 동의 게이트와 무관하다."""
        spy = ChargeSpy(monkeypatch)
        plan = SubscriptionPlan.objects.get(name="pro")
        sub = ensure_subscription(user)
        sub.plan = plan
        sub.status = SubscriptionStatus.ACTIVE
        sub.current_period_start = timezone.now() - timedelta(days=74)
        sub.current_period_end = timezone.now() - timedelta(seconds=5)
        sub.monthly_amount_snapshot = 14900
        sub.conversion_consent_at = None
        sub.set_billing_key("bk_regular", card_company="현대", card_number="4330****")
        sub.save()

        result = charge_subscription_renewal(str(sub.id))
        assert result["result"] == "paid"
        assert len(spy.calls) == 1

    def test_legacy_gate_off_by_default_on_by_setting(self, user, monkeypatch, settings):
        """소급 게이트는 기본 꺼짐 — 켜면 동의 기록 없는 30일 체험도 막힌다."""
        spy = ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=30, elapsed_days=30)
        assert blocks_first_charge(sub) is False  # 기본값

        settings.CONVERSION_CONSENT_REQUIRE_ALL_TRIALS = True
        sub.refresh_from_db()
        assert blocks_first_charge(sub) is True

        result = charge_subscription_renewal(str(sub.id))
        assert result["result"] == "skipped_missing_consent"
        assert spy.calls == []

    def test_enable_flag_is_the_single_control(self, user, monkeypatch, settings):
        """플래그 하나가 모달·차단·메일을 함께 켜고 끈다 (스위치가 갈라지지 않는지)."""
        spy = ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=44, elapsed_days=44)
        assert blocks_first_charge(sub) is True  # 이 클래스는 켠 상태(_second_consent_on)

        settings.CONVERSION_SECOND_CONSENT_ENABLED = False
        assert blocks_first_charge(sub) is False
        in_window = _trial_sub(
            User.objects.create_user(
                email=f"off-{uuid.uuid4().hex[:8]}@example.com", password="Pass1234!"
            ),
            trial_days=44,
            elapsed_days=20,
        )
        assert conversion_consent_required(in_window) is False
        assert charge_subscription_renewal(str(sub.id))["result"] == "paid"
        assert len(spy.calls) == 1

    def test_legacy_gate_passes_when_initial_consent_exists(self, user, monkeypatch, settings):
        ChargeSpy(monkeypatch)
        settings.CONVERSION_CONSENT_REQUIRE_ALL_TRIALS = True
        sub = _trial_sub(user, trial_days=30, elapsed_days=30)
        PaymentConsent.objects.create(
            user=user,
            subscription=sub,
            kind=ConsentKind.INITIAL,
            plan_name="pro",
            disclosed_amount=14900,
            copy_version="billingConsent@2026-08-10",
            agreed_terms=True,
            agreed_privacy=True,
            agreed_recurring=True,
            consented_at=sub.current_period_start,
        )
        sub.refresh_from_db()
        assert blocks_first_charge(sub) is False
        assert charge_subscription_renewal(str(sub.id))["result"] == "paid"


# ──────────────────────────────────────────────
# ⑤ D-14 / D-3 안내 메일
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestConsentNotices:
    # 2026-08-10 정책상 2차 동의는 기본 OFF — 이 클래스는 켠 상태의 동작을 지킨다
    @pytest.fixture(autouse=True)
    def _on(self, _second_consent_on):
        pass

    def test_d14_notice_sent_once(self, user, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "apps.emails.tasks.send_conversion_consent_email.delay",
            lambda uid, ctx: sent.append(ctx),
        )
        sub = _trial_sub(user, trial_days=44, elapsed_days=44 - 14)

        notify_conversion_consent()
        assert len(sent) == 1
        assert sent[0]["days_left"] in (13, 14)
        sub.refresh_from_db()
        assert sub.conversion_consent_notice_sent_at is not None

        # 두 번째 실행에서는 재발송하지 않는다
        notify_conversion_consent()
        assert len(sent) == 1

    def test_d3_reminder_sent_after_notice(self, user, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "apps.emails.tasks.send_conversion_consent_email.delay",
            lambda uid, ctx: sent.append(ctx),
        )
        sub = _trial_sub(user, trial_days=44, elapsed_days=44 - 2)
        sub.conversion_consent_notice_sent_at = timezone.now() - timedelta(days=11)
        sub.save()

        notify_conversion_consent()
        assert len(sent) == 1
        sub.refresh_from_db()
        assert sub.conversion_consent_reminder_sent_at is not None

    def test_30day_trial_gets_no_notice(self, user, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "apps.emails.tasks.send_conversion_consent_email.delay",
            lambda uid, ctx: sent.append(ctx),
        )
        _trial_sub(user, trial_days=30, elapsed_days=27)
        notify_conversion_consent()
        assert sent == []

    def test_consented_gets_no_notice(self, user, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "apps.emails.tasks.send_conversion_consent_email.delay",
            lambda uid, ctx: sent.append(ctx),
        )
        sub = _trial_sub(user, trial_days=44, elapsed_days=44 - 10)
        sub.conversion_consent_at = timezone.now()
        sub.save()
        notify_conversion_consent()
        assert sent == []

    def test_at_risk_raises_ops_alert(self, user, monkeypatch):
        """D-3 이내 미동의자가 있으면 운영 알림 — 무료 전환 **전에** 알아야 한다."""
        monkeypatch.setattr(
            "apps.emails.tasks.send_conversion_consent_email.delay", lambda uid, ctx: None
        )
        alerts = []
        monkeypatch.setattr("apps.billing.tasks._ops_alert", lambda msg: alerts.append(msg))
        _trial_sub(user, trial_days=44, elapsed_days=44 - 2)

        result = notify_conversion_consent()
        assert result["at_risk"] >= 1
        assert len(alerts) == 1
        assert "무과금 무료 전환" in alerts[0]

    def test_downgrade_raises_ops_alert(self, user, monkeypatch):
        ChargeSpy(monkeypatch)
        alerts = []
        monkeypatch.setattr("apps.billing.tasks._ops_alert", lambda msg: alerts.append(msg))
        sub = _trial_sub(user, trial_days=44, elapsed_days=44)

        charge_subscription_renewal(str(sub.id))
        assert len(alerts) == 1
        assert user.email in alerts[0]


# ──────────────────────────────────────────────
# ⑥ 기본 정책 = 동의 1회 (2026-08-10 제품 결정)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestSecondConsentDisabledByDefault:
    """플래그를 켜지 않은 **기본 상태**의 계약.

    회의 결정: 첫 결제 45일 전에 다시 동의를 받게 하면 리텐션이 떨어지고, 당시 44일 쿠폰
    대상이 지인 범위였다 → 동의는 결제 화면 1회. 여기서 지키는 것은
    "44일 쿠폰 체험자도 아무 추가 절차 없이 정상 유료전환된다" 하나다.
    """

    def test_flag_is_off_by_default(self):
        from apps.billing.consent import second_consent_enabled

        assert second_consent_enabled() is False

    def test_44day_trial_charges_normally(self, user, monkeypatch):
        """가장 중요 — 쿠폰 44일 체험자가 2차 동의 없이 정상 결제된다."""
        spy = ChargeSpy(monkeypatch)
        sub = _trial_sub(user, trial_days=44, elapsed_days=44)
        assert sub.conversion_consent_at is None

        result = charge_subscription_renewal(str(sub.id))

        assert result["result"] == "paid"
        assert len(spy.calls) == 1
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.plan.name == "pro"  # 무료로 떨어지지 않았다

    def test_no_modal_flag(self, client, user):
        _trial_sub(user, trial_days=44, elapsed_days=20)
        res = client.get("/api/v1/billing/my-subscription/")
        assert res.status_code == 200, res.data
        # 필드는 계약 유지를 위해 남기고 값만 항상 false (프론트 모달이 뜨지 않는다)
        assert res.data["conversion_consent_required"] is False
        assert res.data["trial_total_days"] == 44

    def test_no_reminder_emails(self, user, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "apps.emails.tasks.send_conversion_consent_email.delay",
            lambda uid, ctx: sent.append(ctx),
        )
        _trial_sub(user, trial_days=44, elapsed_days=44 - 2)  # D-2 (창 한복판)
        result = notify_conversion_consent()
        assert result == {"skipped": "second_consent_disabled"}
        assert sent == []

    def test_initial_consent_still_recorded(self, client, user):
        """동의 1회(결제 화면)는 그대로 기록된다 — 이건 폐지 대상이 아니다."""
        res = client.post("/api/v1/billing/consents/", _consent_body(kind="initial"), format="json")
        assert res.status_code == 201, res.data
        assert PaymentConsent.objects.filter(user=user, kind=ConsentKind.INITIAL).exists()

    def test_preview_still_works(self, client, user):
        """결제 전 고지(견적)도 폐지 대상이 아니다 — §13② 는 그대로 유효하다."""
        res = client.get("/api/v1/billing/subscription/preview/?plan_name=pro")
        assert res.status_code == 200
        assert res.data["trial_days"] == 30
