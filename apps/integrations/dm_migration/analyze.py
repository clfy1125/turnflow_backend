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


EMOJI_TOKEN = "«emoji»"


def comment_key(text: str) -> str:
    """댓글 군집화용 키 — **이모지만 있는 댓글도 한 종류로 살려둔다.**

    ``normalize_comment`` 은 이모지를 지운다. 그래서 "🙌" 같은 댓글이 빈 문자열이 되고,
    호출부가 빈 것을 버리면서 **그 댓글은 존재 자체가 사라졌다.** "아무 댓글이나 달면 DM"
    캠페인은 사람들이 이모지 하나만 달기 때문에, 그런 게시물은 댓글이 0개인 것처럼 보였다.
    여기서는 이모지-only 를 하나의 키로 묶어 반복률 계산에 포함시킨다.
    """
    n = normalize_comment(text)
    if n:
        return n
    return EMOJI_TOKEN if (text or "").strip() else ""


def comment_shape(texts: list[str], *, tiny_chars: int = 3) -> dict:
    """댓글 뭉치의 '모양' — 캠페인 지문을 숫자로.

    캠페인 게시물의 댓글은 **짧고 서로 비슷하다**(키워드 복붙·이모지). 일상 게시물의 댓글은
    길고 제각각이다. 실측(@highestlevel33 459개): 복붙 20%+ 는 확실한 캠페인의 99%에서
    나오고 오탐의 29%에서만 나온다. 3자 이하 초단문 30%+ 는 64% vs 17%.
    """
    keys = [k for k in (comment_key(t) for t in texts) if k]
    n = len(keys) or 1
    top = Counter(keys).most_common(1)
    stripped = [normalize_comment(t) for t in texts if (t or "").strip()]
    emoji_only = sum(1 for s in stripped if not s)
    tiny = sum(1 for s in stripped if 0 < len(s) <= tiny_chars)
    return {
        "total": len(keys),
        "repetition": round((top[0][1] / n) if top else 0.0, 3),
        "top_key": top[0][0] if top else "",
        "top_count": top[0][1] if top else 0,
        "emoji_only_ratio": round(emoji_only / n, 3),
        "tiny_ratio": round((emoji_only + tiny) / n, 3),
    }


# 어느 게시물에나 나오는 말 — 내용 대조에서 빼야 우연 일치가 안 생긴다.
_GENERIC_WORDS = {
    "감사합니다",
    "감사",
    "안녕하세요",
    "여러분",
    "댓글",
    "디엠",
    "링크",
    "자료",
    "확인",
    "보내드릴게요",
    "보내드려요",
    "드릴게요",
    "드려요",
    "드립니다",
    "받으세요",
    "신청",
    "팔로우",
    "팔로",
    "무료",
    "지금",
    "바로",
    "아래",
    "여기",
    "그리고",
    "하지만",
    "제가",
    "저는",
    "이제",
    "정말",
    "너무",
    "많이",
    "합니다",
    "했습니다",
    "있습니다",
    "없습니다",
    "instagram",
    "http",
    "https",
    "www",
    "com",
}
_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")
_MATCH_STEM = 4  # 한국어는 조사가 붙는다 — 앞 4글자로 대조해야 '가이드북을' ↔ '가이드북' 이 걸린다


