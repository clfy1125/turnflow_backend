"""DM 이전 — **귀속 정리** 계약 테스트 (이 DM 이 정말 이 게시물 것인가).

게시물을 하나씩 조사하는 구조라, 한 사람이 여러 게시물에 댓글을 달면 그 사람이 받은 DM 이
**모든 게시물의 근거로 중복 계산**된다. 실측(@highestlevel33): 지지 1~2명짜리가 92건 생겼고,
연구에서 그런 얕은 지지는 89%가 오답이었다.

수집이 끝난 뒤 이미 받아둔 정보만으로 거른다(추가 API 호출 0).
"""

from __future__ import annotations

import pytest

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
        # 링크를 준다 — 2026-08-18 부터 **옮길 링크가 없으면 자동채택하지 않는다.**
        # 이 테스트가 보려는 건 '지지가 바뀌면 등급도 바뀐다' 이므로 링크 규칙과 분리한다.
        a = _rec(
            "m-a",
            probed=12,
            offer=_slot(
                "자료",
                [{"u": f"u{i}", "m": f"d{i}", "g": 10} for i in range(12)],
                url="https://ex.co/a",
            ),
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
        assert stats == {"checked": 1, "kept": 1, "doubted": 0, "blocked": 0}

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
        assert stats == {"checked": 0, "kept": 0, "doubted": 0, "blocked": 0}

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
    # recover._pack 과 같은 정의로 파생 수치를 만든다 — 여기서 갈리면 테스트가 현실을
    # 검증하지 않는다.
    s["gap_median"] = sorted(gaps)[len(gaps) // 2] if gaps else None
    s["auto_hits"] = sum(1 for g in gaps if 0 <= g <= 60)
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
        """7초 만에 온 DM 은 사람이 못 쓴다 — 1명이 받았어도 자동 발송이다.

        단 **글·댓글이 캠페인이라고 말할 때만**이다(아래 테스트 참조).
        """
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=12, content_score=0.7)
        r.offer = {"text": "자료", "url": "", "hits": 1, "score": 0.05, "gap_median": 7}
        assert r.grade == "needs_review"

    def test_human_label_beats_the_gap_signal(self):
        """글 점수가 사장님 라벨(0.55) 이하면 7초짜리 1통도 제외다.

        순서가 뒤집혀 있었다 — 간격 검사가 먼저라서 "60초 안에 왔으면 인정" 이 라벨을
        덮었다. 실측(@highestlevel33, 2026-08-18): 검수 17건 중 11건이 글에 캠페인 기미가
        거의 없는데(내용 0.00~0.40) 이 경로로 살아남았다. 간격은 **추론**이고 0.55 컷은
        사장님이 59건을 눈으로 보고 매긴 **사실**이다.
        """
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=12, content_score=0.1)
        r.offer = {"text": "자료", "url": "", "hits": 1, "score": 0.05, "gap_median": 7}
        assert r.grade == "excluded"
        assert r.reject_reason == "content_says_no"

    def test_strong_reach_wins_even_when_the_post_is_quiet(self):
        """글이 조용해도 도달이 넓으면 확정이다 — 라벨 컷이 여기까지 내려오면 안 된다.

        실측 `46/46 · 내용 0.245`. 46명 전원이 같은 문구를 받은 것을 글 점수로 죽이면
        회수가 무너진다(CLAUDE.md §1).
        """
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=46, content_score=0.245)
        r.offer = {
            "text": "자료",
            "url": "https://x",
            "hits": 46,
            "ratio": 1.0,
            "score": 0.9,
            "gap_median": 8,
            "auto_hits": 46,
        }
        assert r.grade == "auto_draft"
        assert r.reject_reason == ""

    def test_slow_dm_with_thin_support_is_not_auto(self):
        """간격이 하루를 넘고 **받은 사람이 1명**이면 자동채택하지 않는다.

        ⚠️ 2026-08-18 부터 '제외' 가 아니라 **검수**로 간다. 1~7일 구간은 인스타 Private
        Reply 창 안이고, 도구 지연·수동 발송이 실제로 있어 지워버릴 근거가 없다
        (사장님 @reels_drgn 검수 확인). 대신 자동채택은 인원으로 막는다 — 2.3일이면
        9명이 받았어야 하는데 1명뿐이다.
        """
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=12, content_score=0.9, is_campaign_signal=True)
        r.offer = {
            "text": "자료",
            "url": "https://x",
            "hits": 1,
            "score": 0.05,
            "gap_median": 200000,
        }
        from apps.integrations.dm_migration import collect as C

        assert C.required_hits(200000, 3) == 9  # 2.3일 → 9명 필요
        assert r.grade != "auto_draft"
        assert r.grade == "needs_review"  # 지우지 않고 사람에게 묻는다

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
        # 8일 — 인스타 Private Reply 창(7일) 밖이라 이 댓글의 응답일 수 없다.
        # (5.8일짜리는 2026-08-18 부터 살아남는다 — 창을 하루에서 7일로 넓혔다.)
        stale = _rec("m-stale", offer=_slot_g("자료", [8 * 86400], url="https://x"))
        stats = attribute.resolve([stale, good])
        assert stats["impossible"] == 1
        assert stale["offer"] is None
        assert good["offer"]["hits"] == 1


