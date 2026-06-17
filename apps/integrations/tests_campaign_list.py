"""Auto DM 캠페인 목록(list) 필터·정렬 테스트.

GET /api/v1/integrations/auto-dm-campaigns/
  - status 필터 (단일/다중/잘못된 값)
  - 생성일 범위 필터 (created_after/created_before, 날짜만/경계 포함/잘못된 형식)
  - ordering 정렬 (필드/내림차순/잘못된 필드)
  - 기본 정렬 -created_at

함수 스코프 fixture 라 각 테스트는 자신의 workspace 캠페인만 본다(테넌시 격리)
→ 전역 카운트가 아니라 내 캠페인 이름 집합/순서로 단언한다.
"""

from datetime import UTC, datetime

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection
from apps.workspace.models import Membership, Workspace

URL = "/api/v1/integrations/auto-dm-campaigns/"


@pytest.fixture
def ws_user(db):
    User = get_user_model()
    user = User.objects.create_user(
        email="listfilter@example.com", password="pw12345!", full_name="List Tester"
    )
    ws = Workspace.objects.create(name="List WS", slug="list-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


@pytest.fixture
def conn(ws_user):
    ws, _ = ws_user
    c = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id="ig_list_001",
        username="listuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    c.access_token = "mock_token_list"
    c.save()
    return c


def _make(conn, name, **kwargs):
    # media_id 를 비워 두어 list 의 Graph API media_url 보강 경로를 타지 않게 한다.
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": name,
        "message_template": "hi",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kwargs)
    return AutoDMCampaign.objects.create(**defaults)


def _set_created(campaign, dt):
    # auto_now_add 우회: update() 는 auto_now_add 를 트리거하지 않는다.
    AutoDMCampaign.objects.filter(id=campaign.id).update(created_at=dt)


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestListStatusFilter:
    def test_single_status(self, ws_user, conn):
        _, user = ws_user
        _make(conn, "a", status=AutoDMCampaign.Status.ACTIVE)
        _make(conn, "p", status=AutoDMCampaign.Status.PAUSED)
        resp = _client(user).get(URL, {"status": "paused"})
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["p"]

    def test_multiple_status_comma(self, ws_user, conn):
        _, user = ws_user
        _make(conn, "a", status=AutoDMCampaign.Status.ACTIVE)
        _make(conn, "p", status=AutoDMCampaign.Status.PAUSED)
        _make(conn, "i", status=AutoDMCampaign.Status.INACTIVE)
        resp = _client(user).get(URL, {"status": "active,paused"})
        assert resp.status_code == 200, resp.content
        assert {c["name"] for c in resp.data} == {"a", "p"}

    def test_invalid_status_returns_400(self, ws_user, conn):
        _, user = ws_user
        _make(conn, "a")
        resp = _client(user).get(URL, {"status": "bogus"})
        assert resp.status_code == 400, resp.content


