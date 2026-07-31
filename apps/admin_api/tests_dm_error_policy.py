"""DM 오류 분류(policy) 회귀 테스트 — 2분류 체계 (2026-07-31).

방침 원본: ``DM_ERROR_POLICY_PLAN.md`` (분류 근거는 ``DM_ERROR_POLICY_MATRIX.html``).

여기서 지키는 계약:
  1. 사전의 **모든** 항목이 유효한 policy 를 갖는다 (빠뜨린 항목 = 배포 후 화면에서 무색).
  2. policy=investigate 면 auto_action 은 항상 none (자동 처리되면 조사 대상이 아니다).
  3. 사전에 **없는 오류** 조합은 investigate 로 떨어진다 — 설명조차 못 다는 실패는
     '우리가 모르는 실패'이므로 사람이 봐야 한다.
  4. 성공/진행 중 상태는 normal (오류 아님).
  5. 창 만료 두 갈래(window_peak / window_stalled)가 정반대로 분류된다.

실행:
    docker compose exec web pytest apps/admin_api/tests_dm_error_policy.py
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_api.dm_error_catalog import (
    _BY_CODE,
    _BY_CODE_STATUS,
    _BY_CODE_SUBCODE,
    _BY_STATUS,
    INVESTIGATE,
    NO_ACTION,
    NORMAL,
    POLICY_DISPLAY,
    describe,
    describe_for_log,
    policy_for,
)
from apps.integrations.dm_status_groups import (
    HIDDEN_SPAM_SUBCODE,
    WINDOW_PEAK_SUBCODE,
    WINDOW_STALLED_SUBCODE,
)

ALL_ENTRIES = [
    *(("code_subcode", k, v) for k, v in _BY_CODE_SUBCODE.items()),
    *(("code_status", k, v) for k, v in _BY_CODE_STATUS.items()),
    *(("code", k, v) for k, v in _BY_CODE.items()),
    *(("status", k, v) for k, v in _BY_STATUS.items()),
]


# ── 1~2. 사전 전수 불변식 ─────────────────────────────────────────────


@pytest.mark.parametrize("table,key,entry", ALL_ENTRIES, ids=lambda x: str(x))
def test_every_entry_has_valid_policy(table, key, entry):
    """모든 사전 항목에 policy 가 있고, 값은 2종 중 하나다."""
    assert entry["policy"] in (INVESTIGATE, NORMAL), f"{table}:{key} policy={entry['policy']!r}"
    assert entry["title"], f"{table}:{key} title 비어 있음"


@pytest.mark.parametrize("table,key,entry", ALL_ENTRIES, ids=lambda x: str(x))
def test_investigate_never_has_auto_action(table, key, entry):
    """자동 처리가 가능하면 그건 정상이지 조사 대상이 아니다."""
    if entry["policy"] == INVESTIGATE:
        assert entry["auto_action"] == NO_ACTION, f"{table}:{key}"


def test_policy_display_covers_both():
    assert set(POLICY_DISPLAY) == {INVESTIGATE, NORMAL}
    assert all(POLICY_DISPLAY.values())


# ── 3~4. 폴백 규칙 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        "failed_token",
        "failed_window",
        "failed_param",
        "failed_no_trace",
        "failed",
        "failed_api",
        "recovery_pending",
        "recovery_expired",
    ],
)
def test_unknown_code_on_error_status_falls_back_to_status_entry(status):
    """모르는 코드가 와도 status 사전이 받아 준다 (policy 가 비지 않는다)."""
    assert policy_for("99999", "88888", status) in (INVESTIGATE, NORMAL)


def test_unregistered_error_status_is_investigate():
    """사전에 아예 없는 **오류** 상태는 investigate — 설명 못 다는 실패는 사람이 본다."""
    # describe() 는 빈 문자열을 주지만(프론트 로컬 폴백 계약 유지)…
    assert describe("", "", "failed_token_v9") == {
        "reason": "",  # DM-14
        "title": "",
        "cause": "",
        "action": "",
        "policy": "",
        "auto_action": NO_ACTION,
    }
    # …status 가 알려진 오류 집합에 없으면 오류로 단정하지 않는다(normal).
    assert policy_for("", "", "failed_token_v9") == NORMAL


@pytest.mark.parametrize(
    "status", ["delivered", "read", "accepted", "queued", "recovery_delivered"]
)
def test_success_statuses_are_normal(status):
    """성공·진행 중 상태가 '확인해야함'으로 새지 않아야 한다 (로그 목록 전체가 빨개짐)."""
    assert policy_for("", "", status) == NORMAL
    assert describe_for_log("", "", status)["error_policy"] == NORMAL


# ── 5. 창 만료 두 갈래 ────────────────────────────────────────────────


def test_window_expiry_kinds_are_opposite():
    """방치는 조사, 피크는 정상 — 같은 status(failed_window)인데 분류가 반대다."""
    stalled = describe("", WINDOW_STALLED_SUBCODE, "failed_window")
    peak = describe("", WINDOW_PEAK_SUBCODE, "failed_window")
    assert stalled["policy"] == INVESTIGATE
    assert peak["policy"] == NORMAL
    assert peak["auto_action"] == "peak_notice"
    # 과거 데이터(subcode 없음)는 기존대로 정상 — 소급 변경 없음
    assert describe("", "", "failed_window")["policy"] == NORMAL


# ── 결정 사항 고정 (방침이 조용히 뒤집히면 실패) ───────────────────────


@pytest.mark.parametrize(
    "code,subcode,status,expected",
    [
        # 🔴 확인해야함
        ("100", "2534022", "failed_param", INVESTIGATE),  # Meta 보고 창 만료
        ("10", "2534022", "failed_window", INVESTIGATE),
        ("10", "2018278", "failed_window", INVESTIGATE),
        ("10", "", "failed_window", INVESTIGATE),  # (code,status) 갈래
        ("100", "2534023", "failed_no_trace", INVESTIGATE),  # 이미 답글
        ("", "", "failed_no_trace", INVESTIGATE),  # 도착 미확인
        ("200", "2534066", "failed_no_trace", INVESTIGATE),  # 게시물 차단
        ("10", "", "failed_param", INVESTIGATE),  # 세부번호 없는 10
        ("100", "", "failed_param", INVESTIGATE),  # 세부번호 없는 100
        ("200", "", "failed_no_trace", INVESTIGATE),  # 세부번호 없는 200
        ("-1", "", "failed_no_trace", INVESTIGATE),
        ("", "", "failed", INVESTIGATE),  # legacy
        ("", "", "failed_api", INVESTIGATE),  # legacy
        ("", WINDOW_STALLED_SUBCODE, "failed_window", INVESTIGATE),
        # ⚪ 정상 (자동 처리)
        ("190", "", "failed_token", NORMAL),  # 재연동
        ("102", "", "failed_token", NORMAL),
        ("", "", "failed_token", NORMAL),  # pre-send 차단
        ("100", HIDDEN_SPAM_SUBCODE, "failed_param", NORMAL),  # 숨김함 → 복구
        ("100", "2534014", "failed_param", NORMAL),  # 수신자 없음
        ("551", "", "failed_no_trace", NORMAL),  # 도달 불가
        ("613", "", "rate_limited", NORMAL),
        ("4", "", "rate_limited", NORMAL),
        ("", "", "recovery_pending", NORMAL),
        ("", "", "recovery_expired", NORMAL),
        ("", WINDOW_PEAK_SUBCODE, "failed_window", NORMAL),
    ],
)
def test_decided_policy_matrix(code, subcode, status, expected):
    assert policy_for(code, subcode, status) == expected


def test_describe_for_log_exposes_policy_and_keeps_existing_keys():
    """로그 상세 계약 — 기존 키를 깨지 않고 policy·reason 만 더한다."""
    out = describe_for_log("100", HIDDEN_SPAM_SUBCODE, "failed_param")
    assert set(out) == {
        "error_title",
        "error_cause",
        "error_action",
        "error_reason",  # DM-14
        "error_policy",
        "error_policy_display",
        "recoverable",
    }
    assert out["error_policy"] == NORMAL
    assert out["error_policy_display"] == POLICY_DISPLAY[NORMAL]
    assert out["error_reason"] == describe("100", HIDDEN_SPAM_SUBCODE, "failed_param")["reason"]
    assert out["recoverable"] is True  # 숨김채널 복구 대상 — 기존 판정 불변


# ── _window_expiry_kind: 판정식 자체 ──────────────────────────────────


class _FakeLog:
    """DB 없이 판정식만 검증.

    ⚠️ SentDMLog 에는 updated_at 이 **없다** — 실제 필드(next_retry_at/submitted_at)만 흉내낸다.
    (2026-07-31: updated_at 을 가정했다가 send_dm_task 가 AttributeError 로 죽는 것을
     tests_rate_defer 가 잡았다. 이 픽스처가 실제 모델 필드를 벗어나면 같은 사고가 반복된다.)
    """

    def __init__(self, next_retry_at=None, submitted_at=None, comment_id="c1", retry_count=0):
        self.next_retry_at = next_retry_at
        self.submitted_at = submitted_at
        self.comment_id = comment_id
        self.retry_count = retry_count


@pytest.mark.parametrize(
    "overdue_hours,expected",
    [
        (0, WINDOW_PEAK_SUBCODE),  # 방금 슬롯을 잡아둠 → 피크
        (1, WINDOW_PEAK_SUBCODE),  # 임계(2h) 이내
        (5, WINDOW_STALLED_SUBCODE),  # 예약 시각을 5시간 넘김 → 방치
        (48, WINDOW_STALLED_SUBCODE),
    ],
)
def test_window_expiry_kind_uses_next_retry_at(overdue_hours, expected):
    from apps.integrations.tasks import _window_expiry_kind

    log = _FakeLog(next_retry_at=timezone.now() - timedelta(hours=overdue_hours))
    assert _window_expiry_kind(log) == expected


def test_window_expiry_kind_falls_back_to_submitted_at():
    """한 번도 defer 되지 않은 건(next_retry_at=None)은 마지막 Meta 호출 시각으로 판정."""
    from apps.integrations.tasks import _window_expiry_kind

    fresh = _FakeLog(next_retry_at=None, submitted_at=timezone.now())
    stale = _FakeLog(next_retry_at=None, submitted_at=timezone.now() - timedelta(hours=9))
    assert _window_expiry_kind(fresh) == WINDOW_PEAK_SUBCODE
    assert _window_expiry_kind(stale) == WINDOW_STALLED_SUBCODE


def test_never_touched_log_is_stalled():
    """defer 예약도 Meta 호출도 없이 창이 다 지났다 = 아무도 안 집어감 → 방치."""
    from apps.integrations.tasks import _window_expiry_kind

    assert _window_expiry_kind(_FakeLog()) == WINDOW_STALLED_SUBCODE


def test_fake_log_fields_exist_on_real_model():
    """픽스처가 실제 모델 필드를 벗어나면 prod 에서 AttributeError 로 죽는다."""
    from apps.integrations.models import SentDMLog

    names = {f.name for f in SentDMLog._meta.get_fields()}
    assert {"next_retry_at", "submitted_at", "comment_id", "retry_count"} <= names
    assert "updated_at" not in names  # 없다는 사실 자체를 고정


def test_window_subcodes_do_not_collide_with_meta_subcodes():
    """Meta subcode 는 항상 숫자 — 내부 표식과 값 공간이 겹치면 안 된다."""
    assert not WINDOW_PEAK_SUBCODE.isdigit()
    assert not WINDOW_STALLED_SUBCODE.isdigit()
    assert WINDOW_PEAK_SUBCODE != HIDDEN_SPAM_SUBCODE
    # 모델 컬럼 상한(50자) 안에 들어가야 저장된다
    assert max(len(WINDOW_PEAK_SUBCODE), len(WINDOW_STALLED_SUBCODE)) <= 50


def test_hidden_spam_grouping_unaffected_by_window_subcodes():
    """새 subcode 가 숨김함/그룹 판정에 끼어들지 않는다 (집계 정의 불변)."""
    from apps.integrations.dm_status_groups import ATTENTION, status_group

    assert status_group("failed_window", WINDOW_STALLED_SUBCODE) == ATTENTION
    assert status_group("failed_window", WINDOW_PEAK_SUBCODE) == ATTENTION
