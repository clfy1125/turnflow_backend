"""전체 현황 타일 명단 (SNAP-1/2) 테스트.

대상: apps/admin_api/views/snapshot.py + snapshot_rosters.py

가장 중요한 계약은 **타일 숫자와 명단 count 의 항등**이다 (요청서 §공통 ①):
    SNAP-1 count               == snapshot.paying.total
    SNAP-1 ?plan=X count       == snapshot.paying.by_plan[X].count
    SNAP-2 count               == trial_now.will_charge + trial_now.cancelled
    SNAP-2 ?bucket=Y count     == trial_now.Y

주의:
- 파일명이 tests_*.py 라 **경로 명시 실행** 필요.
- 테스트 DB 가 더러울 수 있어 ``clean_slate`` 로 기존 구독을 모수 밖으로 밀어낸다.
- 공유 Redis 라 cache.clear() 금지 — 대시보드/스냅샷 키만 삭제.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_api.roles import ROLE_MARKETING_VIEWER
from apps.billing.models import (
    PaymentHistory,
    PaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)

User = get_user_model()

PAYING_URL = "/api/v1/admin/snapshot/paying/"
TRIAL_URL = "/api/v1/admin/snapshot/trial/"
MKT_URL = "/api/v1/admin/dashboard/marketing/"
CACHE_KEYS = [f"admin:dash:mkt:{p}" for p in ("7d", "30d", "90d", "all")] + [
    "admin:dash:mkt:snapshot"
]
LONG_AGO = timedelta(days=400)


@pytest.fixture(autouse=True)
def _no_dashboard_cache(db):
    cache.delete_many(CACHE_KEYS)
    yield
    cache.delete_many(CACHE_KEYS)


def _mk_user(email=None, staff=False):
    return User.objects.create_user(
        email=email or f"snap-{uuid.uuid4().hex[:8]}@test.com",
        password="Pass1234!",
        is_staff=staff,
        full_name="테스트회원",
    )


@pytest.fixture
def staff_client(db):
    c = APIClient()
    c.force_authenticate(user=_mk_user(email=f"snapstaff-{uuid.uuid4().hex[:6]}@x.com", staff=True))
    return c


@pytest.fixture
def clean_slate(db):
    """기존 구독을 두 모수 밖으로 (free 플랜 + 기간 없음). 결제 이력도 창 밖으로."""
    free, _ = SubscriptionPlan.objects.get_or_create(
        name="free", defaults={"display_name": "무료", "monthly_price": 0, "sort_order": 0}
    )
    UserSubscription.objects.all().update(
        plan=free,
        status=SubscriptionStatus.CANCELLED,
        current_period_end=None,
        cancelled_during_trial_at=None,
        billing_key_issued_at=None,
    )
    PaymentHistory.objects.all().update(status=PaymentStatus.FAILED)


@pytest.fixture
def pro_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="pro", defaults={"display_name": "프로", "monthly_price": 14900, "sort_order": 2}
    )
    if obj.monthly_price != 14900:
        obj.monthly_price = 14900
        obj.save(update_fields=["monthly_price"])
    return obj


@pytest.fixture
def basic_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="basic", defaults={"display_name": "베이직", "monthly_price": 9900, "sort_order": 1}
    )
    return obj


def _mk_paying(plan, *, amount=14900, extra=0, paid_n=1):
    """실결제 이력 있고 현재 ACTIVE 인 회원 1명."""
    user = _mk_user()
    now = timezone.now()
    sub, _ = UserSubscription.objects.update_or_create(
        user=user,
        defaults={
            "plan": plan,
            "status": SubscriptionStatus.ACTIVE,
            "current_period_start": now - timedelta(days=5),
            "current_period_end": now + timedelta(days=25),
            "monthly_amount_snapshot": amount,
            "extra_ig_accounts": extra,
        },
    )
    for i in range(paid_n):
        PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=amount,
            status=PaymentStatus.PAID,
            toss_order_id=f"snap-{uuid.uuid4().hex[:14]}-{i}",
            paid_at=now - timedelta(days=30 * (paid_n - i)),
        )
    return user, sub


def _mk_trial(plan, *, days=30, elapsed=1, card=True, cancelled=False):
    user = _mk_user()
    now = timezone.now()
    start = now - timedelta(days=elapsed)
    sub, _ = UserSubscription.objects.update_or_create(
        user=user,
        defaults={
            "plan": plan,
            "trial_plan": plan,
            "status": (SubscriptionStatus.CANCELLED if cancelled else SubscriptionStatus.TRIALING),
            "current_period_start": start,
            "current_period_end": start + timedelta(days=days),
            "monthly_amount_snapshot": 14900,
            "trial_used_at": start,
            "cancelled_during_trial_at": (start + timedelta(hours=1)) if cancelled else None,
        },
    )
    if card:
        # ⚠️ billing_key_issued_at 만 세우면 안 된다 — 타일은 그 컬럼을 '카드 등록' 축으로
        # 쓰지만 유료전환 동의 판정(consent.py)은 실제 빌링키(_encrypted_*) 유무를 본다.
        # 운영에서는 set_billing_key / clear_billing_key 가 둘을 항상 함께 움직인다.
        sub.set_billing_key("bk_snap_test", card_company="현대", card_number="433012******123*")
        sub.save()
    return user, sub


def _tile(staff_client) -> dict:
    res = staff_client.get(f"{MKT_URL}?period=30d")
    assert res.status_code == 200, res.data
    return res.data["snapshot"]


# ──────────────────────────────────────────────
# SNAP-1 — 실결제 명단
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestPayingRoster:
    def test_count_matches_tile_and_rows_have_user_id(
        self, staff_client, clean_slate, pro_plan, basic_plan
    ):
        _mk_paying(pro_plan)
        _mk_paying(pro_plan)
        _mk_paying(basic_plan, amount=9900)

        tile = _tile(staff_client)
        res = staff_client.get(PAYING_URL)
        assert res.status_code == 200, res.data
        assert res.data["count"] == tile["paying"]["total"] == 3
        assert res.data["as_of"] == tile["as_of"]  # 같은 캐시 항목에서 나온 시각
        assert res.data["is_live"] is False
        for row in res.data["results"]:
            assert row["user_id"]  # 이 값이 없으면 행이 무용지물
            assert row["email"]

    def test_plan_filter_matches_by_plan(self, staff_client, clean_slate, pro_plan, basic_plan):
        _mk_paying(pro_plan)
        _mk_paying(pro_plan)
        _mk_paying(basic_plan, amount=9900)

        tile = _tile(staff_client)
        by_plan = {r["name"]: r["count"] for r in tile["paying"]["by_plan"]}
        for name, expected in by_plan.items():
            res = staff_client.get(f"{PAYING_URL}?plan={name}")
            assert res.data["count"] == expected, name

    def test_monthly_amount_is_server_computed(self, staff_client, clean_slate, pro_plan):
        user, sub = _mk_paying(pro_plan, amount=14900, extra=2)
        res = staff_client.get(f"{PAYING_URL}?search={user.email}")
        row = res.data["results"][0]
        # 스냅샷가 + 추가 IG 계정 (프론트가 재계산하면 갈라지는 값)
        assert row["monthly_amount"] == sub.renewal_amount == 14900 + 2 * 9900
        assert row["extra_ig_accounts"] == 2

    def test_paid_count_excludes_refunds(self, staff_client, clean_slate, pro_plan):
        user, sub = _mk_paying(pro_plan, paid_n=3)
        PaymentHistory.objects.filter(user=user).order_by("paid_at").first().delete()
        # 환불 1건 추가 → status=refunded 라 세지 않는다
        PaymentHistory.objects.create(
            user=user,
            subscription=sub,
            amount=14900,
            status=PaymentStatus.REFUNDED,
            toss_order_id=f"snap-rf-{uuid.uuid4().hex[:12]}",
            paid_at=timezone.now(),
            refunded_at=timezone.now(),
        )
        res = staff_client.get(f"{PAYING_URL}?search={user.email}")
        assert res.data["results"][0]["paid_count"] == 2

    def test_past_due_excluded(self, staff_client, clean_slate, pro_plan):
        user, sub = _mk_paying(pro_plan)
        UserSubscription.objects.filter(pk=sub.pk).update(status=SubscriptionStatus.PAST_DUE)
        tile = _tile(staff_client)
        assert tile["paying"]["total"] == 0
        assert staff_client.get(PAYING_URL).data["count"] == 0

    def test_trial_without_payment_excluded(self, staff_client, clean_slate, pro_plan):
        _mk_trial(pro_plan)
        assert staff_client.get(PAYING_URL).data["count"] == 0

    def test_search_matches_email_and_name(self, staff_client, clean_slate, pro_plan):
        user, _ = _mk_paying(pro_plan)
        assert staff_client.get(f"{PAYING_URL}?search={user.email}").data["count"] == 1
        assert staff_client.get(f"{PAYING_URL}?search=테스트회원").data["count"] >= 1
        assert staff_client.get(f"{PAYING_URL}?search=존재하지않는값zzz").data["count"] == 0

    def test_ordering_whitelist_and_400(self, staff_client, clean_slate, pro_plan):
        _mk_paying(pro_plan)
        for value in ("last_paid_at", "-last_paid_at", "monthly_amount", "-date_joined"):
            assert staff_client.get(f"{PAYING_URL}?ordering={value}").status_code == 200, value
        res = staff_client.get(f"{PAYING_URL}?ordering=-nope")
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "ordering"
        assert "allowed" in res.data["error"]["details"]

    def test_page_size_capped_at_500(self, staff_client, clean_slate, pro_plan):
        _mk_paying(pro_plan)
        res = staff_client.get(f"{PAYING_URL}?page_size=100")
        assert res.status_code == 200
        # 상한 초과 요청은 상한으로 클램프 (오류가 아니다)
        assert staff_client.get(f"{PAYING_URL}?page_size=9999").status_code == 200

    def test_default_ordering_is_recent_payment_first(self, staff_client, clean_slate, pro_plan):
        old, _ = _mk_paying(pro_plan, paid_n=1)
        PaymentHistory.objects.filter(user=old).update(paid_at=timezone.now() - timedelta(days=90))
        new, _ = _mk_paying(pro_plan, paid_n=1)
        PaymentHistory.objects.filter(user=new).update(paid_at=timezone.now())
        rows = staff_client.get(PAYING_URL).data["results"]
        assert rows[0]["user_id"] == new.pk


# ──────────────────────────────────────────────
# SNAP-2 — 체험 명단
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestTrialRoster:
    def test_count_matches_will_charge_plus_cancelled(self, staff_client, clean_slate, pro_plan):
        _mk_trial(pro_plan)  # will_charge
        _mk_trial(pro_plan)  # will_charge
        _mk_trial(pro_plan, cancelled=True)  # cancelled
        _mk_trial(pro_plan, card=False)  # no_card → 제외

        tile = _tile(staff_client)
        trial_now = tile["trial_now"]
        res = staff_client.get(TRIAL_URL)
        assert res.status_code == 200, res.data
        assert res.data["count"] == trial_now["will_charge"] + trial_now["cancelled"] == 3
        # total 은 no_card 를 포함하므로 명단 count 와 다르다 (계약 확인)
        assert trial_now["total"] == 4
        assert res.data["as_of"] == tile["as_of"]

    def test_bucket_filter_matches_tile(self, staff_client, clean_slate, pro_plan):
        _mk_trial(pro_plan)
        _mk_trial(pro_plan)
        _mk_trial(pro_plan, cancelled=True)

        trial_now = _tile(staff_client)["trial_now"]
        for bucket in ("will_charge", "cancelled"):
            res = staff_client.get(f"{TRIAL_URL}?bucket={bucket}")
            assert res.data["count"] == trial_now[bucket], bucket
            assert {r["bucket"] for r in res.data["results"]} <= {bucket}

    def test_bad_bucket_is_400(self, staff_client, clean_slate, pro_plan):
        res = staff_client.get(f"{TRIAL_URL}?bucket=nope")
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "bucket"
        assert res.data["error"]["details"]["allowed"] == ["will_charge", "cancelled"]

    def test_cancelled_has_no_expected_amount(self, staff_client, clean_slate, pro_plan):
        _mk_trial(pro_plan, cancelled=True)
        row = staff_client.get(f"{TRIAL_URL}?bucket=cancelled").data["results"][0]
        assert row["expected_amount"] is None
        assert row["bucket"] == "cancelled"

    def test_will_charge_has_amount_and_card(self, staff_client, clean_slate, pro_plan):
        _mk_trial(pro_plan)
        row = staff_client.get(f"{TRIAL_URL}?bucket=will_charge").data["results"][0]
        assert row["expected_amount"] == 14900
        assert row["card_company"] == "현대"
        assert row["card_number_masked"] == "433012******123*"
        assert row["trial_total_days"] == 30

    def test_consent_required_follows_policy_flag(
        self, staff_client, clean_slate, pro_plan, settings
    ):
        """`conversion_consent_required` 는 정책 플래그를 그대로 반영한다.

        2026-08-10 제품 결정으로 동의는 결제 화면 1회 → 기본값에서는 44일 쿠폰 체험자도
        **false**(운영이 따로 안내할 대상이 없다). 플래그를 켜면 다시 true 가 된다.
        """
        _mk_trial(pro_plan, days=44, elapsed=20)

        row = staff_client.get(TRIAL_URL).data["results"][0]
        assert row["trial_total_days"] == 44
        assert row["conversion_consent_required"] is False  # 기본 정책

        settings.CONVERSION_SECOND_CONSENT_ENABLED = True
        row = staff_client.get(TRIAL_URL).data["results"][0]
        assert row["conversion_consent_required"] is True

    def test_30day_trial_not_flagged(self, staff_client, clean_slate, pro_plan):
        _mk_trial(pro_plan, days=30, elapsed=25)
        row = staff_client.get(TRIAL_URL).data["results"][0]
        assert row["conversion_consent_required"] is False

    def test_default_ordering_is_ending_soonest(self, staff_client, clean_slate, pro_plan):
        _, later = _mk_trial(pro_plan, days=44, elapsed=1)
        soon, _ = _mk_trial(pro_plan, days=30, elapsed=28)
        rows = staff_client.get(TRIAL_URL).data["results"]
        assert rows[0]["user_id"] == soon.pk

    def test_ordering_whitelist_and_400(self, staff_client, clean_slate, pro_plan):
        _mk_trial(pro_plan)
        for value in ("trial_ends_at", "-trial_ends_at", "trial_started_at", "-date_joined"):
            assert staff_client.get(f"{TRIAL_URL}?ordering={value}").status_code == 200, value
        res = staff_client.get(f"{TRIAL_URL}?ordering=-last_sent_at")
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "ordering"

    def test_expired_trial_excluded(self, staff_client, clean_slate, pro_plan):
        """기간이 이미 지난 체험은 '지금 체험 중' 이 아니다."""
        _mk_trial(pro_plan, days=30, elapsed=31)
        assert staff_client.get(TRIAL_URL).data["count"] == 0


# ──────────────────────────────────────────────
# 권한 — 최고 관리자 전용
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestSnapshotRosterPermissions:
    def test_anonymous_is_401(self, db):
        assert APIClient().get(PAYING_URL).status_code == 401
        assert APIClient().get(TRIAL_URL).status_code == 401

    def test_non_staff_is_403(self, db):
        c = APIClient()
        c.force_authenticate(user=_mk_user())
        assert c.get(PAYING_URL).status_code == 403
        assert c.get(TRIAL_URL).status_code == 403

    def test_marketing_viewer_is_403_by_middleware(self, db):
        """RBAC 화이트리스트에 없는 새 경로 → 미들웨어가 deny-by-default 로 막는다.

        ⚠️ 세션 로그인(django.test.Client)으로 확인해야 한다 — APIClient.force_authenticate
        는 뷰 단계에서만 사용자를 주입하므로 **미들웨어 시점엔 익명**이고, 미들웨어가 아무
        판단도 하지 않아(권한을 넓히지 않는 설계) 통과한다. tests_rbac_and_spam.py 와 동일 패턴.
        """
        group, _ = Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)
        user = _mk_user(staff=True)
        user.groups.add(group)
        c = Client()
        c.force_login(user)
        for url in (PAYING_URL, TRIAL_URL):
            res = c.get(url)
            assert res.status_code == 403, url
            assert res.json()["error"]["details"]["code"] == "section_forbidden"

    def test_full_admin_passes_middleware(self, db):
        """대조군 — full 역할은 세션 로그인으로도 200 (위 403 이 경로 문제가 아님을 확인)."""
        c = Client()
        c.force_login(_mk_user(staff=True))
        assert c.get(PAYING_URL).status_code == 200
        assert c.get(TRIAL_URL).status_code == 200