@pytest.mark.django_db
class TestListDateFilter:
    def _setup_three(self, conn):
        c1 = _make(conn, "may")
        c2 = _make(conn, "jun")
        c3 = _make(conn, "jul")
        _set_created(c1, timezone.make_aware(datetime(2026, 5, 1, 12, 0)))
        _set_created(c2, timezone.make_aware(datetime(2026, 6, 15, 12, 0)))
        _set_created(c3, timezone.make_aware(datetime(2026, 7, 1, 12, 0)))
        return c1, c2, c3

    def test_created_after(self, ws_user, conn):
        _, user = ws_user
        self._setup_three(conn)
        resp = _client(user).get(URL, {"created_after": "2026-06-01"})
        assert resp.status_code == 200, resp.content
        assert {c["name"] for c in resp.data} == {"jun", "jul"}

    def test_created_before(self, ws_user, conn):
        _, user = ws_user
        self._setup_three(conn)
        resp = _client(user).get(URL, {"created_before": "2026-06-30"})
        assert resp.status_code == 200, resp.content
        assert {c["name"] for c in resp.data} == {"may", "jun"}

    def test_created_range(self, ws_user, conn):
        _, user = ws_user
        self._setup_three(conn)
        resp = _client(user).get(
            URL, {"created_after": "2026-06-01", "created_before": "2026-06-30"}
        )
        assert resp.status_code == 200, resp.content
        assert {c["name"] for c in resp.data} == {"jun"}

    def test_created_before_is_inclusive_of_day(self, ws_user, conn):
        # 같은 날짜를 created_before 로 주면 그날 생성분 포함.
        # 06:00 UTC(=15:00 KST)는 UTC/KST 어느 해석으로도 06-15 → 경계 모호성 없음.
        _, user = ws_user
        c = _make(conn, "exact")
        _set_created(c, datetime(2026, 6, 15, 6, 0, tzinfo=UTC))
        resp = _client(user).get(URL, {"created_before": "2026-06-15"})
        assert resp.status_code == 200, resp.content
        assert {x["name"] for x in resp.data} == {"exact"}

    def test_iso_datetime_boundary(self, ws_user, conn):
        # 시각(ISO8601)까지 지정하면 그 instant 기준으로 필터 (instant 비교, 날짜 변환 없음).
        _, user = ws_user
        c = _make(conn, "noon")
        _set_created(c, datetime(2026, 6, 15, 12, 0, tzinfo=UTC))
        # 13:00 UTC 이후만 → noon(12:00 UTC) 제외
        after = _client(user).get(URL, {"created_after": "2026-06-15T13:00:00+00:00"})
        assert after.status_code == 200, after.content
        assert [x["name"] for x in after.data] == []
        # 11:00 UTC 이후만 → noon(12:00 UTC) 포함
        before = _client(user).get(URL, {"created_after": "2026-06-15T11:00:00+00:00"})
        assert before.status_code == 200, before.content
        assert [x["name"] for x in before.data] == ["noon"]

    def test_invalid_date_returns_400(self, ws_user, conn):
        _, user = ws_user
        _make(conn, "x")
        resp = _client(user).get(URL, {"created_after": "not-a-date"})
        assert resp.status_code == 400, resp.content


@pytest.mark.django_db
class TestListOrdering:
    def test_default_ordering_is_desc_created(self, ws_user, conn):
        _, user = ws_user
        old = _make(conn, "old")
        new = _make(conn, "new")
        _set_created(old, timezone.make_aware(datetime(2026, 1, 1, 0, 0)))
        _set_created(new, timezone.make_aware(datetime(2026, 12, 1, 0, 0)))
        resp = _client(user).get(URL)
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["new", "old"]

    def test_ordering_by_total_sent_desc(self, ws_user, conn):
        _, user = ws_user
        _make(conn, "low", total_sent=1)
        _make(conn, "high", total_sent=100)
        _make(conn, "mid", total_sent=50)
        resp = _client(user).get(URL, {"ordering": "-total_sent"})
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["high", "mid", "low"]

    def test_ordering_by_name_asc(self, ws_user, conn):
        _, user = ws_user
        _make(conn, "banana")
        _make(conn, "apple")
        _make(conn, "cherry")
        resp = _client(user).get(URL, {"ordering": "name"})
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["apple", "banana", "cherry"]

    def test_invalid_ordering_field_returns_400(self, ws_user, conn):
        _, user = ws_user
        _make(conn, "x")
        resp = _client(user).get(URL, {"ordering": "password"})
        assert resp.status_code == 400, resp.content


@pytest.mark.django_db
class TestListCombinedFilters:
    def test_status_and_date_and_ordering(self, ws_user, conn):
        _, user = ws_user
        a = _make(conn, "old-active", status=AutoDMCampaign.Status.ACTIVE, total_sent=5)
        b = _make(conn, "new-active", status=AutoDMCampaign.Status.ACTIVE, total_sent=9)
        p = _make(conn, "new-paused", status=AutoDMCampaign.Status.PAUSED, total_sent=99)
        _set_created(a, timezone.make_aware(datetime(2026, 1, 1, 0, 0)))
        _set_created(b, timezone.make_aware(datetime(2026, 6, 10, 0, 0)))
        _set_created(p, timezone.make_aware(datetime(2026, 6, 10, 0, 0)))
        resp = _client(user).get(
            URL,
            {"status": "active", "created_after": "2026-06-01", "ordering": "-total_sent"},
        )
        assert resp.status_code == 200, resp.content
        # active + 6월 이후 → new-active 만 (old-active 는 1월, new-paused 는 paused)
        assert [c["name"] for c in resp.data] == ["new-active"]
