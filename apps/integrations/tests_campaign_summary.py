"""Auto DM 캠페인 조회 고도화 테스트 (v4.1).

커버리지:
  - GET .../summary/ : counts / usage(월 사용량·한도) / delivery / last_activity_at
  - 목록 항목 enrichment: delivery_rate / needs_attention_count / delivered_count /
    last_sent_at / thumbnail_url
  - 검색(?search=) : name / description / ig username
  - facet 필터: trigger_type / follow_gate_enabled / public_reply_enabled
  - ordering=last_sent_at (미발송 nulls-last)
  - 벌크: bulk-pause / bulk-resume / bulk-delete (succeeded/failed 부분 성공)

함수 스코프 fixture 라 각 테스트는 자신의 workspace 캠페인/로그만 본다(테넌시 격리).
"""

import itertools

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace

LIST_URL = "/api/v1/integrations/auto-dm-campaigns/"
SUMMARY_URL = "/api/v1/integrations/auto-dm-campaigns/summary/"

_seq = itertools.count()


def _make_ws_user(email, slug, plan="starter", is_staff=False):
    User = get_user_model()
    user = User.objects.create_user(email=email, password="pw12345!", full_name=email)
    if is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    ws = Workspace.objects.create(name=slug, slug=slug, owner=user, plan=plan)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


def _make_conn(ws, ext="ig_x", username="user"):
    c = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=ext,
        username=username,
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    c.access_token = "tok"
    c.save()
    return c


def _make_campaign(conn, name="c", **kw):
    defaults = {
        "ig_connection": conn,
        "trigger_type": AutoDMCampaign.TriggerType.ANY_MEDIA,
        "name": name,
        "message_template": "hi",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, status, **kw):
    n = next(_seq)
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"cm{n}",
        recipient_user_id=f"u{n}",
        recipient_username=f"ru{n}",
        message_sent="hi",
        idempotency_key=f"idem-{n}",
        status=status,
        **kw,
    )


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _by_name(items):
    return {i["name"]: i for i in items}


@pytest.fixture
def ws_user(db):
    return _make_ws_user("sumtest@example.com", "sum-ws")


@pytest.fixture
def conn(ws_user):
    ws, _ = ws_user
    return _make_conn(ws, ext="ig_sum_1", username="sumuser")


@pytest.mark.django_db
class TestSummaryCounts:
    def test_counts_by_status(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "a1", status=AutoDMCampaign.Status.ACTIVE)
        _make_campaign(conn, "a2", status=AutoDMCampaign.Status.ACTIVE)
        _make_campaign(conn, "p1", status=AutoDMCampaign.Status.PAUSED)
        _make_campaign(conn, "i1", status=AutoDMCampaign.Status.INACTIVE)

        resp = _client(user).get(SUMMARY_URL, {"ig_connection_id": str(conn.id)})

        assert resp.status_code == 200, resp.content
        counts = resp.data["counts"]
        assert counts == {"active": 2, "paused": 1, "completed": 0, "inactive": 1, "total": 4}

    def test_scoped_by_ig_connection(self, ws_user):
        ws, user = ws_user
        c1 = _make_conn(ws, ext="ig_a", username="a")
        c2 = _make_conn(ws, ext="ig_b", username="b")
        _make_campaign(c1, "on-a")
        _make_campaign(c2, "on-b1")
        _make_campaign(c2, "on-b2")

        resp = _client(user).get(SUMMARY_URL, {"ig_connection_id": str(c2.id)})

        assert resp.status_code == 200, resp.content
        assert resp.data["counts"]["total"] == 2

    def test_multiple_workspaces_without_scope_400(self, db):
        ws1, user = _make_ws_user("multi@example.com", "multi-1")
        # 같은 유저를 두 번째 워크스페이스 멤버로
        ws2 = Workspace.objects.create(name="multi-2", slug="multi-2", owner=user)
        Membership.objects.create(workspace=ws2, user=user, role=Membership.Role.OWNER)

        resp = _client(user).get(SUMMARY_URL)
        assert resp.status_code == 400, resp.content

    def test_single_workspace_auto_resolves(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "only")
        resp = _client(user).get(SUMMARY_URL)  # id 미지정이지만 워크스페이스 1개
        assert resp.status_code == 200, resp.content
        assert resp.data["counts"]["total"] == 1

    def test_requires_auth(self, conn):
        resp = APIClient().get(SUMMARY_URL, {"ig_connection_id": str(conn.id)})
        assert resp.status_code == 401, resp.content