def content_tokens(text: str) -> list[str]:
    """대조용 낱말 — 흔한 말은 뺀다."""
    out, seen = [], set()
    for t in _TOKEN_RE.findall(text or ""):
        t = t.casefold()
        if len(t) < 3 or t in _GENERIC_WORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def content_match(caption: str, dm_text: str, trigger: str | None = None) -> list[str]:
    """**게시물 글과 DM 문구가 같은 이야기인가** — 겹치는 고유 낱말을 돌려준다.

    지지비율(몇 명이 받았나)만으로는 도달률 낮은 게시물이 억울하게 잘린다. 받은 사람이
    1명이어도 캡션이 "AI 툴 7가지 정리했어요" 이고 DM 이 "요청하신 AI 툴 7가지 자료" 면
    그건 이 게시물 캠페인이 맞다. 사람이 눈으로 하는 판단을 그대로 옮긴 것.

    한국어는 조사가 붙으므로 앞 4글자로 대조한다('가이드북을' ↔ '가이드북').
    흔한 말(감사합니다·자료·링크…)은 제외해야 아무 DM 이나 걸리지 않는다.
    """
    dm_norm = (dm_text or "").casefold()
    if not dm_norm:
        return []
    matched = []
    if trigger and len(trigger) >= 2 and trigger.casefold() in dm_norm:
        matched.append(trigger)
    for t in content_tokens(caption):
        stem = t[:_MATCH_STEM]
        if stem in dm_norm and t not in matched:
            matched.append(t)
    return matched


# ── 개인 대화 DM 걸러내기 ─────────────────────────────────────────────
# 인플루언서는 팬과 1:1 잡담도 한다("존맛탱이죠 ㅎㅎ", "행복한 명절되세용", "응원드립니다🔥").
# 그 사람이 마침 이 게시물에도 댓글을 달았으면 그 잡담이 '캠페인 문구' 로 잡힌다.
# **정보 전달 목적이 없고 캠페인화해도 인플루언서가 얻을 게 없는 말** 은 후보가 되면 안 된다.
# (사장님 검수, 2026-08-17: 애매 59건 중 29건이 이런 개인 대화였다. 확실한 캠페인
#  159건에는 하나도 안 걸린다 — 실측으로 부작용 0 확인.)
_CHAT_RE = re.compile(r"ㅋㅋ|ㅎㅎ|ㅠㅠ|ㅜㅜ|헤헤|앜|넵|넹")
_SOCIAL_RE = re.compile(
    r"응원|고마워|고맙|감사해|명절|행복|축하|멋져|멋지|화이팅|파이팅|반가|친해|만나|"
    r"소통|잘\s*봤|귀여|사랑|축복|건강하",
    re.I,
)
# 뭔가 **주겠다**는 말 — 이게 있으면 잡담이 아니라 캠페인 쪽이다.
_OFFERISH_RE = re.compile(
    r"자료|링크|가이드|정리|보내드|받아|신청|다운|무료|템플릿|특강|전자책|프롬프트|"
    r"강의|클래스|노하우|비법|공유",
    re.I,
)
_PERSONAL_Q_RE = re.compile(r"까요\?|나요\?|괜찮으시|혹시\s|어떻게\s")
# 팔로우 게이트 문구 — **짧고 링크도 없지만 캠페인의 일부다.**
# "팔로우 확인 부탁드려요"(11자)는 잡담 규칙에 그대로 걸린다. 게이트를 죽이면 2단 구조
# 캠페인(게이트 → 오퍼)의 앞부분이 통째로 사라진다.
_GATE_RE = re.compile(r"팔로(우|잉)?.{0,6}(확인|눌러|클릭|해주|했|하시)|구독.{0,4}확인|인증", re.I)
PERSONAL_MAX_LEN = 60
PERSONAL_BARE_LEN = 25


def is_personal_dm(text: str, *, has_url: bool = False, has_button: bool = False) -> bool:
    """캠페인 문구가 될 수 없는 **개인 대화**인가.

    아래 중 하나라도 있으면 개인 대화로 보지 않는다 — 전부 '자동화된 전달' 의 표식이다.
        · 링크        · 버튼        · 뭔가 주겠다는 말        · 팔로우 확인(게이트)
    남는 것(링크도 버튼도 없고 줄 것도 없는 짧은 잡담·인사·응원)만 걸러낸다.
    """
    t = (text or "").strip()
    if not t or has_url or has_button:
        return False
    if _OFFERISH_RE.search(t) or _GATE_RE.search(t):
        return False
    if len(t) <= PERSONAL_MAX_LEN and (
        _CHAT_RE.search(t) or _SOCIAL_RE.search(t) or _PERSONAL_Q_RE.search(t)
    ):
        return True
    # 링크도 버튼도 없고 줄 것도 없는 아주 짧은 말 — 정보 전달이 아니다.
    return len(t) <= PERSONAL_BARE_LEN


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


