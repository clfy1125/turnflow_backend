"""후속 DM 축 · 사람 단위 policy 집계 회귀 테스트 (DM-6 / DM-7 / DM-8 / DM-11).

프론트 요청서 11차(2026-07-31)가 화면에서 그대로 쓰는 계약을 고정한다.

  DM-6  follow_up 블록 — **마지막 후속 DM 1건** 기준 (오프닝 축과 규칙이 다름)
        불변식: targets == delivered + waiting + failed
  DM-7  not_sent 블록 — 사람 단위 🔴/⚪ 분해 + 사유별 인원
        불변식: investigate + normal == total == Σ breakdown[].people
  DM-8  수신자 목록의 error_policy / latest_followup_status / ?error_policy= 필터
  DM-11 unique_accepted_pending — 화면 '대기중' 줄 = waiting + accepted_pending

실행:
    docker compose exec web pytest apps/admin_api/tests_dm_followup_policy.py
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.admin_api.dm_error_catalog import INVESTIGATE, NORMAL
from apps.admin_api.dm_policy_rollup import followup_not_sent, opening_not_sent
from apps.integrations.campaign_stats import build_dm_stats, followup_rollup
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

CAMPAIGNS_URL = "/api/v1/admin/auto-dm/campaigns/"
RECIPIENTS_URL = "/api/v1/admin/auto-dm/recipients/"

pytestmark = pytest.mark.django_db


# ── 픽스처 (DB 가 더러워도 안전하도록 uuid 이메일 · 캠페인 단위 스코프) ──────────


@pytest.fixture
def staff_client(db):
    user = get_user_model().objects.create_user(
        email=f"staff-{uuid.uuid4().hex[:8]}@t.dev", password="x", is_staff=True
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _mk_campaign():
    owner = get_user_model().objects.create_user(
        email=f"own-{uuid.uuid4().hex[:8]}@t.dev", password="x"
    )
    ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=uuid.uuid4().hex[:12],
        username="ig",
        status=IGAccountConnection.Status.ACTIVE,
    )
    return AutoDMCampaign.objects.create(
        ig_connection=conn, name=f"c-{uuid.uuid4().hex[:6]}", media_id="m1"
    )


def _log(camp, rcpt, status, *, kind=SentDMLog.DMKind.OPENING, code="", subcode="", parent=None):
    return SentDMLog.objects.create(
        campaign=camp,
        recipient_user_id=rcpt,
        recipient_username=rcpt,
        status=status,
        dm_kind=kind,
        error_code=code,
        error_subcode=subcode,
        parent_log=parent,
        idempotency_key=uuid.uuid4().hex,
    )


def _reward(camp, rcpt, status, *, code="", subcode=""):
    return _log(camp, rcpt, status, kind=SentDMLog.DMKind.REWARD, code=code, subcode=subcode)


# ── DM-6: follow_up 블록 ──────────────────────────────────────────────


class TestFollowUpBlock:
    def test_latest_wins_not_bucket_priority(self):
        """첫 후속은 도착했지만 **마지막 후속이 실패**한 사람 = 발송 안 됨.

        요청서의 핵심 근거: "하나라도 성공" 방식으로 접으면 이 사람이 성공으로 잡힌다.
        """
        camp = _mk_campaign()
        _reward(camp, "A", SentDMLog.Status.DELIVERED)
        _reward(camp, "A", SentDMLog.Status.FAILED_PARAM, code="100", subcode="2534001")

        fu = followup_rollup(camp.dm_logs.all())
        assert fu["targets"] == 1
        assert fu["delivered"] == 0
        assert fu["failed"] == 1
        assert fu["basis"] == "latest_per_person"

    def test_buckets_sum_to_targets(self):
        camp = _mk_campaign()
        _reward(camp, "A", SentDMLog.Status.READ)
        _reward(camp, "B", SentDMLog.Status.DELIVERED)
        _reward(camp, "C", SentDMLog.Status.QUEUED)
        _reward(camp, "D", SentDMLog.Status.ACCEPTED)
        _reward(camp, "E", SentDMLog.Status.FAILED_NO_TRACE)
        _reward(camp, "F", SentDMLog.Status.FAILED_TOKEN)

        fu = followup_rollup(camp.dm_logs.all())
        assert fu["targets"] == 6
        assert fu["targets"] == fu["delivered"] + fu["waiting"] + fu["failed"]
        assert fu["delivered"] == 2 and fu["read"] == 1
        # DM-17 — 키 이름에 부모 집합을 박았다: 후속 축은 waiting 의 부분집합이고
        # 오프닝 축의 unique_accepted_pending 은 sent 의 부분집합이라 관계가 반대다.
        assert fu["waiting"] == 2 and fu["accepted_pending_in_waiting"] == 1
        assert "accepted_pending" not in fu
        assert fu["failed"] == 2
        # unconfirmed 는 failed 의 부분집합 (오프닝 축과 반대 — 계약으로 고정)
        assert fu["unconfirmed"] == 1
        assert fu["reach_rate"] == round(2 / 6, 4)

    def test_opening_axis_untouched_by_rewards(self):
        """후속 DM 이 오프닝 축(unique_*) 모수에 새어 들어오지 않아야 한다."""
        camp = _mk_campaign()
        op = _log(camp, "A", SentDMLog.Status.DELIVERED)
        _reward(camp, "A", SentDMLog.Status.FAILED_TOKEN)
        _log(camp, "A", SentDMLog.Status.QUEUED, parent=op)  # 오프닝 재시도(child)

        s = build_dm_stats(camp.dm_logs.all())
        assert s["unique_targets"] == 1
        assert s["unique_delivered"] == 1
        assert s["follow_up"]["targets"] == 1
        assert s["follow_up"]["failed"] == 1

    def test_retry_is_not_counted_as_followup(self):
        """오프닝 재시도(dm_kind=opening + parent)는 후속이 아니다 — 이중 계상 방지."""
        camp = _mk_campaign()
        op = _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)
        _log(camp, "A", SentDMLog.Status.DELIVERED, parent=op)

        assert followup_rollup(camp.dm_logs.all())["targets"] == 0

    def test_empty_is_zero_not_error(self):
        camp = _mk_campaign()
        fu = followup_rollup(camp.dm_logs.all())
        assert fu["targets"] == 0 and fu["reach_rate"] == 0.0


# ── DM-11: unique_accepted_pending ────────────────────────────────────


class TestAcceptedPending:
    def test_waiting_row_adds_up(self):
        """화면 3줄(도착/대기중/발송 안 됨)의 합이 전체 요청과 같아야 한다."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.DELIVERED)
        _log(camp, "B", SentDMLog.Status.ACCEPTED)  # 접수 후 도착 대기
        _log(camp, "C", SentDMLog.Status.QUEUED)  # 큐 대기
        _log(camp, "D", SentDMLog.Status.FAILED_NO_TRACE)  # 도착 미확인
        _log(camp, "E", SentDMLog.Status.FAILED_TOKEN)  # 실패

        s = build_dm_stats(camp.dm_logs.all())
        assert s["unique_accepted_pending"] == 1
        delivered = s["unique_delivered"]
        waiting_row = s["unique_waiting"] + s["unique_accepted_pending"]
        not_sent_row = s["unique_failed"] + s["unique_unconfirmed"]
        assert delivered + waiting_row + not_sent_row == s["unique_targets"] == 5

    def test_identity_sent_equals_delivered_pending_unconfirmed(self):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.READ)
        _log(camp, "B", SentDMLog.Status.ACCEPTED)
        _log(camp, "C", SentDMLog.Status.FAILED_NO_TRACE)

        s = build_dm_stats(camp.dm_logs.all())
        assert (
            s["unique_sent"]
            == s["unique_delivered"] + s["unique_accepted_pending"] + s["unique_unconfirmed"]
        )