# ══════════════ 6. 자동 발송 지문 — 비율이 아니라 속도로 자동채택 ══════════════
#
# 왜 필요한가 (실측 @highestlevel33, 2026-08-17 · 251건 시점):
#   조회 인원을 10명 → 50명으로 키우자 **비율이 저절로 떨어졌다.** 깊게 파면 캠페인
#   시작 전 댓글·키워드 불일치 댓글·DM 차단 계정이 분모에 섞이기 때문이다.
#   그 결과 검수필요 77건 중 **65건이 "지지 3명+ 인데 비율<60%"** 였고, 그 안에
#   `29/49(0.59) · 댓글 후 34초`, `28/48(0.58) · 32초` 처럼 자동 발송이 명백한 건이
#   줄줄이 있었다. 비율 컷은 10명 조회 시절 기준이라 개선이 스스로 판정을 망가뜨렸다.
# → 분모가 없는 잣대(auto_hits = 60초 내 수신 인원)로 두 번째 자동채택 문을 만든다.


class TestFastSendFingerprint:
    def _rec_obj(self, *, probed, gaps, url="https://x", text="자료"):
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=probed, content_score=0.75, is_campaign_signal=True)
        r.offer = _slot_g(text, gaps, url=url)
        return r

    def test_low_ratio_but_many_fast_sends_is_auto(self):
        """29/49 = 0.59 인데 전원이 30초 안에 받았다 → 사람이 못 하는 일이다."""
        r = self._rec_obj(probed=49, gaps=[30] * 29)
        assert r.offer["ratio"] == 0.0  # 비율 컷(0.60)에는 못 미치게 둔 상태
        assert r.grade == "auto_draft"
        assert r.confirm_required is False

    def test_ratio_gate_alone_would_have_held_it(self):
        """비율만 보던 규칙이라면 이 건은 검수필요였다 — 회귀 감시용."""
        from apps.integrations.dm_migration import recover

        r = self._rec_obj(probed=49, gaps=[30] * 29)
        hits, ratio = r.offer["hits"], r.offer["hits"] / r.probed
        assert ratio < recover.GRADE_AUTO_RATIO and hits >= recover.MIN_SUPPORT_HITS
        assert r.offer["auto_hits"] >= recover.AUTO_FAST_MIN_HITS

    def test_four_fast_sends_is_not_enough(self):
        """지문 컷 아래(4명)는 이 문으로 승격하지 않는다 — 실측에서 gap 52초/4명은 약했다.

        다른 문이 끼어들지 않게 격리한다 — 중앙값을 1시간 밖에 둬 페이서 문(③)을 막고,
        글 점수를 사장님 라벨 컷 아래로 둬 곡선 문(④)을 막는다.
        (곡선 문 자체는 TestGapCurve 가 따로 시험한다.)
        """
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=49, content_score=0.40, is_campaign_signal=True)
        r.offer = _slot_g("자료", [52] * 4 + [90000] * 5, url="https://x")
        assert r.offer["auto_hits"] == 4
        assert r.offer["gap_median"] == 90000  # 페이서 문 범위(1시간) 밖
        assert r.grade != "auto_draft"

    def test_gate_only_is_not_auto_adopted(self):
        """게이트 문구는 원래 전 게시물 공유다. 옮길 오퍼가 없으면 자동채택 안 한다."""
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=49, content_score=0.75, is_campaign_signal=True)
        r.gate = _slot_g("팔로우 확인 부탁드려요", [5] * 20)
        assert r.offer is None
        assert r.grade != "auto_draft"

    def test_shallow_probe_still_blocked(self):
        """조회 인원이 적으면 지문이어도 승격하지 않는다(초소표본 방어 유지)."""
        r = self._rec_obj(probed=4, gaps=[5] * 6)
        assert r.grade != "auto_draft"


