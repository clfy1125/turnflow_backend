"""DM 이전 — 게시물 판정 **재사용 캐시**.

이 기능에서 비싼 것은 순서대로 ①발신함 훑기(실측 122분·1,577페이지) ②게시물별 조회
(19.6콜/게시물 × 수백) ③댓글 끝까지 페이징(1만 개 게시물이 200페이지)이다.
그런데 **게시물의 캡션은 안 바뀌고, 2024년에 끝난 캠페인은 앞으로도 안 바뀐다.**
이미 깔끔하게 판정된 게시물을 사용자가 기능을 다시 쓸 때마다 재조사하는 것은 Meta 앱
쿼터 낭비이고, 그 쿼터는 다른 워크스페이스의 댓글 수집·DM 발송에서 빼오는 것이다
(CLAUDE.md §1).

**무엇을 저장하고 무엇을 저장하지 않는가**
    · 저장한다 — 우리가 만든 판정 수치(등급·점수·지지 인원·간격), 어디까지 팠는지,
      끝까지 파서 얻은 조회 대상 풀(사용자 id·시각).
    · 저장하지 않는다 — **타인의 DM 원문·댓글 원문.** 7일 파기 정책
      (``tasks.purge_dm_migration_raw``)의 대상이다. 문구가 필요한 재사용은 이미 영구
      보관되는 :class:`~apps.integrations.models.DMCampaignCandidate` 에서 가져온다.

**안전장치 — 규칙 버전**
    판정 규칙을 고치면 :data:`RULES_VERSION` 을 올려 옛 판정을 전부 무효화한다. 안 하면
    버그가 있던 버전의 결론이 영구히 남는다. 2026-08-17 에 "게이트 하나 찾으면 탐색 종료"
    버그가 있었고, 그때의 '문구 없음' 판정이 영원히 캐시될 수 있었다.
"""

from __future__ import annotations

import logging

from ..models import DMCampaignCandidate, IGPostAnalysis
from . import recover

logger = logging.getLogger(__name__)

# ⚠️ 판정 로직을 바꿀 때마다 올릴 것. 올리면 그 계정의 옛 판정이 전부 재조사 대상이 된다.
#   1 → 2 (2026-08-17): 게이트 오판·두 축 소진·지문 판정·페이서 문 도입.
#   2 → 3 (2026-08-18): **캐시가 LLM 초안을 '복원된 DM' 으로 되먹이던 순환을 끊었다.**
#     `to_recovery` 가 offer.text 에 `draft_opening_message` 를 넣었는데, 그 값은
#     `_create_candidate` 에서 **LLM 초안이 우선**(first_dm_draft or offer.text)이다.
#     그래서 2회차부터 "LLM 이 캡션 보고 쓴 글" 을 캡션과 대조해 '내용 일치' 로 판정했다.
#     실측(C3SqJuhxpah): 댓글 10,050개 게시물이 **조회 50명·지지 3명·두 축 미소진**인데
#     그 순환으로 auto_draft 로 굳었고, is_settled 가 auto_draft 를 '끝났다' 로 보아
#     **다시 파지도 않았다.** 문구는 "자료는 [링크]에서" — 자리표시자가 그대로 남아 있다.
#     → 이번엔 **반드시 올려야 한다.** 오염된 판정이 auto_draft 로 굳어 있어서
#       is_settled 가 재조사를 막고, 규칙만 고쳐도 그 게시물에는 닿지 못한다.
#       (직전 2026-08-18 변경들은 auto_draft 를 내리지 않아 올리지 않았다 — 판단 근거는
#        "굳은 판정 중 뒤집히는 것이 있나" 다.)
RULES_VERSION = 3

# 댓글이 이만큼 늘었으면 새 댓글러가 DM 을 받았을 수 있다 → 다시 본다.
COMMENT_GROWTH_RATIO = 1.10
COMMENT_GROWTH_ABS = 20
PROBE_POOL_MAX = 500  # 저장할 조회 대상 수 상한(게시물당)


def load(conn) -> dict:
    """이 연결의 게시물 판정 캐시를 ``{media_id: IGPostAnalysis}`` 로 읽는다."""
    rows = IGPostAnalysis.objects.filter(ig_connection=conn)
    return {r.media_id: r for r in rows}


def is_settled(row, media: dict) -> bool:
    """이 게시물은 **다시 조사할 필요가 없나**.

    두 가지만 '끝났다' 고 본다.
      · 자동채택 — 옮길 문구·링크·근거를 다 얻었다. 더 조사해도 결론이 같다.
      · 명백히 캠페인 아님(글 점수가 캠페인 컷 미만) — 캡션이 안 바뀌므로 결론이 같다.

    ``needs_review`` 와 '글은 강한데 문구를 못 살림' 은 **끝난 게 아니다.** 다음 실행에서
    더 좋은 코드나 더 깊은 조사로 건질 수 있으므로 다시 본다.
    """
    if row is None or row.rules_version != RULES_VERSION:
        return False
    now = int(media.get("comments_count") or 0)
    was = int(row.comments_count or 0)
    if now > max(was * COMMENT_GROWTH_RATIO, was + COMMENT_GROWTH_ABS):
        return False  # 댓글이 늘었다 = 새 댓글러가 받았을 수 있다
    if row.grade == "auto_draft":
        return True
    if row.grade == "excluded" and row.content_score < recover.CONTENT_CAMPAIGN_MIN:
        return True
    return False


