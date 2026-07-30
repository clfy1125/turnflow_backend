"""팔로워 인사이트용 댓글 파이프라인 — 필터(코드) → 분류(Gemini) → 통계(코드).

필터(전부 결정적 코드 — DM 캠페인 오염 제거):
 1. 계정주 본인 댓글 제외
 2. 트리거 키워드 댓글 제외 — 캡션 cta_keyword + 영상피처 comment_keyword_verbatim
    과 정규화 일치(±부착어 4자)
 3. 반복 문구 자동 감지 — 정규화 후 동일 텍스트가 REPEAT_THRESHOLD회 이상이면
    캠페인/스팸으로 간주 (어떤 DM 툴을 썼든 서비스 무관하게 잡힘)
 4. 무의미 제외 — 이모지만·2자 미만·멘션(@친구태그)만
분류: 남은 댓글만 Gemini Flash 텍스트 배치(50개/콜, id 기반 귀속 — 영상과 달리
텍스트는 id 매핑이라 배치 안전). 인용은 이후 단계에서 quote_id 로만 참조.
"""

import json
import re
import time
from collections import Counter

import requests

from . import config
from .costs import CostLedger

REPEAT_THRESHOLD = 5
CLASSIFY_CACHE_VERSION = 3  # v3: dm_not_received 정의 정밀화(상품 고객지원 문의 분리)
# 팔로워 인사이트를 만들기에 댓글이 이만큼 미만이면 "표본 부족" 경고를 리포트에 노출
MIN_COMMENTS_FOR_INSIGHT = 60
CATEGORIES = [
    "request",
    "dm_not_received",
    "question",
    "praise",
    "empathy",
    "support",
    "testimonial",
    "other",
]
CATEGORY_KO = {
    "request": "자료 요청",
    "dm_not_received": "DM 못 받았다는 문의",
    "question": "질문",
    "praise": "감탄·칭찬",
    "empathy": "공감",
    "support": "응원·팬심",
    "testimonial": "후기·경험담",
    "other": "기타",
}
# 팔로워 동기 매핑 (코드 고정 — pct 는 코드 계산)
# dm_not_received 는 팔로워 '동기'가 아니라 발송 사고 신호이므로 어느 동기에도 넣지 않는다.
MOTIVATION_MAP = {
    "practical": {"label": "실용 — 따라해서 결과를 얻고 싶다", "cats": ["request", "testimonial"]},
    "question": {"label": "확신 — 나도 할 수 있는지 확인하고 싶다", "cats": ["question"]},
    "wow": {"label": "감탄 — 내용·결과물이 신기하다", "cats": ["praise", "empathy"]},
    "fan": {"label": "응원 — 크리에이터 자체를 좋아한다", "cats": ["support"]},
}

EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️‍]+")
MENTION_RE = re.compile(r"@[\w.]+")


def _norm(t: str) -> str:
    t = EMOJI_RE.sub("", t)
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    return t.lower()


def collect_triggers(canon: dict, features: dict) -> set:
    trig = set()
    for p in canon["posts"]:
        k = p["caption_features"].get("cta_keyword")
        if k:
            trig.add(_norm(k))
    for env in features.values():
        k = (env["feature"]["cta"].get("comment_keyword_verbatim") or "").strip()
        if k:
            trig.add(_norm(k))
    return {t for t in trig if t}


def filter_comments(canon: dict, triggers: set) -> dict:
    """반환: {pool: [{id,text,likes,shortcode,views}], stats: {...}}"""
    all_c, owner_n = [], 0
    for p in canon["posts"]:
        for c in p.get("comments_sample", []):
            if c["is_owner"]:
                owner_n += 1
                continue
            all_c.append({**c, "shortcode": p["shortcode"], "norm": _norm(c["text"])})

    freq = Counter(c["norm"] for c in all_c if c["norm"])
    pool, excluded = [], {"trigger": 0, "repeat": 0, "meaningless": 0}
    for c in all_c:
        n = c["norm"]
        if len(n) < 2 or not re.search(r"[가-힣a-z]", n):
            excluded["meaningless"] += 1
            continue
        if MENTION_RE.sub("", c["text"]).strip() == "":
            excluded["meaningless"] += 1
            continue
        if any(n == t or (t in n and len(n) <= len(t) + 4) for t in triggers):
            excluded["trigger"] += 1
            continue
        if freq[n] >= REPEAT_THRESHOLD:
            excluded["repeat"] += 1
            continue
        pool.append({k: c[k] for k in ("id", "text", "likes", "shortcode")})

    return {
        "pool": pool,
        "stats": {
            "total_collected": len(all_c) + owner_n,
            "owner_removed": owner_n,
            "excluded_trigger": excluded["trigger"],
            "excluded_repeat": excluded["repeat"],
            "excluded_meaningless": excluded["meaningless"],
            "analyzed": len(pool),
        },
    }


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "c": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["i", "c"],
            },
        }
    },
    "required": ["classifications"],
}