class TestDerivedNumbersFollowEvidence:
    """근거가 걷힌 뒤 파생 수치가 남아 있으면 **방금 내린 것을 자동채택한다.**"""

    def test_impossible_removal_recomputes_auto_hits(self):
        """근거 창은 **7일**이다 — 하루 컷이던 것을 2026-08-18 에 넓혔다.

        5.8일(500,000초)짜리는 살아남고(신뢰도 0.20 으로 낮게 셈), 8일짜리는 인스타
        Private Reply 창 밖이라 걷어낸다.
        """
        rec = _rec(
            "m1",
            probed=49,
            offer=_slot_g("자료", [8] * 6 + [500000] * 2 + [8 * 86400] * 3, url="https://x"),
        )
        attribute.drop_impossible([rec])
        assert rec["offer"]["hits"] == 8  # 6 + 5.8일짜리 2건 (8일짜리 3건은 빠진다)
        assert rec["offer"]["auto_hits"] == 6  # '거의 동시' 는 6명뿐
        assert rec["offer"]["gap_median"] == 8

    def test_time_pairing_loss_lowers_auto_hits(self):
        """같은 DM 을 두 게시물이 주장하면 가까운 쪽만 남고, 진 쪽 지문도 줄어야 한다."""
        near = _rec("m-near", probed=49, offer=_slot("자료", [], url="https://x"))
        far = _rec("m-far", probed=49, offer=_slot("자료", [], url="https://x"))
        shared = [{"u": f"u{i}", "m": f"d{i}", "g": 5} for i in range(6)]
        near["offer"]["users"] = [dict(ev) for ev in shared]
        far["offer"]["users"] = [dict(ev, g=40) for ev in shared]
        for r in (near, far):
            r["offer"]["auto_hits"] = 6
            r["offer"]["gap_median"] = 5
        attribute.by_time([near, far])
        assert near["offer"]["auto_hits"] == 6
        assert far["offer"]["auto_hits"] == 0  # 전부 near 로 갔다
        assert far["offer"]["gap_median"] is None

    def test_template_demotion_zeroes_the_fingerprint(self):
        """문구 경쟁에서 내려간 오퍼가 지문으로 되살아나면 안 된다."""
        owner = _rec("m-owner", probed=49, offer=_slot_g("같은 자료", [5] * 30, url="https://x"))
        loser = _rec("m-loser", probed=49, offer=_slot_g("같은 자료", [5] * 2, url="https://x"))
        loser["offer"]["auto_hits"] = 9  # 오귀속으로 부풀려진 상태를 흉내낸다
        attribute.by_template([owner, loser])
        assert loser["offer"]["auto_hits"] == 0
        assert loser["offer"]["shared"] is True

    def test_resolve_then_regrade_does_not_auto_adopt_demoted(self):
        """resolve 전체를 거쳐도 내려간 건은 자동채택으로 안 올라간다."""
        owner = _rec("m-owner", probed=49, offer=_slot_g("같은 자료", [5] * 30, url="https://x"))
        loser = _rec("m-loser", probed=49, offer=_slot_g("같은 자료", [5] * 2, url="https://x"))
        attribute.resolve([owner, loser])
        assert owner["grade"] == "auto_draft"
        assert loser["grade"] != "auto_draft"


class TestSkewWindowAndPacedSends:
    """지문 창을 부호로 자르면 안 되고, 페이서로 밀린 발송도 살려야 한다."""

    def test_negative_gap_within_skew_still_counts_as_fingerprint(self):
        """중앙값 -80초에 17명이 같은 문구를 받았다 — 이걸 검수로 내리면 안 된다.

        한 사람이 몇 초 사이에 댓글을 두 번 달면(신청 → 완료) 캠페인은 첫 댓글에 반응하는데
        우리가 쥔 건 두 번째 댓글이라 음수가 된다. 실측 11건이 이 모양이었다.
        """
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=45, content_score=0.95, is_campaign_signal=True)
        r.offer = _slot_g("자료", [-80] * 17, url="https://x")
        assert r.offer["auto_hits"] == 0  # 헬퍼의 옛 정의(0 이상만)로는 0 이다
        attribute._repack(r.offer, r.probed)  # 실제 파생 계산을 태운다
        assert r.offer["auto_hits"] == 17
        assert r.grade == "auto_draft"

    def test_beyond_skew_tolerance_is_not_a_fingerprint(self):
        """시계 오차를 넘는 음수(-643초)는 이 댓글의 응답이 아니다."""
        slot = _slot_g("자료", [-643] * 17, url="https://x")
        attribute._repack(slot, 45)
        assert slot["auto_hits"] == 0

    def test_paced_send_with_three_supporters_is_auto(self):
        """3명이 8분 안에 같은 문구를 받았고 글도 캠페인이라 말한다 → 안 묻는다."""
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=46, content_score=0.75, is_campaign_signal=True)
        r.offer = _slot_g("자료", [462, 470, 480], url="https://x")
        assert r.grade == "auto_draft"

    def test_paced_send_needs_the_content_to_agree(self):
        """사장님 라벨 컷(0.55) 아래면 페이서 문은 열지 않는다."""
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=46, content_score=0.40, is_campaign_signal=True)
        r.offer = _slot_g("자료", [462, 470, 480], url="https://x")
        assert r.grade != "auto_draft"

    def test_paced_send_beyond_an_hour_uses_the_curve_not_the_pacer_gate(self):
        """1시간 밖은 페이서 문(③)이 안 열린다 — 대신 곡선 문(④)이 인원으로 판정한다.

        2026-08-18 이전에는 1시간 밖이면 무조건 검수였다. 사장님이 @reels_drgn 31건을
        검수해 27건이 실제 캠페인임을 확인한 뒤, 간격을 컷에서 곡선으로 바꿨다.
        """
        from apps.integrations.dm_migration import collect as C
        from apps.integrations.dm_migration.recover import PostRecovery

        gap = 29246  # 8.1시간 → 신뢰도 0.70 → 5명 필요
        assert C.required_hits(gap, 3) == 5
        thin = PostRecovery(media_id="m1", probed=46, content_score=0.95, is_campaign_signal=True)
        thin.offer = _slot_g("자료", [gap] * 4, url="https://x")
        assert thin.grade != "auto_draft"  # 4명 → 아직 아니다
        ok = PostRecovery(media_id="m2", probed=46, content_score=0.95, is_campaign_signal=True)
        ok.offer = _slot_g("자료", [gap] * 11, url="https://x")
        assert ok.grade == "auto_draft"  # 11명 → 살린다

    def test_pack_and_repack_agree_on_the_window(self):
        """두 곳이 갈리면 귀속 정리 뒤 등급이 옛 숫자로 매겨진다 — 단일 소스 고정."""
        from apps.integrations.dm_migration import collect

        gaps = [-200, -100, -1, 0, 30, 60, 61, 5000]
        slot = _slot("자료", [{"u": f"u{i}", "m": f"d{i}", "g": g} for i, g in enumerate(gaps)])
        attribute._repack(slot, 10)
        assert slot["auto_hits"] == collect.fast_hits(gaps) == 5