# ══════════════ 발신 DM 내용 추출 (단일 소스) ══════════════
#
# 버튼/템플릿 DM 은 ``message`` 가 빈 문자열이고 본문이 ``attachments`` 안에 들어간다.
# 도구마다 래퍼 키가 다를 수 있으므로(4차 연구: media_url·subtitle·image_data 변종 실재)
# **스키마 무관 재귀 순회**로 텍스트키/URL키를 수집한다. 미지 포맷도 통과시키는 게 목적.
# 이 함수를 거치지 않고 ``msg["message"]`` 를 직접 읽는 코드를 만들지 말 것.

# 첨부에서 본문으로 쓸 수 있는 텍스트 키 / 링크로 쓸 수 있는 URL 키
_ATT_TEXT_KEYS = {"title", "subtitle", "text", "name", "description", "caption"}
_ATT_URL_KEYS = {"url", "media_url", "link", "file_url", "image_url", "permalink"}
# 미디어 첨부 판정용 (프론트 transfer.drops 의 attachment_* 코드로 매핑)
_MEDIA_ATT_KEYS = {
    "image_data": "attachment_image",
    "video_data": "attachment_video",
    "file_url": "attachment_file",
}


def extract_dm_content(msg: dict) -> dict:
    """발신 DM 1건 → 구조화된 내용.

    Returns:
        {
          "text": str,             # 본문(평문 or 템플릿 제목)
          "buttons": [{"label","url","type"}],
          "urls": [str],           # 본문·버튼·첨부에서 발견된 모든 URL
          "media_drops": [str],    # attachment_image / attachment_video / attachment_file
          "carousel": bool,        # 카드 2장 이상(넘겨보는 형태)
          "has_gate_button": bool, # url 없는 postback 버튼 = 팔로우 확인 게이트
          "kind": "text" | "template" | "media_only" | "empty",
        }
    """
    out = {
        "text": "",
        "buttons": [],
        "urls": [],
        "media_drops": [],
        "carousel": False,
        "has_gate_button": False,
        "kind": "empty",
    }
    if not isinstance(msg, dict):
        return out

    body = (msg.get("message") or "").strip()
    if body:
        out["text"] = body
        out["kind"] = "text"
        out["urls"] = [u.rstrip(").,") for u in _URL_RE.findall(body)]

    data = ((msg.get("attachments") or {}).get("data") or []) if msg.get("attachments") else []
    if len(data) > 1:
        out["carousel"] = True

    texts: list[str] = []
    urls: list[str] = list(out["urls"])

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if kl in _MEDIA_ATT_KEYS and v:
                    code = _MEDIA_ATT_KEYS[kl]
                    if code not in out["media_drops"]:
                        out["media_drops"].append(code)
                if isinstance(v, str) and v.strip():
                    if kl in _ATT_URL_KEYS or v.startswith("http"):
                        urls.append(v.strip())
                    elif kl in _ATT_TEXT_KEYS:
                        texts.append(v.strip())
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for att in data:
        gt = att.get("generic_template") if isinstance(att, dict) else None
        if isinstance(gt, dict):
            for c in gt.get("cta") or []:
                if not isinstance(c, dict):
                    continue
                btn = {
                    "label": (c.get("title") or "").strip(),
                    "url": (c.get("url") or "").strip(),
                    "type": (c.get("type") or "").strip(),
                }
                out["buttons"].append(btn)
                # 링크 없는 버튼 = 도구 서버로 돌아가는 콜백 = 팔로우 확인 게이트.
                # cta.type 은 Meta 표준이라 도구 종류와 무관하게 판별된다(4차 연구).
                if not btn["url"] and (btn["type"] == "postback" or not btn["type"]):
                    out["has_gate_button"] = True
        walk(att)

    if not out["text"] and texts:
        out["text"] = texts[0]
        out["kind"] = "template"
    elif out["text"] and data:
        out["kind"] = "template"
    # dedupe (순서 보존)
    seen: set[str] = set()
    out["urls"] = [u for u in urls if not (u in seen or seen.add(u))]
    if not out["text"] and (out["media_drops"] or data):
        out["kind"] = "media_only"
    return out


