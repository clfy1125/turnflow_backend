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

import concurrent.futures as cf
import json
import logging
import re
import time
from collections import Counter

import requests

from . import config
from .costs import CostLedger

logger = logging.getLogger(__name__)

REPEAT_THRESHOLD = 5
# v4: 분류 체계를 리드젠 퍼널 전용 → **범용**으로 재설계(카테고리가 바뀌어 구 캐시 무효).
CLASSIFY_CACHE_VERSION = 4
# 팔로워 인사이트를 만들기에 댓글이 이만큼 미만이면 "표본 부족" 경고를 리포트에 노출
MIN_COMMENTS_FOR_INSIGHT = 60
# 분류 실패가 이 비율을 넘으면 리포트에 "분류 신뢰도 낮음"을 노출한다(조용히 기타로 넘기지 않는다).
MAX_UNCLASSIFIED_PCT = 20

# ⚠️ **범용 분류 (2026-08-04 재설계).** 이전 8종은 `request`("코드 주세요")·`dm_not_received`
#    ("DM 안 왔어요") 중심의 **리드젠 퍼널 계정 전용**이었다. 그래서 일반 크리에이터 계정은
#    댓글 대부분이 정직하게 `other` 로 떨어졌다(@jinyongjin92 실측 91%). 실제 댓글을 표본
#    조사해 보니 논쟁·혐오·의견·경험담·외국어가 다수인데 담을 칸이 아예 없었다.
CATEGORIES = [
    # ── 콘텐츠 반응 (계정 종류와 무관하게 가장 흔함)
    "reaction",  # 짧은 감탄·웃음·이모지 ("ㅋㅋㅋ", "미쳤다", "쫀득")
    "praise",  # 콘텐츠 내용을 칭찬
    "empathy",  # 공감·동의
    "support",  # 크리에이터 응원·팬심
    # ── 대화·참여 (다음 소재와 커뮤니티 건강도의 근거)
    "curiosity",  # 영상 내용에 대한 추가 궁금증 ("저기 어디예요?", "다음편 언제")
    "opinion",  # 자기 주장·정보 보충·훈수 (상대를 공격하지 않음)
    "debate",  # 다른 시청자와의 반박·언쟁 (보통 @멘션 대댓)
    "hostile",  # 욕설·혐오·비방·조롱
    "personal_story",  # 자기 경험·사연 공유
    # ── 전환 신호 (퍼널 계정에서 중요 — 유지)
    "request",  # 자료·링크·혜택 요청
    "question",  # 조건·방법·가능 여부 질문 (실행/구매 결정형)
    "testimonial",  # 직접 해본 후기
    "dm_not_received",  # 약속한 자동 DM 미수신 문의
    # ── 기타
    "foreign",  # 한국어가 아닌 댓글 (해외 유입 신호)
    "other",
    "unclassified",  # 분류 호출 실패 — **기타와 구분한다**(리포트가 거짓말하지 않게)
]
CATEGORY_KO = {
    "reaction": "짧은 반응",
    "praise": "감탄·칭찬",
    "empathy": "공감",
    "support": "응원·팬심",
    "curiosity": "더 알고 싶다는 궁금증",
    "opinion": "자기 의견·정보 보충",
    "debate": "시청자끼리의 논쟁",
    "hostile": "욕설·비방",
    "personal_story": "자기 경험담",
    "request": "자료 요청",
    "question": "조건·방법 질문",
    "testimonial": "직접 해본 후기",
    "dm_not_received": "DM 못 받았다는 문의",
    "foreign": "외국어 댓글",
    "other": "기타",
    "unclassified": "분류 못함",
}

# 댓글 분위기 묶음 — "이 계정 댓글창이 어떤 곳인가"를 한 줄로 말해 주는 상위 축.
# 카테고리 하나하나의 %보다 이 묶음이 실제 피드백이 된다(논쟁 41% 같은 신호).
TONE_MAP = {
    "positive": {
        "label": "호응·공감",
        "cats": ["reaction", "praise", "empathy", "support", "testimonial"],
    },
    "engaged": {
        "label": "대화·참여",
        "cats": ["curiosity", "opinion", "personal_story"],
    },
    "conflict": {"label": "논쟁·비방", "cats": ["debate", "hostile"]},
    "funnel": {"label": "전환 신호", "cats": ["request", "question", "dm_not_received"]},
    "foreign": {"label": "외국어", "cats": ["foreign"]},
}

