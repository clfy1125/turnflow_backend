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
    EMOJI_TOKEN,
    caption_keywords,
    comment_shape,
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

# 캡션 행동유도. 어미 변화를 놓치면 안 된다 — 실측에서 "아무 댓글 **남기면**" 캠페인이
# "남겨" 만 보던 패턴에 안 걸려 통째로 탈락했다(점수 0.34, 기준 0.35).
_CAP_CTA_RE = re.compile(
    r"댓글에|댓글로|댓글\s*남|댓글\s*달|댓글\s*주|댓글\s*만|아무\s*댓글|남겨|남기|"
    r"입력|디엠|\bDM\b|dm\s*드|메시지\s*드",
    re.I,
)
# 인스타 시스템 알림·상투 문구 — 캠페인 DM 이 아니다.
_SYSTEM_NOISE = re.compile(r"^(답글\s*\d+개|좋아요\s*\d+개|스토리|사진을 보냈습니다)", re.I)


@dataclass
class PostRecovery:
    """게시물 1건 복원 결과."""

    media_id: str
    probed: int = 0
    trigger: str | None = None
    repetition: float = 0.0
    is_campaign_signal: bool = False  # 글·댓글 종합 판정 (judge_content)
    content_score: float = 0.0  # 콘텐츠 판정 점수 0~1
    content_reasons: list = field(default_factory=list)  # 어떤 신호가 걸렸나
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
        """등급 판정 점수 — **오퍼 기준**(사용자에게 중요한 건 자료 링크).

        키 접근이 아니라 get 을 쓴다 — 재개·이전 버전 캐시에서 온 기록은 필드가 빠져 있을
        수 있고, 여기서 KeyError 가 나면 잡 전체가 죽는다.
        """
        if self.offer:
            return float(self.offer.get("score") or 0.0)
        return float(self.gate.get("score") or 0.0) if self.gate else 0.0

    @property
    def grade(self) -> str:
        """등급. **DM 증거와 콘텐츠 판정을 함께 본다.**

        예전에는 DM 증거만 봤다. 그래서 두 방향으로 틀렸다(실측 @highestlevel33):
          · 글이 "캠페인 맞다" 는데 DM 을 못 찾아 **52개를 통째로 버렸다**
          · 글이 "캠페인 아니다" 는데 DM 한 통 나왔다고 **35개를 통과시켰다**
        이제 콘텐츠가 강하면 DM 이 없어도 후보로 내고(문구는 사용자가 작성), 콘텐츠가
        아니라는데 지지가 1~2명뿐이면 내린다(= 남의 게시물 DM 이 흘러든 것).
        """
        s = self.score
        hits = (self.offer or self.gate or {}).get("hits", 0)
        if not self.found:
            # DM 원문을 못 건진 건. 밴드는 excluded 를 유지하고(프론트 계약), 후보로 낼지는
            # 파이프라인이 content_score 로 정한다 — 글·댓글이 캠페인이라고 말하면 낸다.
            return "excluded"
        if s >= GRADE_AUTO and hits >= MIN_SUPPORT_HITS:
            return "auto_draft"
        if s >= GRADE_REVIEW:
            return "needs_review"
        # 지지가 얕다 — 콘텐츠가 뒷받침하면 검수 대상, 아니면 오귀속으로 보고 제외.
        if hits >= MIN_SUPPORT_HITS or self.is_campaign_signal:
            return "needs_review"
        return "excluded"

    @property
    def confirm_required(self) -> bool:
        """사용자에게 '이 링크가 맞나요?' 를 물어야 하는가.

        지지 표본이 부족하면 링크가 다른 캠페인 것일 수 있다. 자동채택 등급이 아니면 확인.
        DM 을 아예 못 찾아 콘텐츠만으로 낸 후보도 당연히 확인 대상이다(문구가 비어 있다).
        """
        return self.grade != "auto_draft" and self.grade != "excluded"


def _is_noise(text: str, has_url: bool) -> bool:
    if not text:
        return True
    if _SYSTEM_NOISE.match(text.strip()):
        return True
    if has_url:
        return False
    compact = placeholder_normalize(text).replace("{emoji}", "").replace(" ", "")
    return len(compact) < 6