# ══════════════ 7. 끝난 잡 재채점 (manage.py dm_migration_regrade) ══════════════
#
# 등급 규칙은 판정 로직이지 수집 결과가 아니다. 규칙을 고쳤을 때 3~4시간 걸린 수집을
# 처음부터 다시 돌리면 Meta 쿼터를 또 써서 다른 워크스페이스에 피해를 준다(CLAUDE.md §1).


class TestRegradeCommand:
    def _setup(self):
        from apps.integrations.models import DMCampaignCandidate, DMMigrationJob
        from apps.integrations.test_dm_migration import _conn, _job, _user, _ws

        conn = _conn(_ws(_user()))
        job = _job(conn, status=DMMigrationJob.Status.READY)
        # 비율 0.59 · 60초 내 17명 → 옛 규칙은 검수필요, 새 규칙은 자동채택
        rec = {
            "media_id": "m-fast",
            "probed": 49,
            "content_score": 0.75,
            "signal": True,
            "grade": "needs_review",
            "score": 0.44,
            "confirm_required": True,
            "offer": {
                "text": "자료 보내드려요",
                "url": "https://ex.co/a",
                "hits": 29,
                "ratio": 0.59,
                "score": 0.44,
                "gap_median": 34,
                "auto_hits": 17,
            },
            "gate": None,
        }
        job.stage_data = {"recoveries": [rec]}
        job.save(update_fields=["stage_data"])
        cand = DMCampaignCandidate.objects.create(
            job=job,
            ig_connection=conn,
            band=DMCampaignCandidate.Band.NEEDS_REVIEW,
            media_id="m-fast",
            confirm_required=True,
            support_hits=29,
            support_probed=49,
        )
        return job, cand

    def test_dry_run_does_not_write(self, db):
        from io import StringIO

        from django.core.management import call_command

        job, cand = self._setup()
        out = StringIO()
        call_command("dm_migration_regrade", str(job.id), stdout=out)
        cand.refresh_from_db()
        assert cand.band == "needs_review"  # 손대지 않았다
        assert "미리보기" in out.getvalue()

    def test_apply_promotes_candidate_and_stage_data(self, db):
        from io import StringIO

        from django.core.management import call_command

        job, cand = self._setup()
        call_command("dm_migration_regrade", str(job.id), "--apply", stdout=StringIO())
        cand.refresh_from_db()
        job.refresh_from_db()
        assert cand.band == "auto_draft"
        assert cand.confirm_required is False
        assert job.stage_data["recoveries"][0]["grade"] == "auto_draft"
        assert job.stage_data["regraded_at_rule"]["changed"] == 1

    def test_refuses_a_job_without_recoveries(self, db):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        from apps.integrations.test_dm_migration import _conn, _job, _user, _ws

        job = _job(_conn(_ws(_user())))
        with pytest.raises(CommandError, match="recoveries"):
            call_command("dm_migration_regrade", str(job.id))

    def test_regrade_backfills_the_match_from_stored_caption(self, db):
        """★ 옛 실행에도 소급돼야 한다 — 없으면 계정마다 재실행을 해야 한다.

        캡션은 stage_data["media"] 에 전문이 있고 DM 문구는 offer 에 있으니 추가 호출 없이
        계산된다. 2026-08-18 이전 실행에는 content_match 가 아예 없다.
        """
        from io import StringIO

        from django.core.management import call_command

        from apps.integrations.models import DMCampaignCandidate, DMMigrationJob
        from apps.integrations.test_dm_migration import _conn, _job, _user, _ws

        conn = _conn(_ws(_user()))
        job = _job(conn, status=DMMigrationJob.Status.READY)
        job.stage_data = {
            "media": [
                {
                    "id": "m-wave",
                    "caption": "쓰나미 연인 영상 제작법 🌊 프로필 링크에서 “파도” 검색하면 얻을 수 있습니다",
                }
            ],
            "recoveries": [
                {
                    "media_id": "m-wave",
                    "probed": 10,
                    "signal": True,
                    "content_score": 0.45,
                    "grade": "needs_review",
                    "score": 0.1,
                    "trigger": "파도",
                    # content_match 없음 — 옛 실행
                    "offer": {
                        "text": "파도와 연인 프롬프트 전달드립니다!",
                        "url": "https://x",
                        "hits": 3,
                        "ratio": 0.3,
                        "score": 0.1,
                        "gap_median": int(71.9 * 3600),
                        "auto_hits": 0,
                    },
                    "gate": None,
                }
            ],
        }
        job.save(update_fields=["stage_data"])
        cand = DMCampaignCandidate.objects.create(
            job=job,
            ig_connection=conn,
            band=DMCampaignCandidate.Band.NEEDS_REVIEW,
            media_id="m-wave",
            confirm_required=True,
        )
        out = StringIO()
        call_command("dm_migration_regrade", str(job.id), "--apply", stdout=out)
        cand.refresh_from_db()
        job.refresh_from_db()
        assert "캡션↔DM 일치 계산 1건" in out.getvalue(), out.getvalue()
        assert job.stage_data["recoveries"][0]["content_match"] == ["파도"]
        assert cand.band == "auto_draft"


