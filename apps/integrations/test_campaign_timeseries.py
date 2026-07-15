"""캠페인 신규 요청자 시계열 테스트 — Feature B (new_requester_timeseries + endpoint).

커버리지:
  - 사람 단위 최초 요청 버킷팅(전 생애 MIN 후 윈도우 필터), 재요청은 신규 아님
  - reward/child(parent_log) 제외, 복구 재전송 중복 없음
  - 제로필·버킷 개수·KST 일 경계·불변식(sum==window, all==people_rollup total)
  - 빈 캠페인, range 오류 400, 타 워크스페이스 404, 익명 401, retention 카나리

NOTE(test-db-not-clean): 캠페인 스코프 격리(엔드포인트가 캠페인 단위)로 델타 없이 단언.
"""

import uuid
from datetime import UTC, datetime, timedelta
from datetime import timezone as dt_tz

import pytest
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.campaign_stats import ROOT_DM_Q, new_requester_timeseries, people_rollup
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace

KST = dt_tz(timedelta(hours=9))
UTC = UTC
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=KST)  # 고정 기준 시각
URL = "/api/v1/integrations/auto-dm-campaigns/{}/timeseries/"


def _user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email=f"ts_{uuid.uuid4().hex[:10]}@example.com", password="pw12345!"
    )


def _setup(user):
    ws = Workspace.objects.create(name="ts-ws", slug=f"ts-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return ws, conn


def _campaign(conn, **kw):
    defaults = {
        "ig_connection": conn,
        "name": f"ts-{uuid.uuid4().hex[:6]}",
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "message_template": "hi",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, *, created_at, recipient="r1", dm_kind=None, parent=None, status=None, **kw):
    log = SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"c-{uuid.uuid4().hex[:8]}",
        comment_text="hi",
        recipient_user_id=recipient,
        recipient_username="buyer",
        message_sent="msg",
        status=status or SentDMLog.Status.ACCEPTED,
        idempotency_key=uuid.uuid4().hex,
        dm_kind=dm_kind or SentDMLog.DMKind.OPENING,
        parent_log=parent,
        **kw,
    )
    # created_at 은 auto_now_add 라 직접 대입 불가 → update 로 백데이트.
    SentDMLog.objects.filter(id=log.id).update(created_at=created_at)
    log.refresh_from_db()
    return log


def _bucket_map(result):
    """series → {'YYYY-MM-DD' or 'YYYY-MM-DD HH': new_requesters}."""
    fmt = "%Y-%m-%d %H" if result["granularity"] == "hour" else "%Y-%m-%d"
    return {p["bucket"].strftime(fmt): p["new_requesters"] for p in result["series"]}