# ── DM-7: not_sent 사람 단위 policy 분해 ──────────────────────────────


class TestNotSentBlock:
    def test_contract_totals(self):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")  # ⚪ 재연동 안내
        _log(camp, "B", SentDMLog.Status.FAILED_NO_TRACE)  # 🔴 도착 미확인
        _log(camp, "C", SentDMLog.Status.FAILED_WINDOW, code="100", subcode="2534022")  # 🔴
        _log(camp, "D", SentDMLog.Status.DELIVERED)  # 성공 — 들어오면 안 됨

        blk = opening_not_sent(camp.dm_logs.all())
        assert blk["total"] == 3
        assert blk["investigate"] + blk["normal"] == blk["total"]
        assert sum(b["people"] for b in blk["breakdown"]) == blk["total"]
        assert blk["investigate"] == 2 and blk["normal"] == 1

    def test_matches_unique_failed_plus_unconfirmed(self):
        """DM-7 계약: total == unique_failed + unique_unconfirmed (표와 팝업이 같은 수)."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")
        _log(camp, "B", SentDMLog.Status.FAILED_NO_TRACE)
        _log(camp, "C", SentDMLog.Status.RECOVERY_PENDING, code="100", subcode="2534025")
        _log(camp, "D", SentDMLog.Status.READ)
        _log(camp, "E", SentDMLog.Status.QUEUED)

        s = build_dm_stats(camp.dm_logs.all())
        blk = opening_not_sent(camp.dm_logs.all())
        assert blk["total"] == s["unique_failed"] + s["unique_unconfirmed"]

    def test_representative_is_latest_failure(self):
        """대표 사유 = 가장 최근 실패 로그 (요청서 DM-7 §4)."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")
        _log(camp, "A", SentDMLog.Status.FAILED_WINDOW, code="100", subcode="2534022")

        blk = opening_not_sent(camp.dm_logs.all())
        assert blk["total"] == 1
        assert len(blk["breakdown"]) == 1
        assert blk["breakdown"][0]["policy"] == INVESTIGATE  # 최신(창 만료) 쪽

    def test_investigate_sorted_first(self):
        camp = _mk_campaign()
        for i in range(3):
            _log(camp, f"n{i}", SentDMLog.Status.FAILED_TOKEN, code="190")  # ⚪ 3명
        _log(camp, "x", SentDMLog.Status.FAILED_NO_TRACE)  # 🔴 1명

        blk = opening_not_sent(camp.dm_logs.all())
        assert blk["breakdown"][0]["policy"] == INVESTIGATE  # 인원이 적어도 🔴 먼저

    def test_followup_axis_uses_latest_rule(self):
        camp = _mk_campaign()
        _reward(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")
        _reward(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)  # 마지막
        _reward(camp, "B", SentDMLog.Status.DELIVERED)

        blk = followup_not_sent(camp.dm_logs.all())
        assert blk["total"] == 1
        assert blk["investigate"] == 1  # 마지막이 도착 미확인 → 🔴

    def test_empty_block_shape(self):
        camp = _mk_campaign()
        blk = opening_not_sent(camp.dm_logs.all())
        assert blk == {"total": 0, "investigate": 0, "normal": 0, "breakdown": []}

    def test_campaign_detail_exposes_both_axes(self, staff_client):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)
        _reward(camp, "B", SentDMLog.Status.FAILED_TOKEN, code="190")

        stats = staff_client.get(f"{CAMPAIGNS_URL}{camp.id}/").data["stats"]
        assert stats["not_sent"]["total"] == 1
        assert stats["follow_up"]["not_sent"]["total"] == 1
        assert stats["follow_up"]["basis"] == "latest_per_person"

    def test_user_console_stats_has_no_policy_vocabulary(self):
        """policy 는 운영자 어휘 — 공용 build_dm_stats 응답에 새면 안 된다."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)
        s = build_dm_stats(camp.dm_logs.all())
        assert "not_sent" not in s
        assert "not_sent" not in s["follow_up"]


# ── DM-8: 수신자 목록 ─────────────────────────────────────────────────


class TestRecipientPolicyFields:
    def test_row_has_policy_and_followup_status(self, staff_client):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)
        _reward(camp, "A", SentDMLog.Status.DELIVERED)

        row = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)}).data["results"][0]
        assert row["error_policy"] == INVESTIGATE
        assert row["latest_followup_status"] == "delivered"

    def test_policy_blank_when_no_failure(self, staff_client):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.READ)
        row = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)}).data["results"][0]
        assert row["error_policy"] == ""
        assert row["latest_followup_status"] == ""

    def test_filter_matches_card_count(self, staff_client):
        """드릴다운 계약 — 카드 인원과 목록 건수가 정확히 같아야 한다."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)  # 🔴
        _log(camp, "B", SentDMLog.Status.FAILED_WINDOW, code="100", subcode="2534022")  # 🔴
        _log(camp, "C", SentDMLog.Status.FAILED_TOKEN, code="190")  # ⚪
        _log(camp, "D", SentDMLog.Status.READ)  # 분류 없음

        card = opening_not_sent(camp.dm_logs.all())
        res = staff_client.get(
            RECIPIENTS_URL, {"campaign_id": str(camp.id), "error_policy": INVESTIGATE}
        ).data
        assert res["count"] == card["investigate"] == 2

        res_normal = staff_client.get(
            RECIPIENTS_URL, {"campaign_id": str(camp.id), "error_policy": NORMAL}
        ).data
        assert res_normal["count"] == card["normal"] == 1

    def test_invalid_policy_is_400(self, staff_client):
        camp = _mk_campaign()
        res = staff_client.get(
            RECIPIENTS_URL, {"campaign_id": str(camp.id), "error_policy": "nope"}
        )
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "error_policy"

    def test_all_is_passthrough(self, staff_client):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)
        _log(camp, "B", SentDMLog.Status.READ)
        res = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id), "error_policy": "all"})
        assert res.data["count"] == 2
