"""DM 캠페인 이전 — 게시물 1건 복원 (정밀도 우선).

연구(2026-08-13~14, 실계정 4곳·게시물 1,000+)에서 확정된 규칙만 담는다.

핵심 3가지
    1. **첨부를 읽는다** — 버튼 DM 은 ``message`` 가 비어 있고 본문이 ``attachments`` 안에 있다.
       실측 은닉율 67~100%. 이걸 안 읽으면 복원율이 0 이 된다.
    2. **지지비율로 판정한다** — 같은 게시물의 여러 댓글러에게 *공통으로* 간 문구만 그 게시물의
       캠페인이다. 1명에게만 간 DM 은 86%가 다른 게시물에서 흘러든 것.
       실측 정밀도: ≤20% → 11% · 40~60% → 77% · 60%+ → 100%.
    3. **게이트와 오퍼를 따로 뽑는다** — 게이트(팔로우 확인)는 댓글러 전원에게 가서 지지 100%,
       오퍼(자료 링크)는 게이트 통과자만 받아 지지가 낮다. 최고 지지 하나만 뽑으면
       **구조적으로 링크 없는 게이트만** 나온다. 분리하니 오퍼 URL 확보가 3건 → 42건(100%).

버린 신호 (측정으로 기각)
    · 시간 컷오프를 1분까지 좁혀도 정밀도 76% — 한 사람이 여러 게시물에 연달아 댓글을 달면
      각 캠페인 DM 이 모두 몇 초 안에 도착해 시간으로 갈리지 않는다.
    · 트리거 일치율 — 수신자는 정의상 이 게시물 댓글러라 항상 트리거를 포함한다(무의미).
    · 공개답글 수 — 일상 게시물에 답글이 많고 대형 캠페인엔 없어 신호가 역방향.
    · 배타 할당(한 문구를 한 게시물에만) — 같은 게이트 문구를 전 게시물에서 쓰는 계정에서
      42개 중 35개를 죽였다. 지지비율만으로 충분하다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import collect as C
from .analyze import (
    caption_keywords,
    fingerprint,
    normalize_comment,
    placeholder_normalize,
    wilson_lower_bound,
)

logger = logging.getLogger(__name__)

# 등급 컷 (지지 신뢰하한 기준)
GRADE_AUTO = 0.60  # 자동 채택 — 실측 정밀도 100%
GRADE_REVIEW = 0.40  # 확인 권장 — 77%
MIN_SUPPORT_HITS = 3  # 표본이 작을 때 비율만 믿지 않기 위한 절대 하한

_CAP_CTA_RE = re.compile(r"댓글에|댓글로|남겨|입력|디엠|\bDM\b", re.I)
# 인스타 시스템 알림·상투 문구 — 캠페인 DM 이 아니다.
_SYSTEM_NOISE = re.compile(r"^(답글\s*\d+개|좋아요\s*\d+개|스토리|사진을 보냈습니다)", re.I)


@dataclass
class PostRecovery:
    """게시물 1건 복원 결과."""

    media_id: str
    probed: int = 0
    trigger: str | None = None
    repetition: float = 0.0
    is_campaign_signal: bool = False  # 캡션 트리거 or 반복률 높음
    offer: dict | None = None  # {text, url, label, hits, ratio, score}
    gate: dict | None = None
    drops: list = field(default_factory=list)
    samples: list = field(default_factory=list)  # 근거 원문(7일 후 파기)
    keyword_hits: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.offer or self.gate)

    @property
    def score(self) -> float:
        """등급 판정 점수 — **오퍼 기준**(사용자에게 중요한 건 자료 링크)."""
        if self.offer:
            return self.offer["score"]
        return self.gate["score"] if self.gate else 0.0

    @property
    def grade(self) -> str:
        s = self.score
        hits = (self.offer or self.gate or {}).get("hits", 0)
        if s >= GRADE_AUTO and hits >= MIN_SUPPORT_HITS:
            return "auto_draft"
        if s >= GRADE_REVIEW:
            return "needs_review"
        return "needs_review" if self.found else "excluded"

    @property
    def confirm_required(self) -> bool:
        """사용자에게 '이 링크가 맞나요?' 를 물어야 하는가.

        지지 표본이 부족하면 링크가 다른 캠페인 것일 수 있다. 자동채택 등급이 아니면 확인.
        """
        return self.found and self.grade != "auto_draft"


def _is_noise(text: str, has_url: bool) -> bool:
    if not text:
        return True
    if _SYSTEM_NOISE.match(text.strip()):
        return True
    if has_url:
        return False
    compact = placeholder_normalize(text).replace("{emoji}", "").replace(" ", "")
    return len(compact) < 6


def detect_signal(media: dict, commenters: list[dict]) -> tuple[str | None, float, bool]:
    """캠페인 서명 판정 → (트리거 단어, 반복률, 서명 여부).

    판별식은 **캡션 인용 트리거 + 댓글 반복률** 둘뿐이다. 공개답글 수는 쓰지 않는다
    (일상 게시물에 답글이 몰려 신호가 역방향이었다 — 7차 연구).
    """
    norms = [normalize_comment(u.get("text") or "") for u in commenters]
    norms = [n for n in norms if n]
    short = [n for n in norms if len(n) <= 15]
    top = Counter(short).most_common(1)
    repetition = (top[0][1] / len(norms)) if (top and norms) else 0.0

    caption = (media.get("caption") or "").strip()
    _, quoted = caption_keywords(caption)
    trigger = None
    for q in quoted:
        qn = q.replace(" ", "")
        # 캡션에 인용됐고 **실제로 댓글에도** 나타나야 트리거로 인정
        if qn and sum(1 for n in norms if qn in n.replace(" ", "")) >= 2:
            trigger = qn
            break
    if not trigger and top and top[0][1] >= 3:
        trigger = top[0][0].replace(" ", "")

    has_cta = bool(_CAP_CTA_RE.search(caption))
    signal = bool(trigger) or repetition >= 0.20 or (has_cta and repetition >= 0.10)
    return trigger, round(repetition, 3), signal


def recover_post(
    ctx: C.CollectContext,
    media: dict,
    *,
    is_own_dm,
    seed: int = C.SEED_PROBE,
    full: int = C.FULL_PROBE,
    big: int = C.BIG_PROBE,
    workers: int = C.PROBE_WORKERS,
) -> PostRecovery:
    """게시물 1건을 복원한다.

    Args:
        is_own_dm: ``(msg_id, text) -> bool`` — 우리(TurnFlow)가 보낸 DM 판정.
            ⚠️ 이 콜러블은 **DB 를 만지지 않아야** 한다(스레드에서 호출됨).
    """
    mid = media.get("id") or ""
    mts = C.parse_graph_time(media.get("timestamp"))
    ncmt = media.get("comments_count") or 0
    out = PostRecovery(media_id=mid)

    commenters, more = C.collect_commenters(ctx, mid, media_ts=mts, pages=1)
    if not commenters:
        return out
    trigger, repetition, signal = detect_signal(media, commenters)
    out.trigger, out.repetition, out.is_campaign_signal = trigger, repetition, signal

    tmpl: dict = {}
    probed_ids: set = set()

    def _probe(users: list[dict]) -> None:
        todo = [u for u in users if u["id"] not in probed_ids]
        if not todo:
            return
        # 병렬 조회 — 스레드는 HTTP 만. 집계는 아래 루프(메인 스레드)에서.
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda u: (u, C.fetch_outbound_for_commenter(ctx, u)), todo))
        for user, dms in results:
            probed_ids.add(user["id"])
            for d in dms:
                content = d.get("content") or {}
                urls = content.get("urls") or []
                if _is_noise(d["text"], bool(urls)):
                    continue
                if is_own_dm(d.get("msg_id"), d["text"]):
                    continue
                key = placeholder_normalize(d["text"])[:120]
                slot = tmpl.setdefault(
                    key,
                    {
                        "users": set(),
                        "text": content.get("text") or d["text"],
                        "urls": Counter(),
                        "labels": Counter(),
                        "drops": set(),
                        "gate": False,
                        "samples": [],
                    },
                )
                slot["users"].add(user["id"])
                for u_ in urls:
                    slot["urls"][u_] += 1
                for b in content.get("buttons") or []:
                    if b.get("label"):
                        slot["labels"][b["label"]] += 1
                for code in content.get("media_drops") or []:
                    slot["drops"].add(code)
                if content.get("carousel"):
                    slot["drops"].add("carousel")
                if content.get("has_gate_button"):
                    slot["gate"] = True
                if len(slot["samples"]) < 3:
                    slot["samples"].append(
                        {"text": d["text"][:400], "created_time": d.get("created_time", "")}
                    )

    order = C.order_probe_targets(commenters, trigger)
    _probe(order[:seed])

    if tmpl:
        cap = big if ncmt >= C.BIG_COMMENTS else full
        _probe(order[:cap])
    elif signal and more:
        # 서명은 강한데 0건 → 댓글을 캠페인 기간까지 파고 재시도.
        # 실측: 대형 게시물 4개가 이 단계에서 **1번째 사람**에 복원됐다.
        commenters, _ = C.collect_commenters(
            ctx, mid, media_ts=mts, pages=C.COMMENTS_OLDEST_MAX_PAGES
        )
        if commenters:
            trigger, repetition, signal = detect_signal(media, commenters)
            out.trigger, out.repetition, out.is_campaign_signal = trigger, repetition, signal
            order = C.order_probe_targets(commenters, trigger)
            _probe(order[:seed])
            if tmpl:
                _probe(order[: (big if ncmt >= C.BIG_COMMENTS else full)])

    out.probed = max(len(probed_ids), 1)
    if not tmpl:
        return out

    def _pack(slot) -> dict:
        hits = len(slot["users"])
        url = slot["urls"].most_common(1)[0][0] if slot["urls"] else ""
        label = slot["labels"].most_common(1)[0][0] if slot["labels"] else ""
        return {
            "text": slot["text"],
            "url": url,
            "label": label,
            "hits": hits,
            "ratio": round(hits / out.probed, 3),
            "score": round(wilson_lower_bound(hits, out.probed), 3),
            "drops": sorted(slot["drops"]),
            "samples": slot["samples"],
        }

    best_offer = best_gate = None
    for slot in tmpl.values():
        packed = _pack(slot)
        if packed["url"]:
            if best_offer is None or packed["hits"] > best_offer["hits"]:
                best_offer = packed
        else:
            if best_gate is None or packed["hits"] > best_gate["hits"]:
                best_gate = packed
                best_gate["is_gate"] = slot["gate"]
    out.offer, out.gate = best_offer, best_gate

    drops: Counter = Counter()
    for p in (best_offer, best_gate):
        for code in (p or {}).get("drops", []):
            drops[code] += 1
    # 게이트가 있는데 오퍼를 못 찾았다 = 2단 구조의 뒷부분이 빠졌다
    if best_gate and not best_offer:
        drops["message_sequence"] += 1
    out.drops = [{"code": k, "count": v} for k, v in drops.items()]
    out.samples = ((best_offer or {}).get("samples") or []) + (
        (best_gate or {}).get("samples") or []
    )[:2]

    if trigger:
        out.keyword_hits = {
            trigger: sum(1 for u in commenters if trigger in (u.get("text") or "").replace(" ", ""))
        }
    return out


def build_own_dm_matcher(sent_mids: set, sent_fps: set, tmpl_norms: list):
    """자기(TurnFlow) 발송 판정 콜러블. DB 를 미리 읽어 넘겨받는다(스레드 안전).

    첨부 DM 은 ``message`` 가 비어 지문이 안 잡히던 문제가 있었는데,
    추출기를 거치면 제목 지문이 살아나 2층 방어가 복구된다(실측 225/225).
    """
    import difflib

    def _is_own(msg_id, text) -> bool:
        if msg_id and msg_id in sent_mids:
            return True
        if not text:
            return False
        if fingerprint(text) in sent_fps:
            return True
        n = placeholder_normalize(text)
        for tn in tmpl_norms:
            if abs(len(n) - len(tn)) / max(len(n), len(tn), 1) > 0.30:
                continue
            if difflib.SequenceMatcher(None, n, tn).ratio() >= 0.92:
                return True
        return False

    return _is_own


# ── 예상 소요 시간 ─────────────────────────────────────────────────────────
# 실측 단가(prod, 순차): 대화 조회 1.05~1.29초 · 댓글 1페이지 0.84초.
# 병렬 4워커에서 게시물당 5.4~6.3초로 수렴했다(성공률 높은 계정 기준).
SECONDS_PER_POST = 6.0
SECONDS_PER_POST_SLOW = 10.0  # 실패가 많아 2·3단계까지 도는 계정


def estimate_seconds(media_with_comments: int, *, workers: int = C.PROBE_WORKERS) -> dict:
    """게시물 수만으로 예상 소요를 계산한다(1단계).

    게시물 수가 곧 비용이다 — 호출 1건이 1초를 넘어 총 시간은 호출 수에 비례한다.
    """
    n = max(int(media_with_comments), 0)
    low = int(n * SECONDS_PER_POST)
    high = int(n * SECONDS_PER_POST_SLOW)
    return {
        "seconds": low,
        "seconds_max": high,
        "media_with_comments": n,
        "per_post_seconds": SECONDS_PER_POST,
        "workers": workers,
    }
