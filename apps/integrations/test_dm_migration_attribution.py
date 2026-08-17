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
    def test_weak_claim_loses_support_but_keeps_the_text(self):
        """진 쪽도 **문구는 남긴다** — 지우면 사용자가 처음부터 써야 한다.

        인플루언서가 여러 게시물에 같은 DM 을 돌려쓰는 경우가 많아(실측 34종이 여러
        게시물에 걸침) 이 문구가 이 게시물 문구일 가능성도 있다. 지지 근거만 무효화하고
        표시를 남겨, 사용자는 "다른 게시물에서 더 많이 쓰인 문구" 라는 안내와 함께 받는다.
        """
        strong = _rec("m-owner", offer=_slot("AI 자료 보내드려요", [], url="https://x", hits=21))
        weak = _rec("m-leak", offer=_slot("AI 자료 보내드려요", [], url="https://x", hits=1))
        demoted = attribute.by_template([strong, weak])
        assert demoted == 1
        assert strong["offer"]["hits"] == 21
        assert weak["offer"]["text"] == "AI 자료 보내드려요"  # 문구는 남는다
        assert weak["offer"]["hits"] == 0  # 지지 근거만 무효화
        assert weak["offer"]["shared"] is True
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


# ══════════════ 4. AI 내용 대조 반영 ══════════════
#
# 지지비율만으로는 도달률 낮은 게시물이 억울하게 잘리고, 남의 게시물에서 흘러든 DM 이
# 살아남는다. 낱말 겹침으로도 재봤지만(진짜 58% vs 가짜 6%) 같은 주제를 계속 올리는
# 계정에서는 "인스타·수익화" 가 양쪽에 다 나와 안 갈렸다 → 의미 판단은 AI 에 맡긴다.


class TestApplyVerdicts:
    def test_match_rescues_a_weak_candidate(self):
        rec = _rec("m1", offer=_slot("자료", [], hits=1))
        rec["grade"] = "excluded"
        stats = attribute.apply_verdicts(
            [rec], {"m1": {"match": True, "confidence": 0.9, "reason": "같은 자료"}}
        )
        assert rec["grade"] == "needs_review"
        assert rec["ai_match"]["reason"] == "같은 자료"
        assert stats == {"checked": 1, "kept": 1, "doubted": 0}

    def test_mismatch_marks_doubt_but_never_deletes(self):
        """AI 에 거부권을 주면 안 된다 — 실측에서 확실한 캠페인의 20%를 '아니다' 라고 했다.

        캡션 "조회수 터지는 인스타 비밀 점수표" ↔ DM "노출 높이는 5가지 필수 세팅" 처럼
        인플루언서는 예고한 말과 실제 문구를 그대로 맞추지 않는다. 표시만 하고 사람이 판단.
        """
        rec = _rec("m1", offer=_slot("특강 신청", [], hits=2))
        before = rec["grade"]
        stats = attribute.apply_verdicts(
            [rec], {"m1": {"match": False, "confidence": 0.8, "reason": "주제 다름"}}
        )
        assert rec["grade"] == before, "AI 가 후보를 지웠다"
        assert rec["ai_doubt"] is True
        assert rec["confirm_required"] is True
        assert stats["doubted"] == 1

    def test_empty_dm_text_is_not_sent_to_ai(self):
        """빈 문구를 물어보면 '안 맞는다' 는 답이 와서 멀쩡한 건이 의심 처리된다."""
        from apps.integrations.dm_migration.pipeline import _needs_verify

        assert _needs_verify({"offer": {"text": "", "hits": 1}}) is False
        assert _needs_verify({"offer": {"text": "   ", "hits": 1}}) is False
        assert _needs_verify({"offer": {"text": "짧음", "hits": 1}}) is False
        assert _needs_verify({"offer": {"text": "요청하신 자료 보내드려요", "hits": 1}}) is True

    def test_no_verdict_leaves_it_alone(self):
        """판정을 못 받으면 그대로 둔다 — LLM 실패가 후보를 지우면 안 된다(fail-open)."""
        rec = _rec("m1", offer=_slot("자료", [], hits=2))
        before = rec["grade"]
        stats = attribute.apply_verdicts([rec], {})
        assert rec["grade"] == before
        assert "ai_match" not in rec
        assert stats == {"checked": 0, "kept": 0, "doubted": 0}

    def test_strong_support_is_not_sent_to_ai(self):
        """지지 3명+ 는 이미 정밀도가 충분하다 — 비용을 거기 쓰지 않는다."""
        from apps.integrations.dm_migration.pipeline import _needs_verify

        real = "요청하신 자료 보내드려요 아래 링크 확인해주세요"
        assert _needs_verify({"offer": {"text": real, "hits": 1}}) is True
        assert _needs_verify({"offer": {"text": real, "hits": 2}}) is True
        assert _needs_verify({"offer": {"text": real, "hits": 3}}) is False
        assert _needs_verify({"offer": None, "gate": None}) is False
        assert _needs_verify({"offer": {"text": "", "hits": 1}}) is False


