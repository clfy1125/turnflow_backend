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
from datetime import date, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai_jobs.models import AiJob
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
    from apps.analytics.models import (
        CancellationEvent,
        CheckoutEvent,
        LandingVisit,
        SignupAttribution,
    )

    HAS_ANALYTICS = True
except (ImportError, RuntimeError):
    CancellationEvent = None
    CheckoutEvent = None
    LandingVisit = None
    SignupAttribution = None
    HAS_ANALYTICS = False

requires_analytics = pytest.mark.skipif(
    not HAS_ANALYTICS, reason="apps.analytics 미탑재 — attribution_available=false 강등 경로"
)

User = get_user_model()

URL = "/api/v1/admin/dashboard/marketing/"
# snapshot(R-2)은 기간 무관 별도 키라 함께 비워야 테스트 간 값이 새지 않는다
CACHE_KEYS = [f"admin:dash:mkt:{p}" for p in ("7d", "30d", "90d", "all")] + [
    "admin:dash:mkt:snapshot"
]
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
    # MKT-3: 환불은 refunded_at 으로 귀속되므로 이것도 창 밖으로 밀어야 한다 —
    # 안 하면 기존 환불이 '이번 기간 환불'로 잡혀 period_revenue.net 이 음수가 된다.
    PaymentHistory.objects.filter(refunded_at__isnull=False).update(refunded_at=long_ago)
    # trial_ends_at/converted_at 도 함께 이동 — P-1 종료 시점 집계가 잔존행에 오염되지 않게
    ReferralRedemption.objects.all().update(trial_started_at=long_ago, trial_ends_at=long_ago)
    ReferralRedemption.objects.filter(converted_at__isnull=False).update(converted_at=long_ago)
    UserSubscription.objects.all().update(
        status=SubscriptionStatus.CANCELLED, trial_used_at=None, extra_ig_accounts=0
    )
    if HAS_ANALYTICS:
        LandingVisit.objects.all().update(created_at=long_ago)
        # 더러운 테스트 DB에 커밋된 이벤트 행이 기간 창 안에 남아 cancel_defense 등을
        # 오염시키는 케이스 방어 (LandingVisit 과 동일한 이유)
        CancellationEvent.objects.all().update(created_at=long_ago)
        CheckoutEvent.objects.all().update(created_at=long_ago)


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


def _mk_trial_sub(user, plan, *, card=True, **kwargs):
    """무료체험(TRIALING) 구독 — R-4 이후 '카드 등록 완료'(billing_key_issued_at)만 집계.

    card=False 는 어드민 수동 부여(무카드) 계정 — 전환/체험 카운트에서 빠져야 한다.
    """
    return UserSubscription.objects.create(
        user=user,
        plan=plan,
        status=SubscriptionStatus.TRIALING,
        billing_key_issued_at=timezone.now() if card else None,
        **kwargs,
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


def _all_nodes(variant) -> list[dict]:
    """variant 의 전 노드 (head + branch.steps + activation + conversion)."""
    nodes = list(variant["head"])
    for br in variant["branches"]:
        nodes.extend(br["steps"])
    nodes.append(variant["activation"])
    nodes.append(variant["conversion"])
    return nodes


def _node(res, key, channel="all"):
    """분기 퍼널 노드 1개를 key 로 조회 (head + branch.steps + activation + conversion)."""
    return next(n for n in _all_nodes(_variant(res, channel)) if n["key"] == key)


def _branch(res, branch_key, channel="all"):
    return next(b for b in _variant(res, channel)["branches"] if b["key"] == branch_key)


# ─── MKT-2: 채널 행(other / link / referral_code) 조회 헬퍼 ──────────────
# 테스트 DB 가 dev DB 라 남의 링크·코드 행이 섞여 들어온다 → 전체 배열 비교 금지,
# 반드시 내가 만든 key 로 집어서 단언할 것.


def _channel_row(res, key):
    return next(r for r in res.data["channels"]["rows"] if r["key"] == str(key))


def _source(res, key):
    """other 행을 펼친 소스 1줄."""
    return next(s for s in _channel_row(res, "other")["sources"] if s["key"] == key)


def _mk_link(name, **utm):
    """저장한 채널 링크 1개 (url/channel 은 대시보드 집계에 안 쓰이므로 단순 값)."""
    from apps.admin_api.models import MarketingChannelLink

    fields = {f"utm_{k}": v for k, v in utm.items()}
    return MarketingChannelLink.objects.create(
        name=name,
        base_url="https://turnflow.link/",
        url="https://turnflow.link/?x=1",
        channel="meta_ads",
        **{
            "utm_source": "",
            "utm_medium": "",
            "utm_campaign": "",
            "utm_content": "",
            **fields,
        },
    )


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

    @pytest.mark.parametrize("period", ["7d", "30d", "90d", "all"])
    def test_valid_periods(self, staff_client, period):
        res = staff_client.get(URL, {"period": period})
        assert res.status_code == 200
        assert res.data["period"] == period

    def test_invalid_period_400_project_error_format(self, staff_client):
        res = staff_client.get(URL, {"period": "1y"})
        assert res.status_code == 400
        assert res.data["success"] is False
        assert res.data["error"]["code"] == 400
        assert res.data["error"]["details"]["allowed"] == ["7d", "30d", "90d", "all"]


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
        # R-3: 분기와 나란히 단일 활성화 노드 + 교집합
        assert v["activation"]["key"] == "activated"
        assert v["activation"]["rate_of"] == "signup"
        assert v["activation_overlap"] == {"both": 0}
        for key in (
            "visit",
            "signup",
            "activated",
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
        # MKT-2: other 행은 항상 존재한다(첫 행) — 빈 상태에서도 0 으로 나온다
        other = _channel_row(res, "other")
        assert other["kind"] == "other"
        assert (other["visits"], other["signups"], other["sources"]) == (0, 0, [])
        assert other["signup_rate"] is None
        assert "conversion_rate" not in other  # 코드 행 전용 필드는 붙지 않는다
        assert res.data["mrr_breakdown"]["total"] == 0
        # 기간 매출은 dev DB 의 기존 결제가 섞이므로 절대값이 아니라 **항등**으로 단언한다
        rev = res.data["period_revenue"]
        assert rev["net"] == rev["gross"] - rev["refunded"]
        assert sum(p["net"] for p in rev["by_plan"]) + rev["extra_ig_accounts"]["net"] == rev["net"]
        assert rev["vat_included"] is True
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
        # M-6 이후 formula 는 한국어 정의 문장 — 핵심 구성요소만 단언 (문구 리팩터 내성)
        # FMT-1 이후 내부 표기(÷ 가입 수) 대신 읽는 문장이다 → 의미어로 단언한다.
        assert "인스타그램 계정을 연동한" in ig["formula"]
        assert "이 기간에 가입한 회원" in ig["formula"]

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
        assert "페이지를 만든" in created["formula"]
        assert "이 기간에 가입한 회원" in created["formula"]

        page = _node(res, "page_published")
        assert page["count"] == 2
        assert page["rate"] == 1.0  # published/created = 2/2
        assert page["rate_of"] == "page_created"
        assert "페이지를 공개한" in page["formula"]
        assert "페이지를 만든 회원 중" in page["formula"]  # 분모가 '가입'이 아니라 '생성'

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
        # R-4: 분모가 가입(5) 이 아니라 활성화 유저 — 여기선 활성화 0 이므로 rate=null
        assert paid["rate_of"] == "activated"
        assert paid["rate"] is None
        # 유료플랜 전환(무료체험+실결제) — 체험 없으면 전부 실결제 쪽
        assert "무료체험을 시작했거나 실제 결제를 완료한" in paid["formula"]
        assert "카드 등록을 완료한 경우만" in paid["formula"]  # 체험 정의 (R-4)
        assert paid["breakdown"] == {
            "pro_trial": 0,
            "basic_trial": 0,
            "pro_paid": 0,  # 구독 레코드가 없는 결제자 → other
            "basic_paid": 0,
            "other": 1,
        }
        assert sum(paid["breakdown"].values()) == paid["count"]

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
        # MKT-2: 리퍼러 추정 채널은 최상위 행이 아니라 other 행의 소스 줄이 된다
        other = _channel_row(res, "other")
        # MKT-10: 귀속 행이 없는 두 번째 회원은 채널 분해에서 빠진다(attribution_gap 으로)
        assert other["visits"] == 2 and other["signups"] == 1
        assert res.data["channels"]["attribution_gap"]["signups_unattributed"] == 1

        ig = _source(res, "instagram_organic")
        assert ig["visits"] == 2
        assert ig["signups"] == 1
        assert ig["signup_rate"] == 0.5
        assert ig["label"] == "인스타그램 유입"
        # 비순차 분기 컬럼: 페이지 생성/공개는 1, IG·DM 갈래는 0
        assert ig["page_created"] == 1
        assert ig["page_published"] == 1
        assert ig["ig_connected"] == 0
        assert ig["dm_campaign"] == 0

        # MKT-6 ②: 저장 channel 이 unknown 인 경우는 direct 로 접힌다(라벨 중복 방지)
        assert "unknown" not in {s["key"] for s in other["sources"]}

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
        # MKT-2: 제휴코드는 최상위 행 1개 (사용자는 저장 채널이 아니라 코드 행으로 간다)
        code_row = _channel_row(res, code.code)
        assert code_row["kind"] == "referral_code"
        assert code_row["signups"] == 1
        assert code_row["visits"] is None  # 코드에 귀속되는 '방문'은 존재하지 않음
        assert code_row["signup_rate"] is None
        assert code_row["redemptions"] == 1
        # 원래 있었을 행(저장 UTM 없음 → other)에 과소 집계량이 표기된다
        assert _channel_row(res, "other")["referral_overlap"] == 1

    def test_kpi_visits_counted(self, staff_client, clean_slate):
        vid = uuid.uuid4()
        LandingVisit.objects.create(visitor_id=vid, channel="direct")
        LandingVisit.objects.create(visitor_id=vid, channel="direct")  # 같은 방문자 재방문

        res = staff_client.get(URL)
        assert res.data["kpis"]["visits"]["current"] == 2  # KPI visits 는 세션 단위 유지
        assert res.data["kpis"]["unique_visitors"]["current"] == 1
        # 퍼널 head 는 고유 방문자 단위 — 재방문 세션은 1명
        assert _node(res, "visit")["count"] == 1

    def test_channel_visits_dedupe_by_visitor(self, staff_client, clean_slate):
        """행·소스의 visits = distinct visitor_id (재방문 세션 dedupe)."""
        vid = uuid.uuid4()
        for _ in range(3):  # 같은 방문자가 3세션
            LandingVisit.objects.create(visitor_id=vid, channel="instagram_organic")
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="instagram_organic")

        res = staff_client.get(URL)
        assert _source(res, "instagram_organic")["visits"] == 2  # 4세션 → 방문자 2명
        assert _channel_row(res, "other")["visits"] == 2

    def test_channel_variant_matches_available_channels(self, staff_client, clean_slate):
        """MKT-4: 드롭다운 value == channels.rows[].key, label 은 서버가 준다."""
        now = timezone.now()
        in_cohort = now - timedelta(days=5)
        link = _mk_link("퍼널용 링크", source="meta", medium="cpc", campaign="fn")
        utm = {"utm_source": "meta", "utm_medium": "cpc", "utm_campaign": "fn"}
        for i in range(2):
            u = _mk_user(joined=in_cohort)
            SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email", **utm)
            if i == 0:
                _mk_page(u, public=True)
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="meta_ads", **utm)

        res = staff_client.get(URL)
        funnel = res.data["funnel"]
        values = [c["value"] for c in funnel["available_channels"]]
        assert values[0] == "all"
        assert str(link.pk) in values
        assert set(values) == set(funnel["variants"].keys())
        assert funnel["available_channels_truncated"] is False
        # 라벨 정본은 서버 — 링크 이름은 프론트 사전에 없다
        opt = next(c for c in funnel["available_channels"] if c["value"] == str(link.pk))
        assert opt["label"] == "퍼널용 링크"
        assert opt["label"] == _channel_row(res, link.pk)["label"]
        # 드롭다운 키는 전부 표의 행으로 존재해야 조인이 된다 (all 제외)
        row_keys = {r["key"] for r in res.data["channels"]["rows"]}
        assert {v for v in values if v != "all"} <= row_keys
        # variant 노드 카운트
        assert _node(res, "signup", channel=str(link.pk))["count"] == 2
        assert _node(res, "page_published", channel=str(link.pk))["count"] == 1
        # signup.rate = signups/방문자 = 2/1 (분모=해당 행 고유 방문자)
        assert _node(res, "signup", channel=str(link.pk))["rate"] == 2.0

    def test_zero_signup_link_is_not_in_dropdown(self, staff_client, clean_slate):
        """표에는 남지만 드롭다운에서는 뺀다 — 고르면 전부 0인 빈 퍼널이라 고를 이유가 없다."""
        link = _mk_link("아무도 안 온 링크", source="nobody", medium="cpc")
        res = staff_client.get(URL)
        values = [c["value"] for c in res.data["funnel"]["available_channels"]]
        assert str(link.pk) not in values
        assert _channel_row(res, link.pk)["visits"] == 0  # 표에는 있다

    def test_link_variants_capped(self, staff_client, clean_slate, monkeypatch):
        monkeypatch.setattr("apps.admin_api.views.dashboard_marketing.FUNNEL_LINK_VARIANTS_MAX", 1)
        now = timezone.now()
        for i in range(2):
            _mk_link(f"캡링크{i}", source=f"cap{i}", medium="cpc")
            u = _mk_user(joined=now - timedelta(days=2))
            SignupAttribution.objects.create(
                user=u,
                channel="other_campaign",
                signup_kind="email",
                utm_source=f"cap{i}",
                utm_medium="cpc",
            )

        funnel = staff_client.get(URL).data["funnel"]
        assert funnel["available_channels_truncated"] is True
        # all + other(가입 0이라 제외될 수도) + 링크 1개
        link_values = [c["label"] for c in funnel["available_channels"] if "캡링크" in c["label"]]
        assert len(link_values) == 1


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

        # CLN-1: 별도 referral_codes 블록은 제거 — rows 의 코드 행이 상위집합
        assert "referral_codes" not in res.data["channels"]
        row = _channel_row(res, code.code)
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
        # 각 버킷 키 계약 (Q-1: activated + by_channel / Q-B: unattributed 추가)
        b = trends["buckets"][0]
        assert set(b) == {
            "date",
            "signups",
            "paid",
            "dm_delivered",
            "page_views",
            "page_clicks",
            "visits",
            "activated",
            "by_channel",
            "unattributed",
        }
        # unattributed 는 빈 버킷에도 항상 존재 (프론트가 존재 여부로 분기하지 않게)
        assert b["unattributed"] == {"signups": 0, "activated": 0, "paid": 0}
        # MKT-18 ②: 헤더용 기간 전체 합계 (항상 존재)
        assert set(trends["totals"]) == {"visits", "signups", "activated", "paid"}

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


# ─── MKT-18: 추이 visits 사람 단위 + totals ────────────────────────────


@pytest.mark.skipif(not HAS_ANALYTICS, reason="analytics 앱 미탑재")
class TestTrendsVisitUnit:
    """MKT-18 — 추이의 방문 숫자가 세션이 아니라 사람이어야 한다.

    사고: 같은 화면에서 퍼널 `방문자`(사람) 옆에 추이 `방문`(세션)이 나란히 있어
    받는 사람이 어느 쪽이 방문자인지 알 수 없었다.
    """

    def _visit(self, vid, when, **kw):
        """LandingVisit 1건 — created_at 은 auto_now_add 라 생성 후 갱신해야 한다."""
        v = LandingVisit.objects.create(visitor_id=vid, channel=kw.pop("channel", "direct"), **kw)
        LandingVisit.objects.filter(pk=v.pk).update(created_at=when)
        return v

    def test_bucket_visits_are_unique_visitors_not_sessions(self, staff_client, clean_slate):
        tz = timezone.get_current_timezone()
        day = timezone.make_aware(timezone.datetime(2026, 6, 15, 10, 0, 0), tz)
        vid = uuid.uuid4()
        for _ in range(3):  # 같은 사람이 하루에 3세션
            self._visit(vid, day)
        self._visit(uuid.uuid4(), day)  # 다른 사람 1세션

        start, end = "2026-06-01", "2026-06-30"
        cache.delete(f"admin:dash:mkt:custom:{start}:{end}")
        res = staff_client.get(URL, {"start": start, "end": end})
        bucket = next(b for b in res.data["trends"]["buckets"] if b["date"] == "2026-06-15")
        # 4세션이지만 사람은 2명 — 예전 구현은 4를 줬다
        assert bucket["visits"] == 2
        assert sum(s["visits"] for s in bucket["by_channel"].values()) == 2

    def test_totals_visits_equals_funnel_visitor_node(self, staff_client, clean_slate):
        """`totals.visits` == 퍼널 head[0].count — 화면 두 곳이 맞물리는 항등."""
        tz = timezone.get_current_timezone()
        d1 = timezone.make_aware(timezone.datetime(2026, 6, 10, 10, 0, 0), tz)
        d2 = timezone.make_aware(timezone.datetime(2026, 6, 20, 10, 0, 0), tz)
        repeat = uuid.uuid4()
        self._visit(repeat, d1)  # 같은 사람이
        self._visit(repeat, d2)  # 다른 날 재방문 → 두 버킷에 각각 1
        self._visit(uuid.uuid4(), d1)

        start, end = "2026-06-01", "2026-06-30"
        cache.delete(f"admin:dash:mkt:custom:{start}:{end}")
        res = staff_client.get(URL, {"start": start, "end": end})
        trends = res.data["trends"]
        bucket_sum = sum(b["visits"] for b in trends["buckets"])

        # 버킷 합(3)은 실제 인원(2)보다 크다 — 프론트가 헤더를 버킷 합으로 만들 수 없는 이유
        assert bucket_sum == 3
        assert trends["totals"]["visits"] == 2
        assert trends["totals"]["visits"] == _node(res, "visit")["count"]

    def test_totals_signups_and_paid_match_bucket_sum(self, staff_client, clean_slate):
        """사람당 버킷이 하나인 지표(signups)는 버킷 합 == totals."""
        tz = timezone.get_current_timezone()
        for day in (10, 20):
            _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 6, day, 10, 0, 0), tz))

        start, end = "2026-06-01", "2026-06-30"
        cache.delete(f"admin:dash:mkt:custom:{start}:{end}")
        res = staff_client.get(URL, {"start": start, "end": end})
        trends = res.data["trends"]
        assert trends["totals"]["signups"] == 2
        assert sum(b["signups"] for b in trends["buckets"]) == trends["totals"]["signups"]


