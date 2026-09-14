"""홈 알림 판정·닫기 테스트.

⚠️ 이 저장소의 pytest 는 **dev DB 를 그대로 쓴다**(test-db-not-clean). 그래서
   - 사용자 이메일은 uuid 로 만들어 기존 행과 충돌하지 않게 하고,
   - "전체 개수" 대신 **내가 만든 워크스페이스의 알림 코드 집합**만 단언한다.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.models import SubscriptionPlan, SubscriptionStatus, UserSubscription
from apps.home.alerts import (
    CAMPAIGN_POST_RESTRICTED,
    CREATE_CAMPAIGN,
    DM_QUOTA_EXHAUSTED,
    DM_QUOTA_WARNING,
    DM_SEND_BLOCKED,
    IG_ACCOUNT_DISABLED,
    IG_DISCONNECTED,
    LEVEL_CRITICAL,
    LEVEL_TODO,
    PAYMENT_FAILED,
    RECENT_POST_NO_CAMPAIGN,
    Alert,
    build_home_alerts,
)
from apps.integrations.models import AutoDMCampaign, DMAccountBlock, IGAccountConnection
from apps.workspace.models import Membership, Workspace

pytestmark = pytest.mark.django_db


# ── 헬퍼 ────────────────────────────────────────────────────────────────────
def make_user(plan="free", **sub_fields):
    U = get_user_model()
    user = U.objects.create_user(
        email=f"home-test-{uuid.uuid4().hex[:10]}@test.com", password="Test1234!"
    )
    ws = Workspace.objects.create(
        name="홈알림 테스트", slug=f"home-t-{uuid.uuid4().hex[:10]}", owner=user
    )
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    sub, _ = UserSubscription.objects.get_or_create(
        user=user, defaults={"plan": SubscriptionPlan.objects.get(name=plan)}
    )
    sub.plan = SubscriptionPlan.objects.get(name=plan)
    for k, v in sub_fields.items():
        setattr(sub, k, v)
    sub.save()
    return user, ws


def make_conn(ws, suffix=0, **fields):
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"{uuid.uuid4().int % 10**15:015d}",
        username=f"t{suffix}",
        _encrypted_access_token="",
        status=IGAccountConnection.Status.ACTIVE,
    )
    conn.access_token = "FAKE"
    for k, v in fields.items():
        setattr(conn, k, v)
    conn.save()
    return conn


def codes(ws, user):
    return {a["code"] for a in build_home_alerts(ws, user)["alerts"]}


def find(ws, user, code):
    for a in build_home_alerts(ws, user)["alerts"]:
        if a["code"] == code:
            return a
    return None


# ── 기본 상태 ───────────────────────────────────────────────────────────────
def test_no_alerts_still_returns_default_card():
    """알릴 게 없어도 홈이 비면 안 된다 — create_campaign 이 기본 상태로 남는다."""
    user, ws = make_user()
    make_conn(ws, webhook_healthy=True)
    result = build_home_alerts(ws, user)
    assert result["counts"]["critical"] == 0
    assert CREATE_CAMPAIGN in {a["code"] for a in result["alerts"]}


def test_default_card_moves_to_back_when_campaign_exists():
    user, ws = make_user()
    conn = make_conn(ws, webhook_healthy=True)
    AutoDMCampaign.objects.create(
        ig_connection=conn, name="c1", media_id="m1", message_template="hi"
    )
    a = find(ws, user, CREATE_CAMPAIGN)
    assert a["data"]["is_default"] is True
    assert a["rank"] == 999


def test_default_card_is_not_dismissible():
    """닫히면 홈이 빈다 — 이것만 todo 지만 닫을 수 없다."""
    assert Alert(code=CREATE_CAMPAIGN, level=LEVEL_TODO).dismissible is False


# ── 멈춤 판정 ───────────────────────────────────────────────────────────────
def test_ig_disconnected():
    user, ws = make_user()
    make_conn(
        ws,
        status=IGAccountConnection.Status.ERROR,
        reconnect_reason="token_invalidated",
        webhook_healthy=True,
    )
    a = find(ws, user, IG_DISCONNECTED)
    assert a is not None
    assert a["level"] == LEVEL_CRITICAL
    assert a["dismissible"] is False
    assert a["data"]["reason"] == "token_invalidated"
    assert a["data"]["revives_on_reconnect"] is True


def test_payment_failed():
    user, ws = make_user(
        plan="basic",
        status=SubscriptionStatus.PAST_DUE,
        current_period_end=timezone.now() - timedelta(days=1),
    )
    a = find(ws, user, PAYMENT_FAILED)
    assert a is not None and a["rank"] == 10  # 서열 1위 — 방치하면 서비스 전체가 내려간다


def test_send_blocked_uses_db_fallback():
    """캐시가 비어 있어도(Redis flush·DR) DB 폴백으로 같은 판정이 나와야 한다."""
    user, ws = make_user()
    conn = make_conn(ws, webhook_healthy=True)
    DMAccountBlock.objects.update_or_create(
        external_account_id=conn.external_account_id,
        defaults={"cooldown_until": timezone.now() + timedelta(hours=2), "level": 1},
    )
    a = find(ws, user, DM_SEND_BLOCKED)
    assert a is not None
    assert a["data"]["seconds_remaining"] > 0
    assert a["data"]["auto_resumes"] is True


def test_campaign_post_restricted():
    user, ws = make_user()
    conn = make_conn(ws, webhook_healthy=True)
    AutoDMCampaign.objects.create(
        ig_connection=conn,
        name="제한 캠페인",
        media_id="m9",
        message_template="hi",
        status=AutoDMCampaign.Status.PAUSED,
        auto_paused_at=timezone.now(),
        auto_paused_reason="post_restricted",
    )
    a = find(ws, user, CAMPAIGN_POST_RESTRICTED)
    assert a is not None
    # 범위를 모르면 오해가 생기는 경우 — 다른 게시물은 정상이라는 사실을 함께 준다
    assert a["data"]["other_posts_unaffected"] is True


def test_user_paused_campaign_is_not_reported():
    """사용자가 직접 멈춘 것은 알림이 아니다 — 시스템 자동 정지만 알린다."""
    user, ws = make_user()
    conn = make_conn(ws, webhook_healthy=True)
    AutoDMCampaign.objects.create(
        ig_connection=conn,
        name="내가 끈 캠페인",
        media_id="m8",
        message_template="hi",
        status=AutoDMCampaign.Status.PAUSED,
    )
    assert CAMPAIGN_POST_RESTRICTED not in codes(ws, user)


def test_inactive_account_only_when_slot_is_free():
    """슬롯이 꽉 찼으면 꺼둔 것은 사용자의 선택이다 — 알리지 않는다."""
    user, ws = make_user()  # free = 허용량 1
    make_conn(ws, 1, webhook_healthy=True)  # 활성 1 = 허용량 소진
    make_conn(ws, 2, is_active=False)
    assert IG_ACCOUNT_DISABLED not in codes(ws, user)


# ── 한도 ────────────────────────────────────────────────────────────────────
def test_quota_exhausted_reports_blocked_and_resumable(monkeypatch):
    user, ws = make_user()
    make_conn(ws, webhook_healthy=True)
    monkeypatch.setattr("apps.billing.dm_limits.check_dm_quota", lambda o: (False, 200, 200))
    monkeypatch.setattr("apps.billing.dm_limits.count_quota_skipped_dms", lambda o: 37)
    monkeypatch.setattr("apps.billing.dm_limits.count_revivable_quota_skipped_dms", lambda o: 31)
    a = find(ws, user, DM_QUOTA_EXHAUSTED)
    assert a is not None
    # 결제를 유도하려면 "지금 몇 건이 막혔나"와 "결제하면 몇 건이 바로 나가나"가 둘 다 필요하다
    assert a["data"]["blocked_count"] == 37
    assert a["data"]["resumable_count"] == 31
    assert a["data"]["resumes_on_upgrade"] is True


def test_quota_warning_at_80_percent(monkeypatch):
    user, ws = make_user()
    make_conn(ws, webhook_healthy=True)
    monkeypatch.setattr("apps.billing.dm_limits.check_dm_quota", lambda o: (True, 170, 200))
    a = find(ws, user, DM_QUOTA_WARNING)
    assert a is not None and a["data"]["remaining"] == 30


def test_unlimited_plan_has_no_quota_alert(monkeypatch):
    user, ws = make_user(plan="pro")
    make_conn(ws, webhook_healthy=True)
    monkeypatch.setattr("apps.billing.dm_limits.check_dm_quota", lambda o: (True, 0, -1))
    got = codes(ws, user)
    assert DM_QUOTA_EXHAUSTED not in got and DM_QUOTA_WARNING not in got


# ── 최근 게시물 ─────────────────────────────────────────────────────────────
def test_recent_post_without_campaign():
    user, ws = make_user()
    make_conn(
        ws,
        webhook_healthy=True,
        latest_media_id="media-新",
        latest_media_at=timezone.now() - timedelta(days=2),
    )
    assert RECENT_POST_NO_CAMPAIGN in codes(ws, user)


def test_recent_post_older_than_a_week_is_ignored():
    user, ws = make_user()
    make_conn(
        ws,
        webhook_healthy=True,
        latest_media_id="media-old",
        latest_media_at=timezone.now() - timedelta(days=9),
    )
    assert RECENT_POST_NO_CAMPAIGN not in codes(ws, user)


def test_recent_post_with_campaign_is_ignored():
    user, ws = make_user()
    conn = make_conn(
        ws,
        webhook_healthy=True,
        latest_media_id="media-has",
        latest_media_at=timezone.now() - timedelta(hours=3),
    )
    AutoDMCampaign.objects.create(
        ig_connection=conn, name="c", media_id="media-has", message_template="hi"
    )
    assert RECENT_POST_NO_CAMPAIGN not in codes(ws, user)


# ── 강제 팝업 ───────────────────────────────────────────────────────────────
def test_blocking_modal_when_over_allowance():
    user, ws = make_user()  # free = 1개
    for i in range(3):
        make_conn(ws, i, webhook_healthy=True)
    result = build_home_alerts(ws, user)
    assert result["blocking"]["code"] == "ig_account_selection_required"
    assert result["blocking"]["data"]["active_accounts"] == 3


def test_blocking_matches_activation_view_single_source():
    """홈 강제팝업과 계정 선택 화면이 **같은 함수**를 봐야 한다.

    조건을 복제하면 "팝업은 떴는데 조정할 게 없다"(또는 그 반대)가 생긴다.
    """
    from apps.billing.subscription_utils import ig_activation_state

    user, ws = make_user()
    for i in range(3):
        make_conn(ws, i, webhook_healthy=True)
    result = build_home_alerts(ws, user)
    state = ig_activation_state(user)
    assert bool(result["blocking"]) is state["needs_activation_adjustment"]


# ── 정렬·닫기 ───────────────────────────────────────────────────────────────
def test_alerts_are_sorted_by_rank():
    user, ws = make_user(
        plan="basic",
        status=SubscriptionStatus.PAST_DUE,
        current_period_end=timezone.now() - timedelta(days=1),
        extra_ig_accounts=3,
    )
    make_conn(ws, 1, status=IGAccountConnection.Status.ERROR)
    make_conn(ws, 2, webhook_healthy=True)
    ranks = [a["rank"] for a in build_home_alerts(ws, user)["alerts"]]
    assert ranks == sorted(ranks)
    assert ranks[0] == 10  # 결제 실패가 가장 위


def test_dismissal_hides_only_matching_target():
    from apps.home.models import HomeAlertDismissal

    user, ws = make_user()
    make_conn(
        ws,
        webhook_healthy=True,
        latest_media_id="media-A",
        latest_media_at=timezone.now() - timedelta(hours=2),
    )
    assert RECENT_POST_NO_CAMPAIGN in codes(ws, user)

    HomeAlertDismissal.objects.create(
        user=user, workspace=ws, code=RECENT_POST_NO_CAMPAIGN, target_key="media-A"
    )
    assert RECENT_POST_NO_CAMPAIGN not in codes(ws, user)

    # 새 게시물이면 다시 떠야 한다 — 대상이 바뀌었으므로
    conn = IGAccountConnection.objects.filter(workspace=ws).first()
    conn.latest_media_id = "media-B"
    conn.save()
    assert RECENT_POST_NO_CAMPAIGN in codes(ws, user)


def test_critical_dismissal_is_ignored_even_if_row_exists():
    """멈춤은 닫을 수 없다 — 행이 있어도 계속 떠야 한다(잘못 쌓인 행 방어)."""
    from apps.home.models import HomeAlertDismissal

    user, ws = make_user()
    make_conn(ws, status=IGAccountConnection.Status.ERROR, webhook_healthy=True)
    conn = IGAccountConnection.objects.filter(workspace=ws).first()
    HomeAlertDismissal.objects.create(
        user=user, workspace=ws, code=IG_DISCONNECTED, target_key=str(conn.id)
    )
    assert IG_DISCONNECTED in codes(ws, user)


def test_one_broken_check_does_not_kill_the_rest(monkeypatch):
    """홈 첫 화면이다 — 판정 하나가 터져도 나머지는 나와야 한다."""
    user, ws = make_user()
    make_conn(ws, webhook_healthy=True)

    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("apps.home.alerts._check_link_pages", boom)
    result = build_home_alerts(ws, user)
    assert CREATE_CAMPAIGN in {a["code"] for a in result["alerts"]}