# ── 콘텐츠만으로 보는 캠페인 판정 (가중치는 실측값) ────────────────────────
#
# DM 을 찾았는지와 **무관하게**, 게시물 글과 댓글 모양만 보고 "여기 캠페인이 돌았나" 를
# 판정한다. 이게 있어야 "글은 캠페인이라는데 DM 을 못 건진" 게시물을 버리지 않을 수 있다.
#
# 가중치는 감이 아니라 실측이다. @highestlevel33 459개를 "확실(받은 사람 3명+)" 147개와
# "오탐 의심(받은 사람 1~2명)" 59개로 갈라, 각 신호가 양쪽에서 몇 %나 나오는지 쟀다.
#
#   신호                  확실   의심   차이
#   댓글 복붙 20%+         99%   29%   +0.71
#   캡션 행동유도           96%   41%   +0.55
#   초단문(3자↓) 30%+      64%   17%   +0.47
#   캡션 제공약속           81%   51%   +0.30
#   계정 대댓글 '보냈다'      2%    7%   -0.05  ← 이 계정은 안 쓴다. 다른 계정 대비 낮은 가중치로만
#
# 대댓글 **수**는 쓰지 않는다 — 캠페인 게시물은 댓글이 수백 개라 첫 페이지에 답글이 안 잡히고,
# 규모를 맞춰 비교하면 차이가 82% vs 94% 로 사라진다(수집 창 편향).
CONTENT_WEIGHTS = {
    "repetition": 0.35,  # 같은 말 복붙
    "caption_cta": 0.30,  # "댓글에 ○○ 남겨주세요"
    "tiny_comments": 0.20,  # 이모지·초단문 위주
    "caption_offer": 0.10,  # "무료 자료 드려요"
    "owner_reply_sent": 0.15,  # 계정이 "DM 보내드렸어요" 라고 답글
}
CONTENT_CAMPAIGN_MIN = 0.35  # 이 이상이면 "캠페인으로 본다"
CONTENT_STRONG_MIN = 0.60  # 이 이상이면 DM 을 못 찾아도 후보로 낸다

_CAP_OFFER_RE = re.compile(
    r"무료|자료|전자책|가이드|템플릿|특강|드려요|드립니다|보내드|정리해|나눔|공유해", re.I
)
_REPLY_SENT_RE = re.compile(r"보내드|보냈|드렸|디엠|\bDM\b|확인해\s*주|메시지\s*확인", re.I)


@dataclass
class ContentVerdict:
    """게시물 글 + 댓글 모양만으로 본 캠페인 판정."""

    trigger: str | None = None
    repetition: float = 0.0
    score: float = 0.0
    reasons: list = field(default_factory=list)
    shape: dict = field(default_factory=dict)

    @property
    def is_campaign(self) -> bool:
        return self.score >= CONTENT_CAMPAIGN_MIN

    @property
    def is_strong(self) -> bool:
        return self.score >= CONTENT_STRONG_MIN


def judge_content(
    media: dict, commenters: list[dict], owner_replies: list[str] | None = None
) -> ContentVerdict:
    """게시물 글·댓글·계정 대댓글을 **종합**해 캠페인 여부를 판정한다.

    하나의 신호로 자르지 않는다 — 캠페인 방식이 계정마다 달라서(키워드 지정형 / 아무 댓글이나
    받는 형 / 팔로우 게이트형) 어느 하나만 보면 그 방식만 잡힌다.
    """
    caption = (media.get("caption") or "").strip()
    texts = [u.get("text") or "" for u in commenters]
    shape = comment_shape(texts)
    v = ContentVerdict(repetition=shape["repetition"], shape=shape)

    hits: dict = {}
    if shape["repetition"] >= 0.20:
        hits["repetition"] = 1.0 if shape["repetition"] >= 0.40 else 0.7
    if _CAP_CTA_RE.search(caption):
        hits["caption_cta"] = 1.0
    if shape["tiny_ratio"] >= 0.30:
        hits["tiny_comments"] = 1.0
    if _CAP_OFFER_RE.search(caption):
        hits["caption_offer"] = 1.0
    if owner_replies and any(_REPLY_SENT_RE.search(t or "") for t in owner_replies):
        hits["owner_reply_sent"] = 1.0

    v.score = round(sum(CONTENT_WEIGHTS[k] * w for k, w in hits.items()), 3)
    v.reasons = sorted(hits)

    # 트리거 단어 — 캡션이 인용한 것 우선, 없으면 가장 많이 복붙된 댓글.
    norms = [normalize_comment(t) for t in texts]
    norms = [n for n in norms if n]
    _, quoted = caption_keywords(caption)
    for q in quoted:
        qn = q.replace(" ", "")
        if qn and sum(1 for n in norms if qn in n.replace(" ", "")) >= 2:
            v.trigger = qn
            break
    if not v.trigger and shape["top_count"] >= 3 and shape["top_key"] != EMOJI_TOKEN:
        v.trigger = shape["top_key"].replace(" ", "")
    return v


def detect_signal(media: dict, commenters: list[dict]) -> tuple[str | None, float, bool]:
    """(하위 호환) → (트리거, 반복률, 캠페인 여부). 내부는 judge_content 가 판단한다."""
    v = judge_content(media, commenters)
    return v.trigger, v.repetition, v.is_campaign


