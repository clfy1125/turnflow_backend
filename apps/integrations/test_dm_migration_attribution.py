"""DM 이전 — **귀속 정리** 계약 테스트 (이 DM 이 정말 이 게시물 것인가).

게시물을 하나씩 조사하는 구조라, 한 사람이 여러 게시물에 댓글을 달면 그 사람이 받은 DM 이
**모든 게시물의 근거로 중복 계산**된다. 실측(@highestlevel33): 지지 1~2명짜리가 92건 생겼고,
연구에서 그런 얕은 지지는 89%가 오답이었다.

수집이 끝난 뒤 이미 받아둔 정보만으로 거른다(추가 API 호출 0).
"""

from __future__ import annotations

from apps.integrations.dm_migration import attribute


def _slot(text, users, *, url="", hits=None):
    return {
        "text": text,
        "url": url,
        "label": "",
        "users": users,
        "hits": hits if hits is not None else len(users),
        "ratio": 0.0,
        "score": 0.0,
        "drops": [],
        "samples": [],
    }


def _rec(mid, *, probed=10, offer=None, gate=None, signal=True, content=0.7):
    return {
        "media_id": mid,
        "probed": probed,
        "offer": offer,
        "gate": gate,
        "signal": signal,
        "content_score": content,
        "grade": "needs_review",
        "score": 0.0,
        "confirm_required": True,
    }


# ══════════════ 1. 시간 짝짓기 ══════════════


class TestByTime:
    def test_same_dm_goes_to_the_closest_post(self):
        """한 사람의 같은 DM 을 두 게시물이 주장하면 **댓글과 가까운 쪽**만 남는다."""
        near = _rec("m-near", offer=_slot("자료 드려요", [{"u": "u1", "m": "dm1", "g": 30}]))
        far = _rec("m-far", offer=_slot("자료 드려요", [{"u": "u1", "m": "dm1", "g": 4000}]))
        moved = attribute.by_time([near, far])
        assert moved == 1
        assert near["offer"]["hits"] == 1
        assert far["offer"]["hits"] == 0

    def test_threshold_would_not_have_separated_them(self):
        """문턱값 방식이 왜 실패했는지 고정 — 둘 다 '몇 초 이내' 라도 상대비교는 갈린다."""
        a = _rec("m-a", offer=_slot("자료", [{"u": "u1", "m": "dm1", "g": 20}]))
        b = _rec("m-b", offer=_slot("자료", [{"u": "u1", "m": "dm1", "g": 45}]))
        attribute.by_time([a, b])
        assert (a["offer"]["hits"], b["offer"]["hits"]) == (1, 0)

    def test_different_messages_are_not_in_conflict(self):
        """팔로우게이트는 한 사람에게 2통을 보낸다 — 서로 다른 메시지는 경쟁시키지 않는다."""
        rec = _rec(
            "m1",
            offer=_slot("자료 링크", [{"u": "u1", "m": "dm1", "g": 10}], url="https://x"),
            gate=_slot("팔로우 확인", [{"u": "u1", "m": "dm2", "g": 5}]),
        )
        attribute.by_time([rec])
        assert rec["offer"]["hits"] == 1 and rec["gate"]["hits"] == 1

    def test_other_users_survive(self):
        """겹치는 사람만 빠지고 나머지 지지는 그대로다."""
        a = _rec(
            "m-a",
            offer=_slot(
                "자료",
                [{"u": "u1", "m": "dm1", "g": 5000}, {"u": "u2", "m": "dm2", "g": 10}],
            ),
        )
        b = _rec("m-b", offer=_slot("자료", [{"u": "u1", "m": "dm1", "g": 20}]))
        attribute.by_time([a, b])
        assert a["offer"]["hits"] == 1  # u2 는 살아남음
        assert b["offer"]["hits"] == 1


# ══════════════ 2. 문구 경쟁 ══════════════