# ══════════════ 8. 간격은 컷이 아니라 곡선 (2026-08-18 사장님 검수) ══════════════
#
# 하루 컷으로 잘랐더니 @reels_drgn 에서 검수 52건이 나왔고, 사장님이 31건을 눈으로 보고
# **27건이 실제 캠페인**(내용 일치·복원 정확)이라고 확인했다. 간격이 길어지는 실제 사유가
# 있다 — 자동화 도구 오류로 지연 발송, 또는 나중에 손으로 보냄. 인스타 Private Reply 창이
# 7일이므로 1시간에서 0 으로 떨어뜨릴 근거가 없다.
# → 느릴수록 더 많은 사람이 받았어야 인정한다(collect.required_hits).


class TestGapCurve:
    def _rec(self, *, hits, probed, gap, content):
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(
            media_id="m1", probed=probed, content_score=content, is_campaign_signal=True
        )
        r.offer = {
            "text": "자료 보내드려요",
            "url": "https://ex.co/a",
            "hits": hits,
            "ratio": round(hits / probed, 3),
            "score": 0.1,
            "gap_median": gap,
            "auto_hits": 0,
        }
        return r

    def test_curve_shape(self):
        """1분=만점, 12시간까지 강하게, 7일까지 감소, 그 밖은 0."""
        from apps.integrations.dm_migration import collect as C

        assert C.gap_confidence(30) == 1.00
        assert C.gap_confidence(12 * 3600) == 0.70
        assert C.gap_confidence(7 * 86400) == 0.20
        assert C.gap_confidence(8 * 86400) == 0.0  # Private Reply 창 밖
        assert C.gap_confidence(None) == 0.0
        # 느릴수록 더 많이 요구한다
        need = [C.required_hits(g, 3) for g in (30, 3600, 12 * 3600, 86400, 3 * 86400)]
        assert need == sorted(need) and need[0] == 3 and need[-1] > need[0]

    def test_slow_send_with_enough_support_is_adopted(self):
        """3.8시간 뒤에 5명이 같은 문구를 받았다 → 살린다(사장님 확인 27건이 이 모양)."""
        r = self._rec(hits=5, probed=12, gap=int(3.8 * 3600), content=0.85)
        assert r.grade == "auto_draft", (r.grade, r.reject_reason)

    def test_slow_send_with_thin_support_is_not(self):
        """같은 3.8시간인데 3명뿐이면 인정 안 한다(12시간 구간은 5명 필요)."""
        r = self._rec(hits=3, probed=12, gap=int(3.8 * 3600), content=0.85)
        assert r.grade != "auto_draft"

    def test_three_days_needs_many_more(self):
        """3일이면 9명 — 느린 만큼 더 요구한다."""
        assert self._rec(hits=5, probed=30, gap=3 * 86400, content=0.9).grade != "auto_draft"
        assert self._rec(hits=9, probed=30, gap=3 * 86400, content=0.9).grade == "auto_draft"

    def test_beyond_seven_days_never(self):
        """7일 밖은 인원이 아무리 많아도 이 댓글의 응답이 아니다."""
        # 분모를 크게 둬 비율 문(60%+)이 먼저 열리지 않게 한다 — 곡선 문만 시험한다.
        assert self._rec(hits=50, probed=200, gap=8 * 86400, content=0.95).grade != "auto_draft"

    # ── 여기가 회귀 방지의 핵심 ──
    def test_curve_never_bypasses_the_owner_label(self):
        """★ 글 점수 0.55 이하는 이 문으로도 못 들어온다.

        사장님이 애매 59건을 전수 라벨링한 컷이다. 이 문이 그걸 우회하면
        @highestlevel33 에서 제대로 걸러낸 것들이 풀린다(시뮬레이션: 제외 127→127 유지).
        """
        for cs in (0.0, 0.10, 0.40, 0.55):
            r = self._rec(hits=30, probed=200, gap=int(3.8 * 3600), content=cs)
            assert r.grade != "auto_draft", (cs, r.grade)
            r2 = self._rec(hits=30, probed=200, gap=30, content=cs)
            assert r2.grade != "auto_draft", (cs, r2.grade)

    def test_curve_requires_a_message_to_migrate(self):
        """옮길 문구가 없으면(게이트만) 이 문도 안 열린다."""
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(media_id="m1", probed=200, content_score=0.95, is_campaign_signal=True)
        r.gate = {"text": "팔로우 확인", "hits": 30, "ratio": 0.15, "score": 0.1, "gap_median": 30}
        assert r.grade != "auto_draft"