# 팔로워 동기 매핑 (코드 고정 — pct 는 코드 계산)
# dm_not_received 는 팔로워 '동기'가 아니라 발송 사고 신호이므로 어느 동기에도 넣지 않는다.
MOTIVATION_MAP = {
    "practical": {"label": "실용 — 따라해서 결과를 얻고 싶다", "cats": ["request", "testimonial"]},
    "question": {
        "label": "확신 — 나도 할 수 있는지 확인하고 싶다",
        "cats": ["question", "curiosity"],
    },
    "wow": {
        "label": "감탄 — 내용·결과물이 신기하다",
        "cats": ["praise", "empathy", "reaction"],
    },
    "fan": {"label": "응원 — 크리에이터 자체를 좋아한다", "cats": ["support"]},
    "voice": {
        "label": "발언 — 내 생각·경험을 말하고 싶다",
        "cats": ["opinion", "personal_story", "debate"],
    },
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
**모든 번호를 빠짐없이** 출력하세요.

[분류 기준 — 댓글의 '주된' 성격 1개. 애매하면 더 구체적인 쪽을 고르고, other 는 최후에만]
- reaction: 짧은 감탄·웃음·이모지만 ("ㅋㅋㅋㅋ", "미쳤다", "와", "쫀득❤️", "소름")
  → 내용 언급 없이 반응만 있으면 이것. 칭찬 대상이 분명하면 praise.
- praise: 콘텐츠 내용을 칭찬 ("이 영상 진짜 유익해요", "편집 미쳤네요", "꿀팁이네요")
- empathy: 공감·동의 ("저도 그랬어요", "완전 공감", "이거 맞아요", "인정합니다")
- support: 크리에이터 자체를 응원·팬심 ("항상 잘 보고 있어요", "팬이에요", "감사해요")
- curiosity: 영상 내용을 더 알고 싶다는 궁금증·요청 ("저기 어디예요?", "다음편 언제 나와요?",
  "그 사람 누구예요?", "2편 해주세요") → **다음 콘텐츠 소재 신호**
- opinion: 자기 주장·정보 보충·훈수. 특정 시청자를 공격하지 않음
  ("국제재판 하면 분쟁지역 인정이 됩니다", "무임승차 없애고 월 60회로 하자", "일반화는 위험해요")
- debate: **다른 시청자와 다투는 댓글** — 반박·언쟁·조롱. 보통 @멘션으로 시작
  ("@abc 뭐라는거야 애초에", "@abc 진짜 긁혔네ㅋㅋ", "댓글 단 사람들 정신착란인가")
  ⚠️ **@멘션이 있다는 것만으로 debate 가 아닙니다.** 상대에게 동의·감탄·질문을 하는 대댓글은
  각각 empathy / reaction / curiosity 입니다 ("@abc 와 진짜요? 생각보다 얼마 안되네요?"
  → reaction, "@abc 동의합니다" → empathy). **맞서는 태도**가 있을 때만 debate 입니다.
- hostile: 욕설·혐오·비방 (성별·지역·국적·외모 비하, 인신공격, 협박)
  → 대상이 시청자든 크리에이터든 제3자든 **표현이 공격적이면** 이것(debate 보다 우선).
- personal_story: 자기 경험·사연을 나눔 ("저희 아버지도 갈곳이 없어 지하철 타세요",
  "일본에서 고등학교 나왔는데 진짜 그렇게 가르쳐요")
- request: 자료·링크·혜택을 처음 요청 ("코드 주세요", "저도 보내주세요", "자료 받고 싶어요")
- question: 조건·방법·가능 여부 질문 — **실행/구매 결정과 연결** ("무료인가요?",
  "맥에서도 되나요?", "초보도 되나요?") → 영상 내용에 대한 단순 궁금증은 curiosity.
- testimonial: 직접 해본 후기·결과 ("따라해봤는데 됐어요", "저 이걸로 효과 봤어요")
- dm_not_received: **댓글 이벤트로 약속한 자동 DM·자료가 오지 않았다는 문의**
  ("DM 안 왔어요", "메시지가 안 오는데요", "댓글 남겼는데 아직 못 받았어요", "저는 왜 안 오나요")
  ⚠️ 아래는 이 분류가 **아닙니다**:
    · 유료 상품(전자책·강의·멤버십) 구매 후 생긴 문제 → question
    · 단순히 자료를 처음 요청 → request
- foreign: 한국어가 아닌 댓글 (일본어·중국어·영어 등). 내용 성격과 무관하게 언어로 판단.
- other: 위 어디에도 안 맞음 (광고 문구, 무의미한 문자열, 판단 불가)

댓글 목록:
"""


CHUNK_SIZE = 40
# 청크는 서로 독립 → 병렬. 댓글이 Graph 전환으로 99→900여 개가 되면서 순차 호출이 2분 넘게
# 걸렸다(22청크 × ~6초). 6 병렬이면 20초대로 줄고 Gemini 레이트리밋도 여유가 있다.
CLASSIFY_CONCURRENCY = 6
# 이 모델은 thinking 토큰을 쓰고 **그게 maxOutputTokens 예산을 공유한다**.
# 4096 이던 시절 실측: thoughts 3,933 + 출력 148 → finishReason=MAX_TOKENS 로 JSON 이 10번째
# 항목에서 잘렸고, 파싱 실패를 조용히 삼켜 **청크 40~50개가 통째로 '기타'** 가 됐다
# (@jinyongjin92 886개 중 808개=91% 기타). 분류는 추론이 필요 없는 작업이라 thinking 을 끈다.
CLASSIFY_MAX_OUTPUT_TOKENS = 8192
# 잘린 JSON 에서라도 살려내기 위한 항목 패턴 (부분 복구).
ITEM_RE = re.compile(r'\{\s*"i"\s*:\s*(\d+)\s*,\s*"c"\s*:\s*"([a-z_]+)"\s*\}')


def _parse_classifications(text: str, n: int) -> dict[int, str]:
    """응답 텍스트 → {index: category}. 잘린 JSON 도 **완전한 항목까지는 살린다**."""
    valid = set(CATEGORIES)
    out: dict[int, str] = {}
    try:
        for item in (json.loads(text) or {}).get("classifications", []):
            i, c = item.get("i"), item.get("c")
            if isinstance(i, int) and 0 <= i < n and c in valid:
                out[i] = c
        return out
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    # 파싱 실패(대개 MAX_TOKENS 로 잘림) → 정규식으로 완전한 항목만 회수
    for m in ITEM_RE.finditer(text or ""):
        i, c = int(m.group(1)), m.group(2)
        if 0 <= i < n and c in valid:
            out[i] = c
    return out


def classify_comments(pool: list, ledger: CostLedger, username: str) -> dict:
    """{comment_id: category}. 계정 단위 캐시. 실패분은 ``unclassified`` 로 **명시**한다.

    청크는 서로 독립이라 **병렬**로 돈다(댓글이 Graph 전환으로 99→900여 개가 되면서 순차
    호출이 2분 넘게 걸렸다). 캐시 갱신은 메인 스레드에서만 한다.
    """
    cache_p = config.FEATURE_DIR / f"comments_{username}@v{CLASSIFY_CACHE_VERSION}.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    todo = [c for c in pool if c["id"] not in cache]

    chunks = [todo[i : i + CHUNK_SIZE] for i in range(0, len(todo), CHUNK_SIZE)]
    chunks_failed = 0
    if chunks:
        with cf.ThreadPoolExecutor(max_workers=CLASSIFY_CONCURRENCY) as ex:
            for chunk, got, failed in ex.map(
                lambda idx_chunk: _classify_chunk(*idx_chunk, ledger), enumerate(chunks)
            ):
                chunks_failed += failed
                for i, c in enumerate(chunk):
                    cache[c["id"]] = got.get(i, "unclassified")

    config.FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    cache_p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    if chunks_failed:
        logger.warning("insta_report: 댓글 분류 청크 %s개에서 누락 발생", chunks_failed)
    return {c["id"]: cache.get(c["id"], "unclassified") for c in pool}


def _classify_chunk(chunk_no: int, chunk: list, ledger: CostLedger) -> tuple[list, dict, int]:
    """청크 1개 분류 → (chunk, {index: category}, 부분실패 0/1). 스레드에서 호출된다."""
    listing = "\n".join(f"{i}. {c['text'][:150]}" for i, c in enumerate(chunk))
    body = {
        "contents": [{"role": "user", "parts": [{"text": CLASSIFY_PROMPT + listing}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": CLASSIFY_SCHEMA,
            "maxOutputTokens": CLASSIFY_MAX_OUTPUT_TOKENS,
            # ⚠️ 되돌리지 말 것 — thinking 이 출력 예산을 먹어 응답이 잘린다(위 주석).
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    backoff = 2
    got: dict[int, str] = {}
    for _ in range(5):
        try:
            r = requests.post(
                f"{config.GEMINI_BASE}/models/{config.EXTRACT_MODEL}:generateContent",
                headers={
                    "x-goog-api-key": config.GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=120,
            )
        except requests.RequestException as e:
            logger.warning(
                "insta_report: 댓글 분류 호출 실패 chunk=%s %s", chunk_no, type(e).__name__
            )
            break
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
            note=f"chunk {chunk_no}",
        )
        cand = (d.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        got = _parse_classifications(parts[0].get("text", "") if parts else "", len(chunk))
        if len(got) < len(chunk):
            # 조용히 넘기지 않는다 — 이 결함이 91% 기타로 몇 달을 갔다.
            logger.warning(
                "insta_report: 댓글 분류 부분 실패 %s/%s (finish=%s, thoughts=%s, out=%s)",
                len(got),
                len(chunk),
                cand.get("finishReason"),
                u.get("thoughtsTokenCount"),
                u.get("candidatesTokenCount"),
            )
        break
    return chunk, got, int(len(got) < len(chunk))


def comment_stats(pool: list, classes: dict, canon: dict) -> dict:
    """도넛/동기/인용풀/기회 후보 — 전부 코드 계산."""
    by_cat: dict[str, list] = {c: [] for c in CATEGORIES}
    for c in pool:
        cat = classes.get(c["id"], "unclassified")
        by_cat.setdefault(cat, []).append(c)
    n = max(1, len(pool))
    counts = {k: len(v) for k, v in by_cat.items()}
    pcts = {k: round(v / n * 100) for k, v in counts.items()}

    motivations = []
    for key, m in MOTIVATION_MAP.items():
        cnt = sum(counts.get(c, 0) for c in m["cats"])
        motivations.append(
            {"key": key, "label": m["label"], "pct": round(cnt / n * 100), "count": cnt}
        )

    # 댓글창 분위기 — 카테고리 하나하나보다 이 묶음이 실제 피드백이 된다.
    tones = []
    for key, t in TONE_MAP.items():
        cnt = sum(counts.get(c, 0) for c in t["cats"])
        if cnt:
            tones.append(
                {"key": key, "label": t["label"], "count": cnt, "pct": round(cnt / n * 100)}
            )
    tones.sort(key=lambda x: -x["count"])

    unclassified = counts.get("unclassified", 0)
    unclassified_pct = round(unclassified / n * 100)

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
        "tones": tones,
        # 분류 실패는 '기타' 와 구분해 드러낸다 — 예전에는 실패가 기타로 섞여 리포트가
        # "기타 91%" 라고 거짓말했다.
        "unclassified": unclassified,
        "unclassified_pct": unclassified_pct,
        "classify_unreliable": unclassified_pct > MAX_UNCLASSIFIED_PCT,
        # DM 미수신 문의는 팔로워 니즈가 아니라 발송 사고 신호 → 인용 풀에서 제외
        "quote_pool": {
            cat: top_quotes(cat)
            for cat in CATEGORIES
            if by_cat.get(cat) and cat not in ("dm_not_received", "unclassified")
        },
        "dm_not_received_quotes": top_quotes("dm_not_received", 3),
        "dm_not_received_count": dm_issue,
        "dm_not_received_pct": round(dm_issue / max(1, len(pool)) * 100),
        "save_mentions": save_mentions,
    }
