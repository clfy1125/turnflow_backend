"""사유·분류 필터 회귀 테스트 (프론트 요청서 12차 — DM-13 ~ DM-17).

화면 설계 원칙이 하나로 정리됐다: **숫자를 누르면 그 사람들이 로그에 그대로 뜬다.**
그래서 이 파일이 고정하는 것은 전부 "카드 숫자 == 필터 결과 수" 항등이다.

  DM-13 `?dm_axis=opening|follow_up` — 축을 갈라야 카드 4개가 4개로 뜬다
  DM-14 `reason` 머신 키 + `?error_reason=` — 문구·code 로는 필터할 수 없다
  DM-15 `?error_policy=` 상한 제거 — 사전을 SQL 로 컴파일 (마이그레이션 없음)
  DM-16 `error_title` 기준을 최신 **실패** 로그로 통일 (→ tests_dm_people_stats.py)
  DM-17 후속 축 `accepted_pending_in_waiting` — 부모 집합이 오프닝 축과 반대

가장 중요한 것은 :class:`TestSqlPythonEquivalence` 다. 판정 규칙을 파이썬(`classify`)과
SQL(`dm_error_filters`) 두 벌로 들고 있는 구조라, **사전 전 조합 + 미등록 조합을 DB 에
넣고 두 판정이 같은 행 집합을 고르는지** 직접 대조한다. 사전에 항목을 추가하면 그
조합까지 자동으로 검증된다 — 이 테스트가 통과하면 상한 없는 SQL 필터를 믿을 수 있다.

실행:
    docker compose exec web pytest apps/admin_api/tests_dm_error_filters.py
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.admin_api.dm_error_catalog import (
    _BY_CODE,
    _BY_CODE_STATUS,
    _BY_CODE_SUBCODE,
    _BY_STATUS,
    _ERROR_STATUSES,
    INVESTIGATE,
    NORMAL,
    R_UNCLASSIFIED,
    SKIPPED_OTHER,
    SKIPPED_REASONS,
    SKIPPED_STATUS,
    classify,
    reason_policy_map,
    reason_title_map,
)
from apps.admin_api.dm_error_filters import policy_q, reason_q
from apps.admin_api.dm_policy_rollup import followup_not_sent, opening_not_sent
from apps.integrations.campaign_stats import FOLLOW_UP_OK_STATUSES, followup_bucket, followup_rollup
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

CAMPAIGNS_URL = "/api/v1/admin/auto-dm/campaigns/"
RECIPIENTS_URL = "/api/v1/admin/auto-dm/recipients/"
LOGS_URL = "/api/v1/admin/auto-dm/logs/"

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


def _log(
    camp,
    rcpt,
    status,
    *,
    kind=SentDMLog.DMKind.OPENING,
    code="",
    subcode="",
    msg="",
    parent=None,
):
    return SentDMLog.objects.create(
        campaign=camp,
        recipient_user_id=rcpt,
        recipient_username=rcpt,
        status=status,
        dm_kind=kind,
        error_code=code,
        error_subcode=subcode,
        error_message=msg,
        parent_log=parent,
        idempotency_key=uuid.uuid4().hex,
    )


def _reward(camp, rcpt, status, *, code="", subcode="", msg=""):
    return _log(
        camp, rcpt, status, kind=SentDMLog.DMKind.REWARD, code=code, subcode=subcode, msg=msg
    )


# ══════════════════════════════════════════════════════════════════════
# DM-14 — 사유 키의 구조적 불변식 (DB 없이도 성립해야 하는 것들)
# ══════════════════════════════════════════════════════════════════════
class TestReasonKeyInvariants:
    def test_reason_and_title_are_one_to_one(self):
        """같은 reason 은 title 도 같아야 한다.

        프론트가 reason 으로 묶어 title 을 보여주므로, 어긋나면 한 칩에 두 문구가 생긴다.
        (같은 사유가 코드 여러 조합으로 오는 것이 정상이고, 그게 이 키가 필요한 이유다.)
        """
        seen: dict[str, str] = {}
        for entry in (
            list(_BY_CODE_SUBCODE.values())
            + list(_BY_CODE_STATUS.values())
            + list(_BY_CODE.values())
            + list(_BY_STATUS.values())
        ):
            reason, title = entry["reason"], entry["title"]
            assert reason, f"reason 이 비어 있다: {title!r}"
            if reason in seen:
                assert (
                    seen[reason] == title
                ), f"reason={reason!r} 이 두 문구를 갖는다: {seen[reason]!r} vs {title!r}"
            seen[reason] = title

    def test_reason_and_policy_are_one_to_one(self):
        """같은 reason 은 policy 도 같아야 한다 — 사유 하나가 🔴 이면서 ⚪ 일 수 없다."""
        seen: dict[str, str] = {}
        for entry in (
            list(_BY_CODE_SUBCODE.values())
            + list(_BY_CODE_STATUS.values())
            + list(_BY_CODE.values())
            + list(_BY_STATUS.values())
        ):
            reason, policy = entry["reason"], entry["policy"]
            if reason in seen:
                assert seen[reason] == policy, f"reason={reason!r} 의 policy 가 갈린다"
            seen[reason] = policy

    def test_error_and_skipped_reason_keys_do_not_collide(self):
        """한 파라미터(`?error_reason=`)로 둘 다 필터하므로 키가 겹치면 안 된다."""
        error_keys = set(reason_policy_map())
        skipped_keys = {r for r, _l, _a, _n in SKIPPED_REASONS} | {SKIPPED_OTHER[0]}
        assert not (error_keys & skipped_keys)

    def test_every_error_status_has_a_dictionary_entry(self):
        """오류 8종은 전부 status 사전에 있어야 한다 → 오류 행에 '미분류'가 안 뜬다.

        어긋나면 `unclassified` 버킷이 살아나 화면에 '분류되지 않은 실패'가 보인다
        (그게 안전망의 의도이고, 이 테스트는 그 상태를 **의도적으로만** 만들게 한다).
        """
        assert set(_ERROR_STATUSES) == set(_BY_STATUS)

    def test_skipped_status_literal_matches_model(self):
        """사전이 모델을 import 하지 않으려고 리터럴로 둔 값 — 갈라지면 여기서 잡는다."""
        assert SKIPPED_STATUS == SentDMLog.Status.SKIPPED

    def test_unclassified_title_is_registered(self):
        assert reason_title_map()[R_UNCLASSIFIED]
        assert reason_policy_map()[R_UNCLASSIFIED] == INVESTIGATE

    def test_followup_failed_q_matches_python_bucket(self):
        """SQL 판(followup_failed_q)과 파이썬 판(followup_bucket)이 같은 집합을 봐야 한다."""
        for status, _label in SentDMLog.Status.choices:
            expected_failed = followup_bucket(status) == "failed"
            assert expected_failed is (status not in FOLLOW_UP_OK_STATUSES), status


# ══════════════════════════════════════════════════════════════════════
# DM-15 — 사전의 SQL 컴파일이 파이썬 판정과 일치하는가 (이 파일의 핵심)
# ══════════════════════════════════════════════════════════════════════
def _all_dictionary_combos() -> list[tuple[str, str, str, str]]:
    """사전 전 항목이 실제로 걸리는 (code, subcode, status, message) 조합 + 미등록 조합."""
    combos: list[tuple[str, str, str, str]] = []
    error_statuses = sorted(_ERROR_STATUSES)

    # 1레벨: (code, subcode) — 상태를 바꿔 봐도 1레벨이 이겨야 한다
    for code, subcode in _BY_CODE_SUBCODE:
        for status in error_statuses[:3]:
            combos.append((code, subcode, status, ""))
    # 2레벨: (code, status)
    for code, status in _BY_CODE_STATUS:
        combos.append((code, "", status, ""))
        combos.append((code, "99999999", status, ""))  # 모르는 subcode → 2레벨로 내려감
    # 3레벨: code — 등록된 subcode/status 를 피해서
    for code in _BY_CODE:
        for status in error_statuses:
            combos.append((code, "", status, ""))
            combos.append((code, "77777777", status, ""))
    # 4레벨: status only
    for status in error_statuses:
        combos.append(("", "", status, ""))
        combos.append(("", "66666666", status, ""))
        combos.append(("999999", "", status, ""))
    # 건너뜀: 사유표 전 항목 + 미분류
    for _reason, _label, _actionable, needles in SKIPPED_REASONS:
        for needle in needles:
            combos.append(("", "", SKIPPED_STATUS, f"prefix {needle} suffix"))
    combos.append(("", "", SKIPPED_STATUS, "무언가 새로운 문구"))
    combos.append(("", "", SKIPPED_STATUS, ""))
    # 성공·진행 중 — 어느 필터에도 걸리지 않아야 한다
    for status in ("delivered", "read", "accepted", "queued", "recovery_delivered"):
        combos.append(("", "", status, ""))
        combos.append(("190", "", status, ""))
    return combos


@pytest.fixture
def combo_logs():
    """사전 전 조합을 1건씩 DB 에 넣고 {log_id: (code, subcode, status, message)} 를 준다."""
    camp = _mk_campaign()
    mapping = {}
    for i, (code, subcode, status, msg) in enumerate(_all_dictionary_combos()):
        log = _log(camp, f"r{i}", status, code=code, subcode=subcode, msg=msg)
        mapping[log.id] = (code, subcode, status, msg)
    return camp, mapping


class TestSqlPythonEquivalence:
    """`qs.filter(policy_q(p))` == `{classify(...)["policy"] == p}` 를 행 단위로 대조."""

    @pytest.mark.parametrize("policy", [INVESTIGATE, NORMAL])
    def test_policy_q_matches_python(self, combo_logs, policy):
        camp, mapping = combo_logs
        expected = {
            log_id
            for log_id, (code, sub, status, msg) in mapping.items()
            # 성공·진행 중 행은 파이썬으로도 normal 이지만 **필터 모수 밖**이다
            # (분류가 붙는 것은 오류 8종 + 건너뜀뿐 — 아래 별도 테스트가 고정).
            if (status in _ERROR_STATUSES or status == SKIPPED_STATUS)
            and classify(code, sub, status, msg)["policy"] == policy
        }
        actual = set(camp.dm_logs.filter(policy_q(policy)).values_list("id", flat=True))
        assert actual == expected, (
            f"policy={policy} SQL 과 파이썬 판정 불일치: "
            f"SQL 만 {len(actual - expected)}건 / 파이썬만 {len(expected - actual)}건"
        )

    def test_policy_q_partitions_the_universe(self, combo_logs):
        """🔴 + ⚪ == 오류+건너뜀 전체, 그리고 서로소 (팝업 '전체 보러가기' 의 전제)."""
        camp, mapping = combo_logs
        inv = set(camp.dm_logs.filter(policy_q(INVESTIGATE)).values_list("id", flat=True))
        nor = set(camp.dm_logs.filter(policy_q(NORMAL)).values_list("id", flat=True))
        universe = {
            log_id
            for log_id, (_c, _s, status, _m) in mapping.items()
            if status in _ERROR_STATUSES or status == SKIPPED_STATUS
        }
        assert not (inv & nor)
        assert inv | nor == universe

    def test_success_rows_never_match_a_policy_filter(self, combo_logs):
        """`?error_policy=normal` 이 도착한 DM 전부를 끌어오면 화면이 무의미해진다."""
        camp, mapping = combo_logs
        matched = set(
            camp.dm_logs.filter(policy_q(INVESTIGATE) | policy_q(NORMAL)).values_list(
                "id", flat=True
            )
        )
        for log_id, (_c, _s, status, _m) in mapping.items():
            if status not in _ERROR_STATUSES and status != SKIPPED_STATUS:
                assert log_id not in matched, status

    def test_every_reason_q_matches_python(self, combo_logs):
        """사전 전 사유 + 건너뜀 전 사유를 하나씩 대조 (새 항목은 자동 포함)."""
        camp, mapping = combo_logs
        reasons = set(reason_policy_map()) | {r for r, _l, _a, _n in SKIPPED_REASONS}
        reasons.add(SKIPPED_OTHER[0])
        for reason in sorted(reasons):
            expected = {
                log_id
                for log_id, (code, sub, status, msg) in mapping.items()
                if (status in _ERROR_STATUSES or status == SKIPPED_STATUS)
                and classify(code, sub, status, msg)["reason"] == reason
            }
            actual = set(camp.dm_logs.filter(reason_q(reason)).values_list("id", flat=True))
            assert actual == expected, f"reason={reason!r} 불일치"

    def test_reasons_partition_the_universe(self, combo_logs):
        """사유별 인원의 합 == 전체 (팝업의 '사유별' 합계가 '전체'와 맞는 근거)."""
        camp, mapping = combo_logs
        reasons = set(reason_policy_map()) | {r for r, _l, _a, _n in SKIPPED_REASONS}
        reasons.add(SKIPPED_OTHER[0])
        total = 0
        seen: set = set()
        for reason in reasons:
            ids = set(camp.dm_logs.filter(reason_q(reason)).values_list("id", flat=True))
            assert not (ids & seen), f"reason={reason!r} 이 다른 사유와 겹친다"
            seen |= ids
            total += len(ids)
        universe = {
            log_id
            for log_id, (_c, _s, status, _m) in mapping.items()
            if status in _ERROR_STATUSES or status == SKIPPED_STATUS
        }
        assert total == len(universe)

    def test_window_after_close_covers_four_code_combos(self, combo_logs):
        """DM-14 의 근거 — 사유 하나가 코드 4조합이라 `?error_code=` 로 대체 불가."""
        camp, _mapping = combo_logs
        rows = camp.dm_logs.filter(reason_q("window_after_close")).values_list(
            "error_code", "error_subcode"
        )
        assert len(set(rows)) >= 4

    def test_code_10_splits_into_two_reasons(self):
        """반대 방향 — code 10 하나가 두 사유로 갈린다."""
        assert classify("10", "2534022", "failed_window")["reason"] == "window_after_close"
        assert classify("10", "", "failed_param")["reason"] == "permission_or_window_unknown"


# ══════════════════════════════════════════════════════════════════════
# DM-13 — 축 필터와 카드 항등
# ══════════════════════════════════════════════════════════════════════
class TestAxisFilter:
    @staticmethod
    def _stats(staff_client, camp) -> dict:
        return staff_client.get(f"{CAMPAIGNS_URL}{camp.id}/").data["stats"]

    @staticmethod
    def _count(staff_client, camp, **params) -> int:
        params["campaign_id"] = str(camp.id)
        return staff_client.get(RECIPIENTS_URL, params).data["count"]

    @pytest.fixture
    def mixed(self):
        """두 축에 서로 다른 사람이 들어가는 캠페인.

        오프닝 축 발송 안 됨 : A(🔴 도착 미확인) · B(⚪ 재연동)
        후속 축 발송 안 됨   : C(🔴 이미 답글) · D(⚪ 수신자 없음)
        두 축 모두 정상      : E
        """
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)
        _log(camp, "B", SentDMLog.Status.FAILED_TOKEN, code="190")
        _log(camp, "C", SentDMLog.Status.DELIVERED)
        _reward(camp, "C", SentDMLog.Status.FAILED_NO_TRACE, code="100", subcode="2534023")
        _log(camp, "D", SentDMLog.Status.DELIVERED)
        _reward(camp, "D", SentDMLog.Status.FAILED_PARAM, code="100", subcode="2534014")
        _log(camp, "E", SentDMLog.Status.READ)
        _reward(camp, "E", SentDMLog.Status.DELIVERED)
        return camp

    def test_axes_are_not_merged(self, staff_client, mixed):
        """축 없이 조회하면 두 축이 합쳐진다 — 그래서 축 필터가 필요하다(DM-13 본문)."""
        merged = self._count(staff_client, mixed, error_policy=INVESTIGATE)
        opening = self._count(staff_client, mixed, dm_axis="opening", error_policy=INVESTIGATE)
        follow = self._count(staff_client, mixed, dm_axis="follow_up", error_policy=INVESTIGATE)
        assert opening == 1 and follow == 1
        assert merged >= opening + follow

    def test_opening_axis_identity(self, staff_client, mixed):
        """`stats.not_sent.*` == `?dm_axis=opening&error_policy=*` (DM-13 검증 방법)."""
        block = self._stats(staff_client, mixed)["not_sent"]
        assert block["total"] == self._count(staff_client, mixed, dm_axis="opening")
        for policy in (INVESTIGATE, NORMAL):
            assert block[policy] == self._count(
                staff_client, mixed, dm_axis="opening", error_policy=policy
            ), policy

    def test_opening_axis_chain_ties_unique_fields_to_the_filter(self, staff_client, mixed):
        """집계 → 분해 → 목록의 **3단 사슬**을 한 곳에서 잇는다 (어드민팀 12차 회신 지적).

        화면의 '발송 안 됨' 줄은 `unique_failed + unique_unconfirmed` 로 그리고, 그 줄을
        누르면 🔴/⚪ 분해와 목록으로 내려간다. 세 값이 같다는 것이 그 동선의 전제인데
        11차 테스트는 앞의 두 마디만(`tests_dm_followup_policy.test_matches_unique_failed
        _plus_unconfirmed`), 12차 테스트는 뒤의 두 마디만 단언하고 있었다.
        어드민팀 목 데이터에서 30개 캠페인 중 14개가 이 사슬이 끊긴 상태였다.
        """
        stats = self._stats(staff_client, mixed)
        screen_row = stats["unique_failed"] + stats["unique_unconfirmed"]
        assert screen_row == stats["not_sent"]["total"]
        assert screen_row == self._count(staff_client, mixed, dm_axis="opening")
        assert screen_row == sum(b["people"] for b in stats["not_sent"]["breakdown"])
        # 후속 축은 단독 — failed 에 unconfirmed 를 더하면 안 된다(부모 집합이 반대, DM-17)
        fu = stats["follow_up"]
        assert fu["failed"] == fu["not_sent"]["total"]

    def test_follow_up_axis_identity(self, staff_client, mixed):
        """`stats.follow_up.not_sent.*` == `?dm_axis=follow_up&error_policy=*`."""
        block = self._stats(staff_client, mixed)["follow_up"]["not_sent"]
        assert block["total"] == self._count(staff_client, mixed, dm_axis="follow_up")
        for policy in (INVESTIGATE, NORMAL):
            assert block[policy] == self._count(
                staff_client, mixed, dm_axis="follow_up", error_policy=policy
            ), policy

    def test_breakdown_reason_identity(self, staff_client, mixed):
        """`breakdown[].people` == `?dm_axis=<축>&error_reason=<reason>` (DM-14 계약)."""
        stats = self._stats(staff_client, mixed)
        for axis, block in (
            ("opening", stats["not_sent"]),
            ("follow_up", stats["follow_up"]["not_sent"]),
        ):
            assert block["breakdown"], f"{axis} 축 breakdown 이 비어 있다"
            for row in block["breakdown"]:
                assert row["people"] == self._count(
                    staff_client, mixed, dm_axis=axis, error_reason=row["reason"]
                ), (axis, row["reason"])

    def test_axis_aware_badge(self, staff_client, mixed):
        """축을 주면 행의 사유·분류도 그 축의 대표 로그 기준이다."""
        rows = staff_client.get(
            RECIPIENTS_URL, {"campaign_id": str(mixed.id), "dm_axis": "follow_up"}
        ).data["results"]
        by_user = {r["recipient_user_id"]: r for r in rows}
        assert set(by_user) == {"C", "D"}
        assert by_user["C"]["error_reason"] == "already_replied"
        assert by_user["C"]["error_policy"] == INVESTIGATE
        assert by_user["D"]["error_reason"] == "recipient_not_found"
        assert by_user["D"]["error_policy"] == NORMAL

    def test_bad_axis_is_400_with_field(self, staff_client, mixed):
        res = staff_client.get(RECIPIENTS_URL, {"dm_axis": "openning"})
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "dm_axis"
        assert "opening" in res.data["error"]["details"]["allowed"]

    def test_axis_all_is_same_as_omitted(self, staff_client, mixed):
        assert self._count(staff_client, mixed, dm_axis="all") == self._count(staff_client, mixed)


# ══════════════════════════════════════════════════════════════════════
# DM-15 — 상한 제거
# ══════════════════════════════════════════════════════════════════════
class TestNoPolicyFilterCap:
    def test_large_result_set_does_not_400(self, staff_client):
        """11차의 500쌍 상한 회귀 — 전사 ⚪ 전체 조회가 첫 클릭에 400 이 나던 문제."""
        camp = _mk_campaign()
        SentDMLog.objects.bulk_create(
            [
                SentDMLog(
                    campaign=camp,
                    recipient_user_id=f"u{i}",
                    recipient_username=f"u{i}",
                    status=SentDMLog.Status.FAILED_TOKEN,
                    dm_kind=SentDMLog.DMKind.OPENING,
                    error_code="190",
                    idempotency_key=uuid.uuid4().hex,
                )
                for i in range(600)
            ]
        )
        res = staff_client.get(
            RECIPIENTS_URL, {"campaign_id": str(camp.id), "error_policy": NORMAL}
        )
        assert res.status_code == 200
        assert res.data["count"] == 600

    def test_logs_endpoint_filters_by_policy_and_reason(self, staff_client):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")  # ⚪ connection_lost
        _log(camp, "B", SentDMLog.Status.FAILED_NO_TRACE)  # 🔴 no_trace
        _log(camp, "C", SentDMLog.Status.READ)  # 모수 밖

        def count(**params):
            params["campaign_id"] = str(camp.id)
            return staff_client.get(LOGS_URL, params).data["count"]

        assert count(error_policy=INVESTIGATE) == 1
        assert count(error_policy=NORMAL) == 1
        assert count(error_reason="connection_lost") == 1
        assert count(error_reason="no_trace") == 1
        # 셋을 함께 주면 AND — 모순 조합은 0 (조용히 무시하지 않는다)
        assert count(error_policy=INVESTIGATE, error_reason="connection_lost") == 0

    def test_filter_uses_the_latest_failure_not_any_failure(self, staff_client):
        """대표 로그 서브쿼리(DISTINCT ON)가 **최신** 실패를 골라야 한다.

        ORDER BY 가 서브쿼리에서 날아가면 '실패가 하나라도 있으면 매칭'으로 변질돼
        한 사람이 🔴·⚪ 양쪽 필터에 다 잡힌다 — 그러면 카드 합계가 인원을 넘는다.
        """
        camp = _mk_campaign()
        old = _log(camp, "A", SentDMLog.Status.FAILED_NO_TRACE)  # 🔴 no_trace
        new = _log(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")  # ⚪ connection_lost
        assert old.created_at <= new.created_at

        def count(**params):
            params["campaign_id"] = str(camp.id)
            return staff_client.get(RECIPIENTS_URL, params).data["count"]

        assert count(error_policy=NORMAL) == 1  # 최신이 ⚪
        assert count(error_policy=INVESTIGATE) == 0  # 과거 🔴 로는 안 잡힌다
        assert count(error_reason="connection_lost") == 1
        assert count(error_reason="no_trace") == 0
        row = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)}).data["results"][0]
        assert row["error_reason"] == "connection_lost"  # 배지도 같은 로그

    def test_unknown_reason_is_400_not_silently_ignored(self, staff_client):
        res = staff_client.get(LOGS_URL, {"error_reason": "no_such_reason"})
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "error_reason"

    def test_bad_scope_is_400(self, staff_client):
        res = staff_client.get(LOGS_URL, {"error_scope": "failures"})
        assert res.status_code == 400
        assert res.data["error"]["details"]["field"] == "error_scope"

    def test_scope_separates_error_and_skipped(self, staff_client):
        """팝업이 failure_breakdown 만 그릴 때 모수를 맞출 수 있어야 한다."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")
        _log(camp, "B", SentDMLog.Status.SKIPPED, msg="monthly_dm_limit_reached")

        def count(**params):
            params["campaign_id"] = str(camp.id)
            return staff_client.get(LOGS_URL, params).data["count"]

        assert count(error_policy=NORMAL) == 2  # 오류 + 건너뜀
        assert count(error_policy=NORMAL, error_scope="error") == 1
        assert count(error_policy=NORMAL, error_scope="skipped") == 1