class TestCommentSideScoring:
    """캡션에 행동유도를 안 쓰는 계정이 구조적으로 0.45 에 갇혀 있었다."""

    def test_trigger_flood_lifts_the_quiet_caption_case(self):
        """실측 재현(@reels_drgn): 복붙 80.6% · 캡션에 '댓글 남겨주세요' 없음 → 0.45 였다.

        캡션은 프로필 링크만 가리키고 트리거 낱말('deevid')은 댓글에만 쏟아진다.
        11/31명이 "Deevid AI 링크 보냅니다" 를 받았는데 0.45 로 검수에 떨어졌다.
        """
        from apps.integrations.dm_migration.recover import judge_content

        media = {
            "id": "m-quiet",
            "comments_count": 40,
            "caption": (
                "✅ 주요 어플: Deevid AI\n"
                "🎬 해당 프롬프트는 프로필 링크의 상단 링크에서 “엘베” 검색하시면 확인 가능합니다!"
            ),
            "timestamp": "2026-02-27T00:00:00+0000",
        }
        # 댓글의 80% 가 같은 낱말
        cmts = [{"text": "deevid"} for _ in range(32)] + [
            {"text": f"영상 퀄리티 진짜 좋네요 {i}번째 저장합니다"} for i in range(8)
        ]
        v = judge_content(media, cmts)
        assert v.repetition >= 0.60, v.repetition
        assert "trigger_flood" in v.reasons, v.reasons
        assert v.score > 0.55, (v.score, v.reasons)  # 사장님 라벨 컷을 넘는다

    def test_mild_repetition_does_not_get_the_bonus(self):
        """복붙이 40% 대면 가산하지 않는다 — 아무 게시물이나 올라가면 안 된다."""
        from apps.integrations.dm_migration.recover import judge_content

        media = {"id": "m2", "comments_count": 20, "caption": "오늘 산책 사진", "timestamp": ""}
        cmts = [{"text": "좋아요"} for _ in range(8)] + [
            {"text": f"사진 분위기가 참 좋습니다 {i}"} for i in range(12)
        ]
        v = judge_content(media, cmts)
        assert "trigger_flood" not in v.reasons, (v.repetition, v.reasons)

    def test_score_never_exceeds_one(self):
        from apps.integrations.dm_migration.recover import judge_content

        media = {
            "id": "m3",
            "comments_count": 99,
            "caption": "팔로우하고 댓글에 'ai' 남겨주시면 무료 자료 전자책 보내드려요",
            "timestamp": "",
        }
        v = judge_content(
            media, [{"text": "ai"} for _ in range(50)], owner_replies=["보내드렸어요"]
        )
        assert v.score <= 1.0


# ══════════════ 9. 캡션 ↔ DM 내용 일치 (2026-08-18 사장님 검수 13건) ══════════════
#
# 사장님이 @reels_drgn 검수 13건을 눈으로 보고 8건을 "실제로 DM 캠페인도 맞고 내용도
# 일치함" 이라고 지적했다. 아쉬운 8건과 잘 걸른 3건을 가른 것이 정확히 이 신호였다.
# analyze.content_match 는 이미 있었지만 **AI 대조 전처리에만 쓰이고 등급에는 안 썼다.**


