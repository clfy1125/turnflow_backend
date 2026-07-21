"""DM 캠페인 이전 — 순수 파이썬 분석(정규화·댓글 증거·DM 템플릿 군집화·매칭).

외부 무거운 의존(sklearn/numpy/rapidfuzz) 없이 stdlib(difflib)만 쓴다. 템플릿 DM 은
'의미적 이웃'이 아니라 '근접 중복'이라 difflib 로 충분하고, 규모(대화당 ~20개, 최대
~2000 메시지)에서 수 초 내 완료된다. difflib 미도입 근거·복잡도는 계획서 §4 참조.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime, timedelta

# ── 정규화 정규식 (invisible/이모지는 명시적 \u 이스케이프로 — 소스에 보이지 않는 문자 금지) ──
_ZW_RE = re.compile("[​-‍﻿]")  # ZWSP/ZWNJ/ZWJ/BOM
# 흔한 이모지 블록 + variation selector + 심볼/화살표.
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U0001f000-\U0001f0ff"
    "\U00002600-\U000027bf"
    "\U0000fe00-\U0000fe0f"
    "\U0001f1e6-\U0001f1ff"
    "\U00002b00-\U00002bff"
    "\U00002190-\U000021ff"
    "\U00002300-\U000023ff"
    "]+",
    flags=re.UNICODE,
)
_URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b\S+\.(?:com|net|co|kr|io|me|link|shop)/\S*)", re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}")
_MENTION_RE = re.compile(r"@[\w.]+")
_REPEAT_RE = re.compile(r"([ㅋㅎㅠㅜ!~.?])\1{1,}")
_CAPTION_CTA_RE = re.compile(r"댓글에|댓글로|남겨\s?주|남기면|적어\s?주|입력하|디엠|\bDM\b", re.I)
_QUOTED_RE = re.compile(r"[\"'“”‘’「」『』\[\]«»]([^\"'“”‘’「」『』\[\]«»]{1,15})")

# 트리거 키워드로 오탐하기 쉬운 일반 칭찬/리액션 (top_phrases 에서 제외).
_STOPWORDS = {
    "ㅋㅋ",
    "ㅎㅎ",
    "ㅠㅠ",
    "ㅜㅜ",
    "좋아요",
    "예뻐요",
    "예쁘다",
    "멋져요",
    "멋있어요",
    "대박",
    "대박이에요",
    "화이팅",
    "파이팅",
    "잘봤어요",
    "잘 봤어요",
    "잘봤습니다",
    "잘 봤습니다",
    "최고",
    "최고예요",
    "감사해요",
    "감사합니다",
    "고마워요",
    "축하해요",
    "사랑해요",
    "굿",
    "good",
    "nice",
    "wow",
    "오",
    "우와",
    "헐",
    "대애박",
    "귀여워요",
    "부럽다",
    "부러워요",
}

_MEDIA_TYPE_LABEL = {"REELS": "릴스", "FEED": "피드", "STORY": "스토리"}

# DM 템플릿 군집화에서 제외할 노이즈 — 팔로우게이트 버튼 에코·단순 인사·확인 답.
# 이런 건 캠페인 '첫 DM' 이 될 수 없다(mini_ai_ 실데이터에서 "팔로우했어요"·"안녕하세요 🙂"가
# 템플릿으로 잡혀 first_dm 초안이 오염되던 문제). {url} 페이로드가 있으면 노이즈로 보지 않는다.
_DM_NOISE_PHRASES = {
    "팔로우했어요",
    "팔로우 확인",
    "팔로우 완료",
    "팔로우 했어요",
    "팔로우",
    "확인",
    "확인했어요",
    "네",
    "넵",
    "넹",
    "예",
    "ok",
    "okay",
    "yes",
    "감사합니다",
    "감사해요",
    "안녕하세요",
    "안녕하세요 반갑습니다",
    "안녕하세요 반가워요",
}
_DM_NOISE_COMPACT = {p.replace(" ", "") for p in _DM_NOISE_PHRASES}


def is_noise_dm(norm: str) -> bool:
    """placeholder 정규화된 발신 DM 이 캠페인 템플릿이 될 수 없는 노이즈인지."""
    if not norm:
        return True
    if "{url}" in norm:
        return False  # 링크 페이로드는 의미 있음
    compact = norm.replace("{emoji}", "").replace(" ", "").strip()
    if norm in _DM_NOISE_PHRASES or compact in _DM_NOISE_COMPACT:
        return True
    # URL 없이 너무 짧으면(이모지/한두 단어) 자동화 캠페인 DM 으로 보기 어렵다.
    return len(compact) < 6


def _now_utc() -> datetime:
    # settings 시각을 직접 안 쓰고 파라미터 dt 를 받으므로, 여기선 UTC now 만 필요.
    from django.utils import timezone as _tz

    return _tz.now()


def parse_graph_time(value: str) -> datetime | None:
    """Graph created_time('2026-06-26T03:14:15+0000') → aware datetime. 실패 시 None."""
    if not value:
        return None
    v = str(value).replace("+0000", "+00:00").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def normalize_comment(text: str) -> str:
    """댓글 키워드 정규화 — NFKC·zero-width·이모지 제거·casefold·공백/반복 축약·edge punct 제거.

    한국어 스테밍은 하지 않는다(트리거 키워드는 런타임에서 literal 매칭이므로 동일 규칙).
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _ZW_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    t = t.casefold()
    t = re.sub(r"\s+", " ", t).strip()
    t = _REPEAT_RE.sub(r"\1", t)
    return t.strip(" .,!?~…\"'")