def probe_pool_for(row) -> list[dict]:
    """저장해둔 조회 대상 풀 — 댓글 재페이징(최대 200페이지)을 건너뛰게 해준다."""
    if row is None or row.rules_version != RULES_VERSION:
        return []
    return list(row.probe_pool or [])


def texts_for(conn, media_ids: list[str]) -> dict:
    """영구 보관된 후보에서 **문구**를 되찾는다(캐시는 원문을 담지 않으므로).

    같은 게시물에 후보가 여러 번 생겼으면 최신 것을 쓴다.
    """
    out: dict = {}
    qs = (
        DMCampaignCandidate.objects.filter(ig_connection=conn, media_id__in=media_ids)
        .order_by("media_id", "-created_at")
        .only(
            "media_id",
            "draft_opening_message",
            "offer_url",
            "offer_button_label",
            "gate_message",
            "gate_button_label",
            "gate_detected",
            "suggested_keywords",
            "draft_name",
            "draft_description",
            "draft_public_reply_templates",
        )
    )
    for c in qs:
        out.setdefault(c.media_id, c)
    return out


def to_recovery(row, media: dict, cand=None) -> dict:
    """캐시(+영구 후보) → 복원 결과 dict. 파이프라인이 새로 조사한 것과 같은 모양이어야 한다."""
    offer = None
    gate = None
    if cand and (cand.draft_opening_message or cand.offer_url):
        # 복원 원문을 우선한다. 없으면(옛 후보) 초안을 쓰되 **초안임을 표시**해서
        # 판정이 그것을 근거로 쓰지 못하게 한다(recover.PostRecovery 참조).
        mt = cand.matched_template or {}
        recovered = (mt.get("recovered_text") or "").strip()
        offer = {
            "text": recovered or cand.draft_opening_message or "",
            "text_is_draft": not recovered,
            "url": mt.get("recovered_url") or cand.offer_url or "",
            "label": cand.offer_button_label or "",
            "hits": row.support_hits,
            "ratio": round(row.support_hits / max(row.probed, 1), 3),
            "score": 0.0,
            "gap_median": row.gap_median,
            "auto_hits": row.auto_hits,
            "drops": [],
            "samples": [],
        }
    if cand and cand.gate_detected and cand.gate_message:
        gate = {
            "text": cand.gate_message,  # 게이트 문구는 LLM 이 다시 쓰지 않는다(원문 유지)
            "url": "",
            "label": cand.gate_button_label or "",
            "hits": row.support_hits,
            "ratio": 0.0,
            "score": 0.0,
            "is_gate": True,
            "drops": [],
            "samples": [],
        }
    return {
        "media_id": row.media_id,
        "permalink": row.permalink or media.get("permalink", "") or "",
        "caption": (media.get("caption") or "")[:300],
        "timestamp": media.get("timestamp", ""),
        "comments_count": media.get("comments_count", 0),
        "probed": row.probed,
        "trigger": row.trigger or None,
        "repetition": 0.0,
        "signal": row.content_score >= recover.CONTENT_CAMPAIGN_MIN,
        "content_score": row.content_score,
        "content_reasons": list(row.content_reasons or []),
        "offer": offer,
        "gate": gate,
        "grade": row.grade,
        "score": 0.0,
        "confirm_required": row.grade not in ("auto_draft", "excluded"),
        "drops": [],
        "samples": [],
        "keyword_hits": {},
        "from_cache": True,
    }


def save(conn, media: dict, rec: dict, *, dug_all=False, dug_convo=False, probe_pool=None) -> None:
    """게시물 1건의 판정을 캐시에 쓴다(멱등 upsert). 실패해도 잡을 죽이지 않는다."""
    from .analyze import parse_graph_time

    offer = rec.get("offer") or {}
    gate = rec.get("gate") or {}
    best = offer or gate
    pool = [
        {"u": p.get("u") or p.get("id"), "ts": p.get("ts")}
        for p in (probe_pool or [])[:PROBE_POOL_MAX]
        if (p.get("u") or p.get("id"))
    ]
    try:
        IGPostAnalysis.objects.update_or_create(
            ig_connection=conn,
            media_id=rec.get("media_id") or media.get("id") or "",
            defaults={
                "media_timestamp": parse_graph_time(media.get("timestamp")),
                "permalink": (rec.get("permalink") or media.get("permalink") or "")[:500],
                "rules_version": RULES_VERSION,
                "comments_count": int(media.get("comments_count") or 0),
                "grade": rec.get("grade") or "",
                "content_score": float(rec.get("content_score") or 0.0),
                "content_reasons": list(rec.get("content_reasons") or []),
                "trigger": (rec.get("trigger") or "")[:100],
                "probed": int(rec.get("probed") or 0),
                "support_hits": int(best.get("hits") or 0),
                "gap_median": best.get("gap_median"),
                "auto_hits": int(best.get("auto_hits") or 0),
                "has_offer_url": bool(offer.get("url")),
                "dug_all_comments": bool(dug_all),
                "dug_conversations": bool(dug_convo),
                "probe_pool": pool,
            },
        )
    except Exception:  # noqa: BLE001 — 캐시 쓰기 실패가 분석을 죽이면 안 된다
        logger.exception("DM이전 판정 캐시 저장 실패 (media=%s)", rec.get("media_id"))