def dm_text_for_match(msg: dict) -> str:
    """군집화·지문 비교용 텍스트 (본문 + 버튼문구 + URL 을 한 줄로)."""
    c = extract_dm_content(msg)
    parts = [c["text"]] + [b["label"] for b in c["buttons"] if b["label"]] + c["urls"]
    seen: set[str] = set()
    return " ".join(p for p in parts if p and not (p in seen or seen.add(p))).strip()


# Meta 가 정한 DM 본문 한도 — 우리가 고른 값이 아니다.
#   버튼 카드(button template): text **640자**
#   일반 텍스트:                UTF-8 **1000바이트** (한글 ≈ 333자)
# ⚠️ 링크 버튼을 붙이면 한도가 **오히려 늘어난다**(한글 333자 → 640자).
#    그래서 복원한 링크는 본문에 박지 않고 버튼으로 올리는 편이 길이에도 유리하다.
DM_TEXT_MAX_BYTES = 1000
BUTTON_TEMPLATE_TEXT_MAX = 640


def fit_dm_text(text: str, *, has_button: bool) -> tuple[str, dict | None]:
    """DM 본문을 포맷별 한도에 맞춰 자른다. (맞춘 문구, 초과정보 or None)

    타사 도구는 **여러 통으로 쪼개서** 보내기 때문에 한 통이 우리 한도를 넘는 원문이 존재한다
    (실측: 복원된 캠페인이 게이트 DM + 오퍼 DM 2통 구조였다). 그대로 넣으면 생성이 400 으로
    실패하므로, 여기서 잘라 넣고 **얼마나 잘렸는지를 초과정보로 돌려준다**
    (프론트 ``transfer.drops`` 의 ``opening_too_long``).

    자를 때는 문장 경계(``. ! ? \\n``)를 우선해 말이 끊기지 않게 한다.
    """
    t = (text or "").strip()
    if not t:
        return "", None

    if has_button:
        over = len(t) > BUTTON_TEMPLATE_TEXT_MAX
        limit_desc = {"limit": BUTTON_TEMPLATE_TEXT_MAX, "unit": "chars", "format": "button_card"}
        if not over:
            return t, None
        cut = t[:BUTTON_TEMPLATE_TEXT_MAX]
    else:
        raw = t.encode("utf-8")
        over = len(raw) > DM_TEXT_MAX_BYTES
        limit_desc = {"limit": DM_TEXT_MAX_BYTES, "unit": "bytes", "format": "plain_text"}
        if not over:
            return t, None
        cut = raw[:DM_TEXT_MAX_BYTES].decode("utf-8", errors="ignore")

    # 문장 경계로 되감기 (너무 많이 버리지 않는 선에서)
    boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"), cut.rfind("\n"))
    if boundary >= len(cut) * 0.6:
        cut = cut[: boundary + 1]
    cut = cut.rstrip()
    detail = dict(
        limit_desc,
        original_chars=len(t),
        original_bytes=len(t.encode("utf-8")),
        kept_chars=len(cut),
    )
    return cut, detail


def wilson_lower_bound(hits: int, n: int, z: float = 1.96) -> float:
    """지지비율의 95% 신뢰하한.

    표본이 작으면 자동으로 강등된다 — 1/2 → 0.09, 3/3 → 0.44, 10/10 → 0.72.
    실측 정밀도(1~2명 9~14% · 3~4명 43% · 5명+ 95%)와 거의 일치해,
    '비율'과 '표본 크기'를 하나의 값으로 합칠 수 있다.
    """
    if n <= 0:
        return 0.0
    p = hits / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5)
    return max(0.0, (center - margin) / denom)


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