# ══════════════ 5. 댓글 → DM 간격 (자동 발송의 지문) ══════════════
#
# 실측(@highestlevel33, 근거 2,916건):
#   확실한 캠페인(지지 3명+) 중앙값   7초 · 1분 내 79%
#   애매(지지 1~2명)        중앙값 190초 · 1분 내  9%
#   캠페인 아님             중앙값 3.7일 · 1분 내  0%
#   간격별 '지지 3명+' 비율: 0~10초 99% · 10~60초 99% · 1~10분 85% · 1일+ 52%
# 낱말·말투 패턴과 달리 계정 성격을 안 타서 재튜닝이 필요 없다.


def _slot_g(text, gaps, *, url="", hits=None):
    users = [{"u": f"u{i}", "m": f"d{i}", "g": g} for i, g in enumerate(gaps)]
    s = _slot(text, users, url=url, hits=hits)
    s["gap_median"] = sorted(gaps)[len(gaps) // 2] if gaps else None
    return s


class TestTimingRule:
    def test_dm_before_the_comment_is_impossible(self):
        """DM 이 댓글보다 먼저 갔으면 그 댓글의 응답일 수 없다(실측 195건)."""
        rec = _rec("m1", offer=_slot_g("자료", [-5000, 8]))
        removed = attribute.drop_impossible([rec])
        assert removed == 1
        assert rec["offer"]["hits"] == 1

    def test_clock_skew_is_tolerated(self):
        """시계 오차 정도의 음수는 살린다 — 진짜 자동 발송을 잃으면 안 된다."""
        rec = _rec("m1", offer=_slot_g("자료", [-30]))
        assert attribute.drop_impossible([rec]) == 0
        assert rec["offer"]["hits"] == 1

    def test_a_reply_a_week_later_is_not_automation(self):
        rec = _rec("m1", offer=_slot_g("고마워요", [700000]))
        assert attribute.drop_impossible([rec]) == 1
        assert rec["offer"]["hits"] == 0

    def test_seconds_gap_rescues_a_single_supporter(self):
        """7초 만에 온 DM 은 사람이 못 쓴다 — 1명이 받았어도 자동 발송이다."""
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=12, content_score=0.1)
        r.offer = {"text": "자료", "url": "", "hits": 1, "score": 0.05, "gap_median": 7}
        assert r.grade == "needs_review"

    def test_slow_dm_is_dropped_even_with_link_and_score(self):
        """간격이 하루를 넘으면 링크·점수가 좋아도 자동 발송이 아니다."""
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=12, content_score=0.9, is_campaign_signal=True)
        r.offer = {
            "text": "자료",
            "url": "https://x",
            "hits": 1,
            "score": 0.05,
            "gap_median": 200000,
        }
        assert r.grade == "excluded"

    def test_middle_zone_falls_back_to_review_rules(self):
        """1분~1일 구간은 콘텐츠 점수·링크로 가른다(사장님 검수 규칙)."""
        from apps.integrations.dm_migration.recover import PostRecovery

        mid = PostRecovery(media_id="m1", probed=12, content_score=0.9, is_campaign_signal=True)
        mid.offer = {
            "text": "자료",
            "url": "https://x",
            "hits": 1,
            "score": 0.05,
            "gap_median": 600,
        }
        assert mid.grade == "needs_review"

        weak = PostRecovery(media_id="m2", probed=12, content_score=0.4, is_campaign_signal=True)
        weak.offer = {
            "text": "자료",
            "url": "https://x",
            "hits": 1,
            "score": 0.05,
            "gap_median": 600,
        }
        assert weak.grade == "excluded"

    def test_impossible_runs_before_competition(self):
        """순서가 뒤집히면 시간상 말이 안 되는 근거가 경쟁에서 이겨버린다."""
        good = _rec("m-good", offer=_slot_g("자료", [5], url="https://x"))
        stale = _rec("m-stale", offer=_slot_g("자료", [500000], url="https://x"))
        stats = attribute.resolve([stale, good])
        assert stats["impossible"] == 1
        assert stale["offer"] is None
        assert good["offer"]["hits"] == 1