@pytest.mark.django_db
class TestSummaryUsage:
    # 한도 정의가 workspace.plan(PlanLimits) → owner 구독 플랜 features.dm_monthly_limit
    # 로 재배선됨(토스 요금제 개편). 구독 없는 owner 는 free = 200/월.

    def test_free_usage_math(self, ws_user, conn):
        _, user = ws_user
        camp = _make_campaign(conn, "u")
        # quota 소진: accepted/delivered/read/failed_no_trace
        _log(camp, SentDMLog.Status.DELIVERED)
        _log(camp, SentDMLog.Status.READ)
        _log(camp, SentDMLog.Status.ACCEPTED)
        # quota 미소진: queued/skipped/failed_token
        _log(camp, SentDMLog.Status.QUEUED)
        _log(camp, SentDMLog.Status.SKIPPED)
        _log(camp, SentDMLog.Status.FAILED_TOKEN)

        resp = _client(user).get(SUMMARY_URL, {"ig_connection_id": str(conn.id)})

        assert resp.status_code == 200, resp.content
        usage = resp.data["usage"]
        assert usage["sent_this_month"] == 3
        assert usage["monthly_free_limit"] == 200  # free 플랜 (owner 구독 기준)
        assert usage["remaining_this_month"] == 197
        assert usage["is_over_limit"] is False
        assert usage["period_start"] is not None
        assert usage["period_end"] is not None

    def test_over_limit(self, db):
        ws, user = _make_ws_user("over@example.com", "over-ws", plan="starter")
        conn = _make_conn(ws, ext="ig_over")
        camp = _make_campaign(conn, "o")
        SentDMLog.objects.bulk_create(
            [
                SentDMLog(
                    campaign=camp,
                    comment_id=f"oc{i}",
                    recipient_user_id=f"ou{i}",
                    recipient_username=f"oru{i}",
                    message_sent="hi",
                    idempotency_key=f"over-{i}",
                    status=SentDMLog.Status.DELIVERED,
                )
                for i in range(200)
            ]
        )
        resp = _client(user).get(SUMMARY_URL, {"ig_connection_id": str(conn.id)})
        assert resp.status_code == 200, resp.content
        usage = resp.data["usage"]
        assert usage["sent_this_month"] == 200
        assert usage["remaining_this_month"] == 0
        assert usage["is_over_limit"] is True

    def test_pro_owner_unlimited(self, db):
        from apps.billing.models import SubscriptionPlan, UserSubscription

        ws, user = _make_ws_user("ent@example.com", "ent-ws", plan="enterprise")
        pro = SubscriptionPlan.objects.get(name="pro")
        UserSubscription.objects.create(user=user, plan=pro)
        conn = _make_conn(ws, ext="ig_ent")
        camp = _make_campaign(conn, "e")
        _log(camp, SentDMLog.Status.DELIVERED)

        resp = _client(user).get(SUMMARY_URL, {"ig_connection_id": str(conn.id)})
        assert resp.status_code == 200, resp.content
        usage = resp.data["usage"]
        assert usage["monthly_free_limit"] == -1  # pro = DM 무제한
        assert usage["remaining_this_month"] is None
        assert usage["is_over_limit"] is False

    def test_admin_user_unlimited_despite_free_plan(self, db):
        # 관리자(is_staff) 는 free 구독이라도 무제한 — 기본 200 에 막히지 않음.
        ws, user = _make_ws_user("adm@example.com", "adm-ws", plan="starter", is_staff=True)
        conn = _make_conn(ws, ext="ig_adm")
        camp = _make_campaign(conn, "a")
        _log(camp, SentDMLog.Status.DELIVERED)

        resp = _client(user).get(SUMMARY_URL, {"ig_connection_id": str(conn.id)})
        assert resp.status_code == 200, resp.content
        usage = resp.data["usage"]
        assert usage["sent_this_month"] == 1
        assert usage["monthly_free_limit"] == -1  # 200 이 아니라 무제한
        assert usage["remaining_this_month"] is None
        assert usage["is_over_limit"] is False


