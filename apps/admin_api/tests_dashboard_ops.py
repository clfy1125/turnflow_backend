"""어드민 운영 대시보드(GET /api/v1/admin/dashboard/operations/) 테스트.

대상: apps/admin_api/views/dashboard_ops.py (IsAdminUser).

주의:
- 이 파일명(tests_*.py)은 pytest 자동 수집 패턴(test_*.py)과 달라
  **경로를 명시해서 실행**해야 한다: ``pytest apps/admin_api/tests_dashboard_ops.py``.
- 테스트 DB 가 더러울 수 있어(reuse-db) 전역 카운트 단언 전에 ``clean_slate`` 픽스처로
  기존 행을 집계 창 밖으로 밀어낸다 (트랜잭션 내라 롤백됨).
- 캐시는 개발 스택과 공유하는 Redis 라 **flush(cache.clear) 금지** —
  대시보드 키만 삭제한다 (rate_governor fail-closed 센티넬 보호).
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
    SubscriptionPlan,
    SubscriptionStatus,
    TossWebhookLog,
    UserSubscription,
)
from apps.integrations.models import (
    AutoDMCampaign,
    IGAccountConnection,
    SentDMLog,
    SpamCommentLog,
    SpamFilterConfig,
)
from apps.workspace.models import Workspace

User = get_user_model()

URL = "/api/v1/admin/dashboard/operations/"
CACHE_KEYS = [f"admin:dash:ops:{w}" for w in ("1h", "24h", "today", "7d", "30d")]
LONG_AGO = timedelta(days=400)  # 캘린더월/윈도우 어디에도 안 걸리게 멀리

# ─── 공통 픽스처 (tests_subscription.py 패턴) ─────────────────────────


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(email="regular-ops@example.com", password="Pass1234!")


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email="staff-ops@example.com", password="Pass1234!", is_staff=True
    )


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
    """더러운 테스트 DB 방어 — 기존 행을 집계 창/상태 밖으로 이동 (트랜잭션 내)."""
    long_ago = timezone.now() - LONG_AGO
    # READ(종결)로 바꿔 stuck/queued/risk 집계에서도 제외
    SentDMLog.objects.all().update(status=SentDMLog.Status.READ, created_at=long_ago)
    SpamCommentLog.objects.all().update(created_at=long_ago)
    PaymentHistory.objects.all().update(created_at=long_ago)
    TossWebhookLog.objects.filter(processed=False).update(processed=True)
    UserSubscription.objects.all().update(
        status=SubscriptionStatus.CANCELLED, ig_activation_review_needed=False
    )
    IGAccountConnection.objects.all().update(
        status=IGAccountConnection.Status.REVOKED, is_active=True, token_expires_at=None
    )


@pytest.fixture
def free_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="free", defaults={"display_name": "무료", "monthly_price": 0, "sort_order": 0}
    )
    return obj


# ─── 헬퍼 팩토리 ──────────────────────────────────────────────────────


def _mk_owner(email=None):
    return User.objects.create_user(
        email=email or f"owner-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )


def _mk_conn(owner=None, username="brand_official", status=None, token_expires_at=None):
    owner = owner or _mk_owner()
    ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    return IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=username,
        account_type="BUSINESS",
        status=status or IGAccountConnection.Status.ACTIVE,
        token_expires_at=token_expires_at,
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


def _mk_dms(campaign, status, n=1, comment_id=None, error_message="", error_subcode=""):
    logs = SentDMLog.objects.bulk_create(
        SentDMLog(
            campaign=campaign,
            comment_id=comment_id if comment_id is not None else f"c_{uuid.uuid4().hex[:10]}",
            recipient_user_id=f"r_{uuid.uuid4().hex[:10]}",
            recipient_username="",
            message_sent="x",
            status=status,
            error_message=error_message,
            error_subcode=error_subcode,
            idempotency_key=uuid.uuid4().hex,
        )
        for _ in range(n)
    )
    return logs


def _backdate_dms(logs, dt):
    SentDMLog.objects.filter(pk__in=[log.pk for log in logs]).update(created_at=dt)


def _mk_spam_log(conn, status, error_message="", category=""):
    sf, _ = SpamFilterConfig.objects.get_or_create(ig_connection=conn)
    return SpamCommentLog.objects.create(
        spam_filter=sf,
        comment_id=f"sc_{uuid.uuid4().hex[:10]}",
        comment_text="x",
        commenter_user_id="u",
        commenter_username="u",
        status=status,
        error_message=error_message,
        spam_category=category,
    )


def _item(res, key):
    """action_required 배열에서 key 항목을 찾는다."""
    return next(i for i in res.data["action_required"] if i["key"] == key)


# ─── 권한 ────────────────────────────────────────────────────────────


class TestPermissions:
    def test_anonymous_401(self, client, db):
        assert client.get(URL).status_code == 401

    def test_non_staff_403(self, regular_client):
        assert regular_client.get(URL).status_code == 403


# ─── window 파라미터 ─────────────────────────────────────────────────


class TestWindowParam:
    def test_default_is_24h(self, staff_client):
        res = staff_client.get(URL)
        assert res.status_code == 200
        assert res.data["window"] == "24h"

    @pytest.mark.parametrize("window", ["1h", "24h", "today", "7d", "30d"])
    def test_valid_windows(self, staff_client, window):
        res = staff_client.get(URL, {"window": window})
        assert res.status_code == 200
        assert res.data["window"] == window

    def test_invalid_window_400_project_error_format(self, staff_client):
        res = staff_client.get(URL, {"window": "1y"})
        assert res.status_code == 400
        assert res.data["success"] is False
        assert res.data["error"]["code"] == 400
        assert res.data["error"]["details"]["allowed"] == ["1h", "24h", "today", "7d", "30d"]

    @pytest.mark.parametrize("window", ["7d", "30d"])
    def test_long_presets_use_day_granularity(self, staff_client, clean_slate, window):
        res = staff_client.get(URL, {"window": window})
        assert res.status_code == 200
        assert res.data["window"] == window
        assert res.data["dm_quality"]["series"]["granularity"] == "day"
        assert res.data["spam"]["series"]["granularity"] == "day"
        # range 항상 포함 + since == range.start
        assert res.data["range"]["start"] == res.data["since"]

    def test_range_always_present_for_presets(self, staff_client, clean_slate):
        res = staff_client.get(URL)  # 24h 기본
        assert "range" in res.data
        assert res.data["range"]["start"] == res.data["since"]
        # 프리셋 end 는 generated_at
        assert res.data["range"]["end"] == res.data["generated_at"]


# ─── 커스텀 날짜 범위 ────────────────────────────────────────────────


class TestCustomRange:
    def _del_custom(self, start, end):
        cache.delete(f"admin:dash:ops:custom:{start}:{end}")

    def test_custom_start_end_sets_window_custom_and_range(self, staff_client, clean_slate):
        start, end = "2026-07-01", "2026-07-10"
        self._del_custom(start, end)
        res = staff_client.get(URL, {"start": start, "end": end})
        assert res.status_code == 200
        assert res.data["window"] == "custom"
        # range.start = start 로컬 자정, since 와 동일
        assert res.data["range"]["start"] == res.data["since"]
        assert res.data["range"]["start"].startswith("2026-07-01T00:00:00")
        # since 는 start 자정, end 는 end+1일 자정 또는 now 중 작은 값 (과거라 end+1일 자정)
        assert res.data["range"]["end"].startswith("2026-07-11T00:00:00")

    def test_custom_span_over_2days_is_day_granularity(self, staff_client, clean_slate):
        start, end = "2026-07-01", "2026-07-10"  # span 10일 > 2일 → day
        self._del_custom(start, end)
        res = staff_client.get(URL, {"start": start, "end": end})
        assert res.data["dm_quality"]["series"]["granularity"] == "day"

    def test_custom_span_short_is_hour_granularity(self, staff_client, clean_slate):
        start, end = "2026-07-09", "2026-07-10"  # span 2일 → hour
        self._del_custom(start, end)
        res = staff_client.get(URL, {"start": start, "end": end})
        assert res.data["dm_quality"]["series"]["granularity"] == "hour"

    def test_only_start_400(self, staff_client):
        res = staff_client.get(URL, {"start": "2026-07-01"})
        assert res.status_code == 400
        assert res.data["success"] is False
        assert res.data["error"]["code"] == 400
        assert "reason" in res.data["error"]["details"]

    def test_only_end_400(self, staff_client):
        res = staff_client.get(URL, {"end": "2026-07-10"})
        assert res.status_code == 400
        assert res.data["success"] is False

    def test_reversed_range_400(self, staff_client):
        res = staff_client.get(URL, {"start": "2026-07-10", "end": "2026-07-01"})
        assert res.status_code == 400
        assert "reason" in res.data["error"]["details"]

    def test_unparseable_400(self, staff_client):
        res = staff_client.get(URL, {"start": "not-a-date", "end": "2026-07-01"})
        assert res.status_code == 400
        assert "reason" in res.data["error"]["details"]

    def test_span_over_92_days_400(self, staff_client):
        # 2026-01-01 ~ 2026-05-01 = 121일 > 92
        res = staff_client.get(URL, {"start": "2026-01-01", "end": "2026-05-01"})
        assert res.status_code == 400
        assert "92" in res.data["error"]["details"]["reason"]


# ─── 빈 상태 (clean slate) ───────────────────────────────────────────


class TestEmptyState:
    def test_empty_db_zero_counts_no_errors(self, staff_client, clean_slate):
        res = staff_client.get(URL)
        assert res.status_code == 200

        assert res.data["status_summary"]["overall"] == "ok"
        dm_sub = res.data["status_summary"]["subsystems"]["dm"]
        assert dm_sub["delivery_rate"] == 0.0  # ZeroDivisionError 없이 0.0
        assert dm_sub["insufficient_sample"] is True

        dm = res.data["dm_quality"]
        assert dm["requested"] == 0
        assert dm["succeeded"] == 0
        assert dm["failed"] == 0
        assert dm["hidden_spam"] == 0
        assert dm["delivery_rate"] == 0.0

        assert res.data["spam"]["checked"] == 0
        assert res.data["ig_connections"]["by_status"]["expired"] == 0
        assert res.data["ig_connections"]["expiring_24h"] == 0
        assert res.data["ig_connections"]["soft_deactivated"] == 0
        assert res.data["recent_errors"] == []
        assert res.data["risk_accounts"] == []

    def test_action_required_fixed_order_with_zero_counts(self, staff_client, clean_slate):
        res = staff_client.get(URL)
        keys = [i["key"] for i in res.data["action_required"]]
        assert keys == [
            "expired_tokens",
            "expiring_tokens_24h",
            "failed_param_recent",
            "failed_no_trace_recent",
            "stuck_submitting",
            "queued_window_risk",
            "past_due_subscriptions",
            "ig_activation_review",
            "unprocessed_webhooks",
        ]
        for item in res.data["action_required"]:
            assert item["count"] == 0
            assert item["severity"] == "ok"
            assert "link" in item and "params" in item["link"]


# ─── DM 도착률 임계값 경계 ───────────────────────────────────────────


class TestDeliveryThresholds:
    def _seed_rate(self, delivered: int, accepted: int):
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.DELIVERED, delivered)
        _mk_dms(camp, SentDMLog.Status.ACCEPTED, accepted)

    def test_rate_exactly_090_is_ok(self, staff_client, clean_slate):
        # 18/20 = 0.90 — strict < 라 경계값은 ok
        self._seed_rate(delivered=18, accepted=2)
        res = staff_client.get(URL)
        dm = res.data["status_summary"]["subsystems"]["dm"]
        assert dm["delivery_rate"] == 0.9
        assert dm["insufficient_sample"] is False
        assert dm["status"] == "ok"

    def test_rate_089_is_warning(self, staff_client, clean_slate):
        self._seed_rate(delivered=89, accepted=11)
        res = staff_client.get(URL)
        dm = res.data["status_summary"]["subsystems"]["dm"]
        assert dm["delivery_rate"] == 0.89
        assert dm["status"] == "warning"
        assert res.data["status_summary"]["overall"] == "warning"

    def test_rate_exactly_075_is_warning(self, staff_client, clean_slate):
        # 경계 문서화: rate == 0.75 → warning (critical 은 strict <)
        self._seed_rate(delivered=75, accepted=25)
        res = staff_client.get(URL)
        assert res.data["status_summary"]["subsystems"]["dm"]["status"] == "warning"

    def test_rate_074_is_critical(self, staff_client, clean_slate):
        self._seed_rate(delivered=74, accepted=26)
        res = staff_client.get(URL)
        dm = res.data["status_summary"]["subsystems"]["dm"]
        assert dm["status"] == "critical"
        assert res.data["status_summary"]["overall"] == "critical"

    def test_low_rate_small_sample_is_ok_with_flag(self, staff_client, clean_slate):
        # 표본 10 < 20 → 판정 안 함 (rate 0.5 여도 ok + insufficient_sample)
        self._seed_rate(delivered=5, accepted=5)
        res = staff_client.get(URL)
        dm = res.data["status_summary"]["subsystems"]["dm"]
        assert dm["sample"] == 10
        assert dm["insufficient_sample"] is True
        assert dm["status"] == "ok"


# ─── IG 토큰 만료 경계 ───────────────────────────────────────────────


class TestTokenExpiry:
    def test_expiring_boundary_and_expired_counts(self, staff_client, clean_slate):
        now = timezone.now()
        _mk_conn(token_expires_at=now + timedelta(hours=23))  # expiring_24h 포함
        _mk_conn(token_expires_at=now + timedelta(hours=25))  # 미포함
        _mk_conn(status=IGAccountConnection.Status.EXPIRED)

        res = staff_client.get(URL)
        ig = res.data["ig_connections"]
        assert ig["expiring_24h"] == 1
        assert ig["by_status"]["expired"] == 1

        sub = res.data["status_summary"]["subsystems"]["ig_connections"]
        assert sub["status"] == "warning"
        assert res.data["status_summary"]["overall"] == "warning"

        assert _item(res, "expired_tokens")["count"] == 1
        assert _item(res, "expired_tokens")["severity"] == "warning"
        assert _item(res, "expiring_tokens_24h")["count"] == 1


# ─── action_required 신호들 ──────────────────────────────────────────


class TestActionRequired:
    def test_stuck_submitting_10min_boundary(self, staff_client, clean_slate):
        now = timezone.now()
        camp = _mk_campaign(_mk_conn())
        stuck = _mk_dms(camp, SentDMLog.Status.SUBMITTING, 1)
        _backdate_dms(stuck, now - timedelta(minutes=11))  # 포함
        fresh = _mk_dms(camp, SentDMLog.Status.SUBMITTING, 1)
        _backdate_dms(fresh, now - timedelta(minutes=9))  # 미포함

        res = staff_client.get(URL)
        assert _item(res, "stuck_submitting")["count"] == 1

    def test_past_due_and_review_flags(self, staff_client, clean_slate, free_plan):
        u1 = _mk_owner()
        UserSubscription.objects.create(user=u1, plan=free_plan, status=SubscriptionStatus.PAST_DUE)
        u2 = _mk_owner()
        UserSubscription.objects.create(
            user=u2,
            plan=free_plan,
            status=SubscriptionStatus.ACTIVE,
            ig_activation_review_needed=True,
        )

        res = staff_client.get(URL)
        assert _item(res, "past_due_subscriptions")["count"] == 1
        assert _item(res, "ig_activation_review")["count"] == 1
        billing = res.data["status_summary"]["subsystems"]["billing"]
        assert billing["past_due"] == 1
        assert billing["status"] == "warning"

    def test_unprocessed_webhook_10min_boundary(self, staff_client, clean_slate):
        now = timezone.now()
        old = TossWebhookLog.objects.create(
            event_type="PAYMENT_STATUS_CHANGED", dedup_key=uuid.uuid4().hex, processed=False
        )
        TossWebhookLog.objects.filter(pk=old.pk).update(created_at=now - timedelta(minutes=11))
        fresh = TossWebhookLog.objects.create(
            event_type="PAYMENT_STATUS_CHANGED", dedup_key=uuid.uuid4().hex, processed=False
        )
        TossWebhookLog.objects.filter(pk=fresh.pk).update(created_at=now - timedelta(minutes=5))

        res = staff_client.get(URL)
        assert _item(res, "unprocessed_webhooks")["count"] == 1
        assert res.data["status_summary"]["subsystems"]["billing"]["webhook_backlog"] == 1
        assert res.data["status_summary"]["subsystems"]["billing"]["status"] == "warning"

    def test_webhook_stale_30min_is_critical(self, staff_client, clean_slate):
        now = timezone.now()
        stale = TossWebhookLog.objects.create(
            event_type="PAYMENT_STATUS_CHANGED", dedup_key=uuid.uuid4().hex, processed=False
        )
        TossWebhookLog.objects.filter(pk=stale.pk).update(created_at=now - timedelta(minutes=31))

        res = staff_client.get(URL)
        assert res.data["status_summary"]["subsystems"]["billing"]["status"] == "critical"
        assert res.data["status_summary"]["overall"] == "critical"


# ─── recent_errors (3종 병합) ────────────────────────────────────────


class TestRecentErrors:
    def test_three_sources_merged_desc(self, staff_client, clean_slate):
        now = timezone.now()
        conn = _mk_conn(username="brand_official")
        camp = _mk_campaign(conn)

        dm = _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 1, error_message="(#100) Param recipient")
        _backdate_dms(dm, now - timedelta(minutes=3))

        payer = _mk_owner(email="payer@example.com")
        pay = PaymentHistory.objects.create(
            user=payer,
            amount=14900,
            status=PaymentStatus.FAILED,
            failure_code="REJECT_CARD_COMPANY",
            failure_message="카드사 거절",
        )
        PaymentHistory.objects.filter(pk=pay.pk).update(created_at=now - timedelta(minutes=1))

        spam = _mk_spam_log(
            conn, SpamCommentLog.Status.FAILED, error_message="(#10) Permission denied"
        )
        SpamCommentLog.objects.filter(pk=spam.pk).update(created_at=now - timedelta(minutes=2))

        res = staff_client.get(URL)
        errors = res.data["recent_errors"]
        assert [e["type"] for e in errors] == [
            "payment_failure",
            "spam_hide_failure",
            "dm_failure",
        ]
        assert errors[0]["subject"] == "payer@example.com"
        assert "REJECT_CARD_COMPANY" in errors[0]["detail"]
        assert errors[1]["subject"] == "brand_official"
        assert errors[2]["subject"] == "brand_official"
        assert errors[2]["detail"].startswith("failed_param:")
        assert errors[2]["link"] == {
            "page": "/auto-dm/logs",
            "params": {"id": errors[2]["ref_id"]},
        }

    def test_capped_at_20(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 25)
        res = staff_client.get(URL)
        assert len(res.data["recent_errors"]) == 20


# ─── risk_accounts ───────────────────────────────────────────────────


class TestRiskAccounts:
    def test_expired_outranks_low_delivery(self, staff_client, clean_slate):
        # 만료 토큰 계정 (score 3)
        expired_conn = _mk_conn(username="expired_acc", status=IGAccountConnection.Status.EXPIRED)
        # 저조 도착률 계정 (표본 20, rate 0.8 → low_delivery_rate, score 2)
        low_conn = _mk_conn(
            username="low_acc", token_expires_at=timezone.now() + timedelta(days=30)
        )
        camp = _mk_campaign(low_conn)
        _mk_dms(camp, SentDMLog.Status.DELIVERED, 16)
        _mk_dms(camp, SentDMLog.Status.ACCEPTED, 4)

        res = staff_client.get(URL)
        risks = res.data["risk_accounts"]
        assert len(risks) == 2
        assert risks[0]["ig_connection_id"] == str(expired_conn.id)
        assert risks[0]["risk_score"] == 3
        assert risks[0]["reasons"] == ["token_expired"]
        assert risks[0]["metrics"]["status"] == "expired"

        assert risks[1]["ig_connection_id"] == str(low_conn.id)
        assert risks[1]["risk_score"] == 2
        assert risks[1]["reasons"] == ["low_delivery_rate"]
        assert risks[1]["metrics"]["delivery_rate_24h"] == 0.8

    def test_repeated_param_errors_reason(self, staff_client, clean_slate):
        conn = _mk_conn(username="param_acc")
        camp = _mk_campaign(conn)
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 5)

        res = staff_client.get(URL)
        risks = res.data["risk_accounts"]
        assert len(risks) == 1
        assert risks[0]["reasons"] == ["repeated_param_errors"]
        assert risks[0]["metrics"]["failed_param_24h"] == 5

    def test_hidden_spam_2534025_not_repeated_param_risk(self, staff_client, clean_slate):
        # 비팔로워 채널 미개설(2534025)은 계정 위험(repeated_param_errors)이 아니다.
        conn = _mk_conn(username="cold_audience_acc")
        camp = _mk_campaign(conn)
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 5, error_subcode="2534025")
        res = staff_client.get(URL)
        assert res.data["risk_accounts"] == []


# ─── 숨겨진 요청·스팸 분리 (복구/2534025 는 실패 아님) ────────────────


class TestHiddenSpamSeparation:
    def test_failed_excludes_hidden_spam_and_recovery(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn())
        # 진짜 실패(확인 필요)
        _mk_dms(camp, SentDMLog.Status.FAILED_TOKEN, 2)
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 3)  # subcode 없음 → 실패
        # 숨겨진 요청·스팸 (실패 아님)
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 4, error_subcode="2534025")
        _mk_dms(camp, SentDMLog.Status.RECOVERY_PENDING, 5)
        _mk_dms(camp, SentDMLog.Status.RECOVERY_EXPIRED, 1)

        dm = staff_client.get(URL).data["dm_quality"]
        assert dm["failed"] == 5  # 2 token + 3 param(비-2534025)
        assert dm["hidden_spam"] == 10  # 4 param@2534025 + 5 pending + 1 expired
        assert dm["requested"] == 15

    def test_recovery_delivered_counts_as_succeeded(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.RECOVERY_DELIVERED, 3)
        dm = staff_client.get(URL).data["dm_quality"]
        assert dm["succeeded"] == 3
        assert dm["failed"] == 0
        assert dm["hidden_spam"] == 0

    def test_action_required_failed_param_excludes_2534025(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 4, error_subcode="2534025")
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 2)  # 진짜 파라미터 오류
        res = staff_client.get(URL)
        assert _item(res, "failed_param_recent")["count"] == 2

    def test_recent_errors_excludes_hidden_spam(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn(username="hidden_acc"))
        _mk_dms(camp, SentDMLog.Status.FAILED_PARAM, 2, error_subcode="2534025")
        _mk_dms(camp, SentDMLog.Status.RECOVERY_PENDING, 2)
        _mk_dms(camp, SentDMLog.Status.RECOVERY_EXPIRED, 1)
        _mk_dms(camp, SentDMLog.Status.FAILED_TOKEN, 1, error_message="token dead")

        errors = staff_client.get(URL).data["recent_errors"]
        dm_errors = [e for e in errors if e["type"] == "dm_failure"]
        assert len(dm_errors) == 1
        assert dm_errors[0]["detail"].startswith("failed_token")

    def test_series_splits_hidden_spam_from_failed(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.RECOVERY_PENDING, 2)
        _mk_dms(camp, SentDMLog.Status.FAILED_TOKEN, 1)

        buckets = staff_client.get(URL, {"window": "24h"}).data["dm_quality"]["series"]["buckets"]
        assert sum(b["hidden_spam"] for b in buckets) == 2
        assert sum(b["failed"] for b in buckets) == 1


# ─── 시계열 제로필 ───────────────────────────────────────────────────


class TestSeries:
    def test_1h_window_5m_buckets_zero_filled(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.DELIVERED, 3)
        outside = _mk_dms(camp, SentDMLog.Status.DELIVERED, 1)
        _backdate_dms(outside, timezone.now() - timedelta(hours=2))  # 윈도우 밖

        res = staff_client.get(URL, {"window": "1h"})
        series = res.data["dm_quality"]["series"]
        assert series["granularity"] == "5m"
        buckets = series["buckets"]
        assert len(buckets) == 13  # floor(now-1h) ~ floor(now), 5분 간격 제로필
        assert sum(b["requested"] for b in buckets) == 3  # 밖 로그 제외
        # ts 단조 증가 (제로필 연속성)
        ts_list = [b["ts"] for b in buckets]
        assert ts_list == sorted(ts_list)

    def test_24h_window_hour_buckets(self, staff_client, clean_slate):
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.SKIPPED, 2)

        res = staff_client.get(URL, {"window": "24h"})
        series = res.data["dm_quality"]["series"]
        assert series["granularity"] == "hour"
        assert len(series["buckets"]) == 25  # floor(since) ~ floor(now) 포함
        assert sum(b["skipped"] for b in series["buckets"]) == 2

    def test_spam_series_zero_filled(self, staff_client, clean_slate):
        conn = _mk_conn()
        _mk_spam_log(conn, SpamCommentLog.Status.HIDDEN, category="promo")

        res = staff_client.get(URL, {"window": "1h"})
        series = res.data["spam"]["series"]
        assert len(series["buckets"]) == 13
        assert sum(b["hidden"] for b in series["buckets"]) == 1
        assert res.data["spam"]["top_categories"] == [{"category": "promo", "count": 1}]


# ─── 캐싱 ────────────────────────────────────────────────────────────


class TestCaching:
    def test_second_call_served_from_cache(self, staff_client, clean_slate):
        first = staff_client.get(URL)
        camp = _mk_campaign(_mk_conn())
        _mk_dms(camp, SentDMLog.Status.DELIVERED, 1)

        second = staff_client.get(URL)  # 30s TTL 내 — 캐시 히트
        assert second.data["generated_at"] == first.data["generated_at"]
        assert second.data["dm_quality"]["requested"] == 0  # 신규 데이터 미반영(캐시)

        cache.delete("admin:dash:ops:24h")
        third = staff_client.get(URL)
        assert third.data["generated_at"] != first.data["generated_at"]
        assert third.data["dm_quality"]["requested"] == 1

    def test_windows_cached_separately(self, staff_client, clean_slate):
        res_24h = staff_client.get(URL, {"window": "24h"})
        res_1h = staff_client.get(URL, {"window": "1h"})
        assert res_24h.data["window"] == "24h"
        assert res_1h.data["window"] == "1h"