class TestContentMatchGate:
    def _rec(self, *, caption, dm, hits, probed, gap, trigger=None, content=0.45):
        from apps.integrations.dm_migration.analyze import content_match
        from apps.integrations.dm_migration.recover import PostRecovery

        r = PostRecovery(
            media_id="m1",
            probed=probed,
            content_score=content,
            is_campaign_signal=True,
            trigger=trigger,
        )
        r.offer = {
            "text": dm,
            "url": "https://ex.co/a",
            "hits": hits,
            "ratio": round(hits / probed, 3),
            "score": 0.1,
            "gap_median": gap,
            "auto_hits": 0,
        }
        r.content_match = content_match(caption, dm, trigger)
        return r

    def test_matching_dm_survives_a_three_day_gap(self):
        """실측 #6 재현: 캡션 "'파도' 검색하면" ↔ DM "파도와 연인 프롬프트".

        간격 71.9시간(3일)이라 곡선 문은 9명을 요구하는데 3명뿐이다. 그래도 내용이
        일치하므로 살린다 — 사장님이 "확실한 신호들이 있는데" 라고 지적한 건들이다.
        """
        r = self._rec(
            caption="쓰나미 연인 영상 제작법 🌊 해당 프롬프트는 프로필 링크에서 “파도” 검색하면 얻을 수 있습니다!",
            dm="파도와 연인 프롬프트 전달드립니다! https://notion.site/abc",
            hits=3,
            probed=10,
            gap=int(71.9 * 3600),
            trigger="파도",
            content=0.90,
        )
        assert r.content_match, r.content_match
        assert r.grade == "auto_draft", (r.grade, r.reject_reason)

    def test_mismatching_dm_is_still_held(self):
        """실측 #10 재현: 캡션 "캡컷 편집 효과" ↔ DM "Higgsfield X Claude 링크".

        사장님이 "잘 걸렀고, DM 캠페인이 아니야" 라고 확인한 건이다.
        """
        # 간격을 페이서 문(1시간) 밖에 둬서 일치문만 시험한다. 곡선 문은 3.5시간에
        # 5명을 요구하므로 3명으로는 안 열린다 → 남는 것은 일치문 하나뿐이다.
        r = self._rec(
            caption="모르면 손해보는 캡컷 편집 효과 4가지 5탄 ‼️ 스마트폰으로도 쉽게 영상 제작 해봐요!",
            dm="Higgsfield X Claude 링크 및 세팅 가이드 전달드립니다! 감사합니다!",
            hits=3,
            probed=10,
            gap=int(3.5 * 3600),
            content=0.65,
        )
        assert not r.content_match, r.content_match
        assert r.grade != "auto_draft", r.grade

    def test_match_does_not_rescue_a_single_supporter(self):
        """1명짜리는 내용이 맞아도 안 살린다 — 남의 게시물 DM 이 흘러든 것이 86% 였다."""
        r = self._rec(
            caption="Meshy AI 진짜 이게 되네.. 써보고 싶으면 댓글에 Meshy 남겨줘",
            dm="Meshy AI 링크 전달드립니다!",
            hits=1,
            probed=10,
            gap=30,
            trigger="meshy",
        )
        assert r.content_match
        assert r.grade != "auto_draft"

    def test_match_respects_the_seven_day_window(self):
        """8일 밖은 내용이 맞아도 이 댓글의 응답이 아니다."""
        r = self._rec(
            caption="Higgsfield AI 로 할리우드급 영화 만드는 법",
            dm="higgsfield ai 링크 보냅니다!",
            hits=9,
            probed=20,
            gap=8 * 86400,
            trigger="higgsfield",
            content=0.90,
        )
        assert r.content_match
        assert r.grade != "auto_draft"

    def test_regrade_carries_the_match(self):
        """★ 재채점이 content_match 를 안 물고 가면 이 문이 조용히 안 걸린다."""
        rec = {
            "media_id": "m1",
            "probed": 10,
            "signal": True,
            "content_score": 0.45,
            "grade": "needs_review",
            "score": 0.1,
            "content_match": ["파도"],
            "offer": {
                "text": "파도와 연인 프롬프트",
                "url": "https://x",
                "hits": 3,
                "ratio": 0.3,
                "score": 0.1,
                "gap_median": int(71.9 * 3600),
                "auto_hits": 0,
            },
            "gate": None,
        }
        attribute.regrade([rec])
        assert rec["grade"] == "auto_draft", rec["grade"]

    def test_owner_label_still_governs_thin_support(self):
        """★ 이 문은 지지 3명 하한을 지키므로 사장님 라벨 컷을 흔들지 않는다.

        @highestlevel33 의 제외 127건은 대부분 지지 1~2명이라 이 문에 안 걸린다
        (시뮬레이션 확인: 제외 127 → 127 그대로).
        """
        for hits in (1, 2):
            r = self._rec(
                caption="AI 자료 정리해뒀어요",
                dm="AI 자료 보내드립니다",
                hits=hits,
                probed=40,
                gap=30,
                content=0.10,
            )
            assert r.grade != "auto_draft", (hits, r.grade)


# ══════════════ 10. 구제 라운드 — 귀속이 근거를 빼앗은 게시물 ══════════════


