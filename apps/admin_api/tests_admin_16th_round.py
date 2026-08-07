"""어드민 프론트 요청서 16차 회귀 테스트 — DM-16 · DM-17 · USR-1~USR-6.

이 라운드가 고치는 것은 전부 **"한 화면 안에서 위아래 숫자가 다르다"** 계열이다.
그래서 여기서 고정하는 것도 대부분 항등이다:

  DM-16 수신자 목록 기간 파라미터 — 카드와 목록이 같은 창을 본다
        · 창은 **그룹핑 전에** 걸린다 (창 안에서 실패 → 창 뒤 재시도한 사람이 빠지면 안 됨)
        · 사유(대표 로그)도 **창 안에서** 고른다 (창 밖 최신 실패가 사유를 덮으면 안 됨)
  DM-17 dm_quality 의 사람 수 — 팝업(건수)과 목록(사람 수)이 달라 보이던 것을 없앤다
        · `not_sent_people.investigate` == recipients `?error_policy=investigate` 의 count
        · `by_reason[].people`         == recipients `?error_reason=` 의 count
  USR-* 회원 상세 — 모델에 있는데 응답에 없던 값들 (새 집계·새 계산 없음)

⚠️ 테스트 DB 는 dev DB 라 **더럽다**(test-db-not-clean). 전역 집계인 운영 대시보드는
   과거의 좁은 커스텀 범위(아래 `RANGE_*`)를 창으로 써서 우리 데이터만 들어오게 격리한다.
   `created_at` 은 auto_now_add 라 `.update()` 로 되돌려야 그 창에 들어간다.
   캐시는 `?refresh=1` 로 우회한다 — **`cache.clear()` 는 절대 금지**
   (공유 Redis flush → rate_governor fail-closed 센티넬 → DM 1시간 정지).

실행:
    docker compose exec web pytest apps/admin_api/tests_admin_16th_round.py
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_api.dm_error_catalog import INVESTIGATE, NORMAL
from apps.analytics.models import SignupAttribution, SignupKind
from apps.billing.models import (
    AiTokenBalance,
    PaymentHistory,
    PaymentStatus,
    ReferralCode,
    ReferralRedemption,
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

RECIPIENTS_URL = "/api/v1/admin/auto-dm/recipients/"
OPS_URL = "/api/v1/admin/dashboard/operations/"
USERS_URL = "/api/v1/admin/users/"

# 격리용 과거 창 — dev DB 에 이 시기 로그가 없다는 가정을 테스트가 직접 확인한다.
RANGE_START = "2019-03-01"
RANGE_END = "2019-03-05"


def _at(day: int, hour: int = 12):
    """RANGE 안(또는 밖)의 aware datetime — 로컬(Asia/Seoul) 기준."""
    return timezone.make_aware(datetime(2019, 3, day, hour, 0), timezone.get_current_timezone())


pytestmark = pytest.mark.django_db


# ── 픽스처 ────────────────────────────────────────────────────────────
@pytest.fixture
def staff_client(db):
    user = get_user_model().objects.create_user(
        email=f"staff16-{uuid.uuid4().hex[:8]}@t.dev", password="x", is_staff=True
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _mk_user(prefix="u16"):
    return get_user_model().objects.create_user(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@t.dev", password="x"
    )


def _mk_campaign(owner=None):
    owner = owner or _mk_user("own16")
    ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=uuid.uuid4().hex[:12],
        username=f"ig{uuid.uuid4().hex[:6]}",
        status=IGAccountConnection.Status.ACTIVE,
    )
    return AutoDMCampaign.objects.create(
        ig_connection=conn, name=f"c16-{uuid.uuid4().hex[:6]}", media_id="m1"
    )


def _log(camp, rcpt, status, *, created=None, code="", subcode="", msg=""):
    """로그 1건. ``created`` 를 주면 auto_now_add 를 우회해 그 시각으로 되돌린다."""
    log = SentDMLog.objects.create(
        campaign=camp,
        recipient_user_id=rcpt,
        recipient_username=rcpt,
        status=status,
        dm_kind=SentDMLog.DMKind.OPENING,
        error_code=code,
        error_subcode=subcode,
        error_message=msg,
        idempotency_key=uuid.uuid4().hex,
    )
    if created is not None:
        SentDMLog.objects.filter(pk=log.pk).update(created_at=created)
        log.refresh_from_db()
    return log


def _recipients(client, **params):
    return client.get(RECIPIENTS_URL, params)


def _ops_dm_quality(client, **params):
    """운영 대시보드 dm_quality — 캐시는 refresh=1 로 우회(전체 flush 금지)."""
    res = client.get(OPS_URL, {**params, "refresh": "1"})
    assert res.status_code == 200, res.data
    return res.data["dm_quality"]


# ══════════════════════════════════════════════════════════════════════
# DM-16 — 수신자 목록 기간 파라미터
# ══════════════════════════════════════════════════════════════════════
class TestRecipientsTimeWindow:
    def test_window_filters_rows(self, staff_client):
        """`?window=24h` 는 창 안 발송만 접는다. 미지정이면 예전처럼 전체 기간."""
        camp = _mk_campaign()
        now = timezone.now()
        _log(camp, "recent", SentDMLog.Status.FAILED_TOKEN, created=now - timedelta(hours=2))
        _log(camp, "old", SentDMLog.Status.FAILED_TOKEN, created=now - timedelta(days=10))

        res = _recipients(staff_client, campaign_id=str(camp.id), window="24h")
        assert res.status_code == 200
        assert {r["recipient_user_id"] for r in res.data["results"]} == {"recent"}

        # 미지정 = 기존 동작(전체 기간) — 기존 링크·저장된 필터가 깨지지 않는다.
        res_all = _recipients(staff_client, campaign_id=str(camp.id))
        assert {r["recipient_user_id"] for r in res_all.data["results"]} == {"recent", "old"}

        # window=all 도 같은 결과여야 한다 (프리셋 이름으로 전체 기간을 고를 수 있어야 함).
        res_win_all = _recipients(staff_client, campaign_id=str(camp.id), window="all")
        assert {r["recipient_user_id"] for r in res_win_all.data["results"]} == {"recent", "old"}

    def test_window_applied_before_grouping(self, staff_client):
        """창 **안에서** 실패했다가 창 **뒤에** 재시도된 사람도 남아야 한다.

        접은 뒤 `last_activity_at` 으로 잘랐다면 이 사람이 통째로 빠진다 — 프론트가
        DM-16 에서 명시적으로 경고한 케이스라 회귀로 고정한다.
        """
        camp = _mk_campaign()
        _log(camp, "retried", SentDMLog.Status.FAILED_TOKEN, created=_at(2))
        # 재시도는 창 밖(창 종료 이후)
        _log(camp, "retried", SentDMLog.Status.DELIVERED, created=_at(20))

        res = _recipients(staff_client, campaign_id=str(camp.id), start=RANGE_START, end=RANGE_END)
        assert res.status_code == 200
        rows = {r["recipient_user_id"]: r for r in res.data["results"]}
        assert "retried" in rows, "창 안 실패가 있는데 행이 사라졌다 (그룹핑 후 필터 회귀)"
        # 창 안에는 실패만 있으므로 '보냄'이 아니어야 한다 — 창 밖 성공이 새어 들어오면 True 가 된다.
        assert rows["retried"]["sent"] is False
        assert rows["retried"]["dm_count"] == 1

    def test_representative_log_is_chosen_inside_window(self, staff_client):
        """사유(error_*)의 대표 로그도 창 안에서 고른다 — 창 밖 최신 실패가 덮으면 안 된다."""
        camp = _mk_campaign()
        # 창 안: 숨김함 유입(2534025)
        _log(
            camp,
            "who",
            SentDMLog.Status.FAILED_PARAM,
            created=_at(2),
            code="100",
            subcode="2534025",
        )
        # 창 밖(더 최신): 토큰 만료
        _log(camp, "who", SentDMLog.Status.FAILED_TOKEN, created=_at(20), code="190")

        res = _recipients(staff_client, campaign_id=str(camp.id), start=RANGE_START, end=RANGE_END)
        row = res.data["results"][0]
        assert row["error_reason"] == "hidden_spam_inbox", row["error_reason"]

        # 창을 안 걸면 창 밖 최신 실패가 대표가 된다(기존 동작).
        res_all = _recipients(staff_client, campaign_id=str(camp.id))
        assert res_all.data["results"][0]["error_reason"] == "connection_lost"

    def test_window_combines_with_existing_filters(self, staff_client):
        """기존 필터(error_policy 등)와 AND 로 조합된다."""
        camp = _mk_campaign()
        now = timezone.now()
        # 조사 필요(사전 미등록 조합) / 자동 처리(토큰 만료) 각 1명 — 둘 다 창 안
        _log(camp, "inv", SentDMLog.Status.FAILED, created=now - timedelta(hours=1), code="9999")
        _log(
            camp, "nrm", SentDMLog.Status.FAILED_TOKEN, created=now - timedelta(hours=1), code="190"
        )
        _log(camp, "oldinv", SentDMLog.Status.FAILED, created=now - timedelta(days=9), code="9999")

        res = _recipients(
            staff_client, campaign_id=str(camp.id), window="24h", error_policy=INVESTIGATE
        )
        assert {r["recipient_user_id"] for r in res.data["results"]} == {"inv"}

    @pytest.mark.parametrize(
        "params,field",
        [
            ({"window": "bogus"}, "window"),
            ({"start": "2026-01-01"}, "start"),  # end 누락
            ({"start": "not-a-date", "end": "2026-01-02"}, "start"),
            ({"start": "2026-01-05", "end": "2026-01-01"}, "start"),  # 역순
        ],
    )
    def test_invalid_range_is_400_not_silently_ignored(self, staff_client, params, field):
        """잘못된 값을 조용히 무시하면 '필터했는데 전체가 나온다'가 된다."""
        res = _recipients(staff_client, **params)
        assert res.status_code == 400, res.data
        assert res.data["error"]["details"]["field"] == field


# ══════════════════════════════════════════════════════════════════════
# DM-17 — dm_quality 의 사람 수
# ══════════════════════════════════════════════════════════════════════
class TestNotSentPeople:
    @staticmethod
    def _seed(camp):
        """창 안에 4명 · 5건. 한 명은 같은 사유로 2번 실패한다(건수 2 ↔ 사람 1)."""
        # 자동 처리 — 토큰 만료 2건, 같은 사람
        _log(camp, "p1", SentDMLog.Status.FAILED_TOKEN, created=_at(2, 9), code="190")
        _log(camp, "p1", SentDMLog.Status.FAILED_TOKEN, created=_at(2, 10), code="190")
        # 자동 처리 — 토큰 만료, 다른 사람
        _log(camp, "p2", SentDMLog.Status.FAILED_TOKEN, created=_at(3), code="190")
        # 조사 필요 — 사전 미등록 조합
        _log(camp, "p3", SentDMLog.Status.FAILED, created=_at(3), code="9999")
        # 건너뜀 — 월 한도
        _log(
            camp,
            "p4",
            SentDMLog.Status.SKIPPED,
            created=_at(4),
            msg="monthly_dm_limit_reached",
        )

    def test_range_is_isolated(self, staff_client):
        """이 창에 우리 데이터 말고는 없다 — 아래 정확 일치 단언의 전제."""
        block = _ops_dm_quality(staff_client, start=RANGE_START, end=RANGE_END)
        assert (
            block["not_sent_people"]["total"] == 0
        ), "격리 창에 다른 데이터가 있다 — RANGE_START/END 를 비어 있는 구간으로 옮길 것"

    def test_people_is_person_scale_not_event_scale(self, staff_client):
        camp = _mk_campaign()
        self._seed(camp)
        block = _ops_dm_quality(staff_client, start=RANGE_START, end=RANGE_END)

        by_reason = {b["reason"]: b for b in block["not_sent_people"]["by_reason"]}
        # 연결 끊김(190): 3건이지만 2명 (p1 이 두 번 실패)
        assert by_reason["connection_lost"]["people"] == 2
        token_rows = [r for r in block["failure_breakdown"] if r["reason"] == "connection_lost"]
        assert sum(r["count"] for r in token_rows) == 3

    def test_block_identities(self, staff_client):
        """investigate + normal == total, Σ by_reason == total."""
        camp = _mk_campaign()
        self._seed(camp)
        people = _ops_dm_quality(staff_client, start=RANGE_START, end=RANGE_END)["not_sent_people"]
        assert people["total"] == 4  # p1~p4
        assert people["investigate"] + people["normal"] == people["total"]
        assert sum(b["people"] for b in people["by_reason"]) == people["total"]
        assert people["investigate"] == 1  # p3
        assert people["normal"] == 3  # p1, p2(토큰) + p4(건너뜀)

    @pytest.mark.parametrize("policy", [INVESTIGATE, NORMAL])
    def test_policy_people_equals_recipients_count(self, staff_client, policy):
        """★ 이 라운드의 핵심 항등 — 팝업 숫자 == 링크 건너편 목록의 count."""
        camp = _mk_campaign()
        self._seed(camp)
        people = _ops_dm_quality(staff_client, start=RANGE_START, end=RANGE_END)["not_sent_people"]
        res = _recipients(staff_client, start=RANGE_START, end=RANGE_END, error_policy=policy)
        assert res.status_code == 200, res.data
        assert (
            res.data["count"] == people[policy]
        ), f"policy={policy}: 팝업 {people[policy]}명 vs 목록 {res.data['count']}행"

    def test_reason_people_equals_recipients_count(self, staff_client):
        """사유별 `보러가기` 도 같아야 한다 (오류 사유 + 건너뜀 사유 모두)."""
        camp = _mk_campaign()
        self._seed(camp)
        people = _ops_dm_quality(staff_client, start=RANGE_START, end=RANGE_END)["not_sent_people"]
        assert people["by_reason"], "by_reason 이 비었다"
        for row in people["by_reason"]:
            res = _recipients(
                staff_client,
                start=RANGE_START,
                end=RANGE_END,
                error_reason=row["reason"],
            )
            assert res.status_code == 200, res.data
            assert (
                res.data["count"] == row["people"]
            ), f"reason={row['reason']}: 팝업 {row['people']}명 vs 목록 {res.data['count']}행"

    def test_breakdown_rows_carry_reason_level_people(self, staff_client):
        """두 breakdown 의 people 은 사유 단위 값이다 (행 단위가 아님)."""
        camp = _mk_campaign()
        self._seed(camp)
        block = _ops_dm_quality(staff_client, start=RANGE_START, end=RANGE_END)
        by_reason = {b["reason"]: b["people"] for b in block["not_sent_people"]["by_reason"]}
        for row in block["failure_breakdown"] + block["skipped_breakdown"]:
            assert row["people"] == by_reason.get(row["reason"], 0), row["reason"]

    def test_counts_are_unchanged(self, staff_client):
        """건수(count)는 그대로 둔다 — 시계열·배달률이 건수 기준이라 지우면 그쪽이 깨진다."""
        camp = _mk_campaign()
        self._seed(camp)
        block = _ops_dm_quality(staff_client, start=RANGE_START, end=RANGE_END)
        assert sum(r["count"] for r in block["skipped_breakdown"]) == block["skipped"] == 1
        assert block["failed"] == 4  # 토큰 3건 + 미분류 1건 (이벤트 단위)


# ══════════════════════════════════════════════════════════════════════
# USR-1 ~ USR-4 · USR-6 — 회원 상세
# ══════════════════════════════════════════════════════════════════════
def _plan(name: str, price: int = 14900) -> SubscriptionPlan:
    plan, _ = SubscriptionPlan.objects.get_or_create(
        name=name,
        defaults={
            "display_name": {"pro": "프로", "basic": "베이직"}.get(name, name),
            "monthly_price": price,
        },
    )
    return plan


@pytest.fixture
def member_with_subscription(db):
    user = _mk_user("usr16")
    pro, basic = _plan("pro"), _plan("basic", 4900)
    now = timezone.now()
    sub = UserSubscription.objects.create(
        user=user,
        plan=pro,
        status=SubscriptionStatus.PAST_DUE,
        current_period_start=now - timedelta(days=10),
        current_period_end=now + timedelta(days=20),
        card_company="신한",
        card_number_masked="433012******1234",
        billing_key_issued_at=now - timedelta(days=40),
        monthly_amount_snapshot=14900,
        extra_ig_accounts=1,
        trial_used_at=now - timedelta(days=40),
        trial_plan=pro,
        cancelled_during_trial_at=None,
        renewal_attempts=2,
        next_billing_retry_at=now + timedelta(days=1),
        last_billing_error="REJECT_CARD_LIMIT: 한도초과로 결제에 실패했습니다.",
        pending_plan=basic,
        pending_amount_snapshot=4900,
        pending_extra_ig_accounts=0,
        cancelled_at=None,
        pause_ends_at=None,
        paused_months=None,
    )
    return user, sub


class TestUserDetailSubscriptionBlock:
    def test_all_requested_fields_present(self, staff_client, member_with_subscription):
        user, sub = member_with_subscription
        res = staff_client.get(f"{USERS_URL}{user.id}/")
        assert res.status_code == 200, res.data
        block = res.data["subscription"]

        # USR-1
        assert block["has_billing_key"] is False  # 빌링키 원문은 안 넣었으므로
        assert block["card_company"] == "신한"
        assert block["card_number_masked"] == "433012******1234"
        assert block["billing_key_issued_at"] is not None
        assert block["current_period_start"] is not None
        assert block["extra_ig_accounts"] == 1
        # 예약 플랜(베이직) 기준 + 예약 추가계정 0 → 프로 추가계정 가산 없음
        assert block["renewal_amount"] == sub.renewal_amount == 4900
        # USR-2
        assert block["trial_used_at"] is not None
        assert block["trial_plan_name"] == "pro"
        assert block["trial_plan_display_name"] == "프로"
        assert block["cancelled_during_trial_at"] is None
        # USR-3
        assert block["renewal_attempts"] == 2
        assert block["next_billing_retry_at"] is not None
        assert block["last_billing_error_code"] == "REJECT_CARD_LIMIT"
        assert block["last_billing_error_message"] == "한도초과로 결제에 실패했습니다."
        # USR-4
        assert block["pending_plan_name"] == "basic"
        assert block["pending_plan_display_name"] == "베이직"
        assert block["pending_extra_ig_accounts"] == 0
        assert block["cancelled_at"] is None
        assert block["pause_ends_at"] is None
        assert block["paused_months"] is None

    def test_secrets_never_serialized(self, staff_client, member_with_subscription):
        """원본 카드번호·빌링키·해시·customer_key 는 절대 나가지 않는다."""
        user, _ = member_with_subscription
        body = str(staff_client.get(f"{USERS_URL}{user.id}/").data)
        for forbidden in (
            "toss_billing_key",
            "billing_key_hash",
            "toss_customer_key",
            "_encrypted",
        ):
            assert forbidden not in body, forbidden

    def test_list_block_stays_narrow(self, staff_client, member_with_subscription):
        """목록은 4필드 그대로 — 확장하면 20행마다 N+1 이 난다."""
        user, _ = member_with_subscription
        res = staff_client.get(USERS_URL, {"search": user.email})
        assert res.status_code == 200
        row = next(r for r in res.data["results"] if r["id"] == user.id)
        assert set(row["subscription"]) == {
            "plan_name",
            "plan_display_name",
            "status",
            "current_period_end",
        }

    def test_billing_error_without_code_is_treated_as_sentence(self, staff_client):
        """구분자 없는 레거시 값은 통째로 message (코드 자리에 문장이 들어가지 않게)."""
        user = _mk_user("usr16b")
        UserSubscription.objects.create(
            user=user, plan=_plan("pro"), last_billing_error="알 수 없는 오류가 발생했습니다"
        )
        block = staff_client.get(f"{USERS_URL}{user.id}/").data["subscription"]
        assert block["last_billing_error_code"] == ""
        assert block["last_billing_error_message"] == "알 수 없는 오류가 발생했습니다"

    def test_no_subscription_is_null(self, staff_client):
        user = _mk_user("usr16c")
        assert staff_client.get(f"{USERS_URL}{user.id}/").data["subscription"] is None


class TestUserDetailAcquisitionBlock:
    def test_referral_tokens_marketing_and_signup_kind(self, staff_client):
        user = _mk_user("usr16d")
        code = ReferralCode.objects.create(
            code=f"C{uuid.uuid4().hex[:8].upper()}",
            description="크리에이터 협업 · 14일",
            target_plan=_plan("pro"),
            trial_days=14,
        )
        now = timezone.now()
        ReferralRedemption.objects.create(
            user=user,
            referral_code=code,
            trial_started_at=now - timedelta(days=5),
            trial_ends_at=now + timedelta(days=39),
        )
        AiTokenBalance.objects.create(user=user, balance=1200, total_used=800)
        user.marketing_opt_in = True
        user.marketing_opt_in_at = now
        user.save(update_fields=["marketing_opt_in", "marketing_opt_in_at"])
        SignupAttribution.objects.create(
            user=user, signup_kind=SignupKind.GOOGLE, channel="paid_search"
        )

        block = staff_client.get(f"{USERS_URL}{user.id}/").data["acquisition"]
        assert block["referral"]["code"] == code.code
        assert block["referral"]["description"] == "크리에이터 협업 · 14일"
        assert block["referral"]["converted_to_paid"] is False
        assert block["ai_token_balance"] == 1200
        assert block["ai_token_total_used"] == 800
        assert block["marketing_opt_in"] is True
        assert block["marketing_opt_in_at"] is not None
        assert block["signup_kind"] == "google"
        assert block["signup_kind_display"] == "Google 가입"
        assert block["signup_channel"] == "paid_search"

    def test_absent_records_are_null_not_guessed(self, staff_client):
        """귀속 행이 없는 과거 가입자는 signup_kind=null — 추측해서 표시하지 않는다."""
        user = _mk_user("usr16e")
        block = staff_client.get(f"{USERS_URL}{user.id}/").data["acquisition"]
        assert block["referral"] is None
        assert block["ai_token_balance"] is None
        assert block["signup_kind"] is None
        assert block["signup_kind_display"] == ""

    def test_signup_kind_not_derived_from_password(self, staff_client):
        """비밀번호 유무로 근사하지 않는다 — 구글 가입 + 비밀번호 재설정 조합이 오판된다."""
        user = _mk_user("usr16f")  # create_user 라 사용 가능한 비밀번호가 있다
        SignupAttribution.objects.create(user=user, signup_kind=SignupKind.GOOGLE)
        assert user.has_usable_password() is True
        block = staff_client.get(f"{USERS_URL}{user.id}/").data["acquisition"]
        assert block["signup_kind"] == "google"


# ══════════════════════════════════════════════════════════════════════
# USR-5 — 회원별 결제 이력
# ══════════════════════════════════════════════════════════════════════
class TestUserPaymentHistory:
    def test_latest_first_with_refunded_at(self, staff_client):
        user = _mk_user("pay16")
        now = timezone.now()
        old = PaymentHistory.objects.create(
            user=user,
            amount=14900,
            status=PaymentStatus.FAILED,
            description="프로",
            failure_code="REJECT_CARD_LIMIT",
            failure_message="한도초과로 결제에 실패했습니다.",
        )
        new = PaymentHistory.objects.create(
            user=user,
            amount=14900,
            status=PaymentStatus.REFUNDED,
            description="프로 · 추가 계정 1",
            receipt_url="https://example.test/receipt",
            card_company="신한",
            card_number_masked="433012******1234",
            paid_at=now,
            refunded_at=now,
        )
        PaymentHistory.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=30))

        res = staff_client.get(f"{USERS_URL}{user.id}/payments/")
        assert res.status_code == 200, res.data
        assert [r["id"] for r in res.data["results"]] == [str(new.id), str(old.id)]

        top = res.data["results"][0]
        assert top["refunded_at"] is not None
        assert top["receipt_url"] == "https://example.test/receipt"
        # 실패 건은 paid_at 이 null 이라 created_at 폴백이 필요하다 (프론트 계약)
        assert res.data["results"][1]["paid_at"] is None
        assert res.data["results"][1]["created_at"] is not None
        assert res.data["results"][1]["failure_message"] == "한도초과로 결제에 실패했습니다."

    def test_scoped_to_the_member(self, staff_client):
        me, other = _mk_user("pay16a"), _mk_user("pay16b")
        PaymentHistory.objects.create(user=me, amount=1000, status=PaymentStatus.PAID)
        PaymentHistory.objects.create(user=other, amount=2000, status=PaymentStatus.PAID)
        res = staff_client.get(f"{USERS_URL}{me.id}/payments/")
        assert res.data["count"] == 1
        assert res.data["results"][0]["amount"] == 1000

    def test_unknown_member_is_404_not_empty_list(self, staff_client):
        """오타를 조용히 삼키면 '결제 이력 없음'으로 오독된다."""
        res = staff_client.get(f"{USERS_URL}999999999/payments/")
        assert res.status_code == 404

    def test_toss_secrets_absent(self, staff_client):
        user = _mk_user("pay16c")
        PaymentHistory.objects.create(
            user=user,
            amount=1000,
            status=PaymentStatus.PAID,
            toss_payment_key="pk_should_not_leak",
            toss_idempotency_key="idem_should_not_leak",
        )
        body = str(staff_client.get(f"{USERS_URL}{user.id}/payments/").data)
        assert "should_not_leak" not in body

    def test_requires_staff(self, db):
        user = _mk_user("pay16d")
        client = APIClient()
        client.force_authenticate(user=user)
        assert client.get(f"{USERS_URL}{user.id}/payments/").status_code == 403