class TestByTemplate:
    def test_weak_claim_loses_to_dominant_owner(self):
        strong = _rec("m-owner", offer=_slot("AI 자료 보내드려요", [], url="https://x", hits=21))
        weak = _rec("m-leak", offer=_slot("AI 자료 보내드려요", [], url="https://x", hits=1))
        demoted = attribute.by_template([strong, weak])
        assert demoted == 1
        assert strong["offer"] is not None
        assert weak["offer"] is None
        assert weak["offer_demoted"]["owner_hits"] == 21

    def test_evenly_strong_posts_are_left_alone(self):
        """같은 문구가 여러 게시물에서 고르게 강하면 **상시 캠페인**이다.

        예전에 배타 할당을 넣었다가 @mini_ai_ 42개 중 35개를 죽인 실패를 고정한다.
        """
        recs = [
            _rec(f"m{i}", offer=_slot("멤버십 초대", [], url="https://x", hits=h))
            for i, h in enumerate((10, 10, 9))
        ]
        assert attribute.by_template(recs) == 0
        assert all(r["offer"] is not None for r in recs)

    def test_gate_messages_are_never_demoted(self):
        """게이트(팔로우 확인)는 전 게시물 공유가 정상 — 경쟁시키면 대량 오삭제."""
        recs = [
            _rec("m1", gate=_slot("팔로우 확인해주세요", [], hits=30)),
            _rec("m2", gate=_slot("팔로우 확인해주세요", [], hits=1)),
        ]
        assert attribute.by_template(recs) == 0
        assert all(r["gate"] is not None for r in recs)

    def test_owner_must_be_substantial(self):
        """주인 쪽도 표본이 작으면 판단하지 않는다(2:1 로 남의 것을 죽이면 안 된다)."""
        recs = [
            _rec("m1", offer=_slot("자료", [], url="https://x", hits=2)),
            _rec("m2", offer=_slot("자료", [], url="https://x", hits=1)),
        ]
        assert attribute.by_template(recs) == 0


# ══════════════ 3. 정리 후 재채점 ══════════════


class TestResolve:
    def test_regrades_after_support_changes(self):
        """지지가 바뀌면 등급도 바뀌어야 한다 — 안 그러면 화면 숫자와 등급이 어긋난다."""
        a = _rec(
            "m-a",
            probed=12,
            offer=_slot("자료", [{"u": f"u{i}", "m": f"d{i}", "g": 10} for i in range(12)]),
        )
        stats = attribute.resolve([a])
        assert a["offer"]["hits"] == 12
        assert a["grade"] == "auto_draft", (a["grade"], a["score"])
        assert stats["moved"] == 0

    def test_emptied_slot_becomes_none_and_downgrades(self):
        loser = _rec("m-lose", offer=_slot("자료", [{"u": "u1", "m": "dm1", "g": 9999}]))
        winner = _rec("m-win", offer=_slot("자료", [{"u": "u1", "m": "dm1", "g": 5}]))
        attribute.resolve([loser, winner])
        assert loser["offer"] is None
        assert loser["grade"] == "excluded" or loser["grade"] == "needs_review"
        assert winner["offer"]["hits"] == 1

    def test_evidence_is_dropped_after_resolution(self):
        """근거 목록(사용자·메시지 id)은 정리에만 쓰고 남기지 않는다."""
        a = _rec("m1", offer=_slot("자료", [{"u": "u1", "m": "dm1", "g": 5}]))
        attribute.resolve([a])
        assert "users" not in (a["offer"] or {})

    def test_no_crash_on_missing_fields(self):
        """옛 기록(근거 없음)이 섞여도 죽지 않는다 — 재개·재분석 경로에서 실제로 섞인다."""
        old = {"media_id": "m1", "probed": 5, "offer": {"text": "x", "hits": 3}, "gate": None}
        stats = attribute.resolve([old, _rec("m2")])
        assert stats["moved"] == 0
        assert old["offer"] is not None
