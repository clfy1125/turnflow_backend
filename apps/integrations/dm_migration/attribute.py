"""DM 캠페인 이전 — **귀속 정리**: 이 DM 이 정말 이 게시물 것인가.

게시물을 하나씩 조사하는 구조상, 한 사람이 여러 게시물에 댓글을 달면 **그 사람이 받은 DM
전부가 모든 게시물의 근거로 중복 계산**된다. 실측(@highestlevel33): 지지 1~2명짜리 92건이
그렇게 생겼고, 연구에서 이런 얕은 지지는 89%가 오답이었다.

수집이 다 끝난 뒤 **이미 받아둔 정보만으로** 두 번 거른다 (추가 API 호출 0).

1. 시간 짝짓기 (:func:`by_time`)
   같은 DM 이 여러 게시물의 근거로 쓰였다면, **그 사람의 댓글과 시간이 가장 가까운 게시물**
   하나에만 남긴다. 문턱값("N분 이내면 통과")은 이미 기각됐다 — 연달아 댓글을 달면 두
   캠페인 DM 이 모두 몇 초 안에 도착해 문턱으로는 안 갈린다. 갈리는 건 **상대 비교**다.

2. 문구 경쟁 (:func:`by_template`)
   같은 오퍼 문구가 여러 게시물에 걸쳐 있고 한 곳이 압도적으로 강하면, 약한 쪽은 흘러든
   것으로 본다.
   ⚠️ 게이트 문구(팔로우 확인)에는 **적용하면 안 된다** — 원래 전 게시물에서 공유된다.
   예전에 배타 할당을 게이트까지 밀었다가 @mini_ai_ 42개 중 35개를 죽였다.
   ⚠️ 3배 차이를 요구하는 이유: 같은 문구가 여러 게시물에서 **고르게** 강하면 그건 상시
   캠페인이지 오귀속이 아니다(실측 사례: 10/10 · 10/10 · 9/10).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from .analyze import wilson_lower_bound
from .collect import CLOCK_SKEW_TOLERANCE as CLOCK_SKEW
from .collect import EVIDENCE_MAX_GAP as MANUAL_GAP
from .collect import fast_hits

logger = logging.getLogger(__name__)

# AI 내용 대조가 되살릴 수 있는 제외 이유(:func:`apply_verdicts`).
# 되살릴 수 없는 것 = AI 가 **보지 않는 근거**로 내려간 것:
#   impossible_timing — 간격이 하루 넘거나 DM 이 댓글보다 먼저. AI 는 시간을 안 본다.
#   content_says_no   — 글·댓글 점수가 0.55 이하. 사장님이 59건을 눈으로 매긴 라벨이다.
# ``None`` 은 fail-open — 아주 옛 기록(이유 필드가 없던 시절)은 예전처럼 되살린다.
RESCUABLE = frozenset({"", "no_link", "thin_support", None})

# 다른 게시물이 이만큼 강해야 이쪽을 오귀속으로 본다.
TEMPLATE_DOMINANCE = 3.0
TEMPLATE_MIN_OWNER_HITS = 3
TEMPLATE_MAX_LOSER_HITS = 2


def _norm_template(text: str) -> str:
    """문구 뼈대 — 이름·숫자·URL 차이를 지운다."""
    t = re.sub(r"https?://\S+", " ", text or "")
    t = re.sub(r"[@#]\w+", " ", t)
    t = re.sub(r"\d+", " ", t)
    t = re.sub(r"[^\w가-힣]+", " ", t)
    return " ".join(t.split())[:80].casefold()


def _repack(slot: dict, probed: int) -> None:
    """users 가 바뀐 뒤 **근거에서 파생된 모든 수치**를 다시 계산한다.

    ⚠️ ``auto_hits``/``gap_median`` 을 빼먹으면 안 된다. 자동채택 ②(자동 발송 지문)가
    ``auto_hits`` 로 판정하므로, 귀속 정리로 근거를 걷어낸 뒤에도 옛 숫자가 남아 있으면
    **방금 오귀속으로 판정한 것을 그대로 자동채택한다.** 파생 수치는 여기 한 곳에서만
    만든다(:func:`recover._pack` 과 정의가 같아야 한다).
    """
    users = slot.get("users") or []
    hits = len(users)
    slot["hits"] = hits
    slot["ratio"] = round(hits / max(probed, 1), 3)
    slot["score"] = round(wilson_lower_bound(hits, max(probed, 1)), 3)
    gaps = sorted(g for g in (ev.get("g") for ev in users if isinstance(ev, dict)) if g is not None)
    slot["gap_median"] = gaps[len(gaps) // 2] if gaps else None
    slot["auto_hits"] = fast_hits(gaps)


def drop_impossible(recoveries: list[dict]) -> int:
    """**이 댓글의 응답일 수 없는 근거**를 걷어낸다 (시간만으로 판정).

    · DM 이 댓글보다 먼저 갔다 → 그 사람이 예전에 다른 게시물에 단 댓글로 받은 것이다
      (실측 195건이 이 경우였다).
    · **7일**이 지나서 갔다 → 인스타 Private Reply 창 밖이라 이 댓글의 응답일 수 없다.
      ⚠️ 예전에는 **하루**로 잘랐다. 그런데 사장님이 @reels_drgn 검수 31건을 눈으로 보고
      27건이 실제 캠페인이라고 확인했다(2026-08-18) — 도구 오류로 늦게 가거나 나중에 손으로
      보낸 경우가 있어 간격만으로 부정할 수 없다. 하루~7일 구간은 지우지 말고 남기고,
      **신뢰도를 낮춰**(collect.gap_confidence) 더 많은 지지를 요구하는 쪽으로 바꿨다.
    """
    removed = 0
    for rec in recoveries:
        for key in ("offer", "gate"):
            slot = rec.get(key)
            if not slot or not isinstance(slot.get("users"), list):
                continue
            before = len(slot["users"])
            slot["users"] = [
                ev
                for ev in slot["users"]
                if ev.get("g") is None or (-CLOCK_SKEW <= ev["g"] <= MANUAL_GAP)
            ]
            removed += before - len(slot["users"])
            _repack(slot, int(rec.get("probed") or 1))
    if removed:
        logger.info("DM이전 귀속: 시간상 불가능한 근거 %d건 제외", removed)
    return removed


def by_time(recoveries: list[dict]) -> int:
    """같은 DM 을 여러 게시물이 근거로 쓰면 **가장 가까운 게시물** 하나만 남긴다.

    각 recovery 의 offer/gate 에 담긴 ``users`` 는
    ``[{"u": 사용자, "m": 메시지id, "g": 댓글↔DM 시간차(초)}, ...]`` 형태다.
    반환: 걷어낸 근거 수.
    """
    # (사용자, 메시지) → [(시간차, recovery, slot)]
    claims: dict[tuple, list] = defaultdict(list)
    for rec in recoveries:
        for key in ("offer", "gate"):
            slot = rec.get(key)
            for ev in (slot or {}).get("users") or []:
                if isinstance(ev, dict) and ev.get("m"):
                    claims[(ev["u"], ev["m"])].append((abs(ev.get("g") or 0), rec, slot))

    removed = 0
    for (_u, _m), rows in claims.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda x: x[0])  # 시간차가 작은 순
        winner = rows[0][2]
        for _gap, _rec, slot in rows[1:]:
            if slot is winner:
                continue
            before = len(slot.get("users") or [])
            slot["users"] = [
                ev for ev in slot["users"] if not (ev.get("u") == _u and ev.get("m") == _m)
            ]
            removed += before - len(slot["users"])

    for rec in recoveries:
        for key in ("offer", "gate"):
            slot = rec.get(key)
            # ⚠️ users 가 있는 슬롯만 다시 센다. 근거 목록이 없는 **옛 기록**(재개·이전 버전
            # 캐시)까지 재계산하면 hits 가 통째로 0 이 되어 멀쩡한 복원분이 사라진다.
            if slot is not None and isinstance(slot.get("users"), list):
                _repack(slot, int(rec.get("probed") or 1))
    if removed:
        logger.info("DM이전 귀속: 시간 짝짓기로 근거 %d건 이동", removed)
    return removed


def by_template(recoveries: list[dict]) -> int:
    """같은 **오퍼** 문구를 여러 게시물이 주장하면, 압도적으로 강한 곳에만 남긴다.

    게이트 문구는 건드리지 않는다(전 게시물 공유가 정상). 반환: 내려간 오퍼 수.
    """
    groups: dict[str, list] = defaultdict(list)
    for rec in recoveries:
        o = rec.get("offer") or {}
        if o.get("text") and o.get("url"):  # URL 있는 오퍼만 경쟁시킨다
            k = _norm_template(o["text"])
            if k:
                groups[k].append(rec)

    demoted = 0
    for _k, recs in groups.items():
        if len(recs) < 2:
            continue
        best = max(int((r.get("offer") or {}).get("hits") or 0) for r in recs)
        if best < TEMPLATE_MIN_OWNER_HITS:
            continue
        for r in recs:
            hits = int((r.get("offer") or {}).get("hits") or 0)
            if hits > TEMPLATE_MAX_LOSER_HITS or hits >= best:
                continue
            if best < hits * TEMPLATE_DOMINANCE:
                continue  # 고르게 강하다 = 상시 캠페인. 건드리지 않는다
            # ⚠️ 문구를 지우지 않는다. 지우면 그 게시물은 "캠페인은 있는데 문구 없음"
            # 으로 나가고 사용자가 처음부터 써야 한다. 인플루언서가 여러 게시물에 **같은
            # DM 을 돌려쓰는** 경우가 실제로 많아(실측 34종이 여러 게시물에 걸침) 이 문구가
            # 이 게시물의 문구일 가능성도 충분하다.
            # 대신 지지 근거만 무효화하고(등급이 내려간다) 표시를 남긴다 —
            # 사용자는 "다른 게시물에서 더 많이 쓰인 문구" 라는 안내와 함께 문구를 받는다.
            r["offer_demoted"] = {"reason": "owned_elsewhere", "owner_hits": best, "hits": hits}
            # auto_hits 도 같이 0 으로 — 안 하면 자동채택 ②(지문)가 방금 내린 것을 되살린다.
            r["offer"] = {
                **r["offer"],
                "hits": 0,
                "ratio": 0.0,
                "score": 0.0,
                "auto_hits": 0,
                "shared": True,
            }
            demoted += 1
    if demoted:
        logger.info("DM이전 귀속: 문구 경쟁으로 오퍼 %d건 내림", demoted)
    return demoted


def regrade(recoveries: list[dict]) -> int:
    """지지 수가 바뀌었으니 등급·점수·확인필요를 **다시** 계산한다.

    판정 규칙은 :class:`~apps.integrations.dm_migration.recover.PostRecovery` 가 단일
    소스다 — 여기서 규칙을 복제하면 두 곳이 갈라진다.
    """
    from .recover import PostRecovery

    changed = 0
    for rec in recoveries:
        r = PostRecovery(
            media_id=rec.get("media_id", ""),
            probed=int(rec.get("probed") or 1),
            offer=rec.get("offer"),
            gate=rec.get("gate"),
            is_campaign_signal=bool(rec.get("signal")),
            content_score=float(rec.get("content_score") or 0.0),
            # ⚠️ 빼먹으면 재채점에서 자동채택 ⑤(내용 일치)가 조용히 안 걸린다.
            content_match=list(rec.get("content_match") or []),
        )
        grade, reason = r.verdict()
        if rec.get("grade") != grade:
            changed += 1
        rec["score"], rec["grade"] = r.score, grade
        rec["reject_reason"] = reason
        rec["confirm_required"] = r.confirm_required
    return changed


def apply_verdicts(recoveries: list[dict], verdicts: dict) -> dict:
    """AI 내용 대조 결과를 반영한다. **AI 는 후보를 지우지 못한다 — 표시만 한다.**

    · match=True  → 지지가 1~2명이어도 **살린다**(도달률 낮은 게시물 구제).
    · match=False → **지우지 않는다.** 의심 표시만 달아 검수에서 아래로 내린다.
    · 판정 없음   → 손대지 않는다(fail-open).

    ⚠️ 왜 삭제를 안 시키나 — 실측(@highestlevel33, 2026-08-17):
        확실한 캠페인(지지 3명+) 30건에 돌렸더니 **6건(20%)을 '아니다' 라고 했다.**
        예: 캡션 "조회수 터지는 인스타 비밀 점수표" ↔ DM "노출 높이는 5가지 필수 세팅"
            → 30명 중 23명이 받은 확정 캠페인인데 문구가 안 맞는다고 기각.
        인플루언서는 캡션에서 예고한 말과 DM 문구를 그대로 맞추지 않는다. AI 에 거부권을
        주면 진짜를 그만큼 잃는다. 반대로 특이도는 높아서(가짜 33건 중 32건 정확) **의심
        표시로는 값이 있다** — 사람이 30초 보고 지우는 편이 낫다.
        (CLAUDE.md §1: 정밀도와 충돌하면 버리지 말고 등급으로 가른다.)
    """
    kept = doubted = blocked = 0
    for rec in recoveries:
        v = verdicts.get(rec.get("media_id"))
        if not v:
            continue
        rec["ai_match"] = v
        if v.get("match"):
            if rec.get("grade") == "excluded":
                # ⚠️ **왜 제외됐는지 보고 되살린다.** 예전엔 이유를 안 봤다.
                #    실측(@highestlevel33, 2026-08-18): 검수 17건 중 10건이 이 경로로
                #    올라온 것이었고 간격이 516,379초·602,519초(6~7일)였다 — 이 댓글의
                #    응답일 수 없는 DM 이다. AI 는 캡션↔DM 문구만 보고 시간도 사장님
                #    라벨도 보지 않으므로, 그 두 이유로 내려간 건을 뒤집을 자격이 없다.
                #    구제는 원래 목적인 **얕은 지지**(도달률 낮은 게시물)에만 쓴다.
                if rec.get("reject_reason") in RESCUABLE:
                    rec["grade"] = "needs_review"
                    kept += 1
                else:
                    rec["ai_match_blocked"] = rec.get("reject_reason") or "unknown"
                    blocked += 1
                continue
            kept += 1
        else:
            rec["ai_doubt"] = True  # 검수 정렬용. 등급은 건드리지 않는다
            rec["confirm_required"] = True
            doubted += 1
    return {"checked": len(verdicts), "kept": kept, "doubted": doubted, "blocked": blocked}


def resolve(recoveries: list[dict]) -> dict:
    """수집 종료 후 귀속 정리 일괄 실행. 통계 반환.

    순서가 중요하다 — **불가능한 근거를 먼저 걷어내고** 나서 게시물 간 경쟁을 붙인다.
    거꾸로 하면 시간상 말이 안 되는 근거가 경쟁에서 이겨버린다.
    """
    impossible = drop_impossible(recoveries)
    moved = by_time(recoveries)
    demoted = by_template(recoveries)
    # 근거가 다 빠진 슬롯은 제거한다.
    emptied = 0
    for rec in recoveries:
        for key in ("offer", "gate"):
            slot = rec.get(key)
            if slot and not slot.get("hits"):
                rec[key] = None
                emptied += 1
    changed = regrade(recoveries)
    # 근거 목록(사용자·메시지 id)은 정리에만 쓰고 버린다 — stage_data 를 불리지 않고,
    # 개인 식별자를 필요 이상으로 오래 들고 있지 않는다.
    for rec in recoveries:
        for key in ("offer", "gate"):
            if rec.get(key):
                rec[key].pop("users", None)
    return {
        "impossible": impossible,
        "moved": moved,
        "demoted": demoted,
        "emptied": emptied,
        "regraded": changed,
    }
