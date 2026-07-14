"""마케팅 DM 통계 v4.2 — 사람(수신자) 단위 지표 / CTR / recipients 롤업 테스트.

- stats: unique_* + ctr (게이트형=클릭 / 비게이트형=읽음)
- recipients: recipient_user_id 로 묶은 1행/사람 롤업 + follower_status
- 자세히보기: GET /?campaign_id=..&recipient_user_id=.. 개별 타임라인
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Membership, Workspace

User = get_user_model()


def _user():
    return User.objects.create_user(
        email=f"mkt-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _ws(user):
    ws = Workspace.objects.create(name="mkt-ws", slug=f"mkt-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws):
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
    return conn


def _campaign(conn, **kw):
    defaults = {
        "ig_connection": conn,
        "name": f"c-{uuid.uuid4().hex[:6]}",
        "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        "media_id": f"m_{uuid.uuid4().hex[:10]}",
        "message_template": "hello",
        "status": AutoDMCampaign.Status.ACTIVE,
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _log(
    campaign,
    ruid,
    *,
    status=SentDMLog.Status.DELIVERED,
    kind=SentDMLog.DMKind.STANDALONE,
    gate=SentDMLog.GateStatus.NONE,
    parent=None,
):
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"c-{uuid.uuid4().hex[:8]}",
        recipient_user_id=ruid,
        recipient_username=f"user_{ruid}",
        message_sent="hi",
        idempotency_key=f"k-{uuid.uuid4().hex[:12]}",
        status=status,
        dm_kind=kind,
        gate_status=gate,
        parent_log=parent,
    )


@pytest.mark.django_db
class TestGatedCampaignStats:
    """follow-gate 캠페인: CTR=버튼 클릭 기준, follower_status 검증."""

    def _setup(self):
        user = _user()
        conn = _conn(_ws(user))
        camp = _campaign(
            conn,
            follow_gate_enabled=True,
            gate_verify_follow=True,
            reward_message_template="reward!",
        )
        # A: 통과 (opening PASSED + reward child)
        a_open = _log(camp, "A", kind=SentDMLog.DMKind.OPENING, gate=SentDMLog.GateStatus.PASSED)
        _log(
            camp, "A", kind=SentDMLog.DMKind.REWARD, gate=SentDMLog.GateStatus.PASSED, parent=a_open
        )
        # B: 클릭 안 함 (opening PENDING, child 없음)
        _log(camp, "B", kind=SentDMLog.DMKind.OPENING, gate=SentDMLog.GateStatus.PENDING)
        # C: 클릭했으나 미통과 (opening PENDING + retry child)
        c_open = _log(camp, "C", kind=SentDMLog.DMKind.OPENING, gate=SentDMLog.GateStatus.PENDING)
        _log(
            camp,
            "C",
            kind=SentDMLog.DMKind.OPENING,
            gate=SentDMLog.GateStatus.PENDING,
            parent=c_open,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, camp

    def test_stats_unique_and_ctr(self):
        client, camp = self._setup()
        resp = client.get(f"/api/v1/integrations/dm-verification/stats/?campaign_id={camp.id}")
        assert resp.status_code == 200
        d = resp.data
        # 3명에게 발송 (A,B,C — A는 로그 2건이나 사람은 1)
        assert d["unique_sent"] == 3
        assert d["unique_recipients"] == 3
        assert d["unique_delivered"] == 3
        assert d["unique_followers"] == 1  # A만 gate passed
        # CTR: 클릭(child 로그 존재) = A, C → 2/3
        assert d["ctr_basis"] == "click"
        assert d["ctr_interacted"] == 2
        assert d["ctr_denominator"] == 3
        assert d["ctr"] == round(2 / 3, 4)
        # 이벤트 단위(하위호환)는 여전히 존재
        assert "delivery_rate" in d and "total" in d

    def test_recipients_rollup(self):
        client, camp = self._setup()
        resp = client.get(f"/api/v1/integrations/dm-verification/recipients/?campaign_id={camp.id}")
        assert resp.status_code == 200
        assert resp.data["count"] == 3
        by_id = {r["recipient_user_id"]: r for r in resp.data["results"]}
        assert by_id["A"]["follower_status"] == "verified_follower"
        assert by_id["A"]["dm_count"] == 2
        assert by_id["A"]["delivered"] is True
        assert by_id["B"]["follower_status"] == "not_followed"
        assert by_id["B"]["dm_count"] == 1
        assert by_id["C"]["follower_status"] == "clicked_unverified"
        assert by_id["C"]["dm_count"] == 2

    def test_detail_timeline_by_recipient(self):
        client, camp = self._setup()
        resp = client.get(
            f"/api/v1/integrations/dm-verification/?campaign_id={camp.id}&recipient_user_id=A"
        )
        assert resp.status_code == 200
        # A 의 개별 로그 2건 (opening + reward)
        assert resp.data["count"] == 2
        assert all(r["recipient_user_id"] == "A" for r in resp.data["results"])


@pytest.mark.django_db
class TestNonGatedCampaignStats:
    """비게이트형 캠페인: CTR=읽음(READ) 기준, follower_status=unknown."""

    def _setup(self):
        user = _user()
        camp = _campaign(_conn(_ws(user)), follow_gate_enabled=False)
        _log(camp, "X", status=SentDMLog.Status.READ)  # 읽음 → 참여
        _log(camp, "Y", status=SentDMLog.Status.DELIVERED)  # 도착만 → 미참여
        client = APIClient()
        client.force_authenticate(user=user)
        return client, camp

    def test_ctr_read_basis(self):
        client, camp = self._setup()
        resp = client.get(f"/api/v1/integrations/dm-verification/stats/?campaign_id={camp.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["ctr_basis"] == "read"
        assert d["unique_sent"] == 2
        assert d["ctr_interacted"] == 1  # X만 읽음
        assert d["ctr"] == 0.5

    def test_recipients_follower_status_unknown(self):
        client, camp = self._setup()
        resp = client.get(f"/api/v1/integrations/dm-verification/recipients/?campaign_id={camp.id}")
        assert resp.status_code == 200
        for r in resp.data["results"]:
            assert r["follower_status"] == "unknown"


@pytest.mark.django_db
class TestPeopleProcessingStats:
    """v4.4 — 사람 단위 처리 현황 (unique_targets/waiting/failed/unconfirmed/reach_rate).

    루트 DM(오프닝/단독) 기준 — 리워드·child 제외, queue-state.people 과 동일 정의.
    """

    def _setup(self):
        user = _user()
        camp = _campaign(
            _conn(_ws(user)),
            follow_gate_enabled=True,
            gate_verify_follow=True,
            reward_message_template="reward!",
        )
        # A: 오프닝 delivered + 리워드 read → sent 1명 (리워드는 모수 제외)
        a_open = _log(camp, "A", kind=SentDMLog.DMKind.OPENING, gate=SentDMLog.GateStatus.PASSED)
        _log(
            camp,
            "A",
            status=SentDMLog.Status.READ,
            kind=SentDMLog.DMKind.REWARD,
            gate=SentDMLog.GateStatus.PASSED,
            parent=a_open,
        )
        # B: 오프닝 하드실패 → unique_failed (아무것도 못 받은 사람)
        _log(camp, "B", status=SentDMLog.Status.FAILED_PARAM, kind=SentDMLog.DMKind.OPENING)
        # C: 오프닝 큐 대기 → unique_waiting
        _log(camp, "C", status=SentDMLog.Status.QUEUED, kind=SentDMLog.DMKind.OPENING)
        # D: 오프닝 도착 미확인(no_trace) → 발송(쿼터 소진)됐으나 unconfirmed
        _log(camp, "D", status=SentDMLog.Status.FAILED_NO_TRACE, kind=SentDMLog.DMKind.OPENING)
        client = APIClient()
        client.force_authenticate(user=user)
        return client, camp

    def test_people_processing_fields(self):
        client, camp = self._setup()
        resp = client.get(f"/api/v1/integrations/dm-verification/stats/?campaign_id={camp.id}")
        assert resp.status_code == 200
        d = resp.data
        assert d["unique_targets"] == 4  # A,B,C,D (A의 리워드는 사람 수에 안 잡힘)
        assert d["unique_sent"] == 2  # A + D(no_trace 는 발송됨/쿼터 소진)
        assert d["unique_waiting"] == 1  # C
        assert d["unique_failed"] == 1  # B — 하드실패는 delivery_rate 엔 안 잡혀도 여기 노출
        assert d["unique_unconfirmed"] == 1  # D
        # 항등: targets = sent + waiting + failed
        assert d["unique_targets"] == d["unique_sent"] + d["unique_waiting"] + d["unique_failed"]
        # 도달률 = unique_delivered / unique_targets = A(1) / 4
        assert d["unique_delivered"] == 1
        assert d["unique_reach_rate"] == 0.25

    def test_unconfirmed_excludes_later_delivered(self):
        """같은 사람이 no_trace 후 다른 DM 으로 확정 도착하면 unconfirmed 에서 빠진다."""
        client, camp = self._setup()
        # D 에게 재발송이 도착 확정된 상황
        _log(camp, "D", status=SentDMLog.Status.DELIVERED, kind=SentDMLog.DMKind.OPENING)
        resp = client.get(f"/api/v1/integrations/dm-verification/stats/?campaign_id={camp.id}")
        assert resp.status_code == 200
        assert resp.data["unique_unconfirmed"] == 0
        assert resp.data["unique_delivered"] == 2  # A, D


@pytest.mark.django_db
class TestRecipientsGuards:
    def test_campaign_id_required(self):
        user = _user()
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get("/api/v1/integrations/dm-verification/recipients/")
        assert resp.status_code == 400

    def test_foreign_campaign_forbidden(self):
        owner = _user()
        camp = _campaign(_conn(_ws(owner)))
        outsider = _user()
        client = APIClient()
        client.force_authenticate(user=outsider)
        resp = client.get(f"/api/v1/integrations/dm-verification/recipients/?campaign_id={camp.id}")
        assert resp.status_code in (403, 404)


@pytest.mark.django_db
class TestRecipientsCategoryFilter:
    """recipients?category= 서버사이드 상태 필터 (페이지네이션 이전 적용)."""

    _URL = "/api/v1/integrations/dm-verification/recipients/"

    def _setup(self):
        user = _user()
        camp = _campaign(_conn(_ws(user)), follow_gate_enabled=False)
        # 상태가 다른 수신자 5명 (사람 단위 롤업 카테고리 검증용)
        _log(camp, "RD", status=SentDMLog.Status.READ)  # read=True, delivered=True
        _log(camp, "DL", status=SentDMLog.Status.DELIVERED)  # delivered=True, read=False
        _log(camp, "AC", status=SentDMLog.Status.ACCEPTED)  # sent만 (delivered=False)
        _log(camp, "TK", status=SentDMLog.Status.FAILED_TOKEN)  # needs_attention
        _log(camp, "NT", status=SentDMLog.Status.FAILED_NO_TRACE)  # needs_attention
        client = APIClient()
        client.force_authenticate(user=user)
        return client, camp

    def _ids(self, resp):
        return {r["recipient_user_id"] for r in resp.data["results"]}

    def test_category_read(self):
        client, camp = self._setup()
        resp = client.get(f"{self._URL}?campaign_id={camp.id}&category=read")
        assert resp.status_code == 200
        assert resp.data["count"] == 1
        assert self._ids(resp) == {"RD"}

    def test_category_delivered_includes_read(self):
        client, camp = self._setup()
        resp = client.get(f"{self._URL}?campaign_id={camp.id}&category=delivered")
        assert resp.status_code == 200
        # delivered 는 정의상 read 포함 → RD + DL
        assert resp.data["count"] == 2
        assert self._ids(resp) == {"RD", "DL"}

    def test_category_attention(self):
        client, camp = self._setup()
        resp = client.get(f"{self._URL}?campaign_id={camp.id}&category=attention")
        assert resp.status_code == 200
        assert resp.data["count"] == 2
        assert self._ids(resp) == {"TK", "NT"}

    def test_category_all_and_default(self):
        client, camp = self._setup()
        for suffix in ("&category=all", ""):
            resp = client.get(f"{self._URL}?campaign_id={camp.id}{suffix}")
            assert resp.status_code == 200
            assert resp.data["count"] == 5

    def test_invalid_category_400(self):
        client, camp = self._setup()
        resp = client.get(f"{self._URL}?campaign_id={camp.id}&category=foo")
        assert resp.status_code == 400
        assert resp.data["error"]["details"]["field"] == "category"

    def test_filtered_pagination_count(self):
        """count 는 필터 후 총 인원, page_size=20 유지, page 2 로 이어짐."""
        user = _user()
        camp = _campaign(_conn(_ws(user)), follow_gate_enabled=False)
        for i in range(25):
            _log(camp, f"R{i:02d}", status=SentDMLog.Status.READ)
        _log(camp, "DLV", status=SentDMLog.Status.DELIVERED)  # read 아님 → 제외돼야
        client = APIClient()
        client.force_authenticate(user=user)

        p1 = client.get(f"{self._URL}?campaign_id={camp.id}&category=read&page=1")
        assert p1.status_code == 200
        assert p1.data["count"] == 25  # 필터 후 총 인원 (DELIVERED 1명 제외)
        assert len(p1.data["results"]) == 20
        p2 = client.get(f"{self._URL}?campaign_id={camp.id}&category=read&page=2")
        assert p2.data["count"] == 25
        assert len(p2.data["results"]) == 5
        # 페이지 간 중복 없음 (안정 정렬)
        assert self._ids(p1).isdisjoint(self._ids(p2))

    def test_category_with_username(self):
        """category + recipient_username 부분검색 조합 (AND)."""
        user = _user()
        camp = _campaign(_conn(_ws(user)), follow_gate_enabled=False)
        # _log 는 recipient_username=f"user_{ruid}" 로 세팅됨
        _log(camp, "keepme", status=SentDMLog.Status.READ)
        _log(camp, "other", status=SentDMLog.Status.READ)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(
            f"{self._URL}?campaign_id={camp.id}&category=read&recipient_username=keep"
        )
        assert resp.status_code == 200
        assert resp.data["count"] == 1
        assert self._ids(resp) == {"keepme"}