@pytest.mark.django_db
class TestSummaryDelivery:
    def test_delivery_and_needs_attention(self, ws_user, conn):
        _, user = ws_user
        camp = _make_campaign(conn, "d")
        # confirmed = delivered(2)+read(1) = 3
        _log(camp, SentDMLog.Status.DELIVERED)
        _log(camp, SentDMLog.Status.DELIVERED)
        _log(camp, SentDMLog.Status.READ)
        # accepted_or_after = accepted(1)+delivered(2)+read(1)+failed_no_trace(1) = 5
        _log(camp, SentDMLog.Status.ACCEPTED)
        _log(camp, SentDMLog.Status.FAILED_NO_TRACE)
        # needs_attention = failed_token(1) + failed_no_trace(1 위) = 2
        _log(camp, SentDMLog.Status.FAILED_TOKEN)

        resp = _client(user).get(SUMMARY_URL, {"ig_connection_id": str(conn.id)})

        assert resp.status_code == 200, resp.content
        d = resp.data["delivery"]
        assert d["delivery_rate"] == 0.6  # 3/5
        assert d["needs_attention_total"] == 2
        assert d["total_sent"] == 3  # delivered+read
        assert resp.data["last_activity_at"] is not None


@pytest.mark.django_db
class TestListEnrichment:
    def test_per_item_stats_fields(self, ws_user, conn):
        _, user = ws_user
        camp = _make_campaign(conn, "enr")
        _log(camp, SentDMLog.Status.DELIVERED)
        _log(camp, SentDMLog.Status.READ)
        _log(camp, SentDMLog.Status.ACCEPTED)
        _log(camp, SentDMLog.Status.FAILED_NO_TRACE)
        _log(camp, SentDMLog.Status.FAILED_TOKEN)

        resp = _client(user).get(LIST_URL, {"ig_connection_id": str(conn.id)})

        assert resp.status_code == 200, resp.content
        item = _by_name(resp.data)["enr"]
        assert item["delivered_count"] == 2  # delivered+read
        assert item["delivery_rate"] == 0.5  # 2 / (accepted1+delivered1+read1+notrace1=4)
        assert item["needs_attention_count"] == 2  # failed_no_trace + failed_token
        assert item["last_sent_at"] is not None
        assert "thumbnail_url" in item

    def test_empty_campaign_zeroes(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "empty")
        resp = _client(user).get(LIST_URL, {"ig_connection_id": str(conn.id)})
        assert resp.status_code == 200, resp.content
        item = _by_name(resp.data)["empty"]
        assert item["delivered_count"] == 0
        assert item["delivery_rate"] == 0.0
        assert item["needs_attention_count"] == 0
        assert item["last_sent_at"] is None

    def test_order_by_last_sent_at_nulls_last(self, ws_user, conn):
        _, user = ws_user
        active = _make_campaign(conn, "has-logs")
        _make_campaign(conn, "no-logs")
        _log(active, SentDMLog.Status.DELIVERED)

        resp = _client(user).get(
            LIST_URL, {"ig_connection_id": str(conn.id), "ordering": "-last_sent_at"}
        )
        assert resp.status_code == 200, resp.content
        names = [c["name"] for c in resp.data]
        # 발송 있는 캠페인이 먼저, 미발송(null)은 항상 뒤
        assert names == ["has-logs", "no-logs"]