def placeholder_normalize(text: str) -> str:
    """DM 템플릿 군집화용 — 개인화 토큰(URL/이메일/전화/@/숫자/이모지)을 슬롯으로 치환 후 정규화.

    치환 순서 중요: URL(숫자 포함) → 이메일 → 전화 → 멘션 → 이모지 → 잔여 숫자런.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _URL_RE.sub(" {url} ", t)
    t = _EMAIL_RE.sub(" {email} ", t)
    t = _PHONE_RE.sub(" {phone} ", t)
    t = _MENTION_RE.sub(" {mention} ", t)
    t = _EMOJI_RE.sub(" {emoji} ", t)
    t = re.sub(r"\d{2,}", " {num} ", t)
    t = _ZW_RE.sub("", t)
    t = t.casefold()
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(text: str) -> str:
    """placeholder 정규화 텍스트의 안정 지문(자기발송 제외 매칭용)."""
    import hashlib

    return hashlib.sha1(placeholder_normalize(text).encode("utf-8")).hexdigest()


def caption_keywords(caption: str) -> tuple[bool, list[str]]:
    """캡션에서 CTA 여부 + 따옴표/괄호로 강조된 키워드 후보 추출."""
    if not caption:
        return False, []
    has_cta = bool(_CAPTION_CTA_RE.search(caption))
    kws = []
    for m in _QUOTED_RE.findall(caption):
        norm = normalize_comment(m)
        if norm and norm not in _STOPWORDS and 1 <= len(norm) <= 15:
            kws.append(norm)
    # dedupe 보존순
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return has_cta, out[:5]


def top_phrases(
    norm_texts: list[str], *, min_count: int = 3, top_n: int = 10
) -> list[tuple[str, int]]:
    """짧은 댓글 정규화형의 정확-빈도 + 긴 댓글 n-gram(백업). 스톱워드/이모지-only 제외."""
    short = [t for t in norm_texts if t and len(t) <= 15 and t not in _STOPWORDS]
    counter = Counter(short)
    # 긴 댓글 유니그램/바이그램(백업 — "정보 부탁드려요" 변형 흡수)
    for t in norm_texts:
        if not t or len(t) <= 15:
            continue
        toks = [w for w in t.split() if w and w not in _STOPWORDS]
        for w in toks:
            if 1 < len(w) <= 15:
                counter[w] += 1
        for a, b in zip(toks, toks[1:], strict=False):
            bg = f"{a} {b}"
            if len(bg) <= 15:
                counter[bg] += 1
    return [(p, c) for p, c in counter.most_common(top_n) if c >= min_count]


def comment_evidence(
    *,
    media: dict,
    comments: list[dict],
    own_account_id: str,
) -> dict:
    """게시물 1건의 댓글 증거 벡터 (LLM Stage A 입력 + 후보 근거).

    comments: [{"id","text","username","timestamp","parent_id","from":{"id"}}]
    반환: 캡션 CTA·상위문구·반복/짧은/유니크 비율·시간버킷·본인답글 신호 + 원본 샘플.
    """
    media_ts = parse_graph_time(media.get("timestamp", "")) or _now_utc()
    own_id = str(own_account_id or "")

    top_level, owner_replies = [], []
    for c in comments:
        frm = str((c.get("from") or {}).get("id") or "")
        if own_id and frm == own_id:
            owner_replies.append(c)  # 계정 본인 답글(공개답글 신호)
            continue
        if c.get("parent_id"):
            continue  # 대댓글 제외 — top-level 만 평가
        top_level.append(c)

    norms = [normalize_comment(c.get("text", "")) for c in top_level]
    norms = [n for n in norms if n]
    total = len(norms)
    short = [n for n in norms if len(n) <= 15]
    phrases = top_phrases(norms)

    repetition_ratio = (phrases[0][1] / total) if (phrases and total) else 0.0
    short_ratio = (len(short) / total) if total else 0.0
    distinct_ratio = (len(set(norms)) / total) if total else 0.0

    # 시간 버킷(게시 후 경과 시간) + 댓글 발생 '날짜' 집합(매칭 상관용)
    buckets = {"0-1h": 0, "1-6h": 0, "6-24h": 0, "1-3d": 0, "3-7d": 0, "7-30d": 0, ">30d": 0}
    comment_days: set[str] = set()
    for c in top_level:
        cts = parse_graph_time(c.get("timestamp", ""))
        if not cts:
            continue
        comment_days.add(cts.date().isoformat())
        hrs = (cts - media_ts).total_seconds() / 3600.0
        if hrs < 1:
            buckets["0-1h"] += 1
        elif hrs < 6:
            buckets["1-6h"] += 1
        elif hrs < 24:
            buckets["6-24h"] += 1
        elif hrs < 72:
            buckets["1-3d"] += 1
        elif hrs < 168:
            buckets["3-7d"] += 1
        elif hrs < 720:
            buckets["7-30d"] += 1
        else:
            buckets[">30d"] += 1

    has_cta, cap_kws = caption_keywords(media.get("caption", ""))
    owner_norm = Counter(
        normalize_comment(c.get("text", "")) for c in owner_replies if c.get("text")
    )
    owner_top = owner_norm.most_common(1)[0][0] if owner_norm else ""

    return {
        "media_id": media.get("id", ""),
        "caption_excerpt": (media.get("caption", "") or "")[:300],
        "media_type": media.get("media_product_type") or media.get("media_type") or "",
        "comments_count_total": media.get("comments_count", total),
        "comments_analyzed": total,
        "top_phrases": [{"text": p, "count": c} for p, c in phrases],
        "repetition_ratio": round(repetition_ratio, 3),
        "short_comment_ratio": round(short_ratio, 3),
        "distinct_ratio": round(distinct_ratio, 3),
        "time_buckets": buckets,
        "comment_days": sorted(comment_days),
        "caption_cta": has_cta,
        "caption_keywords": cap_kws,
        "account_replied_publicly": bool(owner_replies),
        "owner_reply_count": len(owner_replies),
        "owner_reply_top": owner_top[:200],
        # 원본 샘플(7일 후 파기 대상) — 근거 표시용.
        "sample_comments": [
            {"text": (c.get("text", "") or "")[:200], "timestamp": c.get("timestamp", "")}
            for c in top_level[:5]
        ],
    }


def keyword_hit_counts(comments: list[dict], keywords: list[str]) -> dict:
    """댓글에서 각 키워드(정규화 substring) 히트 수 — 근거 집계용."""
    norm_kws = [normalize_comment(k) for k in (keywords or []) if k]
    out = {k: 0 for k in norm_kws}
    for c in comments:
        n = normalize_comment(c.get("text", ""))
        for k in norm_kws:
            if k and k in n:
                out[k] += 1
    return out


# ══════════════ DM 템플릿 군집화 ══════════════


def cluster_templates(
    messages: list[dict],
    *,
    ratio_threshold: float = 0.87,
    prefilter: float = 0.75,
    len_tol: float = 0.30,
    min_support: int = 3,
    leader_cap: int = 150,
    rep_cap: int = 1500,
) -> list[dict]:
    """발신 DM 을 근접중복 템플릿으로 군집화 (정규화→해시그룹→그리디 리더 clustering).

    messages: [{"conv_id","msg_id","text","created_time"(dt|str)}]
    반환(min-support 통과 템플릿만): [{
        "template_id","normalized","representative","count","conversation_count",
        "conversation_ids"(list),"first_sent_at","last_sent_at","send_times"(list[iso]),
        "variable_slots"(list)}]
    """
    # 1) 정규화 + 정확-해시 그룹 (팔로우게이트 에코·인사 등 노이즈는 제외)
    groups: dict[str, dict] = {}
    for m in messages:
        norm = placeholder_normalize(m.get("text", ""))
        if not norm or len(norm) < 2 or is_noise_dm(norm):
            continue
        ct = m.get("created_time")
        ct = ct if isinstance(ct, datetime) else parse_graph_time(ct or "")
        g = groups.get(norm)
        if g is None:
            g = groups[norm] = {
                "norm": norm,
                "count": 0,
                "conv_ids": set(),
                "rep": m.get("text", "")[:400],
                "times": [],
            }
        g["count"] += 1
        if m.get("conv_id"):
            g["conv_ids"].add(m["conv_id"])
        if ct:
            g["times"].append(ct)

    # 2) 빈도순 그리디 리더 clustering (규모 상한: 상위 rep_cap 개만)
    ordered = sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:rep_cap]
    leaders: list[dict] = []
    for g in ordered:
        placed = False
        for L in leaders:
            ln, gn = L["norm"], g["norm"]
            if abs(len(gn) - len(ln)) / max(len(gn), len(ln), 1) > len_tol:
                continue
            sm = difflib.SequenceMatcher(None, gn, ln)
            if sm.quick_ratio() < prefilter:
                continue
            if sm.ratio() >= ratio_threshold:
                L["count"] += g["count"]
                L["conv_ids"] |= g["conv_ids"]
                L["times"].extend(g["times"])
                placed = True
                break
        if not placed and len(leaders) < leader_cap:
            leaders.append(dict(g))

    # 3) min-support(서로 다른 대화 ≥ N) 필터 + 정리
    out = []
    for i, L in enumerate(leaders):
        if len(L["conv_ids"]) < min_support:
            continue
        times = sorted(L["times"])
        out.append(
            {
                "template_id": f"t{i}",
                "normalized": L["norm"],
                "representative": L["rep"],
                "count": L["count"],
                "conversation_count": len(L["conv_ids"]),
                "conversation_ids": list(L["conv_ids"])[:50],
                "first_sent_at": times[0].isoformat() if times else "",
                "last_sent_at": times[-1].isoformat() if times else "",
                "send_times": [t.isoformat() for t in times],
                "variable_slots": sorted(set(re.findall(r"\{(\w+)\}", L["norm"]))),
            }
        )
    out.sort(key=lambda t: t["conversation_count"], reverse=True)
    return out


# ══════════════ 게시물 ↔ 템플릿 매칭 ══════════════


def _template_send_days(template: dict, start: datetime, end: datetime) -> tuple[set, int]:
    """윈도우 내 템플릿 발송 '날짜' 집합 + 건수."""
    days, cnt = set(), 0
    for iso in template.get("send_times", []):
        dt = parse_graph_time(iso) or (datetime.fromisoformat(iso) if iso else None)
        if dt and start <= dt <= end:
            days.add(dt.date().isoformat())
            cnt += 1
    return days, cnt


def match_candidate(candidate: dict, templates: list[dict]) -> dict | None:
    """게시물 후보 1건에 가장 잘 맞는 템플릿 + 점수/신호 반환 (없으면 None).

    candidate: {"media_id","timestamp"(dt),"keywords"(list),"comment_days"(list),
                "keyword_comment_count"(int)}
    """
    if not templates:
        return None
    media_ts = candidate.get("timestamp")
    if not isinstance(media_ts, datetime):
        media_ts = parse_graph_time(media_ts or "") or _now_utc()
    window_end = media_ts + timedelta(days=30)
    comment_days = set(candidate.get("comment_days") or [])
    kw_norms = [normalize_comment(k) for k in (candidate.get("keywords") or []) if k]
    kw_count = max(int(candidate.get("keyword_comment_count") or 0), 0)

    best = None
    for t in templates:
        send_days, send_cnt = _template_send_days(t, media_ts, window_end)
        union = comment_days | send_days
        jaccard = (len(comment_days & send_days) / len(union)) if union else 0.0
        window_overlap = (send_cnt / max(t["count"], 1)) if t.get("count") else 0.0
        time_score = 0.5 * jaccard + 0.5 * window_overlap
        volume_score = min(send_cnt / max(kw_count, 1), 1.0) if kw_count else 0.0
        kw_in_tmpl = 1.0 if any(k and k in t["normalized"] for k in kw_norms) else 0.0
        python_score = 0.5 * time_score + 0.3 * volume_score + 0.2 * kw_in_tmpl
        if best is None or python_score > best["python_score"]:
            best = {
                "template": t,
                "python_score": round(python_score, 3),
                "time_score": round(time_score, 3),
                "volume_score": round(volume_score, 3),
                "keyword_in_template": bool(kw_in_tmpl),
                "sends_in_window": send_cnt,
            }
    return best


def score_band(final_score: float, stage_a_confidence: float) -> str:
    """최종 점수 → 밴드. (auto_draft/needs_review/excluded — template_only 는 별도 경로)"""
    if final_score >= 0.70 and stage_a_confidence >= 0.70:
        return "auto_draft"
    if final_score >= 0.45:
        return "needs_review"
    return "excluded"


def pick_recovered_opening(dms: list[dict]) -> dict | None:
    """타겟 복원 DM 목록에서 대표 오프닝 1개 선정. URL 포함(페이로드) 우선 → 빈도순.

    dms: [{"text","created_time","recipient"}] (자기발송·노이즈는 상위에서 이미 제외).
    반환: {"representative","recipients","count","has_url","normalized"} 또는 None(전부 비어있음).
    """
    survivors = [d for d in dms if (d.get("text") or "").strip()]
    if not survivors:
        return None
    url_ones = [d for d in survivors if "{url}" in placeholder_normalize(d["text"])]
    pool = url_ones or survivors
    counts = Counter(placeholder_normalize(d["text"]) for d in pool)
    dom_norm, _ = counts.most_common(1)[0]
    rep = next(d["text"] for d in pool if placeholder_normalize(d["text"]) == dom_norm)
    recipients = len(
        {d.get("recipient") for d in pool if placeholder_normalize(d["text"]) == dom_norm}
    )
    return {
        "representative": rep,
        "recipients": recipients,
        "count": len(survivors),
        "has_url": bool(url_ones),
        "normalized": dom_norm,
    }