CLASSIFY_PROMPT = """인스타그램 게시물에 달린 팔로워 댓글들을 분류하세요. 각 댓글의 번호(i)와 분류(c)만 출력합니다.

[분류 기준 — 댓글의 '주된' 성격 1개]
- request: 자료·링크·혜택을 처음 요청 ("코드 주세요", "저도 보내주세요", "자료 받고 싶어요")
- dm_not_received: **댓글 이벤트로 약속한 자동 DM·자료가 오지 않았다는 문의**
  ("DM 안 왔어요", "메시지가 안 오는데요", "댓글 남겼는데 아직 못 받았어요",
   "다시 보내주세요", "저는 왜 안 오나요", "두 번 남겼어요")
  → 이 계정은 댓글 키워드로 자동 DM을 보내는데, 발송이 실패하면 이런 댓글이 달립니다.
  ⚠️ **아래는 이 분류가 아닙니다**:
    · 유료 상품(전자책·강의·멤버십) 구매 후 생긴 문제 → question
      (예: "전자책 구매했는데 노션 페이지가 없어졌어요", "결제했는데 강의가 안 열려요")
    · 단순히 자료를 처음 요청하는 것 → request
    이 분류는 **'댓글 남기면 보내드립니다'에 응답했는데 DM이 안 온 경우'만** 해당합니다.
- question: 방법·조건·가능 여부 질문 ("무료인가요?", "맥에서도 되나요?", "초보도 되나요?")
- praise: 감탄·칭찬 ("와 대박", "미쳤다", "꿀팁이네요")
- empathy: 공감·동의 ("저도 그랬어요", "완전 공감", "이거 맞아요")
- support: 크리에이터 응원·팬심 ("영상 잘 보고 있어요", "항상 감사해요", "팬이에요")
- testimonial: 직접 해본 후기·경험 ("따라해봤는데 됐어요", "저 이걸로 효과 봤어요")
- other: 위 어디에도 안 맞음

댓글 목록:
"""


def classify_comments(pool: list, ledger: CostLedger, username: str) -> dict:
    """{comment_id: category}. 계정 단위 캐시."""
    cache_p = config.FEATURE_DIR / f"comments_{username}@v{CLASSIFY_CACHE_VERSION}.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    todo = [c for c in pool if c["id"] not in cache]

    for chunk_start in range(0, len(todo), 50):
        chunk = todo[chunk_start : chunk_start + 50]
        listing = "\n".join(f"{i}. {c['text'][:150]}" for i, c in enumerate(chunk))
        body = {
            "contents": [{"role": "user", "parts": [{"text": CLASSIFY_PROMPT + listing}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": CLASSIFY_SCHEMA,
                "maxOutputTokens": 4096,
            },
        }
        backoff = 2
        for _ in range(5):
            r = requests.post(
                f"{config.GEMINI_BASE}/models/{config.EXTRACT_MODEL}:generateContent",
                headers={
                    "x-goog-api-key": config.GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=120,
            )
            if r.status_code in (429, 500, 503):
                time.sleep(backoff)
                backoff *= 2
                continue
            r.raise_for_status()
            d = r.json()
            u = d.get("usageMetadata", {})
            ledger.record_llm(
                "S4b_comments",
                config.EXTRACT_MODEL,
                u.get("promptTokenCount", 0),
                u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
                note=f"chunk {chunk_start//50}",
            )
            try:
                out = json.loads(d["candidates"][0]["content"]["parts"][0]["text"])
                for item in out.get("classifications", []):
                    i = item["i"]
                    if 0 <= i < len(chunk):
                        cache[chunk[i]["id"]] = item["c"]
            except (json.JSONDecodeError, KeyError):
                pass  # 이 청크는 미분류로 남김(other 처리)
            break

    config.FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    cache_p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return {c["id"]: cache.get(c["id"], "other") for c in pool}


def comment_stats(pool: list, classes: dict, canon: dict) -> dict:
    """도넛/동기/인용풀/기회 후보 — 전부 코드 계산."""
    by_cat: dict[str, list] = {c: [] for c in CATEGORIES}
    for c in pool:
        by_cat[classes.get(c["id"], "other")].append(c)
    n = max(1, len(pool))
    counts = {k: len(v) for k, v in by_cat.items()}
    pcts = {k: round(v / n * 100) for k, v in counts.items()}

    motivations = []
    for key, m in MOTIVATION_MAP.items():
        cnt = sum(counts[c] for c in m["cats"])
        motivations.append(
            {"key": key, "label": m["label"], "pct": round(cnt / n * 100), "count": cnt}
        )

    def top_quotes(cat, k=6):
        # 인용은 길이가 있는 것 우선(짧은 "저요" 류보다 맥락이 보이는 댓글)
        cands = sorted(by_cat[cat], key=lambda x: (-min(len(x["text"]), 60), -x["likes"]))
        return [{"quote_id": c["id"], "text": c["text"], "likes": c["likes"]} for c in cands[:k]]

    save_mentions = sum(1 for c in pool if "저장" in c["text"])
    dm_issue = counts.get("dm_not_received", 0)
    return {
        "counts": counts,
        "pcts": pcts,
        "n_analyzed": len(pool),
        "insufficient": len(pool) < MIN_COMMENTS_FOR_INSIGHT,
        "min_for_insight": MIN_COMMENTS_FOR_INSIGHT,
        "category_ko": CATEGORY_KO,
        "motivations": motivations,
        # DM 미수신 문의는 팔로워 니즈가 아니라 발송 사고 신호 → 인용 풀에서 제외
        "quote_pool": {
            cat: top_quotes(cat) for cat in CATEGORIES if by_cat[cat] and cat != "dm_not_received"
        },
        "dm_not_received_quotes": top_quotes("dm_not_received", 3),
        "dm_not_received_count": dm_issue,
        "dm_not_received_pct": round(dm_issue / max(1, len(pool)) * 100),
        "save_mentions": save_mentions,
    }