@pytest.mark.django_db
class TestListSearchAndFacets:
    def test_search_by_name(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "여름 이벤트")
        _make_campaign(conn, "겨울 세일")
        resp = _client(user).get(LIST_URL, {"ig_connection_id": str(conn.id), "search": "여름"})
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["여름 이벤트"]

    def test_search_by_description(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "c1", description="블랙프라이데이 특가")
        _make_campaign(conn, "c2", description="신규 가입")
        resp = _client(user).get(
            LIST_URL, {"ig_connection_id": str(conn.id), "search": "블랙프라이데이"}
        )
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["c1"]

    def test_facet_trigger_type(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "any", trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA)
        _make_campaign(conn, "story", trigger_type=AutoDMCampaign.TriggerType.STORY_REPLY)
        resp = _client(user).get(
            LIST_URL, {"ig_connection_id": str(conn.id), "trigger_type": "story_reply"}
        )
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["story"]

    def test_facet_follow_gate_enabled(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "gated", follow_gate_enabled=True)
        _make_campaign(conn, "plain", follow_gate_enabled=False)
        resp = _client(user).get(
            LIST_URL, {"ig_connection_id": str(conn.id), "follow_gate_enabled": "true"}
        )
        assert resp.status_code == 200, resp.content
        assert [c["name"] for c in resp.data] == ["gated"]

    def test_invalid_trigger_type_400(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "x")
        resp = _client(user).get(
            LIST_URL, {"ig_connection_id": str(conn.id), "trigger_type": "bogus"}
        )
        assert resp.status_code == 400, resp.content

    def test_invalid_bool_400(self, ws_user, conn):
        _, user = ws_user
        _make_campaign(conn, "x")
        resp = _client(user).get(
            LIST_URL, {"ig_connection_id": str(conn.id), "follow_gate_enabled": "maybe"}
        )
        assert resp.status_code == 400, resp.content


