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

logger = logging.getLogger(__name__)

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
    """users 가 바뀐 뒤 hits/ratio/score 를 다시 계산한다."""
    hits = len(slot.get("users") or [])
    slot["hits"] = hits
    slot["ratio"] = round(hits / max(probed, 1), 3)
    slot["score"] = round(wilson_lower_bound(hits, max(probed, 1)), 3)


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
            r["offer_demoted"] = {"reason": "owned_elsewhere", "owner_hits": best, "hits": hits}
            r["offer"] = None
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
        )
        if rec.get("grade") != r.grade:
            changed += 1
        rec["score"], rec["grade"] = r.score, r.grade
        rec["confirm_required"] = r.confirm_required
    return changed


def resolve(recoveries: list[dict]) -> dict:
    """수집 종료 후 귀속 정리 일괄 실행. 통계 반환."""
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
    return {"moved": moved, "demoted": demoted, "emptied": emptied, "regraded": changed}
