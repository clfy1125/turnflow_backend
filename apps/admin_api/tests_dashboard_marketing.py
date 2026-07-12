"""어드민 마케팅 대시보드(GET /api/v1/admin/dashboard/marketing/) 테스트.

대상: apps/admin_api/views/dashboard_marketing.py (IsAdminUser).

주의:
- 파일명이 tests_*.py 라 **경로 명시 실행** 필요:
  ``pytest apps/admin_api/tests_dashboard_marketing.py``.
- 테스트 DB 가 더러울 수 있어 ``clean_slate`` 로 기존 행을 집계 창 밖으로 이동한다.
- 어트리뷰션(apps.analytics)은 병렬 워크스트림 — 미탑재 환경에서도 나머지 테스트가
  돌도록 채널 테스트는 skipif 가드.
- 공유 Redis 라 cache.clear() 금지 — 대시보드 키만 삭제.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import (
    PaymentHistory,
    PaymentStatus,
    ReferralCode,
    ReferralRedemption,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog, SpamCommentLog
from apps.pages.models import BlockClick, Page, PageView
from apps.workspace.models import Workspace

# 어트리뷰션 앱은 병렬 워크스트림 — 뷰와 동일하게 가드 (미탑재 시 채널 테스트 skip)
try:
    from apps.analytics.models import CheckoutEvent, LandingVisit, SignupAttribution

    HAS_ANALYTICS = True
except (ImportError, RuntimeError):
    CheckoutEvent = None
    LandingVisit = None
    SignupAttribution = None
    HAS_ANALYTICS = False

requires_analytics = pytest.mark.skipif(
    not HAS_ANALYTICS, reason="apps.analytics 미탑재 — attribution_available=false 강등 경로"
)

User = get_user_model()

URL = "/api/v1/admin/dashboard/marketing/"
CACHE_KEYS = [f"admin:dash:mkt:{p}" for p in ("7d", "30d", "90d")]
LONG_AGO = timedelta(days=400)

# ─── 공통 픽스처 (tests_subscription.py 패턴) ─────────────────────────


@pytest.fixture
def client():
    return APIClient()


def _mk_user(email=None, joined=None, staff=False):
    """유저 생성 — joined 지정 시 date_joined 를 강제 (코호트 제어)."""
    user = User.objects.create_user(
        email=email or f"u-{uuid.uuid4().hex[:8]}@test.com",
        password="Pass1234!",
        is_staff=staff,
    )
    # 지정 없으면 코호트 오염 방지를 위해 기본으로 창 밖으로 밀어낸다
    target = joined if joined is not None else timezone.now() - LONG_AGO
    User.objects.filter(pk=user.pk).update(date_joined=target)
    user.refresh_from_db()
    return user


@pytest.fixture
def staff_user(db):
    return _mk_user(email="staff-mkt@example.com", staff=True)


@pytest.fixture
def regular_user(db):
    return _mk_user(email="regular-mkt@example.com")


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def regular_client(client, regular_user):
    client.force_authenticate(user=regular_user)
    return client


@pytest.fixture(autouse=True)
def _no_dashboard_cache(db):
    """캐시 키만 정리 — 공유 Redis 라 cache.clear() 금지."""
    cache.delete_many(CACHE_KEYS)
    yield
    cache.delete_many(CACHE_KEYS)


@pytest.fixture
def clean_slate(db):
    """더러운 테스트 DB 방어 — 기존 행을 코호트/기간/월 창 밖으로 이동 (트랜잭션 내)."""
    long_ago = timezone.now() - LONG_AGO
    User.objects.all().update(date_joined=long_ago)
    Page.objects.all().update(created_at=long_ago)
    PageView.objects.all().update(viewed_at=long_ago)
    BlockClick.objects.all().update(clicked_at=long_ago)
    AutoDMCampaign.objects.all().update(created_at=long_ago)
    IGAccountConnection.objects.all().update(
        created_at=long_ago, status=IGAccountConnection.Status.REVOKED, is_active=True
    )
    SentDMLog.objects.all().update(created_at=long_ago)
    SpamCommentLog.objects.all().update(created_at=long_ago)
    PaymentHistory.objects.all().update(created_at=long_ago)
    PaymentHistory.objects.filter(paid_at__isnull=False).update(paid_at=long_ago)
    ReferralRedemption.objects.all().update(trial_started_at=long_ago)
    UserSubscription.objects.all().update(
        status=SubscriptionStatus.CANCELLED, trial_used_at=None, extra_ig_accounts=0
    )
    if HAS_ANALYTICS:
        LandingVisit.objects.all().update(created_at=long_ago)


@pytest.fixture
def free_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="free", defaults={"display_name": "무료", "monthly_price": 0, "sort_order": 0}
    )
    # 업셀 쿼터 비율 계산이 결정적이도록 한도를 고정 (시드 features 와 무관하게)
    obj.features = {**(obj.features or {}), "dm_monthly_limit": 200}
    obj.save(update_fields=["features"])
    return obj


@pytest.fixture
def pro_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="pro", defaults={"display_name": "프로", "monthly_price": 14900, "sort_order": 2}
    )
    # 시드된 플랜이 이미 있어도 MRR 폴백 단언이 결정적이도록 판매가 고정 (트랜잭션 내)
    if obj.monthly_price != 14900:
        obj.monthly_price = 14900
        obj.save(update_fields=["monthly_price"])
    return obj


# ─── 헬퍼 팩토리 ──────────────────────────────────────────────────────


def _mk_ws(owner):
    return Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)


def _mk_conn(owner, active=True):
    return IGAccountConnection.objects.create(
        workspace=_mk_ws(owner),
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=f"u_{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=(
            IGAccountConnection.Status.ACTIVE if active else IGAccountConnection.Status.REVOKED
        ),
        is_active=True,
    )


def _mk_campaign(conn):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        name="camp",
        message_template="hi",
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _mk_page(user, public=True):
    return Page.objects.create(user=user, slug=f"p-{uuid.uuid4().hex[:8]}", is_public=public)


def _mk_paid_payment(user, paid_at, amount=14900):
    return PaymentHistory.objects.create(
        user=user, amount=amount, status=PaymentStatus.PAID, paid_at=paid_at
    )


def _mk_quota_dms(campaign, recipient_ids):
    """quota 소진 상태(ACCEPTED) DM 로그 벌크 생성 — (캠페인 × 수신자) 쌍 카운트 검증용.

    recipient_ids 에 같은 값이 반복되면 '같은 쌍에 여러 로그' 케이스가 된다.
    """
    return SentDMLog.objects.bulk_create(
        SentDMLog(
            campaign=campaign,
            comment_id=f"c_{uuid.uuid4().hex[:10]}",
            recipient_user_id=rid,
            recipient_username="",
            message_sent="x",
            status=SentDMLog.Status.ACCEPTED,
            idempotency_key=uuid.uuid4().hex,
        )
        for rid in recipient_ids
    )


def _variant(res, channel="all"):
    """funnel.variants[channel] (분기 퍼널 구조 {head, branches, conversion})."""
    return res.data["funnel"]["variants"][channel]


def _node(res, key, channel="all"):
    """분기 퍼널 노드 1개를 key 로 조회 (head + 모든 branch.steps + conversion 탐색)."""
    v = _variant(res, channel)
    nodes = list(v["head"])
    for br in v["branches"]:
        nodes.extend(br["steps"])
    nodes.append(v["conversion"])
    return next(n for n in nodes if n["key"] == key)


def _branch(res, branch_key, channel="all"):
    return next(b for b in _variant(res, channel)["branches"] if b["key"] == branch_key)


# ─── 권한 / period 파라미터 ──────────────────────────────────────────


class TestPermissionsAndParams:
    def test_anonymous_401(self, client, db):
        assert client.get(URL).status_code == 401

    def test_non_staff_403(self, regular_client):
        assert regular_client.get(URL).status_code == 403

    def test_default_period_30d(self, staff_client):
        res = staff_client.get(URL)
        assert res.status_code == 200
        assert res.data["period"] == "30d"

    @pytest.mark.parametrize("period", ["7d", "30d", "90d"])
    def test_valid_periods(self, staff_client, period):
        res = staff_client.get(URL, {"period": period})
        assert res.status_code == 200
        assert res.data["period"] == period

    def test_invalid_period_400_project_error_format(self, staff_client):
        res = staff_client.get(URL, {"period": "1y"})
        assert res.status_code == 400
        assert res.data["success"] is False
        assert res.data["error"]["code"] == 400
        assert res.data["error"]["details"]["allowed"] == ["7d", "30d", "90d"]


# ─── 빈 상태 ─────────────────────────────────────────────────────────


class TestEmptyState:
    def test_zeros_and_null_deltas(self, staff_client, clean_slate):
        res = staff_client.get(URL)
        assert res.status_code == 200

        kpis = res.data["kpis"]
        for key in (
            "visits",
            "unique_visitors",
            "signups",
            "ig_connected",
            "first_page_published",
            "first_dm_campaign",
            "paid_conversions",
        ):
            assert kpis[key]["current"] == 0, key
            assert kpis[key]["previous"] == 0, key
            assert kpis[key]["delta_pct"] is None, key  # previous==0 → null

        assert kpis["mrr"]["current"] == 0
        assert kpis["mrr"]["previous"] is None
        assert kpis["mrr"]["currency"] == "KRW"

        funnel = res.data["funnel"]
        assert funnel["semantics"] == "signup_cohort"
        # 어트리뷰션 유무와 무관하게 all variant 는 항상 존재
        assert funnel["available_channels"][0] == {"value": "all", "label": "전체 채널"}
        v = funnel["variants"]["all"]
        assert [n["key"] for n in v["head"]] == ["visit", "signup"]
        assert [b["key"] for b in v["branches"]] == ["dm", "biolink"]
        assert [n["key"] for n in _branch(res, "dm")["steps"]] == ["ig_connected", "dm_campaign"]
        assert [n["key"] for n in _branch(res, "biolink")["steps"]] == [
            "page_created",
            "page_published",
        ]
        assert v["conversion"]["key"] == "paid"
        for key in (
            "visit",
            "signup",
            "ig_connected",
            "dm_campaign",
            "page_created",
            "page_published",
            "paid",
        ):
            assert _node(res, key)["count"] == 0  # ZeroDivisionError 없이 0/None
        # 빈 상태: visits 0 → signup.rate null
        assert _node(res, "signup")["rate"] is None

        assert res.data["upsell_candidates"] == []
        assert res.data["channels"]["rows"] == []
        assert res.data["mrr_breakdown"]["total"] == 0
        assert isinstance(res.data["attribution_available"], bool)

    def test_attribution_flag_matches_app_presence(self, staff_client, clean_slate):
        res = staff_client.get(URL)
        assert res.data["attribution_available"] is HAS_ANALYTICS


# ─── 코호트 퍼널 ─────────────────────────────────────────────────────


class TestCohortFunnel:
    def test_parallel_branches_nonlinear(self, staff_client, clean_slate):
        now = timezone.now()
        in_cohort = now - timedelta(days=5)

        # 공개 페이지만 (IG 연동 없음) — 비선형 요건: biolink 분기에만, dm 분기엔 미포함
        u_page = _mk_user(joined=in_cohort)
        _mk_page(u_page, public=True)
        # 캠페인만 (연동 필요 → ig_connected 에도 포함)
        u_camp = _mk_user(joined=in_cohort)
        _mk_campaign(_mk_conn(u_camp))
        # 둘 다
        u_both = _mk_user(joined=in_cohort)
        _mk_page(u_both, public=True)
        _mk_campaign(_mk_conn(u_both))
        # 아무것도 안 함
        _mk_user(joined=in_cohort)

        res = staff_client.get(URL)
        assert _node(res, "signup")["count"] == 4
        ig = _node(res, "ig_connected")
        assert ig["count"] == 2  # u_camp, u_both
        # ig.rate = ig/signups
        assert ig["rate"] == 0.5
        assert ig["rate_of"] == "signup"
        assert ig["formula"] == "IG 연동 수 ÷ 가입 수 × 100"

        # dm 분기: IG-less 페이지 유저는 미포함 (u_camp, u_both 만)
        dm = _node(res, "dm_campaign")
        assert dm["count"] == 2
        assert dm["rate"] == 1.0  # dm/ig = 2/2
        assert dm["rate_of"] == "ig_connected"

        # biolink 분기: 페이지 생성 → 페이지 공개 2단계. 공개 유저 (u_page, u_both) — IG 무관
        created = _node(res, "page_created")
        assert created["count"] == 2  # u_page, u_both (둘 다 공개 페이지=생성 포함)
        assert created["rate"] == 0.5  # created/signups = 2/4
        assert created["rate_of"] == "signup"
        assert created["formula"] == "페이지 생성 수 ÷ 가입 수 × 100"

        page = _node(res, "page_published")
        assert page["count"] == 2
        assert page["rate"] == 1.0  # published/created = 2/2
        assert page["rate_of"] == "page_created"
        assert page["formula"] == "페이지 공개 수 ÷ 페이지 생성 수 × 100"

    def test_paid_rate_of_signups(self, staff_client, clean_slate):
        now = timezone.now()
        in_cohort = now - timedelta(days=5)
        for _ in range(4):
            _mk_user(joined=in_cohort)
        u_paid = _mk_user(joined=in_cohort)
        _mk_paid_payment(u_paid, paid_at=now - timedelta(days=2))

        res = staff_client.get(URL)
        paid = _node(res, "paid")
        assert paid["count"] == 1
        assert paid["rate"] == 0.2  # paid/signups = 1/5
        assert paid["rate_of"] == "signup"
        assert paid["formula"] == "유료 전환 수 ÷ 가입 수 × 100"

    def test_private_page_created_but_not_published(self, staff_client, clean_slate):
        # 비공개 페이지 = '생성'에는 포함, '공개'에는 미포함 (생성→공개 2단계 검증)
        u = _mk_user(joined=timezone.now() - timedelta(days=3))
        _mk_page(u, public=False)
        res = staff_client.get(URL)
        assert _node(res, "page_created")["count"] == 1
        assert _node(res, "page_published")["count"] == 0

    def test_cohort_boundary(self, staff_client, clean_slate):
        now = timezone.now()
        _mk_user(joined=now - timedelta(days=30, minutes=1))  # start 1분 전 — current 제외
        _mk_user(joined=now - timedelta(days=45))  # previous 기간
        _mk_user(joined=now - timedelta(days=1))  # current 기간

        res = staff_client.get(URL)  # period=30d
        assert res.data["kpis"]["signups"]["current"] == 1
        # start 직전 유저 + 45일 전 유저 → previous 2명
        assert res.data["kpis"]["signups"]["previous"] == 2
        assert _node(res, "signup")["count"] == 1


# ─── paid_conversions (첫 PAID 결제 기준) ────────────────────────────


class TestPaidConversions:
    def test_first_paid_in_period_counted_once(self, staff_client, clean_slate):
        now = timezone.now()
        u = _mk_user()
        _mk_paid_payment(u, paid_at=now - timedelta(days=3))
        _mk_paid_payment(u, paid_at=now - timedelta(days=1))  # 같은 유저 2번째 — 중복 금지

        res = staff_client.get(URL)
        assert res.data["kpis"]["paid_conversions"]["current"] == 1

    def test_first_paid_outside_period_not_counted(self, staff_client, clean_slate):
        now = timezone.now()
        u = _mk_user()
        _mk_paid_payment(u, paid_at=now - LONG_AGO)  # 첫 결제가 기간 밖
        _mk_paid_payment(u, paid_at=now - timedelta(days=1))  # 재결제는 기간 내여도 제외

        res = staff_client.get(URL)
        assert res.data["kpis"]["paid_conversions"]["current"] == 0

    def test_pending_failed_not_counted(self, staff_client, clean_slate):
        now = timezone.now()
        u = _mk_user()
        PaymentHistory.objects.create(
            user=u, amount=100, status=PaymentStatus.PENDING, paid_at=now - timedelta(days=1)
        )
        PaymentHistory.objects.create(
            user=u, amount=100, status=PaymentStatus.FAILED, paid_at=now - timedelta(days=1)
        )
        res = staff_client.get(URL)
        assert res.data["kpis"]["paid_conversions"]["current"] == 0

    def test_refund_nulled_pro_activated_at_still_counted(
        self, staff_client, clean_slate, pro_plan
    ):
        # pro_activated_at 이 환불로 null 이어도 PAID 이력 기준이라 카운트 (소스 선택 증명)
        now = timezone.now()
        u = _mk_user()
        UserSubscription.objects.create(
            user=u, plan=pro_plan, status=SubscriptionStatus.ACTIVE, pro_activated_at=None
        )
        _mk_paid_payment(u, paid_at=now - timedelta(days=2))

        res = staff_client.get(URL)
        assert res.data["kpis"]["paid_conversions"]["current"] == 1


# ─── MRR ─────────────────────────────────────────────────────────────


class TestMrr:
    def test_snapshot_extra_accounts_and_fallback(self, staff_client, clean_slate, pro_plan):
        # 스냅샷 12900 + 추가계정 2개(19800)
        u1 = _mk_user()
        UserSubscription.objects.create(
            user=u1,
            plan=pro_plan,
            status=SubscriptionStatus.ACTIVE,
            monthly_amount_snapshot=12900,
            extra_ig_accounts=2,
        )
        # 스냅샷 null → plan.monthly_price(14900) 폴백
        u2 = _mk_user()
        UserSubscription.objects.create(user=u2, plan=pro_plan, status=SubscriptionStatus.ACTIVE)

        res = staff_client.get(URL)
        mrr = res.data["mrr_breakdown"]
        assert mrr["total"] == 12900 + 14900 + 2 * 9900
        pro_row = next(r for r in mrr["by_plan"] if r["name"] == "pro")
        assert pro_row["subscribers"] == 2
        assert pro_row["mrr"] == 12900 + 14900
        assert mrr["extra_ig_accounts"] == {"count": 2, "unit_price": 9900, "mrr": 19800}
        assert res.data["kpis"]["mrr"]["current"] == mrr["total"]
        assert res.data["kpis"]["mrr"]["previous"] is None

    def test_trialing_free_and_admin_excluded(self, staff_client, clean_slate, free_plan, pro_plan):
        UserSubscription.objects.create(
            user=_mk_user(),
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            monthly_amount_snapshot=14900,
        )
        UserSubscription.objects.create(
            user=_mk_user(), plan=free_plan, status=SubscriptionStatus.ACTIVE
        )
        # admin 플랜은 운영용 내부 계정 — ACTIVE + 유료 가격이어도 MRR 에서 제외
        admin_plan, _ = SubscriptionPlan.objects.get_or_create(
            name="admin",
            defaults={"display_name": "관리자", "monthly_price": 18900, "sort_order": 9},
        )
        UserSubscription.objects.create(
            user=_mk_user(),
            plan=admin_plan,
            status=SubscriptionStatus.ACTIVE,
            monthly_amount_snapshot=18900,
        )
        res = staff_client.get(URL)
        mrr = res.data["mrr_breakdown"]
        assert mrr["total"] == 0
        assert all(r["name"] != "admin" for r in mrr["by_plan"])


# ─── 플랜 분포 ───────────────────────────────────────────────────────


class TestPlanDistribution:
    def test_status_columns(self, staff_client, clean_slate, db):
        plan = SubscriptionPlan.objects.create(
            name=f"testplan-{uuid.uuid4().hex[:6]}",
            display_name="테스트플랜",
            monthly_price=1000,
            sort_order=99,
        )
        UserSubscription.objects.create(
            user=_mk_user(), plan=plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.create(
            user=_mk_user(), plan=plan, status=SubscriptionStatus.TRIALING
        )
        UserSubscription.objects.create(
            user=_mk_user(), plan=plan, status=SubscriptionStatus.PAST_DUE
        )

        res = staff_client.get(URL)
        row = next(r for r in res.data["plan_distribution"] if r["name"] == plan.name)
        assert row["display_name"] == "테스트플랜"
        assert row["total"] == 3
        assert row["active"] == 1
        assert row["trialing"] == 1
        assert row["past_due"] == 1
        assert row["cancelled"] == 0

    def test_admin_plan_excluded(self, staff_client, clean_slate, db):
        admin_plan, _ = SubscriptionPlan.objects.get_or_create(
            name="admin",
            defaults={"display_name": "관리자", "monthly_price": 18900, "sort_order": 9},
        )
        UserSubscription.objects.create(
            user=_mk_user(), plan=admin_plan, status=SubscriptionStatus.ACTIVE
        )
        res = staff_client.get(URL)
        assert all(r["name"] != "admin" for r in res.data["plan_distribution"])


# ─── 채널 (어트리뷰션 필요) ──────────────────────────────────────────


@requires_analytics
class TestChannels:
    def test_attributed_unknown_and_visit_rates(self, staff_client, clean_slate):
        now = timezone.now()
        in_cohort = now - timedelta(days=5)

        attributed = _mk_user(joined=in_cohort)
        SignupAttribution.objects.create(
            user=attributed, channel="instagram_organic", signup_kind="email"
        )
        _mk_page(attributed, public=True)  # 페이지 생성 + 공개 (바이오링크 갈래)
        _mk_user(joined=in_cohort)  # 어트리뷰션 없음 → unknown

        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="instagram_organic")
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="instagram_organic")

        res = staff_client.get(URL)
        assert res.data["attribution_available"] is True
        rows = {r["channel"]: r for r in res.data["channels"]["rows"]}

        ig = rows["instagram_organic"]
        assert ig["visits"] == 2
        assert ig["signups"] == 1
        assert ig["signup_rate"] == 0.5
        # 비순차 분기 컬럼: 페이지 생성/공개는 1, IG·DM 갈래는 0
        assert ig["page_created"] == 1
        assert ig["page_published"] == 1
        assert ig["ig_connected"] == 0
        assert ig["dm_campaign"] == 0

        unknown = rows["unknown"]
        assert unknown["visits"] == 0
        assert unknown["signups"] == 1
        assert unknown["signup_rate"] is None  # 방문 0 → null

    def test_referral_overlay_wins_over_stored_channel(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        u = _mk_user(joined=now - timedelta(days=5))
        SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email")
        code = ReferralCode.objects.create(code=f"CR-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now - timedelta(days=4),
            trial_ends_at=now + timedelta(days=26),
        )

        res = staff_client.get(URL)
        rows = {r["channel"]: r for r in res.data["channels"]["rows"]}
        assert rows["referral"]["signups"] == 1
        assert "meta_ads" not in rows  # 저장 채널은 오버레이로 대체

    def test_kpi_visits_counted(self, staff_client, clean_slate):
        vid = uuid.uuid4()
        LandingVisit.objects.create(visitor_id=vid, channel="direct")
        LandingVisit.objects.create(visitor_id=vid, channel="direct")  # 같은 방문자 재방문

        res = staff_client.get(URL)
        assert res.data["kpis"]["visits"]["current"] == 2
        assert res.data["kpis"]["unique_visitors"]["current"] == 1
        assert _node(res, "visit")["count"] == 2

    def test_channel_variant_matches_available_channels(self, staff_client, clean_slate):
        now = timezone.now()
        in_cohort = now - timedelta(days=5)
        # instagram_organic 채널로 2명 가입 (1명 페이지 공개)
        for i in range(2):
            u = _mk_user(joined=in_cohort)
            SignupAttribution.objects.create(
                user=u, channel="instagram_organic", signup_kind="email"
            )
            if i == 0:
                _mk_page(u, public=True)
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="instagram_organic")

        res = staff_client.get(URL)
        funnel = res.data["funnel"]
        # available_channels 에 해당 채널 (all 다음), variants 키와 일치
        values = [c["value"] for c in funnel["available_channels"]]
        assert values[0] == "all"
        assert "instagram_organic" in values
        assert set(values) == set(funnel["variants"].keys())
        # 채널 라벨 = CHANNEL_LABELS
        ig_opt = next(c for c in funnel["available_channels"] if c["value"] == "instagram_organic")
        assert ig_opt["label"] == "인스타 오가닉"
        # 채널 variant 노드 카운트
        assert _node(res, "signup", channel="instagram_organic")["count"] == 2
        assert _node(res, "page_published", channel="instagram_organic")["count"] == 1
        # signup.rate = signups/visits = 2/1 (분모=해당 채널 visits)
        assert _node(res, "signup", channel="instagram_organic")["rate"] == 2.0


# ─── 업셀 후보 ───────────────────────────────────────────────────────


class TestUpsellCandidates:
    def _mk_owner_with_plan(self, plan):
        owner = _mk_user()
        UserSubscription.objects.create(user=owner, plan=plan, status=SubscriptionStatus.ACTIVE)
        return owner

    def test_dm_quota_distinct_pairs_and_80pct(self, staff_client, clean_slate, free_plan):
        owner = self._mk_owner_with_plan(free_plan)
        camp = _mk_campaign(_mk_conn(owner))
        # 고유 수신자 168명 + 첫 수신자에게 중복 발송 1건(같은 캠페인×수신자 쌍)
        _mk_quota_dms(camp, [f"rcpt_{i}" for i in range(168)] + ["rcpt_0"])

        res = staff_client.get(URL)
        cands = res.data["upsell_candidates"]
        assert len(cands) == 1
        cand = cands[0]
        assert cand["user_id"] == owner.id
        assert cand["plan"] == "free"
        assert "dm_quota_80pct" in cand["reasons"]
        assert cand["metrics"]["dm_used_month"] == 168  # 169 로그 → 168 고유쌍
        assert cand["metrics"]["dm_limit"] == 200
        assert cand["metrics"]["dm_usage_ratio"] == 0.84
        assert cand["link"] == {"page": f"/users/{owner.id}", "params": {}}

    def test_pro_owner_excluded(self, staff_client, clean_slate, pro_plan):
        owner = self._mk_owner_with_plan(pro_plan)
        camp = _mk_campaign(_mk_conn(owner))
        _mk_quota_dms(camp, [f"rcpt_{i}" for i in range(30)])
        # 복수 IG 연동도 pro 라 후보 진입 금지
        _mk_conn(owner)

        res = staff_client.get(URL)
        assert res.data["upsell_candidates"] == []

    def test_multi_ig_and_ordering_by_score(self, staff_client, clean_slate, free_plan):
        # 후보 A: 쿼터 80%+ (score 3)
        heavy = self._mk_owner_with_plan(free_plan)
        camp = _mk_campaign(_mk_conn(heavy))
        _mk_quota_dms(camp, [f"h_{i}" for i in range(160)])
        # 후보 B: 활성 IG 2개 (score 2)
        multi = self._mk_owner_with_plan(free_plan)
        _mk_conn(multi)
        _mk_conn(multi)

        res = staff_client.get(URL)
        cands = res.data["upsell_candidates"]
        assert [c["user_id"] for c in cands] == [heavy.id, multi.id]
        assert cands[0]["score"] == 3
        assert cands[1]["score"] == 2
        assert cands[1]["reasons"] == ["multiple_ig_connections"]
        assert cands[1]["metrics"]["active_ig_connections"] == 2

    def test_capped_at_10(self, staff_client, clean_slate, free_plan):
        for _ in range(12):
            owner = self._mk_owner_with_plan(free_plan)
            _mk_conn(owner)
            _mk_conn(owner)
        res = staff_client.get(URL)
        assert len(res.data["upsell_candidates"]) == 10


# ─── 트라이얼 / 기능 통계 ────────────────────────────────────────────


class TestTrialsAndFeatureStats:
    def test_trials_started_and_referral_conversion(
        self, staff_client, clean_slate, free_plan, pro_plan
    ):
        now = timezone.now()
        code = ReferralCode.objects.create(code=f"TR-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)
        # 레퍼럴 트라이얼 2건 (1건 전환)
        for converted in (True, False):
            u = _mk_user()
            ReferralRedemption.objects.create(
                user=u,
                referral_code=code,
                trial_started_at=now - timedelta(days=2),
                trial_ends_at=now + timedelta(days=28),
                converted_to_paid=converted,
            )
        # 카드등록 트라이얼 1건 — started 에만 포함 (전환 미추적)
        card_user = _mk_user()
        UserSubscription.objects.create(
            user=card_user,
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            trial_used_at=now - timedelta(days=1),
        )

        res = staff_client.get(URL)
        trials = res.data["feature_stats"]["trials"]
        assert trials["started"]["current"] == 3  # 레퍼럴 2 + 카드 1
        assert trials["converted"] == 1
        assert trials["conversion_rate"] == 0.5  # 레퍼럴 코호트(2) 기준

        codes = res.data["channels"]["referral_codes"]
        row = next(r for r in codes if r["code"] == code.code)
        assert row["redemptions"] == 2
        assert row["converted"] == 1
        assert row["conversion_rate"] == 0.5

    def test_biolink_views_clicks_and_top_pages(self, staff_client, clean_slate):
        u = _mk_user()
        page = _mk_page(u, public=True)
        for _ in range(3):
            PageView.objects.create(page=page)

        res = staff_client.get(URL)
        biolink = res.data["feature_stats"]["biolink"]
        assert biolink["views"]["current"] == 3
        assert biolink["new_public_pages"]["current"] == 1
        assert biolink["top_pages"][0]["slug"] == page.slug
        assert biolink["top_pages"][0]["views"] == 3
        assert biolink["ctr"] == 0.0  # 클릭 0 — ZeroDivision 없이 0.0

    def test_dm_feature_delivery_rate(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn(_mk_user()))
        for i in range(9):
            SentDMLog.objects.create(
                campaign=camp,
                comment_id=f"c_{i}",
                recipient_user_id=f"r_{i}",
                recipient_username="",
                message_sent="x",
                status=SentDMLog.Status.DELIVERED,
                idempotency_key=uuid.uuid4().hex,
            )
        SentDMLog.objects.create(
            campaign=camp,
            comment_id="c_x",
            recipient_user_id="r_x",
            recipient_username="",
            message_sent="x",
            status=SentDMLog.Status.ACCEPTED,
            idempotency_key=uuid.uuid4().hex,
        )

        res = staff_client.get(URL)
        dm = res.data["feature_stats"]["dm"]
        assert dm["requested"]["current"] == 10
        assert dm["delivered"]["current"] == 9
        assert dm["delivery_rate"] == 0.9
        assert dm["campaigns_created"]["current"] == 1


# ─── 캐싱 ────────────────────────────────────────────────────────────


class TestCaching:
    def test_second_call_served_from_cache(self, staff_client, clean_slate):
        first = staff_client.get(URL)
        _mk_user(joined=timezone.now() - timedelta(days=1))

        second = staff_client.get(URL)  # 300s TTL 내 — 캐시 히트
        assert second.data["generated_at"] == first.data["generated_at"]
        assert second.data["kpis"]["signups"]["current"] == 0

        cache.delete("admin:dash:mkt:30d")
        third = staff_client.get(URL)
        assert third.data["kpis"]["signups"]["current"] == 1

    def test_periods_cached_separately(self, staff_client, clean_slate):
        res_30 = staff_client.get(URL, {"period": "30d"})
        res_7 = staff_client.get(URL, {"period": "7d"})
        assert res_30.data["period"] == "30d"
        assert res_7.data["period"] == "7d"


# ─── trends (일별 추이) ──────────────────────────────────────────────


class TestTrends:
    def test_trends_present_for_presets(self, staff_client, clean_slate):
        res = staff_client.get(URL, {"period": "7d"})
        trends = res.data["trends"]
        assert trends["granularity"] == "day"
        # 7d 는 현재 시각 기준 [now-7d, now) — 로컬 날짜 zero-fill: 7 또는 8 버킷
        assert len(trends["buckets"]) in (7, 8)
        # 각 버킷 키 계약
        b = trends["buckets"][0]
        assert set(b) == {
            "date",
            "signups",
            "paid",
            "dm_delivered",
            "page_views",
            "page_clicks",
            "visits",
        }

    def test_trends_buckets_zero_filled_length_equals_day_count(self, staff_client, clean_slate):
        # 커스텀 6/1~6/30 = 30일 → 30 버킷 (전부 zero-fill 포함)
        start, end = "2026-06-01", "2026-06-30"
        cache.delete(f"admin:dash:mkt:custom:{start}:{end}")
        res = staff_client.get(URL, {"start": start, "end": end})
        buckets = res.data["trends"]["buckets"]
        assert len(buckets) == 30
        # 날짜 오름차순 연속
        dates = [b["date"] for b in buckets]
        assert dates == sorted(dates)
        assert dates[0] == "2026-06-01"
        assert dates[-1] == "2026-06-30"

    def test_signups_land_in_correct_local_day_bucket(self, staff_client, clean_slate):
        # 특정 로컬 날짜에 N명 가입 시드 → 그 date 버킷 signups==N
        tz = timezone.get_current_timezone()
        target = timezone.make_aware(timezone.datetime(2026, 6, 15, 10, 0, 0), tz)  # 로컬 6/15 오전
        n = 3
        for _ in range(n):
            _mk_user(joined=target)

        start, end = "2026-06-01", "2026-06-30"
        cache.delete(f"admin:dash:mkt:custom:{start}:{end}")
        res = staff_client.get(URL, {"start": start, "end": end})
        bucket = next(b for b in res.data["trends"]["buckets"] if b["date"] == "2026-06-15")
        assert bucket["signups"] == n
        # 총합도 N (다른 날은 0)
        assert sum(b["signups"] for b in res.data["trends"]["buckets"]) == n


# ─── 커스텀 날짜 범위 ────────────────────────────────────────────────


class TestCustomRange:
    def _del_custom(self, start, end):
        cache.delete(f"admin:dash:mkt:custom:{start}:{end}")

    def test_custom_sets_period_custom_and_previous_correct(self, staff_client, clean_slate):
        start, end = "2026-06-01", "2026-06-30"  # 30일 (6/1~6/30)
        self._del_custom(start, end)
        res = staff_client.get(URL, {"start": start, "end": end})
        assert res.status_code == 200
        assert res.data["period"] == "custom"

        rng = res.data["range"]
        # current = [6/1 자정, 7/1 자정)  (end+1일)
        assert rng["current_start"].startswith("2026-06-01T00:00:00")
        assert rng["current_end"].startswith("2026-07-01T00:00:00")
        # previous = 직전 동일 길이 (span 30일) → [5/2 자정, 6/1 자정)
        assert rng["previous_start"].startswith("2026-05-02T00:00:00")
        assert rng["previous_end"].startswith("2026-06-01T00:00:00")

    def test_custom_signups_current_and_previous(self, staff_client, clean_slate):
        tz = timezone.get_current_timezone()
        # current 범위(6/1~6/30) 내 2명
        for _ in range(2):
            _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 6, 10, 9, 0), tz))
        # previous 범위(5/2~5/31) 내 1명
        _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 5, 10, 9, 0), tz))

        start, end = "2026-06-01", "2026-06-30"
        self._del_custom(start, end)
        res = staff_client.get(URL, {"start": start, "end": end})
        assert res.data["kpis"]["signups"]["current"] == 2
        assert res.data["kpis"]["signups"]["previous"] == 1

    def test_only_start_400(self, staff_client):
        res = staff_client.get(URL, {"start": "2026-06-01"})
        assert res.status_code == 400
        assert res.data["success"] is False
        assert "reason" in res.data["error"]["details"]

    def test_reversed_range_400(self, staff_client):
        res = staff_client.get(URL, {"start": "2026-06-30", "end": "2026-06-01"})
        assert res.status_code == 400
        assert "reason" in res.data["error"]["details"]

    def test_unparseable_400(self, staff_client):
        res = staff_client.get(URL, {"start": "2026-13-40", "end": "2026-06-01"})
        assert res.status_code == 400
        assert "reason" in res.data["error"]["details"]

    def test_span_over_366_days_400(self, staff_client):
        # 2025-01-01 ~ 2026-06-30 = 546일 > 366
        res = staff_client.get(URL, {"start": "2025-01-01", "end": "2026-06-30"})
        assert res.status_code == 400
        assert "366" in res.data["error"]["details"]["reason"]


# ─── 기능별 사용자 수 (개선2) ────────────────────────────────────────


class TestFeatureStatsUsers:
    def test_active_users_counted_per_feature(self, staff_client, clean_slate):
        now = timezone.now()
        # 바이오링크: 서로 다른 2명이 공개 페이지 생성 → active_users == 2
        page_a, page_b = _mk_user(joined=now), _mk_user(joined=now)
        _mk_page(page_a, public=True)
        _mk_page(page_b, public=True)
        _mk_page(page_a, public=True)  # 같은 유저 추가 페이지 → 여전히 고유 2명

        # DM: 오너 1명이 캠페인 생성 → dm.active_users == 1
        owner = _mk_user(joined=now)
        _mk_campaign(_mk_conn(owner))

        res = staff_client.get(URL)
        stats = res.data["feature_stats"]
        assert stats["biolink"]["active_users"]["current"] == 2
        assert stats["dm"]["active_users"]["current"] == 1
        # 스팸 사용 없음 → 0
        assert stats["spam"]["active_users"]["current"] == 0


# ─── 온보딩 이탈자 (개선3) ───────────────────────────────────────────


class TestOnboardingDropoffs:
    def _seg(self, res, key):
        segs = {s["key"]: s for s in res.data["onboarding_dropoffs"]["segments"]}
        return segs[key]

    def test_measurable_segments(self, staff_client, clean_slate):
        now = timezone.now()
        joined = now - timedelta(days=3)

        _mk_user(joined=joined)  # A: 무행동

        b = _mk_user(joined=joined)  # B: 페이지 생성 후 미공개
        _mk_page(b, public=False)

        c = _mk_user(joined=joined)  # C: IG 연동 후 캠페인 없음
        _mk_conn(c)

        d = _mk_user(joined=joined)  # D: 캠페인 생성 후 미발송
        _mk_campaign(_mk_conn(d))

        res = staff_client.get(URL)
        assert res.data["onboarding_dropoffs"]["cohort_signups"] == 4
        assert self._seg(res, "no_action")["count"] == 1
        assert self._seg(res, "page_created_not_published")["count"] == 1
        assert self._seg(res, "ig_no_campaign")["count"] == 1
        assert self._seg(res, "campaign_no_send")["count"] == 1
        # 샘플 회원 링크 존재
        assert self._seg(res, "no_action")["samples"][0]["link"]["page"].startswith("/users/")

    @requires_analytics
    def test_paywall_segment_from_checkout_event(self, staff_client, clean_slate):
        now = timezone.now()
        e = _mk_user(joined=now - timedelta(days=2))
        CheckoutEvent.objects.create(user=e, event="paywall_viewed", trigger_feature="dm_limit")

        res = staff_client.get(URL)
        seg = self._seg(res, "paywall_no_payment")
        assert seg["available"] is True
        assert seg["count"] == 1


# ─── 유료 전환 분석 (개선4) ──────────────────────────────────────────


class TestPaidConversionAnalysis:
    @requires_analytics
    def test_by_plan_and_post_payment_and_entry_paths(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        paid_at = now - timedelta(days=3)

        u = _mk_user(joined=now - timedelta(days=10))
        UserSubscription.objects.create(user=u, plan=pro_plan, status=SubscriptionStatus.ACTIVE)
        _mk_paid_payment(u, paid_at=paid_at)

        # 결제 후 사용: DM 발송(결제 후 2일) + 페이지 생성(결제 후 2일)
        conn = _mk_conn(u)
        IGAccountConnection.objects.filter(pk=conn.pk).update(created_at=now - timedelta(days=5))
        camp = _mk_campaign(conn)
        dm = _mk_quota_dms(camp, [111])[0]
        SentDMLog.objects.filter(pk=dm.pk).update(created_at=now - timedelta(days=2))
        page = _mk_page(u, public=True)
        Page.objects.filter(pk=page.pk).update(created_at=now - timedelta(days=2))

        # 결제 진입 경로: 결제 이전(5일 전) paywall_viewed[dm_limit]
        ev = CheckoutEvent.objects.create(
            user=u, event="paywall_viewed", trigger_feature="dm_limit"
        )
        CheckoutEvent.objects.filter(pk=ev.pk).update(created_at=now - timedelta(days=5))

        res = staff_client.get(URL)
        pca = res.data["paid_conversion_analysis"]
        assert pca["total"] == 1
        assert pca["by_plan"] == [
            {"name": "pro", "display_name": pro_plan.display_name, "count": 1}
        ]

        usage = {r["key"]: r["users"] for r in pca["post_payment_usage"]}
        assert usage["dm_send"] == 1
        assert usage["page_created"] == 1

        assert pca["entry_paths_available"] is True
        paths = {r["key"]: r["count"] for r in pca["entry_paths"]}
        assert paths.get("dm_limit") == 1

    def test_admin_plan_excluded_from_by_plan(self, staff_client, clean_slate):
        """admin 플랜 전환자는 by_plan 에서 제외 (운영용 내부 계정)."""
        now = timezone.now()
        admin_plan, _ = SubscriptionPlan.objects.get_or_create(
            name="admin", defaults={"display_name": "관리자", "monthly_price": 0, "sort_order": 9}
        )
        u = _mk_user(joined=now - timedelta(days=10))
        UserSubscription.objects.create(user=u, plan=admin_plan, status=SubscriptionStatus.ACTIVE)
        _mk_paid_payment(u, paid_at=now - timedelta(days=2))

        res = staff_client.get(URL)
        pca = res.data["paid_conversion_analysis"]
        assert pca["total"] == 1  # 전환자 수엔 잡히나
        assert all(r["name"] != "admin" for r in pca["by_plan"])  # 플랜 분해엔 제외