# ─── MKT-17: 방문 계측 시작 시각 ───────────────────────────────────────


@pytest.mark.skipif(not HAS_ANALYTICS, reason="analytics 앱 미탑재")
class TestVisitsSince:
    """MKT-17 ① — 방문 계측 시작 시각을 응답에 실어 준다.

    prod 실측(2026-08-19): LandingVisit 최초 = 7/22, SignupAttribution 최초 = 7/16.
    이 6일 차이 때문에 `attribution_gap.since` 를 방문 계측 시작으로 쓸 수 없다 —
    전체 기간 탭의 가입 전환율이 149일치 가입 ÷ 28일치 방문으로 과대평가됐다.
    """

    def test_visits_since_is_min_landing_visit_created_at(self, staff_client, clean_slate):
        # visits_since 는 **전 기간 MIN** 이라 clean_slate 의 '창 밖으로 밀기'로는 부족하다
        # (밀어낸 400일 전 행이 그대로 최솟값이 된다) — 이 테이블만 비운다(트랜잭션 롤백).
        LandingVisit.objects.all().delete()
        tz = timezone.get_current_timezone()
        first = timezone.make_aware(timezone.datetime(2026, 6, 5, 9, 0, 0), tz)
        later = timezone.make_aware(timezone.datetime(2026, 6, 25, 9, 0, 0), tz)
        for when in (later, first):  # 삽입 순서와 무관하게 최솟값이어야 한다
            v = LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="direct")
            LandingVisit.objects.filter(pk=v.pk).update(created_at=when)

        res = staff_client.get(URL, {"period": "all"})
        gap = res.data["channels"]["attribution_gap"]
        assert datetime.fromisoformat(str(gap["visits_since"])) == first

    def test_visits_since_is_period_independent_and_distinct_from_since(
        self, staff_client, clean_slate
    ):
        """기간 파라미터와 무관한 전 기간 최솟값 + `since` 와 다른 값."""
        # 둘 다 전 기간 MIN 이라 잔존행을 비워야 값이 결정적이다 (위 테스트와 같은 이유)
        LandingVisit.objects.all().delete()
        SignupAttribution.objects.all().delete()
        tz = timezone.get_current_timezone()
        visit_at = timezone.make_aware(timezone.datetime(2026, 6, 20, 9, 0, 0), tz)
        signup_at = timezone.make_aware(timezone.datetime(2026, 6, 5, 9, 0, 0), tz)
        v = LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="direct")
        LandingVisit.objects.filter(pk=v.pk).update(created_at=visit_at)
        u = _mk_user(joined=signup_at)
        attr = SignupAttribution.objects.create(user=u, channel="direct", signup_kind="email")
        SignupAttribution.objects.filter(pk=attr.pk).update(created_at=signup_at)

        gap_all = staff_client.get(URL, {"period": "all"}).data["channels"]["attribution_gap"]
        assert datetime.fromisoformat(str(gap_all["visits_since"])) == visit_at
        # 방문 계측 시작(6/20)과 가입 귀속 시작(6/5)은 다른 값 — 대용 불가 (MKT-17 의 근거)
        assert datetime.fromisoformat(str(gap_all["since"])) == signup_at
        assert gap_all["visits_since"] != gap_all["since"]

        # 7d 창에는 두 행 다 없지만 값은 그대로 (전 기간 최솟값)
        gap_7d = staff_client.get(URL, {"period": "7d"}).data["channels"]["attribution_gap"]
        assert datetime.fromisoformat(str(gap_7d["visits_since"])) == visit_at


# ─── MKT-17 ②: 계측 이전이 섞인 기간의 방문자·전환율 억제 ──────────────


@pytest.mark.skipif(not HAS_ANALYTICS, reason="analytics 앱 미탑재")
class TestVisitsPartiallyObservedSuppression:
    """MKT-17 ② — 부분만 관측된 분모로 만든 값은 숫자가 아니라 '없음'이 맞다.

    사고: 전체 기간 탭의 가입 전환율 32.2% 가 가입 164명(149일치) ÷ 방문자 509명(28일치)
    였다. 외주 마케팅 파트너가 이걸 서비스 전체 전환율로 읽는다.
    """

    def _seed_visit(self, when):
        v = LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="direct")
        LandingVisit.objects.filter(pk=v.pk).update(created_at=when)
        return v

    def _get(self, staff_client, start, end):
        cache.delete(f"admin:dash:mkt:custom:{start}:{end}")
        return staff_client.get(URL, {"start": start, "end": end})

    def test_suppressed_when_period_starts_before_measurement(self, staff_client, clean_slate):
        LandingVisit.objects.all().delete()
        tz = timezone.get_current_timezone()
        # 계측 시작 6/10, 조회 기간은 6/01 부터 — 앞 9일이 미관측
        self._seed_visit(timezone.make_aware(timezone.datetime(2026, 6, 10, 9, 0, 0), tz))
        _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 6, 3, 9, 0, 0), tz))

        res = self._get(staff_client, "2026-06-01", "2026-06-30")
        head = res.data["funnel"]["variants"]["all"]["head"]
        visit, signup = head[0], head[1]

        assert visit["key"] == "visit" and signup["key"] == "signup"
        # 방문자: 값·증감 전부 없음 (증감만 남기면 지운 값의 변화율이 화면에 남는다)
        assert visit["count"] is None
        assert visit["previous"] is None
        assert visit["delta_pct"] is None
        # 가입 전환율(분모=방문자)도 없음
        assert signup["rate"] is None
        # ⚠️ 가입자 수는 그대로 숫자 — 방문 계측과 무관하게 정확히 아는 값이다
        assert signup["count"] == 1

    def test_not_suppressed_when_period_fully_after_measurement(self, staff_client, clean_slate):
        LandingVisit.objects.all().delete()
        tz = timezone.get_current_timezone()
        # 계측 시작 6/01, 조회 기간은 6/10 부터 — 창 전체가 관측 구간 안
        self._seed_visit(timezone.make_aware(timezone.datetime(2026, 6, 1, 9, 0, 0), tz))
        self._seed_visit(timezone.make_aware(timezone.datetime(2026, 6, 15, 9, 0, 0), tz))
        _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 6, 12, 9, 0, 0), tz))

        res = self._get(staff_client, "2026-06-10", "2026-06-30")
        head = res.data["funnel"]["variants"]["all"]["head"]
        assert head[0]["count"] == 1  # 6/15 방문 1명 (6/1 은 창 밖)
        assert head[1]["rate"] == 1.0  # 가입 1 ÷ 방문자 1

    def test_suppression_applies_to_every_channel_variant(self, staff_client, clean_slate):
        """채널을 골라도 빈칸이어야 한다 — 어떤 채널만 숫자면 그 채널만 정확해 보인다."""
        LandingVisit.objects.all().delete()
        tz = timezone.get_current_timezone()
        utm = {"utm_source": "meta", "utm_medium": "cpc", "utm_campaign": "fn"}
        _mk_link("억제 확인용", source="meta", medium="cpc", campaign="fn")
        v = LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="meta_ads", **utm)
        LandingVisit.objects.filter(pk=v.pk).update(
            created_at=timezone.make_aware(timezone.datetime(2026, 6, 10, 9, 0, 0), tz)
        )
        u = _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 6, 12, 9, 0, 0), tz))
        SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email", **utm)

        res = self._get(staff_client, "2026-06-01", "2026-06-30")
        variants = res.data["funnel"]["variants"]
        assert len(variants) > 1  # 채널 variant 가 실제로 생겼는지 (빈 단언 방지)
        for key, variant in variants.items():
            assert variant["head"][0]["count"] is None, f"{key} 의 방문자가 안 지워졌다"
            assert variant["head"][1]["rate"] is None, f"{key} 의 전환율이 안 지워졌다"

    def test_activation_and_conversion_nodes_untouched(self, staff_client, clean_slate):
        """방문과 무관한 노드(분모=signups/activated)는 건드리지 않는다."""
        LandingVisit.objects.all().delete()
        tz = timezone.get_current_timezone()
        self._seed_visit(timezone.make_aware(timezone.datetime(2026, 6, 10, 9, 0, 0), tz))
        u = _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 6, 12, 9, 0, 0), tz))
        _mk_page(u, public=True)  # 활성화

        res = self._get(staff_client, "2026-06-01", "2026-06-30")
        variant = res.data["funnel"]["variants"]["all"]
        assert variant["activation"]["count"] == 1
        assert variant["activation"]["rate"] == 1.0  # 활성화 1 ÷ 가입 1
        assert variant["conversion"]["count"] is not None

    def test_trends_totals_keeps_number_while_funnel_is_null(self, staff_client, clean_slate):
        """의도된 비대칭 — 추이는 날짜축이 있어 미관측 구간이 0 으로 드러난다."""
        LandingVisit.objects.all().delete()
        tz = timezone.get_current_timezone()
        self._seed_visit(timezone.make_aware(timezone.datetime(2026, 6, 10, 9, 0, 0), tz))

        res = self._get(staff_client, "2026-06-01", "2026-06-30")
        assert res.data["funnel"]["variants"]["all"]["head"][0]["count"] is None
        assert res.data["trends"]["totals"]["visits"] == 1

    def test_no_suppression_when_measurement_start_unknown(self, staff_client, clean_slate):
        """방문 행이 하나도 없으면 억제하지 않는다 — '없음'과 '기준 미상'을 섞지 않는다."""
        LandingVisit.objects.all().delete()
        tz = timezone.get_current_timezone()
        _mk_user(joined=timezone.make_aware(timezone.datetime(2026, 6, 12, 9, 0, 0), tz))

        res = self._get(staff_client, "2026-06-01", "2026-06-30")
        assert res.data["channels"]["attribution_gap"]["visits_since"] is None
        assert res.data["funnel"]["variants"]["all"]["head"][0]["count"] == 0


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

    def test_renewal_not_counted_as_new_conversion(self, staff_client, clean_slate):
        """갱신 결제는 신규 전환으로 오집계되지 않음 — 첫 PAID 기준 코호트 정확성.

        (PaymentHistory.Meta.ordering 의 GROUP BY 누수 회귀 가드: 유저별 Min(paid_at) 이
        정확해야 창 밖 첫결제 + 창 안 갱신 유저가 전환으로 잡히지 않는다.)
        """
        now = timezone.now()
        u = _mk_user(joined=now - timedelta(days=60))
        _mk_paid_payment(u, paid_at=now - timedelta(days=45))  # 첫 결제 — 창 밖
        _mk_paid_payment(u, paid_at=now - timedelta(days=3))  # 갱신 — 창 안

        pca = staff_client.get(URL).data["paid_conversion_analysis"]
        assert pca["total"] == 0  # 첫 결제가 창 밖이라 전환 아님

    def test_ai_page_in_post_payment(self, staff_client, clean_slate):
        """결제 후 창 내 성공한 AI 페이지 작업 → post_payment_usage.ai_page."""
        now = timezone.now()
        u = _mk_user(joined=now - timedelta(days=10))
        _mk_paid_payment(u, paid_at=now - timedelta(days=3))
        job = AiJob.objects.create(user=u, job_type="bio_remake", status=AiJob.Status.SUCCEEDED)
        AiJob.objects.filter(pk=job.pk).update(created_at=now - timedelta(days=2))

        res = staff_client.get(URL)
        usage = {
            r["key"]: r["users"] for r in res.data["paid_conversion_analysis"]["post_payment_usage"]
        }
        assert usage["ai_page"] == 1


# ─── 구독 유지·해지 분석 (churn/retention) ───────────────────────────