def recover_post(
    ctx: C.CollectContext,
    media: dict,
    *,
    is_own_dm,
    seed: int = C.SEED_PROBE,
    full: int = C.FULL_PROBE,
    big: int = C.BIG_PROBE,
    campaign: int = C.CAMPAIGN_PROBE,
    workers: int = C.PROBE_WORKERS,
    probe: bool = True,
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
    # 댓글을 못 가져와도 **캡션만으로 판정은 남긴다.** 예전에는 여기서 그냥 return 해서
    # "댓글에 ○○ 남겨주세요" 라고 대놓고 쓰인 게시물이 점수 0 으로 사라졌다.
    verdict = judge_content(media, commenters or [])
    out.trigger, out.repetition = verdict.trigger, verdict.repetition
    out.is_campaign_signal = verdict.is_campaign
    out.content_score, out.content_reasons = verdict.score, verdict.reasons
    trigger = verdict.trigger
    if not commenters:
        return out
    if not probe:
        # 가벼운 경로 — 댓글이 적어 지지비율을 낼 수 없는 게시물. 판정만 하고 DM 은 안 본다
        # (표본이 1~7명이면 '몇 명이 같은 문구를 받았나' 가 의미를 못 가진다).
        return out

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
            cts = user.get("ts")
            for d in dms:
                content = d.get("content") or {}
                urls = content.get("urls") or []
                if _is_noise(d["text"], bool(urls)):
                    continue
                if is_own_dm(d.get("msg_id"), d["text"]):
                    continue
                # 댓글↔DM 시간차 — 수집 후 '이 DM 이 어느 게시물 것인가' 를 가리는 근거.
                # 문턱값으로 쓰면 안 갈린다(연달아 댓글 달면 둘 다 몇 초 안에 온다).
                # 여러 게시물이 같은 DM 을 주장할 때 **더 가까운 쪽**을 고르는 데 쓴다.
                dts = C.parse_graph_time(d.get("created_time"))
                gap = int((dts - cts).total_seconds()) if (dts and cts) else None
                key = placeholder_normalize(d["text"])[:120]
                slot = tmpl.setdefault(
                    key,
                    {
                        "users": set(),
                        "evidence": {},
                        "text": content.get("text") or d["text"],
                        "urls": Counter(),
                        "labels": Counter(),
                        "drops": set(),
                        "gate": False,
                        "samples": [],
                    },
                )
                slot["users"].add(user["id"])
                # 사용자당 가장 가까운 DM 1건만 근거로 둔다(팔로우게이트의 2통은 문구가
                # 달라 서로 다른 slot 에 들어가므로 여기서 잘리지 않는다).
                prev = slot["evidence"].get(user["id"])
                if prev is None or (gap is not None and abs(gap) < abs(prev.get("g") or 10**9)):
                    slot["evidence"][user["id"]] = {
                        "u": user["id"],
                        "m": d.get("msg_id") or "",
                        "g": gap,
                    }
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
    elif verdict.is_campaign:
        # ── 글·댓글이 "여기 캠페인 돌았다" 고 말하는데 씨앗에서 0건 ──
        # 예전에는 여기서 사실상 포기했다(댓글을 더 파도 다시 3명만 봤다). 그래서
        # @highestlevel33 에서 콘텐츠상 캠페인 215개 중 52개를 통째로 버렸다.
        # 실측(그 52개를 15명까지 조회): 28개에서 DM 이 나왔고 **8번째까지 보면 71%,
        # 10번째까지 71→82%** 가 걸린다. 3명은 구조적으로 모자란다.
        _probe(order[:campaign])
        if not tmpl and more:
            # 그래도 0건이면 댓글이 모자란 것 — 게시 직후 댓글러까지 파고 다시 본다.
            # 실측: 미복원 253개 중 34개가 댓글 수백~1만 개라 첫 페이지가 엉뚱한
            # (한참 뒤에 단) 사람만 보여주고 있었다.
            deep, _ = C.collect_commenters(
                ctx, mid, media_ts=mts, pages=C.COMMENTS_OLDEST_MAX_PAGES
            )
            if deep:
                verdict = judge_content(media, deep)
                out.trigger, out.repetition = verdict.trigger, verdict.repetition
                out.is_campaign_signal = verdict.is_campaign
                out.content_score, out.content_reasons = verdict.score, verdict.reasons
                commenters = deep
                order = C.order_probe_targets(deep, verdict.trigger)
                _probe(order[:campaign])
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
            # 누가·어느 메시지로·댓글과 몇 초 차이로 뒷받침했나 — 수집이 다 끝난 뒤
            # attribute.resolve 가 이걸 보고 게시물 간 중복 주장을 정리한다.
            "users": list(slot.get("evidence", {}).values()),
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
