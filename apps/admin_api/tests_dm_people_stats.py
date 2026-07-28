"""어드민 자동 DM — 사람 단위 통계 통일(DM-1) · 오류 원인·조치(DM-2) · 큐/시계열(DM-3).

대상:
- ``apps/integrations/campaign_stats.py`` (build_dm_stats / annotate_campaign_people)
- ``apps/admin_api/serializers/autodm.py`` · ``views/autodm.py``
- ``apps/integrations/queue_state.py``

핵심 계약 (이 파일이 지키는 것):
1. **목록의 people.* == 상세의 unique_***. 두 값이 갈라지면 "목록과 상세가 다르다"는
   원래 버그가 형태만 바꿔 재발한 것이다.
2. ``/admin/dm-verification/stats/`` 응답이 선언한 ``DMVerificationStatsSerializer``
   (unique_* 포함)와 실제로 일치한다.
3. 어드민 queue-state/timeseries 가 유저 콘솔과 같은 함수를 쓴다.

주의:
- 파일명이 tests_*.py 라 **경로 명시 실행** 필요.
- 테스트 DB 가 dev DB 라 전역 카운트 단언 금지 — 전부 캠페인 스코프로 좁혔다.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.campaign_stats import (
    annotate_campaign_people,
    build_dm_stats,
    people_from_annotations,
    people_rollup_full,
)
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

User = get_user_model()

CAMPAIGNS_URL = "/api/v1/admin/auto-dm/campaigns/"
LOGS_URL = "/api/v1/admin/auto-dm/logs/"
RECIPIENTS_URL = "/api/v1/admin/auto-dm/recipients/"
STATS_URL = "/api/v1/admin/dm-verification/stats/"


# ─── 픽스처 ──────────────────────────────────────────────────────────


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email=f"staff-dm1-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!", is_staff=True
    )


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(
        email=f"user-dm1-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def regular_client(client, regular_user):
    client.force_authenticate(user=regular_user)
    return client


def _mk_conn():
    owner = User.objects.create_user(
        email=f"owner-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    return IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=f"u_{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    )


def _mk_campaign(conn=None, **kwargs):
    kwargs.setdefault("name", f"camp-{uuid.uuid4().hex[:6]}")
    kwargs.setdefault("status", AutoDMCampaign.Status.ACTIVE)
    return AutoDMCampaign.objects.create(
        ig_connection=conn or _mk_conn(),
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        message_template="hi",
        **kwargs,
    )


def _mk_log(campaign, recipient, status, *, kind=None, parent=None, code="", subcode="", msg=""):
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"c_{uuid.uuid4().hex[:10]}",
        recipient_user_id=recipient,
        recipient_username="",
        message_sent="x",
        status=status,
        dm_kind=kind or SentDMLog.DMKind.OPENING,
        parent_log=parent,
        error_code=code,
        error_subcode=subcode,
        error_message=msg,
        idempotency_key=uuid.uuid4().hex,
    )


def _row(res, campaign):
    return next(r for r in res.data["results"] if r["id"] == str(campaign.id))


def _people_of(campaign) -> dict:
    """annotate → people dict (뷰가 목록에서 쓰는 것과 같은 경로)."""
    obj = annotate_campaign_people(AutoDMCampaign.objects.filter(pk=campaign.pk)).get()
    return people_from_annotations(obj)


# ─── DM-1-a: 통계 단일 소스 ───────────────────────────────────────────


class TestSharedStats:
    def test_admin_campaign_detail_now_has_unique_fields(self, staff_client):
        """어드민 사본에는 unique_* 가 통째로 없었다 — 공용 함수 전환으로 채워진다."""
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)

        stats = staff_client.get(f"{CAMPAIGNS_URL}{camp.id}/").data["stats"]
        for key in (
            "unique_targets",
            "unique_sent",
            "unique_waiting",
            "unique_failed",
            "unique_unconfirmed",
            "unique_hidden_spam",
            "unique_needs_attention_excl_hidden",
            "unique_sent_rate",
            "ctr",
            "ctr_basis",
        ):
            assert key in stats, key
        assert stats["unique_targets"] == 1

    def test_verification_stats_endpoint_matches_declared_schema(self, staff_client):
        """선언은 DMVerificationStatsSerializer 인데 실제 응답에 unique_* 가 없던 불일치."""
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)

        res = staff_client.get(STATS_URL, {"campaign_id": str(camp.id)})
        assert res.status_code == 200
        assert res.data["unique_sent"] == 1
        assert res.data["unique_targets"] == 1

    def test_gate_campaign_events_exceed_people(self, staff_client):
        """1명 = 오프닝+리워드 2건 — 이벤트 수와 인원 수가 다르다는 것이 DM-1 의 전제."""
        camp = _mk_campaign(follow_gate_enabled=True)
        opening = _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        _mk_log(
            camp, "r1", SentDMLog.Status.DELIVERED, kind=SentDMLog.DMKind.REWARD, parent=opening
        )

        stats = build_dm_stats(camp.dm_logs.all())
        assert stats["total"] == 2  # 이벤트
        assert stats["unique_targets"] == 1  # 사람


# ─── DM-1-b: 목록 people 블록 ─────────────────────────────────────────


class TestCampaignListPeople:
    def test_list_people_equals_detail_unique(self, staff_client):
        """목록과 상세가 어긋나면 안 된다 — 요청서 문제 ①의 회귀 가드."""
        camp = _mk_campaign(follow_gate_enabled=True)
        # 발송 2명 (그중 1명은 리워드까지 = 이벤트 3건)
        op1 = _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        _mk_log(camp, "r1", SentDMLog.Status.READ, kind=SentDMLog.DMKind.REWARD, parent=op1)
        _mk_log(camp, "r2", SentDMLog.Status.ACCEPTED)
        # 대기 1명 / 하드실패 1명 / 숨김함 1명 / 도착미확인 1명
        _mk_log(camp, "r3", SentDMLog.Status.QUEUED)
        _mk_log(camp, "r4", SentDMLog.Status.FAILED_TOKEN, code="190")
        _mk_log(camp, "r5", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025")
        _mk_log(camp, "r6", SentDMLog.Status.FAILED_NO_TRACE)
        # ⬇ DM-5 재현: 루트는 숨김함(미발송)인데 자식만 발송된 사람.
        #   전부 DELIVERED 인 픽스처로는 두 모수가 우연히 같아져 이 가드가 통과해버렸다.
        op7 = _mk_log(camp, "r7", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025")
        _mk_log(camp, "r7", SentDMLog.Status.DELIVERED, kind=SentDMLog.DMKind.REWARD, parent=op7)

        detail = staff_client.get(f"{CAMPAIGNS_URL}{camp.id}/").data["stats"]
        people = _row(staff_client.get(CAMPAIGNS_URL, {"search": camp.name}), camp)["people"]

        assert people["targets"] == detail["unique_targets"]
        assert people["sent"] == detail["unique_sent"]
        assert people["waiting"] == detail["unique_waiting"]
        assert people["failed"] == detail["unique_failed"]
        assert people["unconfirmed"] == detail["unique_unconfirmed"]
        assert people["hidden_spam"] == detail["unique_hidden_spam"]
        assert people["needs_attention"] == detail["unique_needs_attention_excl_hidden"]
        assert people["sent_rate"] == detail["unique_sent_rate"]

    def test_detail_identity_holds_when_only_child_was_sent(self, staff_client):
        """DM-5: 루트 미발송 + 자식만 발송 — 상세 응답 내부 항등이 깨지던 케이스.

        예전엔 unique_sent 만 전체 로그 기준이라 그 사람을 sent 로도, failed 로도 세서
        targets(1) != sent(1) + waiting(0) + failed(1) 이 됐다.
        """
        camp = _mk_campaign()
        op = _mk_log(camp, "r1", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025")
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED, kind=SentDMLog.DMKind.REWARD, parent=op)

        d = staff_client.get(f"{CAMPAIGNS_URL}{camp.id}/").data["stats"]
        assert d["unique_targets"] == d["unique_sent"] + d["unique_waiting"] + d["unique_failed"]
        assert (d["unique_targets"], d["unique_sent"], d["unique_failed"]) == (1, 0, 1)
        assert _people_of(camp)["sent"] == d["unique_sent"] == 0

    def test_annotate_and_rollup_return_identical_dicts(self, staff_client):
        """목록 annotate 판과 상세 aggregate 판이 **같은 dict** 여야 한다 (구조적 가드).

        필드별 단언은 픽스처가 그 분기를 안 밟으면 통과해버린다 — 두 경로의 전체
        결과를 통째로 비교해, 조건식이 한쪽만 바뀌면 무조건 빨개지게 한다.
        """
        camp = _mk_campaign(follow_gate_enabled=True)
        op1 = _mk_log(camp, "p1", SentDMLog.Status.DELIVERED)
        _mk_log(camp, "p1", SentDMLog.Status.READ, kind=SentDMLog.DMKind.REWARD, parent=op1)
        _mk_log(camp, "p2", SentDMLog.Status.ACCEPTED)
        _mk_log(camp, "p3", SentDMLog.Status.QUEUED)
        _mk_log(camp, "p4", SentDMLog.Status.FAILED_TOKEN, code="190")
        _mk_log(camp, "p5", SentDMLog.Status.FAILED_NO_TRACE)
        _mk_log(camp, "p6", SentDMLog.Status.RECOVERY_EXPIRED)
        op7 = _mk_log(camp, "p7", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025")
        _mk_log(camp, "p7", SentDMLog.Status.DELIVERED, kind=SentDMLog.DMKind.REWARD, parent=op7)
        _mk_log(camp, "p8", SentDMLog.Status.SKIPPED, msg="monthly_dm_limit_reached")

        assert people_rollup_full(camp.dm_logs.all()) == _people_of(camp)

    def test_people_identity_total_equals_sent_waiting_failed(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        _mk_log(camp, "r2", SentDMLog.Status.QUEUED)
        _mk_log(camp, "r3", SentDMLog.Status.FAILED_WINDOW)

        p = _people_of(camp)
        assert p["targets"] == p["sent"] + p["waiting"] + p["failed"] == 3
        assert p["sent_rate"] == round(1 / 3, 4)

    def test_hidden_spam_excluded_from_needs_attention(self, db):
        """숨겨진 요청·스팸은 실패지만 '확인 필요'에서는 빠진다 (v4.5 정의)."""
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025")
        _mk_log(camp, "r2", SentDMLog.Status.FAILED_TOKEN, code="190")

        p = _people_of(camp)
        assert p["failed"] == 2
        assert p["hidden_spam"] == 1
        assert p["needs_attention"] == 1

    def test_person_with_a_later_success_is_not_counted_as_hidden(self, db):
        """같은 사람이 다른 댓글로 발송됐으면 sent 버킷 — hidden_spam 에서 빠져야 한다."""
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025")
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)

        p = _people_of(camp)
        assert (p["targets"], p["sent"], p["failed"]) == (1, 1, 0)
        assert p["hidden_spam"] == 0

    def test_unconfirmed_ignores_people_who_also_arrived(self, db):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.FAILED_NO_TRACE)  # 도착 미확인만
        _mk_log(camp, "r2", SentDMLog.Status.FAILED_NO_TRACE)
        _mk_log(camp, "r2", SentDMLog.Status.DELIVERED)  # 결국 도착 → 제외

        p = _people_of(camp)
        assert p["unconfirmed"] == 1

    def test_empty_campaign_is_all_zero(self, db):
        p = _people_of(_mk_campaign())
        assert p == {
            "targets": 0,
            "sent": 0,
            "waiting": 0,
            "failed": 0,
            "unconfirmed": 0,
            "hidden_spam": 0,
            "needs_attention": 0,
            "sent_rate": 0.0,
        }

    def test_legacy_total_fields_are_kept(self, staff_client):
        """프론트가 배포 순서와 무관하게 폴백할 수 있어야 한다 — 제거 금지."""
        camp = _mk_campaign()
        row = _row(staff_client.get(CAMPAIGNS_URL, {"search": camp.name}), camp)
        for key in ("total_sent", "total_failed", "total_unconfirmed"):
            assert key in row
        # 목록 카드용 이벤트 지표도 함께
        for key in ("delivered_count", "delivery_rate", "last_sent_at"):
            assert key in row

    def test_last_sent_at_reflects_latest_log(self, staff_client):
        camp = _mk_campaign()
        log = _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        row = _row(staff_client.get(CAMPAIGNS_URL, {"search": camp.name}), camp)
        assert row["last_sent_at"] is not None
        assert row["delivered_count"] == 1
        assert log.campaign_id == camp.id


# ─── DM-5: unique_* 모수 통일 불변식 ──────────────────────────────────


class TestUniqueBaseInvariants:
    """`unique_*` 가 전부 루트 DM 모수라는 계약 — 깨지면 프론트 비율/진행률이 100%를 넘는다."""

    def test_funnel_is_monotone(self, db):
        camp = _mk_campaign(follow_gate_enabled=True)
        op = _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        # 리워드가 read 여도 루트가 delivered 면 read 인원은 늘지 않는다(같은 모수)
        _mk_log(camp, "r1", SentDMLog.Status.READ, kind=SentDMLog.DMKind.REWARD, parent=op)
        _mk_log(camp, "r2", SentDMLog.Status.QUEUED)

        d = build_dm_stats(camp.dm_logs.all())
        assert d["unique_targets"] >= d["unique_sent"] >= d["unique_delivered"] >= d["unique_read"]
        assert (d["unique_targets"], d["unique_sent"], d["unique_delivered"]) == (2, 1, 1)
        assert d["unique_read"] == 0  # 루트는 delivered — 리워드 read 는 모수 밖

    def test_ctr_cannot_exceed_one_for_hidden_folder_clicker(self, db):
        """숨김함에서 버튼을 누른 사람 — 예전엔 분자만 세어 CTR 이 1을 넘을 수 있었다.

        루트가 recovery_pending 이라 '발송'으로 안 잡히는데, 클릭 증거(child)는 남는다.
        분자를 발송 인원과 교집합해 분모 초과를 원천 차단한다.
        """
        camp = _mk_campaign(follow_gate_enabled=True)
        hidden = _mk_log(
            camp, "r1", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025"
        )
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED, kind=SentDMLog.DMKind.REWARD, parent=hidden)

        d = build_dm_stats(camp.dm_logs.all())
        assert d["unique_sent"] == 0 and d["ctr_denominator"] == 0
        assert d["ctr_interacted"] == 0  # 발송 인원 밖 → 분자에서도 제외
        assert d["ctr"] == 0.0

    def test_followers_counted_from_child_evidence_but_capped_by_sent(self, db):
        """게이트 통과 표시가 리워드에만 있어도 인원은 잡히고, 발송 인원을 넘지 않는다."""
        camp = _mk_campaign(follow_gate_enabled=True)
        op = _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        reward = _mk_log(
            camp, "r1", SentDMLog.Status.DELIVERED, kind=SentDMLog.DMKind.REWARD, parent=op
        )
        SentDMLog.objects.filter(pk=reward.pk).update(gate_status=SentDMLog.GateStatus.PASSED)

        d = build_dm_stats(camp.dm_logs.all())
        assert d["unique_followers"] == 1 <= d["unique_sent"]

    def test_unique_delivered_counts_recovery_delivered(self, db):
        """복구 재전송 성공은 실제 도착 — 이벤트 delivery_rate 와 정의를 맞춘다."""
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.RECOVERY_DELIVERED)

        d = build_dm_stats(camp.dm_logs.all())
        assert d["unique_delivered"] == 1
        assert d["unique_reach_rate"] == 1.0


# ─── DM-1-c: 사람 단위 정렬 ───────────────────────────────────────────


class TestPeopleOrdering:
    def test_ordering_by_people_sent(self, staff_client):
        conn = _mk_conn()
        tag = uuid.uuid4().hex[:8]
        small = _mk_campaign(conn, name=f"ord-{tag}-small")
        big = _mk_campaign(conn, name=f"ord-{tag}-big")
        _mk_log(small, "r1", SentDMLog.Status.DELIVERED)
        for i in range(3):
            _mk_log(big, f"b{i}", SentDMLog.Status.DELIVERED)

        res = staff_client.get(CAMPAIGNS_URL, {"search": f"ord-{tag}", "ordering": "-people_sent"})
        ids = [r["id"] for r in res.data["results"]]
        assert ids == [str(big.id), str(small.id)]

        res_asc = staff_client.get(
            CAMPAIGNS_URL, {"search": f"ord-{tag}", "ordering": "people_sent"}
        )
        assert [r["id"] for r in res_asc.data["results"]] == [str(small.id), str(big.id)]

    def test_ordering_by_people_targets(self, staff_client):
        conn = _mk_conn()
        tag = uuid.uuid4().hex[:8]
        few = _mk_campaign(conn, name=f"tg-{tag}-few")
        many = _mk_campaign(conn, name=f"tg-{tag}-many")
        _mk_log(few, "r1", SentDMLog.Status.FAILED_TOKEN)
        for i in range(2):
            _mk_log(many, f"m{i}", SentDMLog.Status.FAILED_TOKEN)

        res = staff_client.get(
            CAMPAIGNS_URL, {"search": f"tg-{tag}", "ordering": "-people_targets"}
        )
        assert [r["id"] for r in res.data["results"]] == [str(many.id), str(few.id)]


# ─── DM-2: 오류 원인·조치 ─────────────────────────────────────────────


class TestLogErrorCatalog:
    def test_detail_exposes_subcode_and_catalog(self, staff_client):
        camp = _mk_campaign()
        log = _mk_log(
            camp,
            "r1",
            SentDMLog.Status.RECOVERY_PENDING,
            code="100",
            subcode="2534025",
            msg="(#100) Param recipient",
        )
        data = staff_client.get(f"{LOGS_URL}{log.id}/").data
        assert data["error_subcode"] == "2534025"
        assert "숨겨진" in data["error_title"]
        assert data["error_cause"] and data["error_action"]
        assert data["recoverable"] is True
        assert data["error_message"] == "(#100) Param recipient"  # 원문 그대로 유지

    def test_window_expiry_is_not_recoverable(self, staff_client):
        """100/2534025(복구 대상) 와 100/2534022(정상 실패)는 조치가 정반대."""
        camp = _mk_campaign()
        log = _mk_log(camp, "r1", SentDMLog.Status.FAILED_WINDOW, code="100", subcode="2534022")
        data = staff_client.get(f"{LOGS_URL}{log.id}/").data
        assert "윈도우" in data["error_title"]
        assert data["recoverable"] is False

    def test_unknown_combination_returns_blanks_not_error(self, staff_client):
        camp = _mk_campaign()
        log = _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        data = staff_client.get(f"{LOGS_URL}{log.id}/").data
        assert data["error_title"] == ""
        assert data["error_cause"] == ""
        assert data["error_action"] == ""
        assert data["recoverable"] is False

    def test_log_list_row_has_title_and_subcode(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.FAILED_TOKEN, code="190")
        row = staff_client.get(LOGS_URL, {"campaign_id": str(camp.id)}).data["results"][0]
        assert row["error_subcode"] == ""
        assert "토큰" in row["error_title"]

    def test_recipient_row_has_title_from_latest_log(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.FAILED_TOKEN, code="190")
        row = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)}).data["results"][0]
        assert "토큰" in row["error_title"]

    def test_recipient_row_title_blank_when_latest_is_success(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.FAILED_TOKEN, code="190")
        _mk_log(camp, "r1", SentDMLog.Status.READ)  # 최신 로그는 성공
        row = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)}).data["results"][0]
        assert row["error_title"] == ""


# ─── DM-3: 어드민 queue-state / timeseries ────────────────────────────


class TestAdminQueueState:
    def _url(self, camp):
        return f"{CAMPAIGNS_URL}{camp.id}/queue-state/"

    def test_returns_same_shape_as_user_console(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.QUEUED)
        _mk_log(camp, "r2", SentDMLog.Status.DELIVERED)

        res = staff_client.get(self._url(camp))
        assert res.status_code == 200
        assert res.data["scope"] == "campaign"
        assert res.data["campaign_id"] == str(camp.id)
        assert set(res.data["gauge"]) == {"sent", "waiting", "in_flight", "failed", "total"}
        assert res.data["gauge"]["waiting"] == 1
        # people 은 상세 stats 의 unique_* 와 같은 정의 (people_rollup)
        assert res.data["people"]["total"] == 2
        assert res.data["people"]["sent"] == 1
        assert res.data["people"]["waiting"] == 1
        assert "blocking_reason" in res.data
        assert "eta_seconds" in res.data

    def test_people_matches_campaign_detail_stats(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        _mk_log(camp, "r2", SentDMLog.Status.QUEUED)

        qs = staff_client.get(self._url(camp)).data["people"]
        stats = staff_client.get(f"{CAMPAIGNS_URL}{camp.id}/").data["stats"]
        assert qs["total"] == stats["unique_targets"]
        assert qs["sent"] == stats["unique_sent"]
        assert qs["waiting"] == stats["unique_waiting"]

    def test_requires_staff(self, regular_client, db):
        camp = _mk_campaign()
        assert regular_client.get(self._url(camp)).status_code == 403
        # regular_client 은 client 픽스처와 같은 인스턴스라 익명 검증은 새 클라이언트로
        assert APIClient().get(self._url(camp)).status_code == 401

    def test_unknown_campaign_404(self, staff_client):
        assert staff_client.get(f"{CAMPAIGNS_URL}{uuid.uuid4()}/queue-state/").status_code == 404

    def test_no_workspace_membership_needed(self, staff_client, staff_user):
        """어드민은 교차-워크스페이스 — 남의 워크스페이스 캠페인도 200."""
        camp = _mk_campaign()
        assert not camp.ig_connection.workspace.memberships.filter(user=staff_user).exists()
        assert staff_client.get(self._url(camp)).status_code == 200


class TestAdminTimeseries:
    def _url(self, camp):
        return f"{CAMPAIGNS_URL}{camp.id}/timeseries/"

    def test_all_range_totals_match_people_total(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)  # 같은 사람 재요청 → 1명
        _mk_log(camp, "r2", SentDMLog.Status.QUEUED)

        res = staff_client.get(self._url(camp))
        assert res.status_code == 200
        totals = res.data["totals"]
        assert totals["lifetime_unique_requesters"] == 2
        assert sum(b["new_requesters"] for b in res.data["series"]) == 2
        assert res.data["granularity"] == "day"

    def test_range_24h_uses_hour_buckets(self, staff_client):
        camp = _mk_campaign()
        _mk_log(camp, "r1", SentDMLog.Status.DELIVERED)
        res = staff_client.get(self._url(camp), {"range": "24h"})
        assert res.data["granularity"] == "hour"
        assert len(res.data["series"]) == 24

    def test_invalid_range_400_project_error_format(self, staff_client):
        camp = _mk_campaign()
        res = staff_client.get(self._url(camp), {"range": "1y"})
        assert res.status_code == 400
        assert res.data["success"] is False
        assert res.data["error"]["details"]["allowed"] == ["24h", "7d", "all"]

    def test_requires_staff(self, regular_client, db):
        camp = _mk_campaign()
        assert regular_client.get(self._url(camp)).status_code == 403

    def test_campaign_status_echoed(self, staff_client):
        camp = _mk_campaign(status=AutoDMCampaign.Status.PAUSED)
        res = staff_client.get(self._url(camp))
        assert res.data["campaign_status"] == "paused"
        assert res.data["is_active"] is False
        assert timezone.now() is not None
