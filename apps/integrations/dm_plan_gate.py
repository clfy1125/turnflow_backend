"""자동 DM **프로 전용 4기능**의 플랜 게이트 — 판정 단일 소스.

## 무엇을 막는가 (2026-10-15 제품 결정)

| 기능 | 서버 값 |
|---|---|
| 모든 게시물 | ``trigger_type = any_media`` |
| 다음 새 게시물(1회) | ``trigger_type = next_media`` |
| 스토리 답장 | ``trigger_type = story_reply`` |
| 팔로우 확인 후 보상 | ``follow_gate_enabled`` **그리고** ``gate_verify_follow`` 둘 다 true |

마지막 항목 주의 — 버튼 링크 모드(``gate_verify_follow=False``)는 **해당 없다.**
게이트를 켰다는 사실만으로 막으면 링크 버튼만 쓰던 사람이 같이 걸린다.

## ⭐ "이미 쓰고 있던 것은 유지한다"

사장님 결정이다. 그래서 막는 것은 **새로 켜는 것뿐**이다.

| 동작 | 게이트 |
|---|---|
| 생성 · 복제 | ✅ 막는다 (새 자동화다) |
| 수정 | ✅ **꺼짐 → 켜짐일 때만** 막는다. 같은 값 재전송은 통과 |
| **재개(resume)** | ❌ **막지 않는다** |

⚠️ **재개를 막으면 안 되는 이유** — 재개는 값이 하나도 바뀌지 않는다. 막으면 비프로
사용자의 기존 자동화가 **한 번 정지되는 순간 영구 봉인**된다. 그리고 정지는 본인이
누르는 것만이 아니다: 게시물 연령제한 자동정지 · DM 월 한도 소진 · 계정 단위 발송
차단(368) 복구가 모두 status 를 내린다. "유지한다"고 해놓고 못 쓰게 만드는 셈이다.
프론트 요청에는 재개가 들어 있었으나 이 이유로 뺐다.

## 플랜 판정

``workspace.owner`` 의 구독 기준이다(이 저장소의 기존 게이팅과 동일 —
[[plan-gating-owner-scope]]). 멤버가 만들어도 워크스페이스 주인의 플랜을 본다.

## 실측 (2026-10-15, prod 캠페인 2,629건)

플랜 분포는 ``pro 2,352 / free 276 / admin 1`` 로 **베이직은 0건**이다. 게이트에 걸릴
기존 캠페인은 전부 **free**(체험이 끝나 무료로 떨어진 사람)이고 46건 — 그중 active 17건.
``follow_gate`` 41 · ``all_posts`` 4 · ``story_reply`` 1 · ``next_media`` 0.
"베이직 사용자 구제" 분기를 따로 만들 필요가 없다는 뜻이다.
"""

from __future__ import annotations

from apps.core.exceptions import PlanRequiredError

# 기능 키 → 사람이 읽는 이름 (프론트 요금제 창 문구·대시보드 라벨과 같은 어휘)
FEATURE_LABELS = {
    "dm_trigger_all_posts": "모든 게시물",
    "dm_trigger_next_media": "다음 새 게시물",
    "dm_trigger_story_reply": "스토리 답장",
    "dm_follow_gate": "팔로우 확인 후 보상",
}

#: 이 기능들을 쓸 수 있는 플랜. ``admin`` 은 운영용이라 항상 통과시킨다.
ALLOWED_PLANS = {"pro", "pro+", "admin"}


def _trigger_feature(trigger_type: str) -> str | None:
    return {
        "any_media": "dm_trigger_all_posts",
        "next_media": "dm_trigger_next_media",
        "story_reply": "dm_trigger_story_reply",
    }.get(trigger_type or "")


def features_in(data) -> set[str]:
    """값 묶음(dict 또는 모델 인스턴스)에서 **켜져 있는** 프로 전용 기능을 뽑는다.

    dict 와 모델을 모두 받는 이유 — 수정 판정이 "요청값 vs 저장값" 비교라서
    양쪽을 같은 함수로 읽어야 조건이 어긋나지 않는다.
    """

    def get(key, default=None):
        if isinstance(data, dict):
            return data.get(key, default)
        return getattr(data, key, default)

    found: set[str] = set()
    if feat := _trigger_feature(get("trigger_type")):
        found.add(feat)
    # ⚠️ 두 값이 **모두** true 일 때만 프로 기능이다(버튼 링크 모드는 제외).
    if bool(get("follow_gate_enabled")) and bool(get("gate_verify_follow")):
        found.add("dm_follow_gate")
    return found


def plan_allows(user) -> bool:
    """이 사용자(워크스페이스 주인 기준)가 프로 전용 기능을 쓸 수 있는가."""
    if user is None:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    from apps.billing.subscription_utils import get_effective_plan

    try:
        plan = get_effective_plan(user)
    except Exception:  # noqa: BLE001 — 판정 실패가 생성 자체를 깨뜨리면 안 된다
        return False
    name = getattr(plan, "name", None) or (str(plan) if plan else "")
    return name in ALLOWED_PLANS


def guard(*, owner, requested, current=None) -> None:
    """프로 전용 기능을 **새로 켜는** 요청이면 ``PlanRequiredError`` 를 던진다.

    Args:
        owner: 워크스페이스 주인(플랜의 주체). 요청자가 아니다.
        requested: 이번 요청의 값 (serializer ``validated_data`` 또는 dict).
        current: 수정 전 저장돼 있던 값(모델 인스턴스). **생성·복제는 None** —
            그때는 켜져 있는 것 전부가 "새로 켜는 것"이다.

    ⚠️ 부분 수정(PATCH)에서 해당 키가 아예 안 왔으면 **기존 값을 유지하는 것**이므로
       새로 켜는 것이 아니다. 그래서 요청에 없는 키는 저장값으로 메워서 비교한다 —
       안 그러면 "문구만 고치는 PATCH" 가 거절된다.
    """
    if plan_allows(owner):
        return

    merged = dict(requested) if isinstance(requested, dict) else requested
    if current is not None and isinstance(merged, dict):
        for key in ("trigger_type", "follow_gate_enabled", "gate_verify_follow"):
            if key not in merged:
                merged[key] = getattr(current, key, None)

    newly_on = features_in(merged)
    if current is not None:
        newly_on -= features_in(current)  # 이미 켜져 있던 것은 통과시킨다

    if newly_on:
        # 걸린 것이 여럿이면 라벨 순서가 흔들리지 않도록 정렬해서 첫 개를 고른다.
        feature = sorted(newly_on)[0]
        raise PlanRequiredError(feature=feature, required_plan="pro")