class TestDemotedTargets:
    """파는 판단이 귀속 정리보다 **먼저** 일어나서 생기는 구멍.

    실측(@highestlevel33 febb6b6c, 2026-08-19): 검수 5건 중 **3건**이 이 경로였다.
    C9zn4g3ItKv 는 댓글 10,878개인데 46명만 보고 멈췄고, 그 46명의 DM 이 옆 게시물 것으로
    판정나서 빈손이 됐는데 남은 1만 명은 안 봤다. 제외 32건 중에도 3건 있었다.
    """

    def test_picks_posts_whose_auto_draft_was_revoked(self):
        recs = [
            {"media_id": "a", "grade": "needs_review"},
            {"media_id": "b", "grade": "auto_draft"},
            {"media_id": "c", "grade": "excluded"},
        ]
        before = {"a": "auto_draft", "b": "auto_draft", "c": "auto_draft"}
        assert attribute.demoted_targets(before, recs) == ["a", "c"]

    def test_ignores_posts_that_never_reached_auto_draft(self):
        """처음부터 자동채택이 아니었으면 **문지기가 멈추지 않았다** — 이미 팠다."""
        recs = [{"media_id": "a", "grade": "excluded"}]
        assert attribute.demoted_targets({"a": "needs_review"}, recs) == []

    def test_ignores_posts_that_kept_auto_draft(self):
        recs = [{"media_id": "a", "grade": "auto_draft"}]
        assert attribute.demoted_targets({"a": "auto_draft"}, recs) == []

    def test_real_shape_the_fast_supporters_get_taken_away(self):
        """실측 모양(DUfiDBkgdQ7) — **빠른 지지자만 옆 게시물이 가져간다.**

        조사 당시엔 60초 내 6명이라 자동 발송 지문으로 자동채택이었다. 시간 짝짓기가 그 6명을
        더 가까운 게시물로 넘기자 남은 것은 간격 38.8시간짜리 6명뿐 — 지문이 사라지고 곡선이
        요구하는 지지(9명)에 못 미쳐 검수필요로 떨어졌다. 그런데 **조사는 이미 끝났다.**
        """
        fast = [{"u": f"u{i}", "m": f"m{i}", "g": 30} for i in range(6)]
        slow = [{"u": f"s{i}", "m": f"t{i}", "g": 139858} for i in range(6)]
        weak = _rec(
            "weak",
            probed=47,
            offer=_slot("자료 보내드려요", fast + slow, url="https://a.b/c"),
            content=0.9,
        )
        # 옆 게시물이 같은 6명의 **같은 DM** 을 더 가까운 간격으로 주장한다.
        closer = _rec(
            "closer",
            probed=50,
            offer=_slot("자료 보내드려요", [{**ev, "g": 10} for ev in fast], url="https://a.b/c"),
            content=0.9,
        )
        recs = [weak, closer]
        # 파생 수치는 production 과 같은 함수로 만든다 — 여기서 갈리면 현실을 검증하지 않는다.
        for r in recs:
            attribute._repack(r["offer"], r["probed"])
        attribute.regrade(recs)
        before = {r["media_id"]: r["grade"] for r in recs}
        assert before["weak"] == "auto_draft", before  # 조사 당시엔 자동채택 → 여기서 멈췄다

        attribute.resolve(recs, keep_users=True)

        assert weak["offer"]["auto_hits"] == 0, "빠른 지지자가 넘어가야 한다"
        assert weak["grade"] != "auto_draft", weak["grade"]
        assert attribute.demoted_targets(before, recs) == ["weak"]

    def test_keep_users_lets_a_second_round_compete(self):
        """⚠️ 근거 목록을 버리면 다시 판 결과를 **경쟁시킬 수 없다**(by_time 이 사용자 단위)."""
        recs = [
            _rec("a", offer=_slot("t", [{"u": "u1", "m": "m1", "g": 10}], url="https://a/b")),
            _rec("b", offer=_slot("t", [{"u": "u1", "m": "m1", "g": 99}], url="https://a/b")),
        ]
        attribute.resolve(recs, keep_users=True)
        assert isinstance(recs[0]["offer"]["users"], list)  # 남아 있어야 2차가 가능하다

        attribute.drop_users(recs)
        assert "users" not in recs[0]["offer"]

    def test_default_still_drops_users(self):
        """기본값은 예전과 같다 — 개인 식별자를 오래 들고 있지 않는다(7일 파기)."""
        recs = [_rec("a", offer=_slot("t", [{"u": "u1", "m": "m1", "g": 10}], url="https://a/b"))]
        attribute.resolve(recs)
        assert "users" not in (recs[0]["offer"] or {})

    def test_resolve_is_idempotent_so_a_second_pass_is_safe(self):
        """구제 후 정리를 **한 번 더** 돌리므로 두 번 돌려도 결과가 같아야 한다."""

        def build():
            return [
                _rec("a", offer=_slot("t", [{"u": "u1", "m": "m1", "g": 10}], url="https://a/b")),
                _rec("b", offer=_slot("t", [{"u": "u1", "m": "m1", "g": 99}], url="https://a/b")),
            ]

        once = build()
        attribute.resolve(once, keep_users=True)
        snap = [(r["media_id"], r["grade"], (r.get("offer") or {}).get("hits")) for r in once]
        attribute.resolve(once)
        twice = [(r["media_id"], r["grade"], (r.get("offer") or {}).get("hits")) for r in once]
        assert snap == twice
