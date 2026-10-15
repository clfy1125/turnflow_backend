"""자동 DM 프로 전용 4기능 게이트 — 판정 테스트.

⭐ **가장 중요한 단언은 "막지 않는다" 쪽**이다. 사장님 결정이 "이미 쓰던 것은 유지" 라서,
   과잉 차단이 곧 기능 회귀다. 특히 재개(값 무변경)와 "같은 값 재전송 수정" 두 가지.
"""

from __future__ import annotations

import pytest

from apps.core.exceptions import PlanRequiredError
from apps.integrations import dm_plan_gate


class _Stub:
    """모델 대신 쓰는 값 묶음 — ``features_in`` 이 dict/객체를 모두 받는지도 같이 본다."""

    def __init__(self, **kw):
        self.trigger_type = kw.get("trigger_type", "specific_media")
        self.follow_gate_enabled = kw.get("follow_gate_enabled", False)
        self.gate_verify_follow = kw.get("gate_verify_follow", False)


class _Owner:
    def __init__(self, pro: bool):
        self.is_superuser = False
        self.is_staff = False
        self._pro = pro


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(dm_plan_gate, "plan_allows", lambda u: True)
    return _Owner(True)


@pytest.fixture
def free(monkeypatch):
    monkeypatch.setattr(dm_plan_gate, "plan_allows", lambda u: False)
    return _Owner(False)


# ── features_in: 무엇이 '프로 기능'인가 ──────────────────────────────────────


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({"trigger_type": "any_media"}, {"dm_trigger_all_posts"}),
        ({"trigger_type": "next_media"}, {"dm_trigger_next_media"}),
        ({"trigger_type": "story_reply"}, {"dm_trigger_story_reply"}),
        ({"trigger_type": "specific_media"}, set()),
        # ⭐ 게이트는 두 값이 **모두** true 일 때만 프로 기능이다.
        ({"follow_gate_enabled": True, "gate_verify_follow": True}, {"dm_follow_gate"}),
        # 버튼 링크 모드 — 게이트를 켰어도 검증을 안 하면 프로 기능이 아니다.
        ({"follow_gate_enabled": True, "gate_verify_follow": False}, set()),
        ({"follow_gate_enabled": False, "gate_verify_follow": True}, set()),
    ],
)
def test_features_in(kw, expected):
    assert dm_plan_gate.features_in(_Stub(**kw)) == expected
    assert dm_plan_gate.features_in(kw) == expected  # dict 로도 같은 결과


# ── 막는 경우 ────────────────────────────────────────────────────────────────


def test_free_cannot_create_pro_feature(free):
    with pytest.raises(PlanRequiredError) as e:
        dm_plan_gate.guard(owner=free, requested={"trigger_type": "any_media"})
    assert e.value.feature == "dm_trigger_all_posts"
    assert e.value.required_plan == "pro"


def test_free_cannot_turn_on_in_update(free):
    """꺼져 있던 것을 켜는 수정은 거절."""
    current = _Stub(follow_gate_enabled=False, gate_verify_follow=False)
    with pytest.raises(PlanRequiredError):
        dm_plan_gate.guard(
            owner=free,
            requested={"follow_gate_enabled": True, "gate_verify_follow": True},
            current=current,
        )


def test_pro_passes(pro):
    dm_plan_gate.guard(owner=pro, requested={"trigger_type": "any_media"})  # 예외 없음


# ── ⭐ 막으면 안 되는 경우 (여기가 회귀 방어의 핵심) ─────────────────────────


def test_existing_feature_survives_update(free):
    """이미 켜져 있던 기능은 비프로여도 그대로 저장된다 — 「유지」 결정."""
    current = _Stub(trigger_type="any_media")
    dm_plan_gate.guard(owner=free, requested={"trigger_type": "any_media"}, current=current)


def test_resume_is_not_blocked(free):
    """재개 = 값 무변경. 차집합이 비므로 통과해야 한다.

    막히면 free 로 떨어진 기존 사용자가 **한 번 정지되는 순간 영구 봉인**된다.
    정지는 본인 의사와 무관하게도 일어난다(연령제한 자동정지·한도 소진·368 복구).
    """
    current = _Stub(follow_gate_enabled=True, gate_verify_follow=True)
    dm_plan_gate.guard(
        owner=free,
        requested={"follow_gate_enabled": True, "gate_verify_follow": True},
        current=current,
    )


def test_partial_update_without_feature_keys_passes(free):
    """문구만 고치는 PATCH 는 통과해야 한다.

    요청에 없는 키를 '꺼짐' 으로 읽으면 반대로 **켜짐→꺼짐** 으로 보여 통과하지만,
    `trigger_type` 처럼 값이 있는 키는 누락 시 비교가 깨진다. 저장값으로 메워서 본다.
    """
    current = _Stub(trigger_type="any_media", follow_gate_enabled=True, gate_verify_follow=True)
    dm_plan_gate.guard(owner=free, requested={"dm_message": "새 문구"}, current=current)


def test_turning_feature_off_passes(free):
    """끄는 방향은 언제나 통과."""
    current = _Stub(trigger_type="any_media")
    dm_plan_gate.guard(owner=free, requested={"trigger_type": "specific_media"}, current=current)


# ── 플랜 판정 ────────────────────────────────────────────────────────────────


def test_staff_always_allowed():
    u = _Owner(False)
    u.is_staff = True
    assert dm_plan_gate.plan_allows(u) is True


def test_none_owner_denied():
    assert dm_plan_gate.plan_allows(None) is False