@pytest.mark.django_db
class TestBulkActions:
    def test_bulk_pause(self, ws_user, conn):
        _, user = ws_user
        a = _make_campaign(conn, "a", status=AutoDMCampaign.Status.ACTIVE)
        b = _make_campaign(conn, "b", status=AutoDMCampaign.Status.ACTIVE)
        resp = _client(user).post(
            LIST_URL + "bulk-pause/", {"ids": [str(a.id), str(b.id)]}, format="json"
        )
        assert resp.status_code == 200, resp.content
        assert set(resp.data["succeeded"]) == {str(a.id), str(b.id)}
        assert resp.data["failed"] == []
        a.refresh_from_db()
        b.refresh_from_db()
        assert a.status == AutoDMCampaign.Status.PAUSED
        assert b.status == AutoDMCampaign.Status.PAUSED

    def test_bulk_resume_clears_past_end(self, ws_user, conn):
        _, user = ws_user
        from datetime import timedelta

        past = timezone.now() - timedelta(days=1)
        c = _make_campaign(conn, "c", status=AutoDMCampaign.Status.PAUSED, scheduled_end_at=past)
        resp = _client(user).post(LIST_URL + "bulk-resume/", {"ids": [str(c.id)]}, format="json")
        assert resp.status_code == 200, resp.content
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.ACTIVE
        assert c.scheduled_end_at is None

    def test_bulk_delete(self, ws_user, conn):
        _, user = ws_user
        c = _make_campaign(conn, "del")
        resp = _client(user).post(LIST_URL + "bulk-delete/", {"ids": [str(c.id)]}, format="json")
        assert resp.status_code == 200, resp.content
        assert resp.data["succeeded"] == [str(c.id)]
        assert not AutoDMCampaign.objects.filter(id=c.id).exists()

    def test_bulk_other_workspace_id_fails_gracefully(self, ws_user, conn):
        _, user = ws_user
        mine = _make_campaign(conn, "mine")
        # 다른 워크스페이스의 캠페인
        other_ws, _ = _make_ws_user("bulkother@example.com", "bulk-other")
        other_conn = _make_conn(other_ws, ext="ig_other")
        theirs = _make_campaign(other_conn, "theirs")

        resp = _client(user).post(
            LIST_URL + "bulk-pause/",
            {"ids": [str(mine.id), str(theirs.id)]},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert resp.data["succeeded"] == [str(mine.id)]
        assert resp.data["failed"] == [{"id": str(theirs.id), "reason": "not_found"}]
        # 남의 캠페인은 변경되지 않음
        theirs.refresh_from_db()
        assert theirs.status == AutoDMCampaign.Status.ACTIVE

    def test_bulk_empty_ids_400(self, ws_user, conn):
        _, user = ws_user
        resp = _client(user).post(LIST_URL + "bulk-pause/", {"ids": []}, format="json")
        assert resp.status_code == 400, resp.content


@pytest.mark.django_db
class TestCampaignStatsV3Statuses:
    """GET .../{id}/stats/ — last_24h 가 v3 상태머신을 집계하는지.

    회귀 배경(2026-07-07 prod 실측): 성공 DM 은 accepted→delivered→read 로 전이하고
    legacy 'sent' 가 되지 않는데, 기존 코드가 status='sent' 만 세서 성공할수록
    sent=0 으로 표시됐다 ("DM 발송 0" 프로덕션 버그).
    """

    def _stats(self, user, campaign):
        resp = _client(user).get(f"{LIST_URL}{campaign.id}/stats/")
        assert resp.status_code == 200, resp.content
        return resp.data

    def test_v3_success_statuses_counted_as_sent(self, ws_user, conn):
        _, user = ws_user
        campaign = _make_campaign(conn, "stats-v3")
        _log(campaign, SentDMLog.Status.ACCEPTED)
        _log(campaign, SentDMLog.Status.DELIVERED)
        _log(campaign, SentDMLog.Status.READ)

        d = self._stats(user, campaign)["last_24h"]
        assert d["total"] == 3
        assert d["sent"] == 3  # 회귀 지점 — 기존 코드는 0
        assert d["delivered"] == 2  # delivered + read
        assert d["read"] == 1
        assert d["failed"] == 0

    def test_failed_and_inflight_and_unconfirmed(self, ws_user, conn):
        _, user = ws_user
        campaign = _make_campaign(conn, "stats-mix")
        _log(campaign, SentDMLog.Status.FAILED_TOKEN)
        _log(campaign, SentDMLog.Status.FAILED_WINDOW)
        _log(campaign, SentDMLog.Status.QUEUED)
        _log(campaign, SentDMLog.Status.SUBMITTING)
        _log(campaign, SentDMLog.Status.SKIPPED)
        _log(campaign, SentDMLog.Status.FAILED_NO_TRACE)

        d = self._stats(user, campaign)["last_24h"]
        assert d["total"] == 6
        assert d["failed"] == 2  # 분류 실패 (no_trace 는 실패 아님)
        assert d["pending"] == 2  # queued + submitting
        assert d["skipped"] == 1
        assert d["unconfirmed"] == 1  # 도착 미확인은 별도 키
        assert d["sent"] == 0

    def test_child_logs_excluded(self, ws_user, conn):
        """재안내/보상(child) 행은 24h 집계에서 제외 — 댓글 1건 = 1행 유지."""
        _, user = ws_user
        campaign = _make_campaign(conn, "stats-child")
        opening = _log(
            campaign,
            SentDMLog.Status.READ,
            dm_kind=SentDMLog.DMKind.OPENING,
            gate_status=SentDMLog.GateStatus.PASSED,
        )
        _log(
            campaign,
            SentDMLog.Status.READ,
            dm_kind=SentDMLog.DMKind.REWARD,
            gate_status=SentDMLog.GateStatus.PASSED,
            parent_log=opening,
        )

        d = self._stats(user, campaign)["last_24h"]
        assert d["total"] == 1
        assert d["sent"] == 1