class TestSubscriptionRetention:
    def _neutralize(self):
        """baseline 구독의 기간/취소 시각을 지워 현재-상태 카운트 오염 제거 (트랜잭션 내)."""
        UserSubscription.objects.update(current_period_end=None, cancelled_at=None)

    def test_cancel_scheduled_at_risk_and_recent(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        self._neutralize()
        u = _mk_user(joined=now - timedelta(days=40))
        sub = UserSubscription.objects.create(
            user=u,
            plan=pro_plan,
            status=SubscriptionStatus.CANCELLED,
            monthly_amount_snapshot=14900,
        )
        UserSubscription.objects.filter(pk=sub.pk).update(
            current_period_end=now + timedelta(days=10), cancelled_at=now - timedelta(days=1)
        )

        r = staff_client.get(URL).data["subscription_retention"]
        assert r["cancel_scheduled"] == 1
        assert r["at_risk_mrr"] == 14900
        assert len(r["recent_cancellations"]) == 1
        rc = r["recent_cancellations"][0]
        assert rc["monthly_amount"] == 14900
        assert rc["days_remaining"] in (9, 10)
        assert rc["email"] == u.email

    def test_past_due_counted(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        self._neutralize()
        u = _mk_user(joined=now - timedelta(days=40))
        UserSubscription.objects.create(
            user=u, plan=pro_plan, status=SubscriptionStatus.PAST_DUE, monthly_amount_snapshot=14900
        )
        r = staff_client.get(URL).data["subscription_retention"]
        assert r["payment_failed"] == 1
        assert r["at_risk_mrr"] == 14900  # past_due 도 예상 이탈에 합산

    def test_realized_churn_only_paying_users(self, staff_client, clean_slate, free_plan):
        now = timezone.now()
        self._neutralize()
        # 결제 이력 있는 free 다운그레이드 (실제 해지)
        u1 = _mk_user(joined=now - timedelta(days=40))
        s1 = UserSubscription.objects.create(
            user=u1, plan=free_plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.filter(pk=s1.pk).update(cancelled_at=now - timedelta(days=2))
        _mk_paid_payment(u1, paid_at=now - timedelta(days=35))
        # 결제 이력 없는 free 다운그레이드 (트라이얼 만료 — 실제 해지 아님)
        u2 = _mk_user(joined=now - timedelta(days=40))
        s2 = UserSubscription.objects.create(
            user=u2, plan=free_plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.filter(pk=s2.pk).update(cancelled_at=now - timedelta(days=2))

        r = staff_client.get(URL).data["subscription_retention"]
        assert r["realized_churn"] == 1  # u1 만

    @requires_analytics
    def test_cancel_reasons_and_defense_from_events(self, staff_client, clean_slate):
        now = timezone.now()
        self._neutralize()
        u = _mk_user(joined=now - timedelta(days=5))
        CancellationEvent.objects.create(user=u, event="cancel_button_clicked")
        CancellationEvent.objects.create(
            user=u, event="cancel_reason_submitted", reason="low_usage"
        )
        u2 = _mk_user(joined=now - timedelta(days=5))
        CancellationEvent.objects.create(user=u2, event="cancel_button_clicked")
        CancellationEvent.objects.create(user=u2, event="subscription_cancel_aborted")

        r = staff_client.get(URL).data["subscription_retention"]
        assert r["cancel_reasons_available"] is True
        reasons = {x["key"]: x["count"] for x in r["cancel_reasons"]}
        assert reasons.get("low_usage") == 1
        # 방어: 클릭 2명 중 1명 유지(중단)
        assert r["cancel_defense"] == {
            "tries": 2,
            "retained": 1,
            "defense_rate": 0.5,
        }


# ─── M-1: 페이지 생성 방식 분해 (created_breakdown) ────────────────────


class TestCreatedBreakdown:
    def test_priority_and_sum_matches_new_public_pages(self, staff_client, clean_slate):
        u = _mk_user()
        _mk_page(u)  # manual
        imported = _mk_page(u)
        Page.objects.filter(pk=imported.pk).update(import_source="litly")
        ai_page = _mk_page(u)
        AiJob.objects.create(
            user=u, page=ai_page, job_type="bio_remake", status=AiJob.Status.SUCCEEDED
        )
        # 우선순위: imported > ai — 임포트 페이지에 성공 AI 잡이 있어도 imported 로 1회만
        AiJob.objects.create(
            user=u, page=imported, job_type="theme_generation", status=AiJob.Status.SUCCEEDED
        )
        # 실패한 AI 잡은 ai 로 안 침 → manual
        failed_page = _mk_page(u)
        AiJob.objects.create(
            user=u, page=failed_page, job_type="bio_remake", status=AiJob.Status.FAILED
        )
        # 비-페이지 job_type(external_import)은 ai 판정 제외 → manual
        ext_page = _mk_page(u)
        AiJob.objects.create(
            user=u, page=ext_page, job_type="external_import", status=AiJob.Status.SUCCEEDED
        )

        bio = staff_client.get(URL).data["feature_stats"]["biolink"]
        assert bio["created_breakdown"] == {"ai": 1, "imported": 1, "manual": 3}
        total = sum(bio["created_breakdown"].values())
        assert total == bio["new_public_pages"]["current"]

    def test_private_pages_excluded(self, staff_client, clean_slate):
        u = _mk_user()
        page = _mk_page(u, public=False)
        Page.objects.filter(pk=page.pk).update(import_source="inpock")

        bio = staff_client.get(URL).data["feature_stats"]["biolink"]
        assert bio["created_breakdown"] == {"ai": 0, "imported": 0, "manual": 0}


# ─── M-2: 유료 전환 정의 + 체험·쿠폰 미결제 동반 지표 ───────────────────


class TestPaidPlanNoPayment:
    def test_kpi_paid_conversions_has_definition(self, staff_client, clean_slate):
        kpi = staff_client.get(URL).data["kpis"]["paid_conversions"]
        assert "실제 결제" in kpi["definition"]
        assert "체험·쿠폰" in kpi["definition"]

    def test_trial_and_coupon_without_payment_counted(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        # 쿠폰(레퍼럴) 트라이얼 — 미결제 → 카운트
        coupon_user = _mk_user()
        code = ReferralCode.objects.create(code=f"NP-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)
        ReferralRedemption.objects.create(
            user=coupon_user,
            referral_code=code,
            trial_started_at=now - timedelta(days=4),
            trial_ends_at=now + timedelta(days=26),
        )
        # 카드등록 체험 — 미결제 → 카운트
        card_user = _mk_user()
        UserSubscription.objects.create(
            user=card_user,
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            trial_used_at=now - timedelta(days=2),
        )
        # 체험 후 실결제 발생 → 제외 (paid_conversions 쪽에 잡힘)
        converted = _mk_user()
        UserSubscription.objects.create(
            user=converted,
            plan=pro_plan,
            status=SubscriptionStatus.ACTIVE,
            trial_used_at=now - timedelta(days=3),
        )
        _mk_paid_payment(converted, paid_at=now - timedelta(days=1))

        analysis = staff_client.get(URL).data["paid_conversion_analysis"]
        block = analysis["paid_plan_no_payment"]
        assert block["count"] == 2
        assert block["referral_trial"] == 1
        assert block["card_trial"] == 1
        assert "미결제" in block["definition"] or "이력이 없는" in block["definition"]
        # 실결제자는 paid_conversions(total) 쪽에만
        assert analysis["total"] == 1


# ─── M-3: 채널 레퍼럴 코드 행에 내부 메모(description) 노출 ─────────────


class TestReferralCodeDescription:
    def test_description_included_in_rows(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        code = ReferralCode.objects.create(
            code=f"DE-{uuid.uuid4().hex[:6]}",
            description="A 인플루언서 릴스 협찬",
            target_plan=pro_plan,
        )
        ReferralRedemption.objects.create(
            user=_mk_user(),
            referral_code=code,
            trial_started_at=now - timedelta(days=1),
            trial_ends_at=now + timedelta(days=29),
        )

        row = _channel_row(staff_client.get(URL), code.code)
        assert row["kind"] == "referral_code"
        assert row["description"] == "A 인플루언서 릴스 협찬"


# ─── M-6: 퍼널 노드 formula 전부 non-null ───────────────────────────────


class TestFunnelFormulas:
    def test_all_nodes_have_formula(self, staff_client, clean_slate):
        variant = staff_client.get(URL).data["funnel"]["variants"]["all"]
        nodes = _all_nodes(variant)
        assert len(nodes) == 8  # head 2 + 분기 4 + activation 1 + conversion 1
        for node in nodes:
            assert node["formula"], f"{node['key']} formula 가 비어 있음"
        # 유료플랜 전환 노드는 체험/실결제 분해 정의를 명시 (N-1)
        assert "무료체험" in variant["conversion"]["formula"]
        assert "Toss" in variant["conversion"]["formula"]

    def test_formula_is_plain_reader_friendly_korean(self, staff_client, clean_slate):
        """FMT-1 — 이 문구는 외주 마케팅 파트너가 툴팁에서 그대로 읽는다.

        내부 표기가 새어 들어가면 프론트가 다시 오버라이드해야 하므로(그 순간 문구
        정본이 둘로 갈라진다) 작성 기준을 테스트로 못 박는다.
        """
        variant = staff_client.get(URL).data["funnel"]["variants"]["all"]
        # 내부 용어 · 마크다운 강조 · 기호 나열 금지
        banned = ("가입 코호트", "중복 제거", "도달 여부", "distinct", "**", " · ", "유저", "고객")
        for node in _all_nodes(variant):
            f = node["formula"]
            for token in banned:
                assert token not in f, f"{node['key']} formula 에 금지 표현 {token!r}: {f!r}"
            # 완전한 문장으로 끝난다 (첫 줄은 '÷ … × 100' 비율식일 수 있어 마지막 줄만 검사)
            assert f.rstrip().endswith("."), f"{node['key']} 문구가 문장으로 안 끝남: {f!r}"


# ─── N-1 + R-4: 퍼널 유료플랜 전환 3분할 (플랜 × 체험/실결제) ─────────────


class TestFunnelPlanConversion:
    def test_conversion_counts_trial_and_payment(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        in_cohort = now - timedelta(days=5)
        # 실결제 회원 (현재 구독 = pro ACTIVE → pro_paid)
        u_paid = _mk_user(joined=in_cohort)
        _mk_paid_payment(u_paid, paid_at=now - timedelta(days=2))
        UserSubscription.objects.create(
            user=u_paid, plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        # 무료체험(카드 등록) 진행 중·미결제 회원 → pro_trial
        u_trial = _mk_user(joined=in_cohort)
        _mk_trial_sub(u_trial, pro_plan, trial_used_at=now - timedelta(days=1))
        # 체험 만료 후 강등(CANCELLED) 회원 — 어느 쪽에도 안 잡힘 (현재 상태 기준)
        u_expired = _mk_user(joined=in_cohort)
        UserSubscription.objects.create(
            user=u_expired, plan=pro_plan, status=SubscriptionStatus.CANCELLED
        )
        # 무행동 회원
        _mk_user(joined=in_cohort)

        res = staff_client.get(URL)
        conv = _variant(res)["conversion"]
        assert conv["label"] == "유료플랜 전환"
        assert conv["count"] == 2  # u_paid + u_trial
        assert conv["breakdown"] == {
            "pro_trial": 1,
            "basic_trial": 0,
            "pro_paid": 1,
            "basic_paid": 0,
            "other": 0,
        }
        assert conv["excluded_no_card"] == 0
        # 체험 중 + 실결제 이력 둘 다인 회원은 실결제로 1회만
        _mk_paid_payment(u_trial, paid_at=now - timedelta(hours=1))
        cache.delete_many(CACHE_KEYS)
        conv = _variant(staff_client.get(URL))["conversion"]
        assert conv["count"] == 2
        assert conv["breakdown"]["pro_paid"] == 2
        assert conv["breakdown"]["pro_trial"] == 0

    def test_no_card_trial_excluded_from_conversion(self, staff_client, clean_slate, pro_plan):
        """R-4 — 어드민 수동 부여(무카드) 체험은 전환 자체에서 제외 + excluded_no_card."""
        now = timezone.now()
        u_nocard = _mk_user(joined=now - timedelta(days=3))
        _mk_trial_sub(u_nocard, pro_plan, card=False)
        u_card = _mk_user(joined=now - timedelta(days=3))
        _mk_trial_sub(u_card, pro_plan, card=True)

        conv = _variant(staff_client.get(URL))["conversion"]
        assert conv["count"] == 1  # 카드 등록 체험만
        assert conv["breakdown"]["pro_trial"] == 1
        assert conv["excluded_no_card"] == 1

    def test_conversion_rate_of_activated(self, staff_client, clean_slate, pro_plan):
        """R-4 — 전환율 분모가 가입이 아니라 활성화 유저."""
        now = timezone.now()
        in_cohort = now - timedelta(days=4)
        # 활성화 2명 (공개 페이지) 중 1명이 카드 체험
        u1 = _mk_user(joined=in_cohort)
        _mk_page(u1, public=True)
        _mk_trial_sub(u1, pro_plan)
        u2 = _mk_user(joined=in_cohort)
        _mk_page(u2, public=True)
        # 비활성 가입자 2명 (분모에 안 들어감)
        _mk_user(joined=in_cohort)
        _mk_user(joined=in_cohort)

        v = _variant(staff_client.get(URL))
        assert v["activation"]["count"] == 2
        assert v["activation"]["rate"] == 0.5  # 2/4 (가입 대비)
        assert v["conversion"]["rate_of"] == "activated"
        assert v["conversion"]["rate"] == 0.5  # 1/2 (활성화 대비)

    @requires_analytics
    def test_channel_variant_has_breakdown(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        link = _mk_link("전환용 링크", source="meta", medium="cpc", campaign="bd")
        u = _mk_user(joined=now - timedelta(days=3))
        SignupAttribution.objects.create(
            user=u,
            channel="meta_ads",
            signup_kind="email",
            utm_source="meta",
            utm_medium="cpc",
            utm_campaign="bd",
        )
        _mk_trial_sub(u, pro_plan)

        conv = _variant(staff_client.get(URL), str(link.pk))["conversion"]
        assert conv["count"] == 1
        assert conv["breakdown"] == {
            "pro_trial": 1,
            "basic_trial": 0,
            "pro_paid": 0,
            "basic_paid": 0,
            "other": 0,
        }


# ─── N-2: 채널별 캠페인/소재 분해 (campaigns[]) ─────────────────────────


@requires_analytics
class TestChannelLinkRows:
    """MKT-2 — 저장한 채널 링크 1개 = 1행, UTM 4-튜플 완전일치로 유입을 붙인다."""

    def test_link_row_collects_matching_traffic(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        link = _mk_link("7월 메타 리타겟팅", source="meta", medium="cpc", campaign="jul")
        utm = {"utm_source": "meta", "utm_medium": "cpc", "utm_campaign": "jul"}
        for _ in range(2):
            LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="meta_ads", **utm)
        u = _mk_user(joined=now - timedelta(days=3))
        SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email", **utm)
        _mk_paid_payment(u, paid_at=now - timedelta(days=1))

        res = staff_client.get(URL)
        row = _channel_row(res, link.pk)
        assert row["kind"] == "link"
        assert row["label"] == "7월 메타 리타겟팅"
        assert row["utm"] == {"source": "meta", "medium": "cpc", "campaign": "jul", "content": ""}
        assert (row["visits"], row["signups"], row["paid"]) == (2, 1, 1)
        assert row["signup_rate"] == 0.5
        # 링크로 잡힌 유입은 other 에 중복으로 들어가지 않는다
        assert _channel_row(res, "other")["visits"] == 0

    def test_saved_link_row_exists_with_zero_traffic(self, staff_client, clean_slate):
        """만들었는데 아무도 안 온 링크를 보는 것이 이 화면의 용도 중 하나다."""
        link = _mk_link("아무도 안 온 링크", source="nobody", medium="cpc")
        row = _channel_row(staff_client.get(URL), link.pk)
        assert (row["visits"], row["signups"]) == (0, 0)
        assert row["signup_rate"] is None

    def test_unmatched_utm_goes_to_its_own_source(self, staff_client, clean_slate):
        """저장 안 된 UTM 은 direct 와 섞지 않는다 — 'UTM 붙였는데 왜 기타?' 추적용."""
        LandingVisit.objects.create(
            visitor_id=uuid.uuid4(), channel="other_campaign", utm_source="손으로만든것"
        )
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="direct")

        res = staff_client.get(URL)
        assert _source(res, "unsaved_utm")["visits"] == 1
        assert _source(res, "unsaved_utm")["label"] == "저장 안 된 링크(UTM)"
        assert _source(res, "direct")["visits"] == 1

    def test_matching_ignores_case_and_whitespace(self, staff_client, clean_slate):
        """저장 시점과 방문 시점의 정규화 규칙이 같아야 한다 (_utm_key 단일 소스)."""
        link = _mk_link("대소문자", source="Meta", medium="CPC")
        LandingVisit.objects.create(
            visitor_id=uuid.uuid4(), channel="meta_ads", utm_source=" meta ", utm_medium="cpc"
        )
        assert _channel_row(staff_client.get(URL), link.pk)["visits"] == 1

    def test_biolink_badge_traffic_is_folded_into_other(self, staff_client, clean_slate):
        """고객 바이오링크 배지 유입은 UTM 이 있어도 우리가 집행한 채널이 아니다."""
        link = _mk_link("배지 흉내", source="turnflow_badge", medium="biolink")
        LandingVisit.objects.create(
            visitor_id=uuid.uuid4(),
            channel="other_campaign",
            utm_source="turnflow_badge",
            utm_medium="biolink",
            utm_content="brand-shop",
        )
        res = staff_client.get(URL)
        assert _source(res, "biolink")["visits"] == 1
        assert _source(res, "biolink")["label"] == "고객 바이오링크 페이지"
        assert _channel_row(res, link.pk)["visits"] == 0  # 링크보다 바이오링크 판정이 우선

    def test_duplicate_utm_link_rejected_on_create(self, staff_client, clean_slate):
        """같은 4-튜플 링크가 둘이면 트래픽이 어느 행에 붙을지 모호해진다 → 생성 차단."""
        _mk_link("먼저 만든 것", source="dupe", medium="cpc", campaign="x")
        res = staff_client.post(
            "/api/v1/admin/marketing/channel-links/",
            {
                "name": "나중에 만든 것",
                "base_url": "https://turnflow.link/",
                "utm_source": "DUPE",  # 대소문자만 다름 — 매칭 규칙상 같은 링크
                "utm_medium": "cpc",
                "utm_campaign": "x",
            },
            format="json",
        )
        assert res.status_code == 400
        assert "먼저 만든 것" in str(res.data)


# ─── N-3: 무료체험 active + 실결제 전환율 ───────────────────────────────


class TestTrialsExpanded:
    def test_active_and_paid_conversion_rate(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        # 현재 체험 진행 중 1명 (기간 내 시작, 미결제)
        u_trial = _mk_user()
        UserSubscription.objects.create(
            user=u_trial,
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            trial_used_at=now - timedelta(days=2),
        )
        # 체험 시작 후 실결제 1명 (체험 종료 → ACTIVE)
        u_paid = _mk_user()
        UserSubscription.objects.create(
            user=u_paid,
            plan=pro_plan,
            status=SubscriptionStatus.ACTIVE,
            trial_used_at=now - timedelta(days=10),
        )
        _mk_paid_payment(u_paid, paid_at=now - timedelta(days=1))

        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert trials["active"] == 1  # TRIALING 유료플랜만 (clean_slate 로 기존 전부 CANCELLED)
        assert trials["started"]["current"] == 2
        # 시작자 2명(dedupe) 중 실결제 1명
        assert trials["paid_conversion_rate"] == 0.5
        assert "실제 결제" in trials["paid_conversion_formula"]
        assert "레퍼럴" in trials["conversion_formula"]

    def test_paid_conversion_rate_dedupes_referral_and_card(
        self, staff_client, clean_slate, pro_plan
    ):
        now = timezone.now()
        # 같은 회원이 레퍼럴+카드 체험 둘 다 → 분모 1 (started 는 이벤트 합산이라 2)
        u = _mk_user()
        code = ReferralCode.objects.create(code=f"DD-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now - timedelta(days=3),
            trial_ends_at=now + timedelta(days=27),
        )
        UserSubscription.objects.create(
            user=u,
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            trial_used_at=now - timedelta(days=2),
        )

        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert trials["started"]["current"] == 2
        assert trials["paid_conversion_rate"] == 0.0  # 분모 1(dedupe), 결제 0


# ─── N-4: 채널 paid=실결제 · free_trial 별도 컬럼 ───────────────────────


@requires_analytics
class TestChannelPaidVsFreeTrial:
    def test_trialing_user_not_in_paid_column(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        u_trial = _mk_user(joined=now - timedelta(days=3))
        SignupAttribution.objects.create(user=u_trial, channel="meta_ads", signup_kind="email")
        _mk_trial_sub(u_trial, pro_plan)
        u_paid = _mk_user(joined=now - timedelta(days=3))
        SignupAttribution.objects.create(user=u_paid, channel="meta_ads", signup_kind="email")
        _mk_paid_payment(u_paid, paid_at=now - timedelta(days=1))

        row = _channel_row(staff_client.get(URL), "other")
        assert row["paid"] == 1  # 실결제만 (체험 미포함)
        assert row["free_trial"] == 1
        assert row["paid_rate"] == 0.5  # 1/2 (실결제 기준 유지)


# ─── P-1: 무료체험 종료 시점 기준 전환율 ─────────────────────────────────


class TestTrialsCancelledDuringTrial:
    """T-1 — '체험 중 취소'는 `ended - ended_converted`(종료 후 미결제)와 다른 값이다."""

    def _cancelled_trial_sub(self, user, plan, *, cancelled_at, trial_plan=None):
        sub = UserSubscription.objects.create(
            user=user, plan=plan, status=SubscriptionStatus.CANCELLED
        )
        UserSubscription.objects.filter(pk=sub.pk).update(
            trial_used_at=cancelled_at - timedelta(days=1),
            cancelled_at=cancelled_at,
            cancelled_during_trial_at=cancelled_at,
            trial_plan=trial_plan or plan,
        )
        return sub

    def test_counts_only_cancels_during_trial(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        # ① 체험 중 취소 — 카운트 대상
        self._cancelled_trial_sub(_mk_user(), pro_plan, cancelled_at=now - timedelta(days=2))
        # ② 그냥 만료(취소 안 누름) — cancelled_during_trial_at 없음 → 제외
        u_exp = _mk_user()
        s = UserSubscription.objects.create(
            user=u_exp, plan=pro_plan, status=SubscriptionStatus.CANCELLED
        )
        UserSubscription.objects.filter(pk=s.pk).update(
            trial_used_at=now - timedelta(days=40), cancelled_at=now - timedelta(days=2)
        )
        # ③ 체험 중이지만 아직 취소 안 함 → 제외
        UserSubscription.objects.create(
            user=_mk_user(),
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            trial_used_at=now - timedelta(days=1),
        )

        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert trials["cancelled_during_trial"] == 1
        assert trials["cancelled_during_trial_by_plan"] == [
            {"name": "pro", "display_name": pro_plan.display_name, "count": 1}
        ]
        # 만료 이탈은 ended 쪽에만 잡힌다 — 두 값이 다르다는 것이 이 요청의 출발점
        assert trials["ended"] >= 1

    def test_by_plan_uses_trial_plan_not_current_plan(
        self, staff_client, clean_slate, pro_plan, free_plan
    ):
        """만료로 free 가 된 회원이 free 체험자로 보이면 안 된다."""
        now = timezone.now()
        u = _mk_user()
        sub = UserSubscription.objects.create(
            user=u, plan=free_plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.filter(pk=sub.pk).update(
            trial_used_at=now - timedelta(days=3),
            cancelled_during_trial_at=now - timedelta(days=2),
            trial_plan=pro_plan,  # 체험은 프로였다
        )
        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert trials["cancelled_during_trial"] == 1
        assert [r["name"] for r in trials["cancelled_during_trial_by_plan"]] == ["pro"]

    def test_sum_by_plan_equals_total(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        basic_plan, _ = SubscriptionPlan.objects.get_or_create(
            name="basic",
            defaults={"display_name": "베이직", "monthly_price": 4900, "sort_order": 1},
        )
        for plan in (pro_plan, pro_plan, basic_plan):
            self._cancelled_trial_sub(_mk_user(), plan, cancelled_at=now - timedelta(days=1))
        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert sum(r["count"] for r in trials["cancelled_during_trial_by_plan"]) == (
            trials["cancelled_during_trial"]
        )
        assert trials["cancelled_during_trial"] == 3

    def test_rate_denominator_is_started_members(self, staff_client, clean_slate, pro_plan):
        """분모는 회원 단위 체험 시작자 — started(이벤트 합산)와 다르다."""
        now = timezone.now()
        u = _mk_user()
        sub = UserSubscription.objects.create(
            user=u, plan=pro_plan, status=SubscriptionStatus.CANCELLED
        )
        UserSubscription.objects.filter(pk=sub.pk).update(
            trial_used_at=now - timedelta(days=2),  # 이번 기간 체험 시작 → 분모 1
            cancelled_during_trial_at=now - timedelta(days=1),
            trial_plan=pro_plan,
        )
        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert trials["trial_cancel_rate"] == 1.0
        assert "무료체험을 쓰던 중" in trials["trial_cancel_formula"]

    def test_rate_null_when_no_starters(self, staff_client, clean_slate):
        assert staff_client.get(URL).data["feature_stats"]["trials"]["trial_cancel_rate"] is None

    def test_snapshot_trial_started_is_cumulative_by_trial_plan(
        self, staff_client, clean_slate, pro_plan, free_plan
    ):
        """snapshot.trial_started 는 '지금 체험 중'이 아니라 '시작한 적 있는' 누적이다."""
        now = timezone.now()
        # 체험 후 만료돼 free 로 내려간 회원 — trialing 에는 없지만 trial_started 에는 남는다
        u = _mk_user()
        sub = UserSubscription.objects.create(
            user=u, plan=free_plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.filter(pk=sub.pk).update(
            trial_used_at=now - timedelta(days=60), trial_plan=pro_plan
        )
        cache.delete_many(CACHE_KEYS)
        snap = staff_client.get(URL, {"refresh": "1"}).data["snapshot"]["trial_started"]
        assert snap["total"] >= 1
        assert sum(r["count"] for r in snap["by_plan"]) == snap["total"]
        assert "pro" in {r["name"] for r in snap["by_plan"]}

    def test_cancel_accurate_since_is_migration_time(self, staff_client, clean_slate):
        """T-2 — 기준일은 상수가 아니라 그 환경의 마이그레이션 적용 시각이어야 한다."""
        from django.db.migrations.recorder import MigrationRecorder

        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        row = MigrationRecorder.Migration.objects.filter(
            app="billing", name="0024_subscription_trial_history"
        ).first()
        if row is None:  # 스쿼시 등으로 못 찾으면 null 이어야 한다(추측 금지)
            assert trials["cancel_accurate_since"] is None
        else:
            assert trials["cancel_accurate_since"] is not None
            assert datetime.fromisoformat(str(trials["cancel_accurate_since"])) == row.applied


@requires_analytics
class TestSnapshotTrialNow:
    """S-2 — 지금 체험 중인 인원 3분해. **will_charge + cancelled + no_card == total**.

    NOTE(test-db-not-clean): snapshot 은 기간 무관 누적이라 dev 잔여가 섞인다 → 델타 단언.
    """

    def _snap(self, staff_client):
        cache.delete_many(CACHE_KEYS)
        return staff_client.get(URL, {"refresh": "1"}).data["snapshot"]["trial_now"]

    def _trialing(self, plan, *, card: bool):
        sub = UserSubscription.objects.create(
            user=_mk_user(), plan=plan, status=SubscriptionStatus.TRIALING
        )
        UserSubscription.objects.filter(pk=sub.pk).update(
            current_period_start=timezone.now() - timedelta(days=3),
            current_period_end=timezone.now() + timedelta(days=27),
            billing_key_issued_at=timezone.now() if card else None,
        )
        return sub

    def _cancelled_in_trial(self, plan, *, cancelled_at=None, period_start=None):
        sub = UserSubscription.objects.create(
            user=_mk_user(), plan=plan, status=SubscriptionStatus.CANCELLED
        )
        start = period_start or (timezone.now() - timedelta(days=3))
        UserSubscription.objects.filter(pk=sub.pk).update(
            current_period_start=start,
            current_period_end=timezone.now() + timedelta(days=27),
            cancelled_during_trial_at=cancelled_at or (timezone.now() - timedelta(days=1)),
            billing_key_issued_at=timezone.now(),
        )
        return sub

    def test_three_buckets_sum_to_total(self, staff_client, clean_slate, pro_plan):
        before = self._snap(staff_client)
        self._trialing(pro_plan, card=True)
        self._trialing(pro_plan, card=True)
        self._trialing(pro_plan, card=False)  # 쿠폰 체험(카드 없음)
        self._cancelled_in_trial(pro_plan)

        after = self._snap(staff_client)
        assert after["will_charge"] == before["will_charge"] + 2
        assert after["no_card"] == before["no_card"] + 1
        assert after["cancelled"] == before["cancelled"] + 1
        assert after["total"] == before["total"] + 4
        # 계약
        assert after["will_charge"] + after["cancelled"] + after["no_card"] == after["total"]
        assert sum(r["count"] for r in after["by_plan"]) == after["total"]

    def test_cancelled_trialist_is_included_unlike_trialing(
        self, staff_client, clean_slate, pro_plan
    ):
        """취소자는 status 가 CANCELLED 지만 기간말까지 체험자다 — 빼면 타일 합이 깨진다."""
        before = self._snap(staff_client)
        before_trialing = staff_client.get(URL).data["snapshot"]["trialing"]["total"]
        self._cancelled_in_trial(pro_plan)

        after = self._snap(staff_client)
        assert after["total"] == before["total"] + 1
        assert after["cancelled"] == before["cancelled"] + 1
        # 기존 trialing 은 status 필터라 이 사람을 못 센다 (S-2 가 필요한 이유)
        assert staff_client.get(URL).data["snapshot"]["trialing"]["total"] == before_trialing

    def test_expired_period_is_excluded(self, staff_client, clean_slate, pro_plan):
        """'지금' 체험 중이 아니면 빠진다 — 기간이 지난 건 만료 배치 대상이다."""
        before = self._snap(staff_client)
        sub = UserSubscription.objects.create(
            user=_mk_user(), plan=pro_plan, status=SubscriptionStatus.TRIALING
        )
        UserSubscription.objects.filter(pk=sub.pk).update(
            current_period_start=timezone.now() - timedelta(days=40),
            current_period_end=timezone.now() - timedelta(days=1),  # 이미 지남
            billing_key_issued_at=timezone.now(),
        )
        assert self._snap(staff_client)["total"] == before["total"]

    def test_stale_trial_cancel_from_previous_trial_is_not_counted(
        self, staff_client, clean_slate, pro_plan
    ):
        """T-3-② 로 재체험 시 기록을 남기게 됐으므로, **과거 체험의 취소**가 지금 유료
        기간을 해지한 회원을 '체험 중 취소'로 오인하지 않아야 한다."""
        before = self._snap(staff_client)
        # 지금 기간은 유료 기간(3일 전 시작)인데, 체험 취소 기록은 그보다 훨씬 과거
        self._cancelled_in_trial(
            pro_plan,
            period_start=timezone.now() - timedelta(days=3),
            cancelled_at=timezone.now() - timedelta(days=90),  # 이전 체험 때의 기록
        )
        after = self._snap(staff_client)
        assert after["cancelled"] == before["cancelled"]
        assert after["total"] == before["total"]


@requires_analytics
class TestSnapshotTrialCancelled:
    """S-1 — 누적 체험 취소 인원. trial_started 와 **같은 축**이어야 한다.

    NOTE(test-db-not-clean): snapshot 은 **기간 무관 누적**이라 clean_slate 로도 dev 잔여
    행이 빠지지 않는다 → 절대 카운트 금지, 반드시 **델타**로 단언한다.
    """

    def _cancelled(self, user, trial_plan, current_plan):
        sub = UserSubscription.objects.create(
            user=user, plan=current_plan, status=SubscriptionStatus.CANCELLED
        )
        now = timezone.now()
        UserSubscription.objects.filter(pk=sub.pk).update(
            trial_used_at=now - timedelta(days=400),  # 기간(30d) 밖 — 누적값임을 보이기 위해
            cancelled_during_trial_at=now - timedelta(days=399),
            trial_plan=trial_plan,
        )
        return sub

    def _snap(self, staff_client):
        cache.delete_many(CACHE_KEYS)
        return staff_client.get(URL, {"refresh": "1"}).data["snapshot"]

    def test_cumulative_and_independent_of_period(
        self, staff_client, clean_slate, pro_plan, free_plan
    ):
        before = self._snap(staff_client)["trial_cancelled"]["total"]
        # 체험 후 만료돼 free 로 내려간 취소자 — 기간(30d) 밖이지만 누적엔 남아야 한다
        self._cancelled(_mk_user(), trial_plan=pro_plan, current_plan=free_plan)
        after = self._snap(staff_client)["trial_cancelled"]
        assert after["total"] == before + 1
        assert sum(r["count"] for r in after["by_plan"]) == after["total"]
        # 같은 응답의 기간 종속 값은 0 — 이 둘을 한 타일에 섞으면 안 된다는 근거
        assert staff_client.get(URL).data["feature_stats"]["trials"]["cancelled_during_trial"] == 0

    def test_is_subset_of_trial_started(self, staff_client, clean_slate, pro_plan, free_plan):
        """화면 계약: trial_cancelled.total <= trial_started.total (같은 축이라 구조적 보장)."""
        before = self._snap(staff_client)
        self._cancelled(_mk_user(), trial_plan=pro_plan, current_plan=free_plan)
        # 취소하지 않은 체험자도 하나
        s = UserSubscription.objects.create(
            user=_mk_user(), plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.filter(pk=s.pk).update(
            trial_used_at=timezone.now() - timedelta(days=300), trial_plan=pro_plan
        )
        after = self._snap(staff_client)
        assert after["trial_cancelled"]["total"] == before["trial_cancelled"]["total"] + 1
        assert after["trial_started"]["total"] == before["trial_started"]["total"] + 2
        assert after["trial_cancelled"]["total"] <= after["trial_started"]["total"]
        assert sum(r["count"] for r in after["trial_cancelled"]["by_plan"]) == (
            after["trial_cancelled"]["total"]
        )

    def test_row_without_trial_plan_is_in_neither(self, staff_client, clean_slate, free_plan):
        """플랜 레코드가 지워진 행은 양쪽에서 함께 빠져야 부분집합이 유지된다."""
        before = self._snap(staff_client)
        s = UserSubscription.objects.create(
            user=_mk_user(), plan=free_plan, status=SubscriptionStatus.CANCELLED
        )
        UserSubscription.objects.filter(pk=s.pk).update(
            cancelled_during_trial_at=timezone.now() - timedelta(days=1), trial_plan=None
        )
        after = self._snap(staff_client)
        assert after["trial_cancelled"]["total"] == before["trial_cancelled"]["total"]
        assert after["trial_started"]["total"] == before["trial_started"]["total"]


class TestTrialsEnded:
    def test_ended_cohort_all_end_types(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        code = ReferralCode.objects.create(code=f"EN-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)

        # (b) 쿠폰 체험 만료 후 이탈 — 분모에만
        u_expired = _mk_user()
        ReferralRedemption.objects.create(
            user=u_expired,
            referral_code=code,
            trial_started_at=now - timedelta(days=35),
            trial_ends_at=now - timedelta(days=5),
        )
        # (a) 쿠폰 체험 중 결제 전환 — 종료 시점=converted_at, ended_converted
        u_conv = _mk_user()
        ReferralRedemption.objects.create(
            user=u_conv,
            referral_code=code,
            trial_started_at=now - timedelta(days=10),
            trial_ends_at=now + timedelta(days=20),
            converted_to_paid=True,
            converted_at=now - timedelta(days=1),
        )
        # (a) 카드 체험 전환 — 첫 PAID 가 종료 시점, ended_converted
        u_card_conv = _mk_user()
        UserSubscription.objects.create(
            user=u_card_conv,
            plan=pro_plan,
            status=SubscriptionStatus.ACTIVE,
            trial_used_at=now - timedelta(days=40),
        )
        _mk_paid_payment(u_card_conv, paid_at=now - timedelta(days=3))
        # (b) 카드 체험 만료 이탈 — cancelled_at(다운그레이드 시각)이 종료 시점, 분모에만
        u_card_exp = _mk_user()
        s = UserSubscription.objects.create(
            user=u_card_exp,
            plan=pro_plan,
            status=SubscriptionStatus.CANCELLED,
            trial_used_at=now - timedelta(days=40),
        )
        UserSubscription.objects.filter(pk=s.pk).update(cancelled_at=now - timedelta(days=2))
        # 진행 중 체험 — 분모 제외
        u_ongoing = _mk_user()
        UserSubscription.objects.create(
            user=u_ongoing,
            plan=pro_plan,
            status=SubscriptionStatus.TRIALING,
            trial_used_at=now - timedelta(days=1),
        )
        # 쿠폰 체험이 다음 기간에 끝남 — 분모 제외
        u_future = _mk_user()
        ReferralRedemption.objects.create(
            user=u_future,
            referral_code=code,
            trial_started_at=now - timedelta(days=1),
            trial_ends_at=now + timedelta(days=29),
        )

        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert trials["ended"] == 4  # u_expired, u_conv, u_card_conv, u_card_exp
        assert trials["ended_converted"] == 2  # u_conv, u_card_conv
        assert trials["ended_conversion_rate"] == 0.5
        assert "종료 시점" in trials["ended_conversion_formula"]

    def test_ended_zero_is_null_rate(self, staff_client, clean_slate):
        trials = staff_client.get(URL).data["feature_stats"]["trials"]
        assert trials["ended"] == 0
        assert trials["ended_converted"] == 0
        assert trials["ended_conversion_rate"] is None


# ─── P-3: 레퍼럴 오버레이 원 채널 보정 표기 (referral_overlap) ───────────


@requires_analytics
class TestReferralOverlap:
    def test_moved_user_counted_on_origin_row(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        # 저장 링크로 유입·가입했으나 제휴코드 사용 → 코드 행으로 배타 이동
        link = _mk_link("원래 링크", source="meta", medium="cpc", campaign="ov")
        u = _mk_user(joined=now - timedelta(days=3))
        SignupAttribution.objects.create(
            user=u,
            channel="meta_ads",
            signup_kind="email",
            utm_source="meta",
            utm_medium="cpc",
            utm_campaign="ov",
        )
        code = ReferralCode.objects.create(code=f"OV-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now - timedelta(days=2),
            trial_ends_at=now + timedelta(days=28),
        )

        res = staff_client.get(URL)
        # 코드 행에만 가입이 잡히고 (배타 — 중복 없음)
        assert _channel_row(res, code.code)["signups"] == 1
        assert _channel_row(res, code.code)["referral_overlap"] == 0
        # 원 링크 행은 잔여 멤버가 없어도 overlap 표기를 위해 값이 채워진다
        assert _channel_row(res, link.pk)["signups"] == 0
        assert _channel_row(res, link.pk)["referral_overlap"] == 1


# ─── P-4: 일별 스냅샷 적재 시작일 노출 (snapshot_since) ─────────────────


class TestSnapshotSince:
    def test_null_without_snapshots(self, staff_client, clean_slate):
        from apps.billing.models import DailySubscriptionSnapshot

        DailySubscriptionSnapshot.objects.all().delete()
        r = staff_client.get(URL).data["subscription_retention"]
        assert r["basis"] == "approx_no_snapshot"
        assert r["snapshot_since"] is None

    def test_earliest_snapshot_date_exposed(self, staff_client, clean_slate):
        from apps.billing.models import DailySubscriptionSnapshot

        DailySubscriptionSnapshot.objects.all().delete()
        d1 = timezone.localdate() - timedelta(days=3)
        d2 = timezone.localdate()
        DailySubscriptionSnapshot.objects.create(snapshot_date=d2)
        DailySubscriptionSnapshot.objects.create(snapshot_date=d1)

        r = staff_client.get(URL).data["subscription_retention"]
        assert r["snapshot_since"] == d1.isoformat()


# ─── Q-1: 일별 추이 activated + 채널 분해 ───────────────────────────────


class TestTrendsActivatedByChannel:
    def test_activated_daily_dedupe(self, staff_client, clean_slate):
        u = _mk_user()
        _mk_campaign(_mk_conn(u))  # 오늘 캠페인 생성
        _mk_page(u)  # 같은 날 페이지 공개 — 같은 유저라 dedupe 로 1

        bucket = staff_client.get(URL).data["trends"]["buckets"][-1]
        assert bucket["activated"] == 1

    @requires_analytics
    def test_by_channel_keys_match_channel_rows(self, staff_client, clean_slate):
        """MKT-2 — 그래프의 층 키 == 표의 rows[].key. 두 분류가 공존하면 안 된다."""
        now = timezone.now()
        link = _mk_link("추이 링크", source="meta", medium="cpc", campaign="tr")
        utm = {"utm_source": "meta", "utm_medium": "cpc", "utm_campaign": "tr"}
        # 방문 2세션은 저장 링크로, 1세션은 리퍼러 추정(other)
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="meta_ads", **utm)
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="meta_ads", **utm)
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="search_organic")
        # 가입 2명 — 링크 귀속 1(활성화+결제) + 어트리뷰션 없음 1(→ other)
        u1 = _mk_user(joined=now)
        SignupAttribution.objects.create(user=u1, channel="meta_ads", signup_kind="email", **utm)
        u2 = _mk_user(joined=now)
        _mk_campaign(_mk_conn(u1))
        _mk_paid_payment(u1, paid_at=now)
        assert u2  # other 귀속 대상

        res = staff_client.get(URL)
        bucket = res.data["trends"]["buckets"][-1]
        assert bucket["signups"] == 2
        by = bucket["by_channel"]
        assert by[str(link.pk)] == {"visits": 2, "signups": 1, "activated": 1, "paid": 1}
        # MKT-10/Q-B: 귀속 기록 없는 u2 는 other 가 아니라 unattributed 로 간다
        assert by["other"]["signups"] == 0
        assert by["other"]["visits"] == 1
        assert bucket["unattributed"] == {"signups": 1, "activated": 0, "paid": 0}
        # 사람 단위 3지표: Σ(키) + unattributed == 버킷 총량 / visits 는 등식 그대로
        for key in ("signups", "activated", "paid"):
            assert (
                sum(s[key] for s in by.values()) + bucket["unattributed"][key] == bucket[key]
            ), key
        assert sum(s["visits"] for s in by.values()) == bucket["visits"]
        # 모든 by_channel 키가 표의 행으로 존재해야 라벨을 찾을 수 있다
        row_keys = {r["key"] for r in res.data["channels"]["rows"]}
        assert set(by) <= row_keys

    @requires_analytics
    def test_referral_code_key_in_by_channel(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        u = _mk_user(joined=now)
        SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email")
        code = ReferralCode.objects.create(code=f"TB-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=30),
        )

        bucket = staff_client.get(URL).data["trends"]["buckets"][-1]
        assert bucket["by_channel"][code.code]["signups"] == 1


# ─── Q-2: 코호트 분석 매트릭스 ─────────────────────────────────────────


class TestCohorts:
    def test_subscription_cohort_approx_and_snapshot(self, staff_client, clean_slate, pro_plan):
        from apps.admin_api.views.dashboard_marketing import _local_midnight, _month_add
        from apps.billing.models import DailyPaidCohortSnapshot

        DailyPaidCohortSnapshot.objects.all().delete()
        today = timezone.localdate()
        m = _month_add(today.replace(day=1), -2)  # 2개월 전 코호트
        paid_dt = _local_midnight(m) + timedelta(days=5)

        # 유지 회원 — 현재 유료 ACTIVE
        keeper = _mk_user()
        UserSubscription.objects.create(
            user=keeper, plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        _mk_paid_payment(keeper, paid_at=paid_dt)
        # 이탈 회원 — 결제 다음날 다운그레이드 (M+1 시점 전 이탈)
        churner = _mk_user()
        s = UserSubscription.objects.create(
            user=churner, plan=pro_plan, status=SubscriptionStatus.CANCELLED
        )
        UserSubscription.objects.filter(pk=s.pk).update(cancelled_at=paid_dt + timedelta(days=1))
        _mk_paid_payment(churner, paid_at=paid_dt)

        sub = staff_client.get(URL).data["cohorts"]["subscription"]
        assert sub["unit"] == "month" and sub["max_periods"] == 5
        assert sub["basis"] == "approx"  # 스냅샷 없음 → 현재 상태 역산
        row = next(r for r in sub["rows"] if r["cohort"] == m.strftime("%Y-%m"))
        assert row["size"] == 2
        assert row["values"][0] == 0.5  # M+1: keeper 만 유지

        # 스냅샷이 있으면 그 값을 우선 사용
        checkpoint = _month_add(m, 1)
        DailyPaidCohortSnapshot.objects.create(
            snapshot_date=checkpoint, cohort_month=m, paying_users=2, mrr=29800
        )
        cache.delete_many(CACHE_KEYS)
        sub = staff_client.get(URL).data["cohorts"]["subscription"]
        row = next(r for r in sub["rows"] if r["cohort"] == m.strftime("%Y-%m"))
        assert row["values"][0] == 1.0  # 스냅샷 소스 (2/2)

    def test_usage_cohort_weekly(self, staff_client, clean_slate):
        from apps.admin_api.views.dashboard_marketing import _local_midnight

        today = timezone.localdate()
        this_monday = today - timedelta(days=today.weekday())
        w = this_monday - timedelta(weeks=2)  # 2주 전 가입 코호트

        active_u = _mk_user(joined=_local_midnight(w) + timedelta(days=1))
        _mk_user(joined=_local_midnight(w) + timedelta(days=2))  # 미사용 가입자
        # W+1 주에 캠페인 생성 (사용)
        camp = _mk_campaign(_mk_conn(active_u))
        AutoDMCampaign.objects.filter(pk=camp.pk).update(
            created_at=_local_midnight(w + timedelta(weeks=1)) + timedelta(days=1)
        )

        usage = staff_client.get(URL).data["cohorts"]["usage"]
        assert usage["unit"] == "week" and usage["max_periods"] == 5
        row = next(r for r in usage["rows"] if r["cohort"] == w.isoformat())
        assert row["size"] == 2
        assert row["values"] == [0.5]  # W+1 완결, W+2=이번 주(진행 중) 생략


# ─── Q-3: 고객 액션 리스트 3종 ─────────────────────────────────────────


class TestCustomerActions:
    def test_payment_failed_rows(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        u = _mk_user()
        UserSubscription.objects.create(
            user=u,
            plan=pro_plan,
            status=SubscriptionStatus.PAST_DUE,
            monthly_amount_snapshot=14900,
            renewal_attempts=1,
            next_billing_retry_at=now + timedelta(days=1),
            last_billing_error="카드 한도 초과",
        )
        PaymentHistory.objects.create(user=u, amount=14900, status=PaymentStatus.FAILED)
        # 재시도 소진 회원
        u2 = _mk_user()
        UserSubscription.objects.create(
            user=u2,
            plan=pro_plan,
            status=SubscriptionStatus.PAST_DUE,
            monthly_amount_snapshot=14900,
            renewal_attempts=3,
        )

        rows = staff_client.get(URL).data["customer_actions"]["payment_failed"]
        by_user = {r["user_id"]: r for r in rows}
        assert set(by_user) == {u.id, u2.id}
        row = by_user[u.id]
        assert row["retry_status"] == "scheduled"
        assert row["retry_count"] == 1 and row["retry_max"] == 3
        assert row["reason"] == "카드 한도 초과"
        assert row["amount"] == 14900
        assert row["failed_at"] is not None and row["next_retry_at"] is not None
        assert by_user[u2.id]["retry_status"] == "exhausted"

    def test_dormant_rows(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        # 45일 미사용 (마지막 활동 = 캠페인 생성)
        idle_u = _mk_user()
        UserSubscription.objects.create(
            user=idle_u, plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        camp = _mk_campaign(_mk_conn(idle_u))
        AutoDMCampaign.objects.filter(pk=camp.pk).update(created_at=now - timedelta(days=45))
        # 활동 이력 전무 — 첫 결제 100일 전 기준
        never_u = _mk_user()
        UserSubscription.objects.create(
            user=never_u, plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        _mk_paid_payment(never_u, paid_at=now - timedelta(days=100))
        # 오늘 페이지 만든 활성 사용자 — 제외
        active_u = _mk_user()
        UserSubscription.objects.create(
            user=active_u, plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        _mk_page(active_u)

        rows = staff_client.get(URL).data["customer_actions"]["dormant"]
        by_user = {r["user_id"]: r for r in rows}
        assert active_u.id not in by_user
        assert 44 <= by_user[idle_u.id]["idle_days"] <= 46
        assert by_user[idle_u.id]["last_active_at"] is not None
        assert by_user[never_u.id]["last_active_at"] is None
        assert by_user[never_u.id]["idle_days"] >= 99
        # 미사용 오래된 순
        assert rows[0]["user_id"] == never_u.id
        assert by_user[idle_u.id]["dm_30d"] == 0

    def test_recent_churn_rows(self, staff_client, clean_slate, free_plan, pro_plan):
        now = timezone.now()
        churn = _mk_user()
        s = UserSubscription.objects.create(
            user=churn, plan=free_plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.filter(pk=s.pk).update(cancelled_at=now - timedelta(days=3))
        _mk_paid_payment(churn, paid_at=now - timedelta(days=130))
        PaymentHistory.objects.create(
            user=churn, amount=17900, status=PaymentStatus.PAID, paid_at=now - timedelta(days=10)
        )
        # 결제 이력 없는 다운그레이드(트라이얼 만료 등) — 실이탈 아님, 제외
        trial_exp = _mk_user()
        s2 = UserSubscription.objects.create(
            user=trial_exp, plan=free_plan, status=SubscriptionStatus.ACTIVE
        )
        UserSubscription.objects.filter(pk=s2.pk).update(cancelled_at=now - timedelta(days=2))
        if HAS_ANALYTICS:
            CancellationEvent.objects.create(
                user=churn, event="cancel_reason_submitted", reason="price", from_plan="pro"
            )

        rows = staff_client.get(URL).data["customer_actions"]["recent_churn"]
        assert [r["user_id"] for r in rows] == [churn.id]
        row = rows[0]
        assert row["monthly_amount"] == 17900  # 마지막 PAID 금액
        assert row["tenure_months"] == 4  # (130-3)일 // 30
        if HAS_ANALYTICS:
            assert row["reason"] == "price"
            assert row["plan"] == "pro"
            assert row["plan_display"] == pro_plan.display_name


# ─── Q-4: referral 채널 campaigns[] = 제휴코드 단위 ─────────────────────


@requires_analytics
class TestReferralCampaignsByCode:
    def test_referral_details_grouped_by_code(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        u = _mk_user(joined=now - timedelta(days=3))
        # 원래 방문 utm 이 있어도 referral 채널 세부는 코드 단위여야 한다
        SignupAttribution.objects.create(
            user=u,
            channel="meta_ads",
            signup_kind="email",
            utm_campaign="spring",
            utm_content="a",
        )
        code = ReferralCode.objects.create(
            code=f"RC{uuid.uuid4().hex[:6].upper()}", target_plan=pro_plan
        )
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now - timedelta(days=2),
            trial_ends_at=now + timedelta(days=28),
        )

        res = staff_client.get(URL)
        row = _channel_row(res, code.code)
        assert row["kind"] == "referral_code"
        assert row["signups"] == 1  # 원래 utm 이 아니라 코드 행으로 잡힌다
        assert row["redemptions"] == 1 and row["conversion_rate"] == 0.0

    def test_code_axis_differs_from_signup_axis(self, staff_client, clean_slate, pro_plan):
        """MKT-7 — 두 축은 기준 시각이 다르다. 제휴사 보고 숫자는 코드 축(redemptions)이다.

        오래 전에 가입한 회원이 이번 기간에 코드를 쓰면 redemptions 에는 잡히지만
        signups(가입 코호트)에는 없다 → 화면에 둘 다 필요하다.
        """
        now = timezone.now()
        code = ReferralCode.objects.create(
            code=f"AX{uuid.uuid4().hex[:6].upper()}", target_plan=pro_plan
        )
        # 이번 기간 가입 + 이번 기간 코드 사용 → 양쪽 모두
        u_new = _mk_user(joined=now - timedelta(days=3))
        ReferralRedemption.objects.create(
            user=u_new,
            referral_code=code,
            trial_started_at=now - timedelta(days=2),
            trial_ends_at=now + timedelta(days=28),
        )
        # 오래 전 가입 + 이번 기간 코드 사용 → redemptions 만
        u_old = _mk_user(joined=now - timedelta(days=200))
        ReferralRedemption.objects.create(
            user=u_old,
            referral_code=code,
            trial_started_at=now - timedelta(days=1),
            trial_ends_at=now + timedelta(days=29),
            converted_to_paid=True,
            converted_at=now,
        )

        row = _channel_row(staff_client.get(URL, {"period": "30d"}), code.code)
        assert row["redemptions"] == 2  # 코드 축 — 제휴사에 보고할 숫자
        assert row["signups"] == 1  # 성과 축 — 이 기간 가입 코호트만
        assert row["converted"] == 1  # ReferralRedemption.converted_to_paid
        assert row["paid"] == 0  # 실결제(PAID) 이력 없음 — converted 와 정의가 다르다

    def test_unused_code_still_gets_a_row(self, staff_client, clean_slate, pro_plan):
        """사용 0건이어도 행이 나온다 — 프론트는 /admin/referral-codes/ 에 접근 권한이 없다."""
        code = ReferralCode.objects.create(
            code=f"ZZ{uuid.uuid4().hex[:6].upper()}", target_plan=pro_plan
        )
        row = _channel_row(staff_client.get(URL), code.code)
        assert (row["redemptions"], row["signups"]) == (0, 0)
        assert row["visits"] is None and row["conversion_rate"] is None


# ─── MKT-5/6/8: unsaved_utm 조합 · 소스 키 집합 · 겹침 규칙 ──────────────


@requires_analytics
class TestSourceContract:
    def test_unsaved_utm_combos_listed_with_raw_utm(self, staff_client, clean_slate):
        """MKT-5 — 합계만 있으면 할 수 있는 일이 없다. 조합이 보여야 저장할 수 있다."""
        now = timezone.now()
        for _ in range(2):
            LandingVisit.objects.create(
                visitor_id=uuid.uuid4(),
                channel="paid_other",
                utm_source="Kakao",  # 원문 보존 확인용 대문자
                utm_medium="cpc",
                utm_campaign="0728_open",
            )
        LandingVisit.objects.create(
            visitor_id=uuid.uuid4(), channel="other_campaign", utm_source="naver_blog"
        )
        u = _mk_user(joined=now - timedelta(days=2))
        SignupAttribution.objects.create(
            user=u,
            channel="paid_other",
            signup_kind="email",
            utm_source="kakao",  # 소문자 — 정규화되어 같은 조합으로 묶여야 한다
            utm_medium="cpc",
            utm_campaign="0728_open",
        )

        src = _source(staff_client.get(URL), "unsaved_utm")
        assert src["combos_truncated"] is False
        combos = {c["utm"]["source"]: c for c in src["combos"]}
        kakao = combos["Kakao"]  # 원문 그대로 (저장 화면에 실어 보내야 하므로)
        assert kakao["visits"] == 2
        assert kakao["signups"] == 1  # 대소문자 달라도 같은 조합으로 합쳐진다
        assert kakao["utm"]["campaign"] == "0728_open"
        assert kakao["first_seen"] is not None and kakao["last_seen"] is not None
        assert combos["naver_blog"]["visits"] == 1

    def test_saving_an_unsaved_combo_is_not_rejected(self, staff_client, clean_slate):
        """MKT-5 질문 — 유입만 있는 조합은 아직 링크가 아니므로 중복이 아니다."""
        LandingVisit.objects.create(
            visitor_id=uuid.uuid4(), channel="paid_other", utm_source="kakao", utm_medium="cpc"
        )
        combo = _source(staff_client.get(URL), "unsaved_utm")["combos"][0]["utm"]
        res = staff_client.post(
            "/api/v1/admin/marketing/channel-links/",
            {
                "name": "카카오 오픈",
                "base_url": "https://turnflow.link/",
                "utm_source": combo["source"],
                "utm_medium": combo["medium"],
                "utm_campaign": combo["campaign"],
                "utm_content": combo["content"],
            },
            format="json",
        )
        assert res.status_code == 201

    def test_combos_only_on_unsaved_utm_source(self, staff_client, clean_slate):
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="direct")
        assert "combos" not in _source(staff_client.get(URL), "direct")

    def test_sources_sorted_by_visits_then_signups(self, staff_client, clean_slate):
        """MKT-6 ③ — visits desc → signups desc → key asc. 방문 0 줄도 가입 순으로 뜬다."""
        now = timezone.now()
        for _ in range(3):
            LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="search_organic")
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="instagram_organic")
        # 방문 0 · 가입 1 인 소스 (blog_organic) — 정렬 꼬리 확인용
        u = _mk_user(joined=now - timedelta(days=2))
        SignupAttribution.objects.create(user=u, channel="blog_organic", signup_kind="email")

        sources = _channel_row(staff_client.get(URL), "other")["sources"]
        keys = [s["key"] for s in sources]
        assert keys[0] == "search_organic"  # 방문 3
        assert keys.index("instagram_organic") < keys.index("blog_organic")  # 방문 1 > 0
        assert _source(staff_client.get(URL), "blog_organic")["visits"] == 0

    def test_small_sources_folded_into_other_referral(self, staff_client, clean_slate, monkeypatch):
        """MKT-6 ① — 줄이 많아지면 서버가 접는다(프론트가 접으면 정의가 화면마다 달라진다)."""
        monkeypatch.setattr("apps.admin_api.views.dashboard_marketing.SOURCE_ROWS_MAX", 2)
        for channel in ("search_organic", "instagram_organic", "youtube_organic", "blog_organic"):
            LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel=channel)

        sources = _channel_row(staff_client.get(URL), "other")["sources"]
        keys = {s["key"] for s in sources}
        assert "other_referral" in keys
        assert len(sources) <= 3  # 상한 2 + 접힘 버킷
        folded = next(s for s in sources if s["key"] == "other_referral")
        assert folded["visits"] >= 1

    def test_only_visits_overlap_across_sources(self, staff_client, clean_slate):
        """MKT-8 — 겹치는 지표는 visits 뿐. 사람 계열은 Σsources == other 등식이 성립한다."""
        now = timezone.now()
        vid = uuid.uuid4()
        # 같은 방문자가 인스타로도 오고 검색으로도 옴 → visits 는 양쪽에 1씩
        LandingVisit.objects.create(visitor_id=vid, channel="instagram_organic")
        LandingVisit.objects.create(visitor_id=vid, channel="search_organic")
        for channel in ("instagram_organic", "search_organic"):
            u = _mk_user(joined=now - timedelta(days=2))
            SignupAttribution.objects.create(user=u, channel=channel, signup_kind="email")
            _mk_page(u, public=True)
            _mk_campaign(_mk_conn(u))

        other = _channel_row(staff_client.get(URL), "other")
        sources = other["sources"]
        assert sum(s["visits"] for s in sources) == 2 > other["visits"] == 1  # 겹침
        for metric in (
            "signups",
            "ig_connected",
            "dm_campaign",
            "page_created",
            "page_published",
            "paid",
            "free_trial",
        ):
            assert sum(s[metric] for s in sources) == other[metric], metric


# ─── MKT-9/10/11: 라벨 · 귀속 공백 분리 · 캐시 무효화 ────────────────────


@requires_analytics
class TestAttributionGap:
    """MKT-10 — 계측 공백은 채널 성과가 아니다. 채널 행에서 빼고 표 밖에 숫자로 둔다."""

    def test_unattributed_signups_excluded_from_rows(self, staff_client, clean_slate):
        now = timezone.now()
        u_ok = _mk_user(joined=now - timedelta(days=2))
        SignupAttribution.objects.create(user=u_ok, channel="search_organic", signup_kind="email")
        _mk_user(joined=now - timedelta(days=2))  # 귀속 행 없음 (계측 공백)
        _mk_user(joined=now - timedelta(days=1))  # 귀속 행 없음

        res = staff_client.get(URL)
        gap = res.data["channels"]["attribution_gap"]
        assert gap["signups_unattributed"] == 2
        assert gap["share"] == round(2 / 3, 4)
        # 항등: Σrows.signups + 공백 == 이 기간 가입자 수 (= 퍼널 signup 노드)
        rows_signups = sum(r["signups"] for r in res.data["channels"]["rows"])
        assert rows_signups + gap["signups_unattributed"] == _node(res, "signup")["count"] == 3
        # 출처 미상 줄에는 계측 공백이 섞이지 않는다
        assert _source(res, "search_organic")["signups"] == 1
        assert "direct" not in {s["key"] for s in _channel_row(res, "other")["sources"]}

    def test_direct_label_is_shortened(self, staff_client, clean_slate):
        """MKT-9 — 표에서 길이 균형이 안 맞아 문구만 줄였다(의미는 동일)."""
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="direct")
        assert _source(staff_client.get(URL), "direct")["label"] == "출처 미상"

    def test_since_is_earliest_attribution_record(self, staff_client, clean_slate):
        u = _mk_user(joined=timezone.now() - timedelta(days=2))
        attr = SignupAttribution.objects.create(user=u, channel="direct", signup_kind="email")
        gap = staff_client.get(URL).data["channels"]["attribution_gap"]
        assert gap["since"] is not None
        # 전 기간 최솟값이라 방금 만든 행보다 이르거나 같다 (직렬화 결과는 문자열 → 파싱)
        assert datetime.fromisoformat(str(gap["since"])) <= attr.created_at

    def test_gap_lives_under_channels_not_top_level(self, staff_client, clean_slate):
        """Q-A — 정본 위치는 channels 안. 최상위에 같은 키를 두면 두 곳이 갈라진다."""
        res = staff_client.get(URL)
        assert "attribution_gap" in res.data["channels"]
        assert "attribution_gap" not in res.data

    def test_trends_uses_same_population_as_rows_and_funnel(self, staff_client, clean_slate):
        """Q-B — 표·퍼널·추이 세 카드의 ``other`` 가 같은 인원을 뜻해야 한다.

        추이만 공백 인원을 other 로 접으면 같은 키가 카드마다 다른 모집단이 된다
        (MKT-10 으로 표·퍼널에서 뺀 것과 같은 문제가 카드 하나에 남는다).
        """
        now = timezone.now()
        u_ok = _mk_user(joined=now)
        SignupAttribution.objects.create(user=u_ok, channel="search_organic", signup_kind="email")
        _mk_user(joined=now)  # 귀속 행 없음

        res = staff_client.get(URL)
        bucket = res.data["trends"]["buckets"][-1]
        gap_today = bucket["unattributed"]["signups"]
        assert gap_today == 1
        # 추이의 채널 층 == 표의 행 (둘 다 공백 제외)
        assert sum(s["signups"] for s in bucket["by_channel"].values()) == 1
        assert sum(r["signups"] for r in res.data["channels"]["rows"]) == 1
        # 버킷 총량에는 남는다 → 항등으로 복원 가능
        assert sum(s["signups"] for s in bucket["by_channel"].values()) + gap_today == (
            bucket["signups"]
        )
        # 기간 전체 합 == 표 밖 숫자 (버킷 쪼개기가 총량을 바꾸지 않는다)
        buckets = res.data["trends"]["buckets"]
        assert sum(b["unattributed"]["signups"] for b in buckets) == (
            res.data["channels"]["attribution_gap"]["signups_unattributed"]
        )

    def test_referral_code_user_is_not_a_trends_gap(self, staff_client, clean_slate, pro_plan):
        """공백 판정은 추이에서도 코드 우선 — 코드 사용자는 귀속 행이 없어도 채널을 안다."""
        now = timezone.now()
        u = _mk_user(joined=now)  # 귀속 행 없음, 코드만 사용
        code = ReferralCode.objects.create(code=f"TG-{uuid.uuid4().hex[:6]}", target_plan=pro_plan)
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=30),
        )

        bucket = staff_client.get(URL).data["trends"]["buckets"][-1]
        assert bucket["by_channel"][code.code]["signups"] == 1
        assert bucket["unattributed"]["signups"] == 0


@requires_analytics
class TestLinkExcludedFromStats:
    """MKT-12 — 링크를 집계에서 빼면 **행만** 사라지고 인원은 other 로 흡수된다.

    인원까지 총합에서 없애면 `Σrows.signups + attribution_gap == 기간 가입자 수` 항등이
    깨지고, 화면에서 "합계가 왜 안 맞나"를 설명해야 한다. 그 항등은 Q-C 로 고정한 것이다.
    """

    def _seed(self, *, excluded: bool):
        now = timezone.now()
        link = _mk_link("종료된 캠페인", source="meta", medium="cpc", campaign="ended")
        if excluded:
            link.excluded_from_stats = True
            link.save(update_fields=["excluded_from_stats"])
        utm = {"utm_source": "meta", "utm_medium": "cpc", "utm_campaign": "ended"}
        LandingVisit.objects.create(visitor_id=uuid.uuid4(), channel="meta_ads", **utm)
        u = _mk_user(joined=now)
        SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email", **utm)
        _mk_campaign(_mk_conn(u))  # 활성화까지 도달 — 파생 지표도 같이 흡수되는지 본다
        return link

    def test_row_disappears_but_people_move_to_other(self, staff_client, clean_slate):
        link = self._seed(excluded=True)
        res = staff_client.get(URL)

        assert all(r["key"] != str(link.pk) for r in res.data["channels"]["rows"])
        # 인원은 other 로 흡수 — 없어지지 않는다
        other = _channel_row(res, "other")
        assert other["signups"] == 1
        assert other["visits"] == 1
        src = _source(res, "excluded_link")
        assert src["label"] == "집계에서 뺀 링크"
        assert (src["visits"], src["signups"]) == (1, 1)
        # 파생 지표도 같이 따라온다 (가입만 옮기고 활성화를 두면 other 가 어긋난다)
        assert src["dm_campaign"] == 1
        # 항등 유지 (Q-C)
        gap = res.data["channels"]["attribution_gap"]["signups_unattributed"]
        rows_signups = sum(r["signups"] for r in res.data["channels"]["rows"])
        assert rows_signups + gap == _node(res, "signup")["count"]

    def test_not_folded_into_unsaved_utm(self, staff_client, clean_slate):
        """'저장 안 된 링크' 로 합치면 라벨이 거짓이 되고 combos 의 저장 버튼이 400 을 낸다."""
        self._seed(excluded=True)
        res = staff_client.get(URL)
        keys = {s["key"] for s in _channel_row(res, "other")["sources"]}
        assert "excluded_link" in keys
        assert "unsaved_utm" not in keys
        # 저장 유도 목록(combos)에도 실리지 않는다
        assert "combos" not in _source(res, "excluded_link")

    def test_removed_from_trends_and_funnel_too(self, staff_client, clean_slate):
        """Q-B 교훈 — 한 곳만 고치면 같은 키가 카드마다 다른 인원을 뜻한다."""
        link = self._seed(excluded=True)
        res = staff_client.get(URL)

        bucket = res.data["trends"]["buckets"][-1]
        assert str(link.pk) not in bucket["by_channel"]
        assert bucket["by_channel"]["other"]["signups"] == 1
        # 버킷 총량·unattributed 는 그대로 (인원이 사라진 게 아니므로 항등 유지)
        assert (
            sum(s["signups"] for s in bucket["by_channel"].values())
            + bucket["unattributed"]["signups"]
            == bucket["signups"]
        )
        assert str(link.pk) not in {c["value"] for c in res.data["funnel"]["available_channels"]}
        assert str(link.pk) not in res.data["funnel"]["variants"]

    def test_toggle_off_restores_the_row(self, staff_client, clean_slate):
        """되돌릴 경로가 없어지면 안 된다 — 목록에 남아 있고 다시 켜면 행이 돌아온다."""
        link = self._seed(excluded=True)
        assert all(r["key"] != str(link.pk) for r in staff_client.get(URL).data["channels"]["rows"])
        link.excluded_from_stats = False
        link.save(update_fields=["excluded_from_stats"])
        cache.delete_many(list(CACHE_KEYS))
        row = _channel_row(staff_client.get(URL), link.pk)
        assert (row["visits"], row["signups"]) == (1, 1)

    def test_active_link_wins_over_excluded_on_same_utm(self, staff_client, clean_slate):
        """같은 4-튜플이 둘일 때(레거시) 제외된 쪽이 이기면 살아있는 행이 0 이 된다."""
        excluded = _mk_link("옛 링크", source="meta", medium="cpc", campaign="dup")
        excluded.excluded_from_stats = True
        excluded.save(update_fields=["excluded_from_stats"])
        active = _mk_link("새 링크", source="meta", medium="cpc", campaign="dup")
        LandingVisit.objects.create(
            visitor_id=uuid.uuid4(),
            channel="meta_ads",
            utm_source="meta",
            utm_medium="cpc",
            utm_campaign="dup",
        )
        res = staff_client.get(URL)
        assert _channel_row(res, active.pk)["visits"] == 1


@requires_analytics
class TestCodeExcludedFromStats:
    """MKT-14/15 — 제휴 코드를 집계에서 빼면 행만 사라지고 가입자는 other 로 흡수된다."""

    def _seed(self, pro_plan, *, excluded: bool, origin_link=None):
        now = timezone.now()
        code = ReferralCode.objects.create(
            code=f"EX-{uuid.uuid4().hex[:6]}",
            target_plan=pro_plan,
            excluded_from_stats=excluded,
        )
        u = _mk_user(joined=now)
        if origin_link is not None:  # 원래 귀속은 저장 링크 → 코드로 이동한 케이스
            SignupAttribution.objects.create(
                user=u,
                channel="meta_ads",
                signup_kind="email",
                utm_source="meta",
                utm_medium="cpc",
                utm_campaign="ov",
            )
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=30),
        )
        return code, u

    def test_row_disappears_but_signups_move_to_other(self, staff_client, clean_slate, pro_plan):
        code, _u = self._seed(pro_plan, excluded=True)
        res = staff_client.get(URL)

        assert all(r["key"] != code.code for r in res.data["channels"]["rows"])
        other = _channel_row(res, "other")
        assert other["signups"] == 1
        src = _source(res, "excluded_code")
        assert src["label"] == "집계에서 뺀 제휴 코드"
        assert src["signups"] == 1
        assert src["visits"] == 0  # 코드는 URL 이 아니라 결제 화면 입력 → 방문 개념 없음
        # 항등 유지 + 공백으로 새지 않는다 (코드 자체가 유입 경로)
        gap = res.data["channels"]["attribution_gap"]["signups_unattributed"]
        assert gap == 0
        rows_signups = sum(r["signups"] for r in res.data["channels"]["rows"])
        assert rows_signups + gap == _node(res, "signup")["count"] == 1

    def test_not_folded_into_excluded_link(self, staff_client, clean_slate, pro_plan):
        """링크와 코드를 한 줄로 합치면 뺀 것이 무엇인지 화면에서 구분할 수 없다."""
        self._seed(pro_plan, excluded=True)
        keys = {s["key"] for s in _channel_row(staff_client.get(URL), "other")["sources"]}
        assert "excluded_code" in keys
        assert "excluded_link" not in keys

    def test_removed_from_trends_and_funnel(self, staff_client, clean_slate, pro_plan):
        code, _u = self._seed(pro_plan, excluded=True)
        res = staff_client.get(URL)

        bucket = res.data["trends"]["buckets"][-1]
        assert code.code not in bucket["by_channel"]
        assert bucket["by_channel"]["other"]["signups"] == 1
        assert bucket["unattributed"]["signups"] == 0  # 공백이 아니다
        assert (
            sum(s["signups"] for s in bucket["by_channel"].values())
            + bucket["unattributed"]["signups"]
            == bucket["signups"]
        )
        assert code.code not in {c["value"] for c in res.data["funnel"]["available_channels"]}
        assert code.code not in res.data["funnel"]["variants"]

    def test_code_row_carries_code_id_and_can_exclude(self, staff_client, clean_slate, pro_plan):
        code, _u = self._seed(pro_plan, excluded=False)
        row = _channel_row(staff_client.get(URL), code.code)
        assert row["code_id"] == str(code.pk)  # PATCH 대상 uuid
        assert row["can_exclude"] is True  # full 역할
        # 링크/기타 행에는 붙지 않는다 (kind 전용 필드)
        assert "code_id" not in _channel_row(staff_client.get(URL), "other")

    def test_referral_overlap_not_inflated_when_origin_is_other(
        self, staff_client, clean_slate, pro_plan
    ):
        """원 귀속이 other 였던 사람은 제외 시 **제자리로 돌아온다** — 과소집계가 없다.

        그런데도 referral_overlap 을 올리면 "1명이 빠져나갔다"는 거짓 표기가 남는다.
        """
        now = timezone.now()
        code = ReferralCode.objects.create(
            code=f"OV-{uuid.uuid4().hex[:6]}", target_plan=pro_plan, excluded_from_stats=True
        )
        u = _mk_user(joined=now)
        SignupAttribution.objects.create(user=u, channel="search_organic", signup_kind="email")
        ReferralRedemption.objects.create(
            user=u,
            referral_code=code,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=30),
        )
        other = _channel_row(staff_client.get(URL), "other")
        assert other["signups"] == 1
        assert other["referral_overlap"] == 0

    def test_referral_overlap_kept_when_origin_row_differs(
        self, staff_client, clean_slate, pro_plan
    ):
        """원 귀속이 저장 링크였으면 그 링크는 실제로 과소 집계된다 → 보정 표기 유지."""
        link = _mk_link("원 귀속 링크", source="meta", medium="cpc", campaign="ov")
        self._seed(pro_plan, excluded=True, origin_link=link)
        res = staff_client.get(URL)
        assert _channel_row(res, link.pk)["referral_overlap"] == 1
        assert _channel_row(res, link.pk)["signups"] == 0  # 이동했으므로

    def test_customer_redemption_unaffected(self, db, pro_plan):
        """⚠️ 집계 제외는 **고객 동작에 영향 없음** — is_redeemable 이 이 값을 보지 않는다."""
        code = ReferralCode.objects.create(
            code=f"CU-{uuid.uuid4().hex[:6]}", target_plan=pro_plan, excluded_from_stats=True
        )
        ok, reason = code.is_redeemable()
        assert ok is True
        assert reason == ""

    def test_toggle_off_restores_the_row(self, staff_client, clean_slate, pro_plan):
        code, _u = self._seed(pro_plan, excluded=True)
        assert all(r["key"] != code.code for r in staff_client.get(URL).data["channels"]["rows"])
        code.excluded_from_stats = False
        code.save(update_fields=["excluded_from_stats"])
        cache.delete_many(list(CACHE_KEYS))
        assert _channel_row(staff_client.get(URL), code.code)["signups"] == 1


class TestChannelLinkCacheBust:
    """MKT-11 — 저장 직후 확인하는 흐름이라 5분 지연이 '동작 안 함'으로 읽힌다."""

    _LINKS = "/api/v1/admin/marketing/channel-links/"

    def test_created_link_appears_immediately(self, staff_client, clean_slate):
        staff_client.get(URL)  # 캐시 적재
        res = staff_client.post(
            self._LINKS,
            {
                "name": "광고용 릴스",
                "base_url": "https://turnflow.link/",
                "utm_source": "reels",
                "utm_medium": "social",
            },
            format="json",
        )
        assert res.status_code == 201
        # 캐시를 지우지 않으면 여기서 행이 없다 (직전 GET 의 본문이 그대로 온다)
        row = _channel_row(staff_client.get(URL), res.data["id"])
        assert row["label"] == "광고용 릴스"

    def test_renamed_link_label_updates_immediately(self, staff_client, clean_slate):
        link = _mk_link("옛 이름", source="rn", medium="cpc")
        staff_client.get(URL)
        res = staff_client.patch(f"{self._LINKS}{link.pk}/", {"name": "새 이름"}, format="json")
        assert res.status_code == 200
        assert _channel_row(staff_client.get(URL), link.pk)["label"] == "새 이름"

    def test_deleted_link_disappears_immediately(self, staff_client, clean_slate):
        link = _mk_link("지울 링크", source="del", medium="cpc")
        assert _channel_row(staff_client.get(URL), link.pk)  # 캐시 적재 + 존재 확인
        assert staff_client.delete(f"{self._LINKS}{link.pk}/").status_code == 204
        keys = {r["key"] for r in staff_client.get(URL).data["channels"]["rows"]}
        assert str(link.pk) not in keys

    def test_snapshot_key_is_kept(self, staff_client, clean_slate):
        """기간 무관 누적치는 링크와 무관 — 매번 지우면 비싼 재계산이 반복된다."""
        from apps.admin_api.dashboard_cache import (
            MKT_CACHE_KEY_SNAPSHOT,
            bust_marketing_dashboard_cache,
        )

        cache.set(MKT_CACHE_KEY_SNAPSHOT, {"sentinel": 1}, 60)
        bust_marketing_dashboard_cache(reason="test")
        assert cache.get(MKT_CACHE_KEY_SNAPSHOT) == {"sentinel": 1}


# ─── MKT-3: 기간 매출 (MRR 대체) ───────────────────────────────────────


class TestPeriodRevenue:
    """귀속 규칙: gross=결제 시점 / refunded=환불 시점 → 과거 기간이 소급 변경되지 않는다."""

    def _rev(self, staff_client, **params):
        # 같은 테스트에서 두 번 호출하면 5분 캐시가 첫 응답을 돌려준다 → 매번 무효화
        cache.delete_many(CACHE_KEYS)
        return staff_client.get(URL, params).data["period_revenue"]

    def _pay(
        self, user, amount, paid_at, *, status=None, refunded_at=None, order_id=None, sub=None
    ):
        from apps.billing.models import PaymentHistory, PaymentStatus

        return PaymentHistory.objects.create(
            user=user,
            subscription=sub,  # 없으면 by_plan 의 "unknown" 버킷
            amount=amount,
            status=status or PaymentStatus.PAID,
            paid_at=paid_at,
            refunded_at=refunded_at,
            toss_order_id=order_id or f"t-{uuid.uuid4().hex[:16]}",
        )

    def test_gross_refunded_net_and_counts(self, staff_client, clean_slate):
        now = timezone.now()
        from apps.billing.models import PaymentStatus

        u1, u2 = _mk_user(), _mk_user()
        base = self._rev(staff_client)
        self._pay(u1, 9900, now - timedelta(days=2))
        self._pay(u1, 9900, now - timedelta(days=1))  # 같은 회원 2건
        self._pay(u2, 19800, now - timedelta(days=3))

        rev = self._rev(staff_client)
        assert rev["gross"] - base["gross"] == 39600
        assert rev["payments"] - base["payments"] == 3
        assert rev["paying_users"] - base["paying_users"] == 2
        assert rev["net"] == rev["gross"] - rev["refunded"]
        assert PaymentStatus.PAID  # 임포트 사용 표시

    def test_refund_is_attributed_to_when_it_happened(self, staff_client, clean_slate):
        """지난 기간 결제를 이번 기간에 환불 — gross 는 그대로, 이번 기간 refunded 로 잡힌다."""
        from apps.billing.models import PaymentStatus

        now = timezone.now()
        base = self._rev(staff_client, period="7d")
        # 60일 전 결제(7d 창 밖) → 어제 환불(7d 창 안)
        self._pay(
            _mk_user(),
            30000,
            now - timedelta(days=60),
            status=PaymentStatus.REFUNDED,
            refunded_at=now - timedelta(days=1),
        )
        rev = self._rev(staff_client, period="7d")
        assert rev["gross"] == base["gross"]  # 과거 결제는 이 창에 없음
        assert rev["refunded"] - base["refunded"] == 30000
        assert rev["net"] - base["net"] == -30000

    def test_same_period_charge_and_refund_cancel_out(self, staff_client, clean_slate):
        from apps.billing.models import PaymentStatus

        now = timezone.now()
        base = self._rev(staff_client)
        self._pay(
            _mk_user(),
            12000,
            now - timedelta(days=3),
            status=PaymentStatus.REFUNDED,
            refunded_at=now - timedelta(days=2),
        )
        rev = self._rev(staff_client)
        assert rev["gross"] - base["gross"] == 12000  # 그 시점엔 실제로 들어온 돈
        assert rev["refunded"] - base["refunded"] == 12000
        assert rev["net"] == base["net"]

    def test_partial_cancel_row_is_refund_not_negative_gross(self, staff_client, clean_slate):
        """부분취소는 음수 금액의 **별도 행**이다 — gross 를 깎으면 안 된다."""
        from apps.billing.models import PaymentStatus

        now = timezone.now()
        base = self._rev(staff_client)
        self._pay(
            _mk_user(),
            -3000,
            now - timedelta(days=1),
            status=PaymentStatus.REFUNDED,
            refunded_at=now - timedelta(days=1),
        )
        rev = self._rev(staff_client)
        assert rev["gross"] == base["gross"]
        assert rev["refunded"] - base["refunded"] == 3000  # 절대값으로 환불에 계상
        assert rev["payments"] == base["payments"]

    def test_extra_ig_account_charge_split_from_plan(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        u = _mk_user()
        sub = UserSubscription.objects.create(
            user=u, plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        base = self._rev(staff_client)
        self._pay(
            u,
            29000,
            now - timedelta(days=1),
            order_id=f"tfsub-{uuid.uuid4().hex[:10]}-a0",
            sub=sub,
        )
        self._pay(
            u,
            9900,
            now - timedelta(days=1),
            order_id=f"tfsub-{uuid.uuid4().hex[:10]}-extra-abc",
            sub=sub,
        )

        rev = self._rev(staff_client)
        assert rev["extra_ig_accounts"]["net"] - base["extra_ig_accounts"]["net"] == 9900
        assert rev["extra_ig_accounts"]["payments"] - base["extra_ig_accounts"]["payments"] == 1
        pro = next(p for p in rev["by_plan"] if p["name"] == "pro")
        base_pro = next((p for p in base["by_plan"] if p["name"] == "pro"), {"net": 0})
        assert pro["net"] - base_pro["net"] == 29000  # 추가계정분은 플랜에서 빠진다
        # 항등: Σby_plan + extra == net
        assert sum(p["net"] for p in rev["by_plan"]) + rev["extra_ig_accounts"]["net"] == rev["net"]

    def test_previous_null_for_period_all(self, staff_client, clean_slate):
        rev = self._rev(staff_client, period="all")
        assert rev["previous"] is None
        assert rev["delta_pct"] is None

    def test_mrr_block_is_kept(self, staff_client, clean_slate):
        """화면에서만 뺀다 — CSV·계약 하위호환을 위해 응답 필드는 유지."""
        res = staff_client.get(URL)
        assert "mrr_breakdown" in res.data
        assert "mrr" in res.data["kpis"]


# ─── R-1: period=all (전체 기간, 직전 기간 없음) ────────────────────────


class TestPeriodAll:
    def test_range_previous_is_null_and_deltas_null(self, staff_client, clean_slate):
        now = timezone.now()
        old = _mk_user(joined=now - timedelta(days=300))
        _mk_user(joined=now - timedelta(days=2))

        res = staff_client.get(URL, {"period": "all"})
        assert res.status_code == 200
        assert res.data["period"] == "all"

        rng = res.data["range"]
        assert rng["previous_start"] is None
        assert rng["previous_end"] is None
        # current_start = 서비스 최초 가입 시각 → 어떤 회원보다도 이르거나 같아야 한다
        assert datetime.fromisoformat(rng["current_start"]) <= old.date_joined
        # 30d 창 밖(300일 전) 가입자도 all 코호트에는 포함 (테스트 DB 가 더러워 델타로 단언)
        assert (
            res.data["kpis"]["signups"]["current"]
            > staff_client.get(URL, {"period": "30d"}).data["kpis"]["signups"]["current"]
        )
        for key in ("visits", "unique_visitors", "signups", "ig_connected", "paid_conversions"):
            kpi = res.data["kpis"][key]
            assert kpi["previous"] is None, key
            assert kpi["delta_pct"] is None, key

        # feature_stats 의 delta 계열도 동일 규칙
        bio = res.data["feature_stats"]["biolink"]
        assert bio["new_public_pages"]["previous"] is None
        assert bio["views"]["delta_pct"] is None
        assert res.data["feature_stats"]["dm"]["requested"]["previous"] is None
        assert res.data["feature_stats"]["spam"]["detected"]["previous"] is None
        assert res.data["feature_stats"]["trials"]["started"]["previous"] is None

    def test_preset_period_still_compares(self, staff_client, clean_slate):
        """회귀 방어 — 프리셋/커스텀은 기존대로 직전 동일 길이 비교 유지."""
        res = staff_client.get(URL, {"period": "7d"})
        assert res.data["range"]["previous_start"] is not None
        assert res.data["kpis"]["signups"]["previous"] == 0  # null 아님

    def test_activated_matches_snapshot_on_all(self, staff_client, clean_slate):
        """R-3 정합성 — period=all 이면 코호트 활성화 == snapshot.activated (누적)."""
        now = timezone.now()
        u_old = _mk_user(joined=now - timedelta(days=250))
        _mk_page(u_old, public=True)
        u_new = _mk_user(joined=now - timedelta(days=1))
        _mk_campaign(_mk_conn(u_new))

        res = staff_client.get(URL, {"period": "all"})
        activated_all = res.data["funnel"]["variants"]["all"]["activation"]["count"]
        assert activated_all == res.data["snapshot"]["activated"]
        assert activated_all >= 2  # 방금 만든 2명 포함 (DB 잔존분이 있어 델타로 단언)


# ─── R-2: 고정 패널 snapshot (전체 기간 누적, 기간 무관) ──────────────────


class TestSnapshotPanel:
    def test_paying_trialing_and_totals(self, staff_client, clean_slate, pro_plan):
        now = timezone.now()
        basic_plan, _ = SubscriptionPlan.objects.get_or_create(
            name="basic",
            defaults={"display_name": "베이직", "monthly_price": 9900, "sort_order": 1},
        )
        # ① 실제 결제 인원 — PAID 이력 + 현재 유료 ACTIVE
        u_pro = _mk_user(joined=now - timedelta(days=200))
        _mk_paid_payment(u_pro, paid_at=now - timedelta(days=190))
        UserSubscription.objects.create(user=u_pro, plan=pro_plan, status=SubscriptionStatus.ACTIVE)
        u_basic = _mk_user(joined=now - timedelta(days=100))
        _mk_paid_payment(u_basic, paid_at=now - timedelta(days=90), amount=9900)
        UserSubscription.objects.create(
            user=u_basic, plan=basic_plan, status=SubscriptionStatus.ACTIVE
        )
        # PAST_DUE 는 제외 (R-7 ②)
        u_pastdue = _mk_user()
        _mk_paid_payment(u_pastdue, paid_at=now - timedelta(days=40))
        UserSubscription.objects.create(
            user=u_pastdue, plan=pro_plan, status=SubscriptionStatus.PAST_DUE
        )
        # 결제 이력 없는 ACTIVE 유료(어드민 수동 부여) → 제외
        u_free_pro = _mk_user()
        UserSubscription.objects.create(
            user=u_free_pro, plan=pro_plan, status=SubscriptionStatus.ACTIVE
        )
        # ② 체험 인원 — 카드 등록 완료만
        _mk_trial_sub(_mk_user(), pro_plan, card=True)
        _mk_trial_sub(_mk_user(), pro_plan, card=False)

        res = staff_client.get(URL)
        snap = res.data["snapshot"]
        assert snap["paying"]["total"] == 2
        by_plan = {r["name"]: r["count"] for r in snap["paying"]["by_plan"]}
        assert by_plan == {"basic": 1, "pro": 1}
        assert sum(r["count"] for r in snap["paying"]["by_plan"]) == snap["paying"]["total"]
        assert snap["trialing"]["total"] == 1  # 카드 등록 건만
        assert snap["trialing"]["by_plan"] == [
            {"name": "pro", "display_name": pro_plan.display_name, "count": 1}
        ]
        # 카드 필터가 없는 trials.active 는 이보다 크거나 같아야 한다 (의도된 차이)
        assert res.data["feature_stats"]["trials"]["active"] >= snap["trialing"]["total"]

    def test_signups_and_activated_ignore_period(self, staff_client, clean_slate):
        """기간 파라미터를 바꿔도 snapshot 은 동일 — 그리고 코호트 퍼널과는 다른 축."""
        now = timezone.now()
        u_old = _mk_user(joined=now - timedelta(days=300))
        _mk_page(u_old, public=True)
        Page.objects.filter(user=u_old).update(created_at=now - timedelta(days=299))
        _mk_user(joined=now - timedelta(days=1))

        res_7d = staff_client.get(URL, {"period": "7d"})
        res_all = staff_client.get(URL, {"period": "all"})
        snap_7d, snap_all = res_7d.data["snapshot"], res_all.data["snapshot"]
        assert snap_7d == snap_all  # 별도 캐시 키를 공유 (as_of 포함 완전 동일)
        assert snap_7d["activated"] >= 1  # 300일 전 활성화 회원도 누적에 포함
        # 반면 7d 퍼널 활성화는 '이 기간 가입 코호트' 기준이라 잡히지 않는다
        assert res_7d.data["funnel"]["variants"]["all"]["activation"]["count"] == 0


# ─── R-3: 활성화 단일 노드 + 교집합 ─────────────────────────────────────


def _node_from(variant, key):
    return next(n for n in _all_nodes(variant) if n["key"] == key)


class TestFunnelActivationNode:
    def test_activation_dedupes_and_exposes_overlap(self, staff_client, clean_slate):
        now = timezone.now()
        in_cohort = now - timedelta(days=5)
        u_page = _mk_user(joined=in_cohort)
        _mk_page(u_page, public=True)
        u_camp = _mk_user(joined=in_cohort)
        _mk_campaign(_mk_conn(u_camp))
        u_both = _mk_user(joined=in_cohort)
        _mk_page(u_both, public=True)
        _mk_campaign(_mk_conn(u_both))
        _mk_user(joined=in_cohort)  # 무행동

        v = _variant(staff_client.get(URL))
        act = v["activation"]
        assert act["key"] == "activated" and act["label"] == "활성화 유저"
        assert act["count"] == 3  # 중복 제거 (u_both 1회)
        assert act["rate"] == 0.75  # 3/4
        assert act["rate_of"] == "signup"
        assert v["activation_overlap"]["both"] == 1

        # 팝업용 분기 4노드는 그대로 유지 — 프론트가 dm_only/page_only 를 계산할 수 있어야 함
        dm = _node_from(v, "dm_campaign")["count"]
        pub = _node_from(v, "page_published")["count"]
        both = v["activation_overlap"]["both"]
        assert dm - both == 1 and pub - both == 1
        assert (dm - both) + (pub - both) + both == act["count"]

    @requires_analytics
    def test_channel_variant_has_activation(self, staff_client, clean_slate):
        now = timezone.now()
        u = _mk_user(joined=now - timedelta(days=3))
        SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email")
        _mk_page(u, public=True)
        _mk_campaign(_mk_conn(u))

        # UTM 없이 저장된 가입은 other 행 (파생 채널은 other.sources 로 접힌다 — MKT-2/4)
        v = _variant(staff_client.get(URL), "other")
        assert v["activation"]["count"] == 1
        assert v["activation_overlap"]["both"] == 1


# ─── R-5: 긴 구간의 trends 버킷 자동 상향 ───────────────────────────────


def _local_midnight_kst(d: date):
    """로컬(Asia/Seoul) 날짜 → 그 날 자정 aware datetime (뷰의 _local_midnight 와 동일 규칙)."""
    from datetime import datetime as _dt

    return timezone.make_aware(_dt.combine(d, _dt.min.time()), timezone.get_current_timezone())


def _custom_trends(staff_client, span_days: int):
    """커스텀 범위 응답의 trends — 커스텀 캐시 키는 autouse 정리 대상이 아니라 직접 비운다."""
    end = timezone.localdate()
    start = end - timedelta(days=span_days)
    cache.delete(f"admin:dash:mkt:custom:{start.isoformat()}:{end.isoformat()}")
    res = staff_client.get(URL, {"start": start.isoformat(), "end": end.isoformat()})
    assert res.status_code == 200, res.data
    return res.data["trends"]


class TestTrendsGranularity:
    def test_preset_stays_daily(self, staff_client, clean_slate):
        trends = staff_client.get(URL, {"period": "90d"}).data["trends"]
        assert trends["granularity"] == "day"
        assert len(trends["buckets"]) == 91  # [now-90d, now] 로컬 날짜 경계 포함

    def test_long_custom_range_switches_to_week(self, staff_client, clean_slate):
        now = timezone.now()
        u = _mk_user(joined=now - timedelta(days=100))

        trends = _custom_trends(staff_client, 200)
        assert trends["granularity"] == "week"
        # 모든 버킷 시작일이 월요일 + 오름차순 + 정확히 7일 간격
        dates = [date.fromisoformat(b["date"]) for b in trends["buckets"]]
        assert all(d.weekday() == 0 for d in dates)
        assert dates == sorted(dates)
        assert all((b - a).days == 7 for a, b in zip(dates, dates[1:], strict=False))
        # 가입 1건이 정확히 1개 버킷에 들어간다 (총량 보존)
        assert sum(b["signups"] for b in trends["buckets"]) == 1
        joined_week = timezone.localtime(u.date_joined).date()
        joined_week -= timedelta(days=joined_week.weekday())
        bucket = next(b for b in trends["buckets"] if b["date"] == joined_week.isoformat())
        assert bucket["signups"] == 1
        if HAS_ANALYTICS:  # 항등 (주별에서도 유지) — 이 유저는 귀속 기록이 없어 gap 쪽
            assert (
                sum(s["signups"] for s in bucket["by_channel"].values())
                + bucket["unattributed"]["signups"]
                == 1
            )

    def test_week_bucket_dedupes_activated(self, staff_client, clean_slate):
        """주 버킷 내 같은 회원의 반복 활동은 1명 (사람 단위 dedupe)."""
        now = timezone.now()
        u = _mk_user(joined=now - timedelta(days=150))
        conn = _mk_conn(u)
        c1, c2 = _mk_campaign(conn), _mk_campaign(conn)
        # 두 시각을 **같은 주 월요일 기준**으로 고정한다 — 단순히 "N일 전, N-1일 전" 으로
        # 잡으면 실행 요일에 따라 주 경계를 넘어 테스트가 날짜 의존으로 깨진다.
        monday = timezone.localtime(now - timedelta(days=100)).date()
        monday -= timedelta(days=monday.weekday())
        same_week = _local_midnight_kst(monday) + timedelta(hours=10)
        AutoDMCampaign.objects.filter(pk=c1.pk).update(created_at=same_week)
        AutoDMCampaign.objects.filter(pk=c2.pk).update(created_at=same_week + timedelta(days=2))

        trends = _custom_trends(staff_client, 210)
        assert trends["granularity"] == "week"
        assert sum(b["activated"] for b in trends["buckets"]) == 1  # 같은 주 2회 → 1명


# ─── MKT-1 (= R-8): 퍼널 노드 자체의 증감 (previous / delta_pct) ────────


class TestFunnelNodeDeltas:
    """노드가 자기 증감을 들고 있는지 — 배지와 숫자가 같은 집계에서 나와야 한다."""

    def test_activation_and_conversion_carry_their_own_delta(
        self, staff_client, clean_slate, pro_plan
    ):
        now = timezone.now()
        cur = now - timedelta(days=5)  # 30d 기간 안
        prev = now - timedelta(days=40)  # 직전 30d 구간 안

        # 직전 기간: 활성화 1명 (그중 실결제 1명)
        u_prev = _mk_user(joined=prev)
        _mk_page(u_prev, public=True)
        _mk_paid_payment(u_prev, paid_at=prev)
        # 현재 기간: 활성화 2명 (그중 실결제 1명)
        for _ in range(2):
            u = _mk_user(joined=cur)
            _mk_page(u, public=True)
        _mk_paid_payment(User.objects.filter(date_joined__gte=cur).first(), paid_at=cur)

        v = _variant(staff_client.get(URL, {"period": "30d"}))
        act = v["activation"]
        assert (act["count"], act["previous"]) == (2, 1)
        assert act["delta_pct"] == 100.0
        conv = v["conversion"]
        assert conv["previous"] == 1  # 직전 기간 유료플랜 전환(실결제 1)
        assert conv["delta_pct"] == 0.0  # 1 → 1

    def test_head_nodes_carry_delta_too(self, staff_client, clean_slate):
        """head(visit/signup)도 같은 출처 — 프론트가 노드별로 분기하지 않아도 된다."""
        now = timezone.now()
        _mk_user(joined=now - timedelta(days=3))
        _mk_user(joined=now - timedelta(days=3))
        _mk_user(joined=now - timedelta(days=40))

        v = _variant(staff_client.get(URL, {"period": "30d"}))
        signup = _node_from(v, "signup")
        assert (signup["count"], signup["previous"], signup["delta_pct"]) == (2, 1, 100.0)
        assert "previous" in _node_from(v, "visit")

    def test_zero_previous_gives_null_delta_not_infinity(self, staff_client, clean_slate):
        now = timezone.now()
        _mk_user(joined=now - timedelta(days=3))  # 직전 기간엔 0명

        v = _variant(staff_client.get(URL, {"period": "30d"}))
        signup = _node_from(v, "signup")
        assert signup["previous"] == 0
        assert signup["delta_pct"] is None

    def test_period_all_has_null_previous_everywhere(self, staff_client, clean_slate):
        """R-1 규칙 — 비교할 직전 기간 자체가 없으므로 0 이 아니라 null."""
        _mk_user(joined=timezone.now() - timedelta(days=3))

        v = _variant(staff_client.get(URL, {"period": "all"}))
        for node in (v["activation"], v["conversion"], *v["head"]):
            assert node["previous"] is None, node["key"]
            assert node["delta_pct"] is None, node["key"]

    def test_branch_steps_keep_the_fields_but_stay_null(self, staff_client, clean_slate):
        """팝업 전용 분기 노드는 스키마만 유지 (증감 배지를 쓰지 않음)."""
        _mk_user(joined=timezone.now() - timedelta(days=3))
        v = _variant(staff_client.get(URL, {"period": "30d"}))
        for br in v["branches"]:
            for step in br["steps"]:
                assert step["previous"] is None and step["delta_pct"] is None

    @requires_analytics
    def test_channel_variant_carries_delta(self, staff_client, clean_slate):
        now = timezone.now()
        link = _mk_link("델타 링크", source="meta", medium="cpc", campaign="dl")
        utm = {"utm_source": "meta", "utm_medium": "cpc", "utm_campaign": "dl"}
        u_prev = _mk_user(joined=now - timedelta(days=40))
        SignupAttribution.objects.create(
            user=u_prev, channel="meta_ads", signup_kind="email", **utm
        )
        _mk_page(u_prev, public=True)
        for _ in range(3):
            u = _mk_user(joined=now - timedelta(days=3))
            SignupAttribution.objects.create(user=u, channel="meta_ads", signup_kind="email", **utm)
            _mk_page(u, public=True)

        v = _variant(staff_client.get(URL, {"period": "30d"}), str(link.pk))
        assert v["activation"]["count"] == 3
        assert v["activation"]["previous"] == 1
        assert v["activation"]["delta_pct"] == 200.0

    @requires_analytics
    def test_channel_absent_in_previous_period_gets_zero_not_null(self, staff_client, clean_slate):
        """직전 기간에 유입이 없던 신규 링크 — '없음(null)'이 아니라 '0명'이 사실."""
        link = _mk_link("신규 링크", source="kakao", medium="cpc", campaign="new")
        u = _mk_user(joined=timezone.now() - timedelta(days=3))
        SignupAttribution.objects.create(
            user=u,
            channel="kakao_ads",
            signup_kind="email",
            utm_source="kakao",
            utm_medium="cpc",
            utm_campaign="new",
        )
        _mk_page(u, public=True)

        v = _variant(staff_client.get(URL, {"period": "30d"}), str(link.pk))
        assert v["activation"]["previous"] == 0
        assert v["activation"]["delta_pct"] is None  # 0 분모 → null


class TestCacheRefreshBypass:
    """?refresh=1 — 캐시 우회 (2026-07-30).

    방문/가입 적재에는 캐시 무효화 훅이 없어 신규 유입은 TTL 만큼 늦게 보인다.
    화면의 '새로고침' 버튼이 이 파라미터를 실어 보내 즉시 재계산한다.
    """

    def test_second_call_is_cache_hit(self, staff_client):
        first = staff_client.get(URL, {"period": "30d"})
        assert first.status_code == 200
        assert first["X-Cache"] == "MISS"
        second = staff_client.get(URL, {"period": "30d"})
        assert second["X-Cache"] == "HIT"
        # 캐시 히트는 집계를 다시 하지 않으므로 generated_at 이 그대로다
        assert second.data["generated_at"] == first.data["generated_at"]

    def test_refresh_bypasses_cache_and_recomputes(self, staff_client):
        first = staff_client.get(URL, {"period": "30d"})
        refreshed = staff_client.get(URL, {"period": "30d", "refresh": "1"})
        assert refreshed.status_code == 200
        assert refreshed["X-Cache"] == "BYPASS"
        assert refreshed.data["generated_at"] != first.data["generated_at"]
        # 우회 후 응답을 **다시 적재**해야 한다 (다음 호출은 갱신된 값으로 히트)
        after = staff_client.get(URL, {"period": "30d"})
        assert after["X-Cache"] == "HIT"
        assert after.data["generated_at"] == refreshed.data["generated_at"]

    def test_refresh_also_recomputes_snapshot(self, staff_client):
        """고정 패널(snapshot)은 별도 900초 키 — 함께 갱신되지 않으면
        '새로고침했는데 일부 숫자만 안 바뀐다'가 된다."""
        first = staff_client.get(URL, {"period": "30d"})
        refreshed = staff_client.get(URL, {"period": "30d", "refresh": "1"})
        assert refreshed.data["snapshot"]["as_of"] != first.data["snapshot"]["as_of"]

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values(self, staff_client, value):
        staff_client.get(URL, {"period": "30d"})
        assert staff_client.get(URL, {"period": "30d", "refresh": value})["X-Cache"] == "BYPASS"

    @pytest.mark.parametrize("value", ["0", "false", "", "no"])
    def test_falsy_values_keep_cache(self, staff_client, value):
        staff_client.get(URL, {"period": "30d"})
        assert staff_client.get(URL, {"period": "30d", "refresh": value})["X-Cache"] == "HIT"

    def test_marketing_viewer_refresh_is_ignored_not_403(self, db):
        """제한 역할(외주)은 비싼 재계산을 반복시킬 수 없다 — 403 대신 조용히 무시.

        역할은 **미들웨어**가 판정해 request.admin_role 로 캐싱하므로, 이 검증은
        미들웨어를 타는 세션 로그인(django test Client)으로 해야 한다 —
        APIClient.force_authenticate 는 미들웨어 시점에 아직 익명이라 full 로 굳는다
        (tests_rbac_and_spam.py 와 같은 이유).
        """
        from django.contrib.auth.models import Group
        from django.test import Client as DjangoClient

        from apps.admin_api.roles import ROLE_MARKETING_VIEWER

        viewer = _mk_user(email=f"viewer-{uuid.uuid4().hex[:6]}@example.com", staff=True)
        group, _ = Group.objects.get_or_create(name=ROLE_MARKETING_VIEWER)
        viewer.groups.add(group)
        session_client = DjangoClient()
        session_client.force_login(viewer)

        first = session_client.get(URL, {"period": "30d"})
        assert first.status_code == 200
        second = session_client.get(URL, {"period": "30d", "refresh": "1"})
        assert second.status_code == 200
        assert second["X-Cache"] == "HIT"
        assert second.json()["generated_at"] == first.json()["generated_at"]