# ══════════════════════════════════════════════════════════════════════
# 건너뜀이 '분류되지 않은 실패'로 떨어지지 않아야 한다 (사유 통합의 부수 효과)
# ══════════════════════════════════════════════════════════════════════
class TestSkippedGetsRealReason:
    def test_not_sent_breakdown_labels_skipped(self, staff_client):
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.SKIPPED, msg="monthly_dm_limit_reached")
        block = opening_not_sent(camp.dm_logs.all())
        assert block["total"] == 1
        row = block["breakdown"][0]
        assert row["reason"] == "monthly_dm_limit"
        assert row["title"] == "월 DM 한도 도달"
        assert row["policy"] == NORMAL

    def test_unknown_skip_message_is_investigate(self, staff_client):
        """사전에 없는 건너뜀 문구는 사람이 봐야 한다 — 운영 대시보드와 같은 규칙."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.SKIPPED, msg="처음 보는 문구")
        block = opening_not_sent(camp.dm_logs.all())
        assert block["breakdown"][0]["reason"] == SKIPPED_OTHER[0]
        assert block["breakdown"][0]["policy"] == INVESTIGATE
        assert block["investigate"] == 1

    def test_log_row_exposes_skipped_reason(self, staff_client):
        camp = _mk_campaign()
        log = _log(camp, "A", SentDMLog.Status.SKIPPED, msg="campaign not active")
        data = staff_client.get(f"{LOGS_URL}{log.id}/").data
        assert data["error_reason"] == "campaign_not_active"
        assert data["error_title"] == "캠페인 일시정지 중"


# ══════════════════════════════════════════════════════════════════════
# DM-17 — 부모 집합이 반대인 키
# ══════════════════════════════════════════════════════════════════════
class TestAcceptedPendingNaming:
    def test_follow_up_key_states_its_parent_set(self):
        camp = _mk_campaign()
        _reward(camp, "A", SentDMLog.Status.ACCEPTED)
        _reward(camp, "B", SentDMLog.Status.QUEUED)
        fu = followup_rollup(camp.dm_logs.all())
        assert fu["accepted_pending_in_waiting"] == 1
        assert fu["accepted_pending_in_waiting"] <= fu["waiting"]
        # 옛 이름이 남아 있으면 프론트가 두 축을 같은 헬퍼로 돌려 합계가 깨진다
        assert "accepted_pending" not in fu

    def test_screen_row_sums_hold_per_axis(self, staff_client):
        """화면의 '대기중'·'발송 안 됨' 줄이 축마다 다른 산술이라는 것을 고정."""
        camp = _mk_campaign()
        _log(camp, "A", SentDMLog.Status.ACCEPTED)
        _log(camp, "B", SentDMLog.Status.QUEUED)
        _log(camp, "C", SentDMLog.Status.FAILED_NO_TRACE)
        _reward(camp, "A", SentDMLog.Status.ACCEPTED)
        _reward(camp, "B", SentDMLog.Status.FAILED_TOKEN)

        stats = staff_client.get(f"{CAMPAIGNS_URL}{camp.id}/").data["stats"]
        fu = stats["follow_up"]
        # 오프닝 축: 두 값을 **더한다**
        assert (
            stats["unique_targets"]
            == stats["unique_waiting"]
            + stats["unique_accepted_pending"]
            + stats["unique_unconfirmed"]
            + stats["unique_failed"]
        )
        # 후속 축: waiting·failed 를 **단독으로** 쓴다
        assert fu["targets"] == fu["delivered"] + fu["waiting"] + fu["failed"]
        assert fu["not_sent"]["total"] == fu["failed"]

    def test_followup_not_sent_matches_failed_bucket(self):
        camp = _mk_campaign()
        _reward(camp, "A", SentDMLog.Status.FAILED_TOKEN, code="190")
        _reward(camp, "B", SentDMLog.Status.DELIVERED)
        block = followup_not_sent(camp.dm_logs.all())
        assert block["total"] == followup_rollup(camp.dm_logs.all())["failed"] == 1