@pytest.mark.django_db
class TestBucketing:
    def test_first_request_counted_once_at_earliest(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        # 같은 사람: 3일 전 + 1시간 전 두 번 요청 → 최초(3일 전)에 1회만.
        _log(camp, recipient="P", created_at=NOW - timedelta(days=3))
        _log(camp, recipient="P", created_at=NOW - timedelta(hours=1))

        res = new_requester_timeseries(camp, "all", now=NOW)
        assert res["totals"]["lifetime_unique_requesters"] == 1
        assert res["totals"]["window_new_requesters"] == 1
        assert sum(p["new_requesters"] for p in res["series"]) == 1
        bmap = _bucket_map(res)
        assert bmap["2026-07-12"] == 1

    def test_existing_requester_not_new_in_24h(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        _log(camp, recipient="P", created_at=NOW - timedelta(days=3))
        _log(camp, recipient="P", created_at=NOW - timedelta(hours=1))

        res = new_requester_timeseries(camp, "24h", now=NOW)
        assert res["totals"]["window_new_requesters"] == 0
        assert sum(p["new_requesters"] for p in res["series"]) == 0

    def test_reward_and_child_excluded(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        root = _log(camp, recipient="Z", created_at=NOW - timedelta(days=1))
        _log(  # reward → 제외
            camp,
            recipient="X",
            created_at=NOW - timedelta(days=1),
            dm_kind=SentDMLog.DMKind.REWARD,
        )
        _log(  # child(parent_log 있음) → 제외
            camp,
            recipient="X2",
            created_at=NOW - timedelta(days=1),
            parent=root,
        )
        res = new_requester_timeseries(camp, "all", now=NOW)
        assert res["totals"]["lifetime_unique_requesters"] == 1  # Z 만

    def test_recovery_delivered_counts_once(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        _log(
            camp,
            recipient="R",
            created_at=NOW - timedelta(days=1),
            status=SentDMLog.Status.RECOVERY_DELIVERED,
        )
        res = new_requester_timeseries(camp, "all", now=NOW)
        assert res["totals"]["lifetime_unique_requesters"] == 1

    def test_zero_fill_and_bucket_counts(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        _log(camp, recipient="P", created_at=NOW - timedelta(hours=2))

        res24 = new_requester_timeseries(camp, "24h", now=NOW)
        assert res24["granularity"] == "hour"
        assert len(res24["series"]) == 24
        res7 = new_requester_timeseries(camp, "7d", now=NOW)
        assert res7["granularity"] == "day"
        assert len(res7["series"]) == 7

    def test_kst_day_boundary(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        # 14:59 UTC = 23:59 KST(07-10), 15:01 UTC = 00:01 KST(07-11) → 다른 일 버킷.
        _log(camp, recipient="A", created_at=datetime(2026, 7, 10, 14, 59, tzinfo=UTC))
        _log(camp, recipient="B", created_at=datetime(2026, 7, 10, 15, 1, tzinfo=UTC))

        res = new_requester_timeseries(camp, "all", now=NOW)
        bmap = _bucket_map(res)
        assert bmap["2026-07-10"] == 1
        assert bmap["2026-07-11"] == 1

    def test_invariants_and_people_rollup_consistency(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        _log(camp, recipient="A", created_at=NOW - timedelta(days=2))
        _log(camp, recipient="B", created_at=NOW - timedelta(days=1))
        _log(camp, recipient="A", created_at=NOW - timedelta(hours=3))  # 재요청

        res = new_requester_timeseries(camp, "all", now=NOW)
        total = res["totals"]["lifetime_unique_requesters"]
        assert total == 2
        assert res["totals"]["window_new_requesters"] == total
        assert sum(p["new_requesters"] for p in res["series"]) == total
        # people_rollup 과 동일 키공간
        assert people_rollup(camp.dm_logs.filter(ROOT_DM_Q))["total"] == total

    def test_empty_campaign(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        res_all = new_requester_timeseries(camp, "all", now=NOW)
        assert res_all["series"] == []
        assert res_all["totals"]["lifetime_unique_requesters"] == 0
        assert res_all["totals"]["first_request_at"] is None
        assert res_all["totals"]["last_request_at"] is None

        res24 = new_requester_timeseries(camp, "24h", now=NOW)
        assert len(res24["series"]) == 24
        assert all(p["new_requesters"] == 0 for p in res24["series"])

    def test_history_complete_flag(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        res = new_requester_timeseries(camp, "all", now=NOW)
        assert res["history_complete"] is True


@pytest.mark.django_db
class TestEndpoint:
    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_ok(self):
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        _log(camp, recipient="A", created_at=NOW - timedelta(days=1))
        res = self._client(user).get(URL.format(camp.id) + "?range=7d")
        assert res.status_code == 200
        body = res.json()
        assert body["range"] == "7d"
        assert body["granularity"] == "day"
        assert body["timezone"] == "Asia/Seoul"
        assert len(body["series"]) == 7
        # ISO8601 +09:00
        assert body["series"][0]["bucket"].endswith("+09:00")

    def test_invalid_range_400(self):
        user = _user()
        _, conn = _setup(user)
        camp = _campaign(conn)
        res = self._client(user).get(URL.format(camp.id) + "?range=1w")
        assert res.status_code == 400

    def test_other_workspace_404(self):
        owner = _user()
        _, conn = _setup(owner)
        camp = _campaign(conn)
        other = _user()  # 다른 워크스페이스 사용자
        res = self._client(other).get(URL.format(camp.id))
        assert res.status_code == 404

    def test_anonymous_401(self):
        _, conn = _setup(_user())
        camp = _campaign(conn)
        res = APIClient().get(URL.format(camp.id))
        assert res.status_code == 401


def test_retention_canary():
    # 로그 보존정책이 켜지면 MIN(created_at) 파생 시계열이 왜곡된다.
    # 활성화 전 업그레이드 경로: config/settings/base.py 의 SENTDMLOG_ARCHIVE_RETENTION_DAYS 주석 참조.
    assert getattr(settings, "SENTDMLOG_ARCHIVE_RETENTION_DAYS", 0) == 0, (
        "SENTDMLOG_ARCHIVE_RETENTION_DAYS 가 활성화됨 — new_requester_timeseries 를 롤업 "
        "테이블 기반으로 전환하기 전에는 '전체 기간' 신규 요청자 차트가 부정확해진다."
    )
