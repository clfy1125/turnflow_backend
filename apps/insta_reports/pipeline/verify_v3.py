"""S7 검증 게이트 — v3 슬롯셋 (verify.py 의 파서·화이트리스트 재사용).

v3 추가 검증:
- fans_wants.quote_id 가 quote_pool 에 실존 (인용 조작 원천 차단 — 원문은 코드가 주입)
- top_posts_why.chips ⊆ 해당 rank 의 allowed_chips (그 영상에서 관찰된 특징만)
- motivation_descs 4키 전부/중복 없음
- has_features=false 게시물 why 의 화면 묘사 어휘 금지
"""

import re

from . import config
from .metrics import man
from .verify import (
    CAUSAL,
    FORBIDDEN,
    HANGUL_QUANT,
    HEDGE,
    HEDGE_REQ,
    _match,
    build_whitelist,
    tokenize_numbers,
)

EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF]")
SCREEN_WORDS = re.compile(r"(첫\s*문장|첫\s*3초|화면|자막|시작\s*장면|도입부)")

# 갈등을 **일부러 만들라는** 조언 탐지. 논쟁 현황을 '설명'하는 문장은 걸리지 않게
# (분위기 블록의 "논쟁·비방 26%" 같은 서술) 동사가 붙은 유도형만 잡는다.
CONFLICT_BAIT = re.compile(
    # ① 갈등 축을 소재로 지목 ② 찬반 편가르기 ③ 논쟁/어그로 + '만들라'는 동사
    r"(남녀|젠더|성별|지역|세대|정치|국적)\s*(갈등|대립|비하)"
    r"|찬반[^.]{0,10}(나[뉘뉠눌]|갈리)"
    r"|(논쟁|갈등|어그로|싸움)[^.]{0,14}(유도|유발|만들|일으키|부추|키우|늘리|선정|제작|다루)"
)

# ── 쉬운 말 강제 (핵심 게이트) ────────────────────────────────────────
# 리포트를 읽는 사람은 마케팅·데이터 용어를 모르는 인플루언서다.
# 내부 enum 번역·업계 용어·한자어 추상명사가 문장에 들어오면 반려하고 대체 표현을 알려준다.
# 따옴표 안(실제 영상 제목·댓글 인용)은 검사 제외.
JARGON_RULES = [
    (r"\bCTA\b|씨티에이", "'댓글 유도' 또는 \"'댓글에 OO 남겨주세요' 문구\""),
    (r"캡션", "'게시물 글' 또는 '글 내용'"),
    (r"훅|후킹|후크\b", "'첫 문장' 또는 '첫 마디'"),
    (r"손실\s*회피\w*", "\"'모르면 손해'라고 말하며 시작하는 영상\""),
    (r"결과\s*제시\w*|결과\s*먼저형", "'결과물을 먼저 보여주는 영상'"),
    (r"개수\s*예고형|리스트형|나열형", "\"'몇 가지'라고 개수를 예고하는 영상\""),
    (
        r"충격\s*경고형|경고형|질문형|설명형|경험담형|상황극형|혜택\s*제공형",
        "입력의 name(사람말 이름)을 그대로 쓰세요 — '~형' 같은 줄임말 금지",
    ),
    (r"인물\s*행동\w*|동작\s*시연형", "'손동작이나 시연 장면으로 시작하는 영상'"),
    (r"말하는\s*얼굴", "'사람이 카메라를 보고 말하며 시작하는 영상'"),
    (r"오프닝|인트로", "'영상 시작 부분'"),
    (r"썸네일", "'표지 화면'"),
    (r"표본", "'영상 수' 또는 '댓글 수'"),
    (r"중앙값|평균값|분위수", "'평소 조회수' 또는 '터진 영상 포함 평균'"),
    (r"잠재력|파괴력|폭발력", "무엇이 얼마나 되는지 숫자로 직접 설명"),
    (
        r"최적화|고도화|제고|증대|극대화|활성화|일원화|패턴화|의무화|필수화",
        "쉬운 우리말 (늘리기·높이기·꼭 넣기 등)",
    ),
    (
        r"포지셔닝|페르소나|퍼널|리텐션|인게이지\w*|바이럴\w*|리드\b|컨버전|트래픽|콘텐츠\s*믹스",
        "쉬운 우리말",
    ),
    (r"니즈|타깃팅|벤치마킹|프레임워크|시너지|레버리지", "쉬운 우리말"),
    (r"유의미|상관\s*관계|변동성|편차", "쉬운 표현 ('차이가 크다' 등)"),
    (r"시그널", "'신호'"),
    (r"알고리즘", "인스타가 노출을 어떻게 정하는지는 알 수 없어요 — 언급하지 마세요"),
    # "전환"은 영상 편집 계정에선 정당한 말(장면 전환·트랜지션) — 마케팅 맥락만 금지
    (
        r"전환율|전환률|(구매|팔로우|매출|리드|고객)\s*전환",
        "실제로 관찰한 것만 (댓글 수·조회수 등)",
    ),
]
JARGON_COMPILED = [(re.compile(p), s) for p, s in JARGON_RULES]
QUOTED_RE = re.compile(r"[‘’'\"“”「」『』]([^‘’'\"“”「」『』]{1,120})[‘’'\"“”「」『』]")
# 어느 영상에나 붙는 뻔한 문구 — 2개 이상이거나 실제 인용이 없으면 반려
GENERIC_WHY = re.compile(
    r"(호기심을?\s*자극|궁금증을?\s*(유발|자극)|시각적으로\s*강렬|관심을?\s*(끌|모)"
    r"|신뢰를?\s*(주|더|얻)|흥미를?\s*유발|이목을?\s*집중|눈길을?\s*(끌|사로)"
    r"|클릭을?\s*유도|참여를?\s*이끌|효과적으로\s*전달|강력한\s*임팩트)"
)


def strip_quotes(text: str) -> str:
    """따옴표 안(실제 인용)은 쉬운 말 검사에서 제외."""
    return QUOTED_RE.sub(" ", text)


_HANGUL_RE = re.compile(r"[가-힣]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
# 한자·일본어 문자 — 한국어 리포트에 들어갈 이유가 없다 (실측: flash 가 "글末尾" 출력)
# 특정 게시물을 가리키지만 정체를 밝히지 않는 지시어 / 정체가 밝혀졌다고 볼 근거
VAGUE_POST_REF = re.compile(r"(이|그|해당|저)\s*(영상|게시물|릴스)(?!들)")
POST_IDENTIFIED = re.compile(r"(\d{1,2}\s*위|\d{1,2}\s*월\s*\d{1,2}\s*일|[“\"'‘][^”\"'’]{2,})")

_CJK_RE = re.compile(r"[一-鿿぀-ヿ]")
# 그대로 써도 되는 고유명사·널리 쓰는 약어
ALLOWED_LATIN = {
    "ai",
    "dm",
    "ppt",
    "chatgpt",
    "gpt",
    "claude",
    "gemini",
    "instagram",
    "reels",
    "sns",
    "url",
    "pdf",
    "youtube",
    "canva",
    "notion",
    "midjourney",
    "veo",
    "sora",
    "capcut",
    "kling",
    "runway",
    "figma",
    "excel",
    "vs",
    "code",
}


def check_jargon(text: str) -> list[str]:
    t = strip_quotes(text)
    out = []
    # 한자·일본어 혼입 (따옴표 인용은 원문일 수 있으므로 제외 후 검사)
    cjk = sorted(set(_CJK_RE.findall(t)))
    if cjk:
        out.append(
            f"한자·외국문자 '{''.join(cjk)}' 금지 — 순 한국어로 쓰세요 "
            f"(예: '글末尾' → '글 마지막')"
        )
    for rx, better in JARGON_COMPILED:
        m = rx.search(t)
        if m:
            out.append(f"어려운 말 '{m.group(0)}' → {better} 로 바꿔 쓰세요")
    # 영어 서술 차단 (따옴표 인용은 이미 제외됨)
    foreign = [w for w in _LATIN_WORD_RE.findall(t) if w.lower() not in ALLOWED_LATIN]
    if len(foreign) >= 2 or (foreign and not _HANGUL_RE.search(t)):
        out.append(
            f"영어 표현 {foreign[:3]} 금지 — 한국어로 쓰세요 "
            f"(영상 속 실제 문구를 인용할 때만 따옴표 안에 원문 허용)"
        )
    return out


def _texts_v3(slots: dict):
    for i, t in enumerate(slots.get("top3", [])):
        yield f"top3[{i}]", f"{t.get('headline','')} {t.get('body','')}"
    for k in ("monthly_observation", "hook_note", "opening_note", "low_line"):
        if slots.get(k):
            yield k, slots[k].get("text", "")
    sf = slots.get("success_formula") or {}
    for k in ("persona", "winning_pattern"):
        if sf.get(k):
            yield f"success_formula.{k}", sf[k].get("text", "")
    for i, w in enumerate(slots.get("top_posts_why", [])):
        yield f"top_posts_why[{i}]", w.get("why", "")
    for i, f_ in enumerate(slots.get("fans_wants", [])):
        yield f"fans_wants[{i}]", f"{f_.get('title','')} {f_.get('note','')}"
    for i, o in enumerate(slots.get("opportunities", [])):
        yield f"opportunities[{i}]", o.get("text", "")
    for i, m_ in enumerate(slots.get("motivation_descs", [])):
        yield f"motivation_descs[{i}]", m_.get("desc", "")
    for i, s in enumerate(slots.get("strengths", [])):
        yield f"strengths[{i}]", f"{s.get('value','')} {s.get('title','')} {s.get('desc','')}"
    pos = slots.get("positioning") or {}
    yield "positioning", f"{pos.get('oneliner','')} {pos.get('body','')}"
    for i, r in enumerate(slots.get("formula_rows", [])):
        yield f"formula_rows[{i}]", f"{r.get('action','')} {r.get('detail','')} {r.get('example','')}"
    for i, r in enumerate(slots.get("recommendations", [])):
        yield (
            f"recommendations[{i}]",
            f"{r.get('title','')} {r.get('what_to_do', r.get('body',''))} "
            f"{r.get('why','')} {r.get('evidence_line','')}",
        )
    for i, c in enumerate(slots.get("checklist", [])):
        yield f"checklist[{i}]", c


def autofix_slots(slots: dict, agg: dict) -> list[str]:
    """반려할 필요 없는 경미한 위반은 코드가 조용히 교정 (재합성 낭비 방지)."""
    fixed = []
    # 칩은 v3.1에서 폐지 (초록 태그 제거) — 모델이 넣어도 무시
    for w in slots.get("top_posts_why", []):
        w.pop("chips", None)

    # 이모지 제거 (템플릿이 아이콘을 따로 배치하므로 본문엔 불필요)
    def strip_emoji(s):
        return EMOJI_RE.sub("", s or "").strip()

    for m_ in slots.get("motivation_descs", []):
        if EMOJI_RE.search(m_.get("desc", "")):
            m_["desc"] = strip_emoji(m_["desc"])
            fixed.append(f"motivation_descs[{m_.get('key')}] 이모지 제거")
    for s in slots.get("strengths", []):
        for k in ("value", "title", "desc"):
            if EMOJI_RE.search(s.get(k, "")):
                s[k] = strip_emoji(s[k])
                fixed.append("strengths 이모지 제거")
    for k in ("monthly_observation", "hook_note", "opening_note", "low_line"):
        if slots.get(k) and EMOJI_RE.search(slots[k].get("text", "")):
            slots[k]["text"] = strip_emoji(slots[k]["text"])
            fixed.append(f"{k} 이모지 제거")
    return fixed


def verify_slots_v3(slots: dict, metrics: dict, agg: dict) -> dict:
    errors = []
    wl = build_whitelist(metrics, agg)
    autofix_slots(slots, agg)

    # ── 구조 ──
    if len(slots.get("top3", [])) != 3:
        errors.append({"slot": "top3", "error": "정확히 3개"})
    recs = slots.get("recommendations", [])
    if not (5 <= len(recs) <= 6):
        errors.append({"slot": "recommendations", "error": "5~6개"})
    # 어떤 영상인지 못 찾는 지시어 차단 — "이 영상은 46.7만을 기록했고"만 읽으면 독자는
    # 그게 어느 게시물인지 알 수 없다(사용자 피드백 2026-08-03). 순위(N위)·날짜·인용 중
    # 하나라도 있으면 통과. 순위 숫자 1~12 는 숫자 화이트리스트 면제 범위라 안전하다.
    for i, r in enumerate(recs):
        if r.get("_fb"):
            continue
        text = f"{r.get('what_to_do', '')} {r.get('why', '')}"
        if VAGUE_POST_REF.search(text) and not POST_IDENTIFIED.search(text):
            errors.append(
                {
                    "slot": f"recommendations[{i}]",
                    "error": "어떤 영상인지 알 수 없어요 — '이 영상' 대신 '1위 영상'처럼 "
                    "순위로 지목하거나 올린 날짜를 넣으세요",
                }
            )

    # ── 갈등 유도 조언 금지 (2026-08-04 실측) ──────────────────────────
    # 실제 리포트가 "논쟁 주제로 댓글 참여 늘리기 — 찬반으로 나뉠 만한 주제(예: 남녀 갈등,
    # 세대 차이)를 만드세요" 를 추천했다. 같은 리포트의 분위기 블록은 "논쟁·비방 26% 이니
    # 비중을 줄이세요" 라고 말하고 있었다 — **정반대 조언**이고, 욕설·비방이 이미 13% 인
    # 계정에 갈등 소재를 권하는 것은 우리 제품이 해선 안 되는 조언이다.
    for i, r in enumerate(recs):
        if r.get("_fb"):
            continue
        text = f"{r.get('title', '')} {r.get('what_to_do', '')} {r.get('why', '')}"
        if CONFLICT_BAIT.search(text):
            errors.append(
                {
                    "slot": f"recommendations[{i}]",
                    "error": "갈등·논쟁을 일부러 만들라는 조언은 쓰지 마세요 — 댓글이 늘어도 "
                    "욕설·비방이 함께 늘고 협업·판매에 해가 됩니다. 참여를 늘리려면 "
                    "'의견을 묻는 질문'이나 '경험 공유 요청'처럼 갈등 없이 말하게 하는 "
                    "방법으로 바꿔 쓰세요",
                }
            )

    if sum(1 for r in recs if r.get("basis") == "data_observation") < 3:
        errors.append({"slot": "recommendations", "error": "data_observation 최소 3개"})
    for i, r in enumerate(recs):
        for fld in ("what_to_do", "why"):
            if not (r.get(fld) or "").strip():
                errors.append(
                    {
                        "slot": f"recommendations[{i}]",
                        "error": f"{fld} 필드가 비어 있음 (무엇을 할지·왜 할지 모두 필요)",
                    }
                )
        # 근거 강제: 일반 가이드 외에는 근거 수치 2개 이상
        if r.get("basis") != "general_guide":
            ev = r.get("evidence_line") or ""
            nums = tokenize_numbers(ev + " " + (r.get("why") or ""))
            if len(nums) < 2:
                errors.append(
                    {
                        "slot": f"recommendations[{i}]",
                        "error": "근거가 부족해요 — evidence_line 에 비교 수치를 "
                        "'항목A 숫자(영상 N개) vs 항목B 숫자(영상 M개)' 형식으로 "
                        "2개 이상 넣으세요",
                    }
                )
        if r.get("basis") == "general_guide" and tokenize_numbers(
            (r.get("what_to_do") or "") + (r.get("why") or "")
        ):
            errors.append({"slot": f"recommendations[{i}]", "error": "general_guide 는 숫자 금지"})
    if sum(1 for r in recs if r.get("basis") == "general_guide") > 1:
        errors.append({"slot": "recommendations", "error": "general_guide 는 최대 1개"})
    cl = slots.get("checklist", [])
    if not (6 <= len(cl) <= 8):
        errors.append({"slot": "checklist", "error": "6~8개"})
    for i, c in enumerate(cl):
        if not re.search(r"(나요|까요|가요)\?$", c.strip()):
            errors.append({"slot": f"checklist[{i}]", "error": "질문형(~나요?/~까요?)으로"})

    # fans_wants — quote_id 실존
    pool_ids = {
        q["quote_id"]
        for cat in (agg.get("comment_stats", {}).get("quote_pool") or {}).values()
        for q in cat
    }
    fw = slots.get("fans_wants", [])
    if len(fw) != 3:
        errors.append({"slot": "fans_wants", "error": "정확히 3개"})
    for i, f_ in enumerate(fw):
        if f_.get("quote_id") not in pool_ids:
            errors.append(
                {
                    "slot": f"fans_wants[{i}]",
                    "error": f"quote_id '{f_.get('quote_id')}' 가 quote_pool 에 없음 — "
                    "입력의 quote_id 를 그대로 복사하세요",
                }
            )

    # top_posts_why — rank 커버 + 칩 검증
    meta = {m["rank"]: m for m in agg.get("top_posts_meta", [])}
    given = {w.get("rank"): w for w in slots.get("top_posts_why", [])}
    missing = set(meta) - set(given)
    if missing:
        errors.append({"slot": "top_posts_why", "error": f"rank 누락: {sorted(missing)}"})
    for r, w in given.items():
        if r not in meta:
            continue
        why = (w.get("why") or "").strip()
        if not meta[r]["has_features"] and SCREEN_WORDS.search(why):
            errors.append(
                {
                    "slot": f"top_posts_why(rank={r})",
                    "error": "영상 미분석 게시물 — 화면·첫 문장 묘사 금지",
                }
            )
        # 품질 기준(길이·인용·뻔한말)은 AI 생성분에만 적용. 코드 폴백은 사실 나열이므로 면제.
        if meta[r]["has_features"] and not w.get("_fb"):
            if len(why) < 110:
                errors.append(
                    {
                        "slot": f"top_posts_why(rank={r})",
                        "error": f"설명이 너무 짧아요({len(why)}자) — 첫 3초에 무엇이 "
                        "보이고 들렸는지, 그래서 왜 멈췄는지, 어떻게 끝까지 "
                        "붙잡았는지 130자 이상으로",
                    }
                )
            generic = GENERIC_WHY.findall(why)
            if len(generic) >= 2 or (generic and not QUOTED_RE.search(why)):
                errors.append(
                    {
                        "slot": f"top_posts_why(rank={r})",
                        "error": f"뻔한 표현 {generic[:2]} — 그 영상의 실제 문구·화면을 "
                        "따옴표로 인용하고 구체적으로 쓰세요",
                    }
                )
            elif not QUOTED_RE.search(why):
                errors.append(
                    {
                        "slot": f"top_posts_why(rank={r})",
                        "error": "그 영상의 실제 첫 마디나 자막을 따옴표로 최소 1개 인용하세요",
                    }
                )

    # motivation_descs — 4키 전부
    keys = [m_.get("key") for m_ in slots.get("motivation_descs", [])]
    if sorted(keys) != sorted(["practical", "question", "wow", "fan"]):
        errors.append(
            {"slot": "motivation_descs", "error": "practical/question/wow/fan 4키 각 1개씩"}
        )
    if len(slots.get("strengths", [])) != 3:
        errors.append({"slot": "strengths", "error": "정확히 3개"})
    fr = slots.get("formula_rows", [])
    if not (3 <= len(fr) <= 4):
        errors.append({"slot": "formula_rows", "error": "3~4개"})

    # ── 텍스트 공통 검증 ──
    for path, text in _texts_v3(slots):
        if not text.strip():
            continue
        for msg in check_jargon(text):
            errors.append({"slot": path, "error": msg})
        m = HANGUL_QUANT.search(text)
        if m:
            errors.append({"slot": path, "error": f"한글 수량어 금지: '{m.group(0)}' → 숫자로"})
        m = FORBIDDEN.search(text)
        if m:
            errors.append({"slot": path, "error": f"금칙어: '{m.group(0)}'"})
        for sent in re.split(r"(?<=[.요다])\s+", text):
            # "A가 B보다 27.7배 높았기 때문이에요"처럼 숫자 근거를 대는 문장은 데이터 서술로 허용.
            # 숫자 없이 원인을 단정하는 문장(관찰 불가한 이유 주장)만 반려한다.
            if CAUSAL.search(sent) and not HEDGE.search(sent) and not tokenize_numbers(sent):
                errors.append(
                    {
                        "slot": path,
                        "error": f"인과 단정: '{sent[:40]}…' — 숫자 근거를 넣거나 "
                        "'~영향도 있을 수 있어요'처럼 표현하세요",
                    }
                )
        for tok in tokenize_numbers(text):
            if not _match(tok, wl):
                errors.append(
                    {"slot": path, "error": f"숫자 '{tok['raw']}' 가 지표에 없음 — 입력 값만 사용"}
                )
        if EMOJI_RE.search(text):
            errors.append({"slot": path, "error": "이모지 금지(코드가 배치)"})

    # 헤지 자동 부착
    mo = slots.get("monthly_observation") or {}
    if mo.get("text") and not HEDGE_REQ.search(mo["text"]):
        mo["text"] = mo["text"].rstrip() + " 다만 주제나 올린 시기 영향도 섞여 있을 수 있어요."
        mo["_auto_hedged"] = True

    return {"ok": not errors, "errors": errors}


# 받침 유무로 목적격 조사를 고른다 — "46.7만를"(X) / "46.7만을"(O).
# 숫자로 끝나면 읽는 소리로 판정: 영·일·삼·육·칠·팔 = 받침 있음, 이·사·오·구 = 없음.
_DIGIT_HAS_FINAL = {
    "0": True,
    "1": True,
    "3": True,
    "6": True,
    "7": True,
    "8": True,
    "2": False,
    "4": False,
    "5": False,
    "9": False,
}


def _has_final(word: str) -> bool:
    if not word:
        return True
    ch = word[-1]
    if ch.isdigit():
        return _DIGIT_HAS_FINAL.get(ch, True)
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    return True


def eul(word: str) -> str:
    """목적격 조사 (을/를)."""
    return "을" if _has_final(str(word)) else "를"


def post_ref(post: dict, rank: int = 1) -> str:
    """리포트 안에서 **어떤 영상인지 찾을 수 있게** 지목하는 문구.

    "이 영상"만 쓰면 독자가 어느 게시물인지 알 수 없다(사용자 피드백 2026-08-03).
    순위는 잘된 게시물 카드의 배지와 같고, 날짜·첫 문장은 그 카드에 그대로 적혀 있다.
    ⚠️ 순위 숫자(1~6)는 검증 게이트의 숫자 화이트리스트 면제 범위(1~12)라 안전하다.
    """
    parts = []
    date = (post.get("date_kst") or "")[:10]
    if len(date) == 10:
        parts.append(f"{int(date[5:7])}월 {int(date[8:10])}일")
    title = re.sub(r"[.·…]{2,}", "…", (post.get("title") or "").strip()).strip(" …")
    if title:
        parts.append(f"“{title[:22].rstrip()}…”" if len(title) > 22 else f"“{title}”")
    tail = f"({' · '.join(parts)})" if parts else ""
    return f"{rank}위 영상{tail}"


def _audience_recs(metrics: dict) -> list[dict]:
    """계정 성격(규모·도달 방식)에 맞는 폴백 추천 0~1개.

    ⚠️ 이게 없으면 폴백이 **모든 계정에 같은 중간 규모용 조언**을 준다. 대형 계정에
    "조회수를 늘리세요", 막 시작한 계정에 "올리는 시간을 실험하세요" 는 둘 다 헛말이다.
    """
    aud = metrics.get("audience") or {}
    vs = metrics["views_stats"]
    followers = aud.get("followers") or 0
    ratio = aud.get("views_per_follower")

    if aud.get("reach_mode") == "explore_driven" and followers:
        return [
            {
                "title": "본 사람을 팔로워로 남기기",
                "what_to_do": "영상 마지막에 '이런 내용 더 보려면 팔로우'를 한 문장으로 넣고, "
                "프로필 첫 화면(소개글·고정 게시물)이 '무슨 계정인지' 3초에 보이게 정리해 보세요.",
                "why": f"평소 조회수 {man(vs['median'])}이 팔로워 {man(followers)}보다 "
                f"{ratio}배 많아요 — 팔로워 밖에서 보고 있는데 팔로워로 남지는 않고 있어요.",
                "evidence_line": f"평소 조회수 {man(vs['median'])} vs 팔로워 {man(followers)} "
                f"({ratio}배)",
                "priority": "high",
                "basis": "data_observation",
                "evidence_keys": ["views_stats", "audience"],
                "numbers_used": [],
            }
        ]
    if aud.get("reach_mode") == "follower_driven" and followers:
        return [
            {
                "title": "팔로워 밖으로 퍼지게 하기",
                "what_to_do": "이미 아는 사람만 이해하는 표현을 줄이고, 처음 보는 사람도 3초에 "
                "상황을 알 수 있게 첫 문장과 자막을 바꿔 보세요.",
                "why": f"평소 조회수 {man(vs['median'])}이 팔로워 {man(followers)}의 "
                f"{ratio}배예요 — 새 시청자에게 퍼지지 않고 있어요.",
                "evidence_line": f"평소 조회수 {man(vs['median'])} vs 팔로워 {man(followers)} "
                f"({ratio}배)",
                "priority": "high",
                "basis": "data_observation",
                "evidence_keys": ["views_stats", "audience"],
                "numbers_used": [],
            }
        ]
    if aud.get("scale") == "starting":
        return [
            {
                "title": "먼저 편수를 쌓기",
                "what_to_do": "다음 2주 동안 같은 주제로 6편을 올려 보세요. 편집·시간대는 "
                "바꾸지 말고 주제만 고정합니다.",
                "why": f"지금은 영상 {metrics['coverage']['reels_with_views']}개라 "
                "무엇이 통했는지 가려낼 수 없어요. 비교할 수 있는 양이 먼저 필요해요.",
                "evidence_line": f"분석한 영상 {metrics['coverage']['reels_with_views']}개",
                "priority": "high",
                "basis": "experiment_suggestion",
                "evidence_keys": ["coverage"],
                "numbers_used": [],
            }
        ]
    return []


def fallback_slots_v3(metrics: dict, agg: dict) -> dict:
    """전 슬롯 결정적 폴백 — 숫자만 끼운 안전 문장."""
    vs = metrics["views_stats"]
    cov = metrics["coverage"]
    d = metrics["dist"]
    cs = agg.get("comment_stats") or {
        "pcts": {},
        "quote_pool": {},
        "motivations": [],
        "tones": [],
        "n_analyzed": 0,
        "save_mentions": 0,
        "unclassified": 0,
        "unclassified_pct": 0,
        "classify_unreliable": False,
    }
    top1 = (metrics.get("top_posts") or [{}])[0]

    def slot(text, keys=None):
        return {"text": text, "evidence_keys": keys or [], "numbers_used": []}

    # 인용 폴백: 각 카테고리 최다 좋아요 1개
    qp = cs.get("quote_pool") or {}

    def first_q(cat):
        return (qp.get(cat) or [{}])[0].get("quote_id", "")

    fw = []
    for cat, title, note in (
        ("request", "받아갈 자료", "자료를 원하는 댓글이 이어졌어요."),
        ("question", "할 수 있다는 확신", "시작을 망설이는 질문이 반복됐어요."),
        ("praise", "새로운 정보", "내용에 대한 호응 댓글이 많았어요."),
    ):
        qid = first_q(cat) or next((first_q(c) for c in qp if first_q(c)), "")
        fw.append({"title": title, "quote_id": qid, "note": note})

    why = []
    for m in agg.get("top_posts_meta", []):
        parts = []
        if m.get("first_screen"):
            # 따옴표로 감싸 관찰 원문임을 표시 (도구명 등 영어가 섞여도 검사에서 제외됨)
            parts.append(f"영상이 시작되면 “{m['first_screen'][:60]}” 장면이 보여요.")
        if m.get("first_words"):
            parts.append(f"첫 마디는 “{m['first_words'][:60]}”였어요.")
        if m.get("on_screen_text"):
            parts.append(f"화면에는 “{m['on_screen_text'][:40]}” 자막이 떴어요.")
        if m.get("value_shown_in_2s"):
            parts.append("처음 2초 안에 얻을 것이 무엇인지 바로 드러났어요.")
        segs = [s["label"] for s in (m.get("segments") or [])[1:4] if s.get("label")]
        if segs:
            parts.append("이어서 " + " → ".join(s[:24] for s in segs) + " 순서로 이어졌어요.")
        if m.get("cta_keyword"):
            parts.append(f"마지막에 댓글로 ‘{m['cta_keyword']}’를 남겨달라고 요청했어요.")
        why.append(
            {
                "rank": m["rank"],
                "_fb": True,
                "why": " ".join(parts) or "이 영상의 조회수가 가장 높았어요.",
            }
        )

    return {
        "top3": [
            {
                "headline": "잘된 영상과 평소의 차이가 컸어요",
                "body": f"최고 조회수는 {man(vs['max'])}, 평소 조회수는 {man(vs['median'])}이었어요. "
                f"(영상 {cov['reels_with_views']}개 기준)",
                "tone": "neutral",
                "evidence_keys": ["views_stats"],
                "numbers_used": [],
            },
            {
                "headline": "기준선을 확인하세요",
                "body": f"영상 {sum(d['counts'])}개 중 {d['counts'][0]}개({d['under_first_pct']}%)가 "
                f"{d['labels'][0]} 조회수예요.",
                "tone": "warn",
                "evidence_keys": ["dist"],
                "numbers_used": [],
            },
            {
                "headline": "잘됐던 주제를 다시 쓰는 게 가장 빨라요",
                "body": "가장 잘된 영상의 주제를 최신 내용으로 다시 만드는 것부터 해보세요.",
                "tone": "good",
                "evidence_keys": ["top_posts"],
                "numbers_used": [],
            },
        ],
        "monthly_observation": slot(
            "이 시기 변화의 원인은 숫자만으로 단정하기 어려워요. "
            "다음 영상에서 첫 문장을 바꿔 올려보고 반응을 비교해 보세요."
        ),
        "success_formula": {
            "persona": slot(
                "댓글에서 자료 요청과 질문이 반복돼요 — 따라할 수 있는 실용 정보를 "
                "원하는 시청자가 중심이에요."
            ),
            "winning_pattern": slot(
                "잘된 영상들은 첫 3초에 볼 이유를 주고, 마지막에 행동을 "
                "요청하는 구성이 많았어요."
            ),
        },
        "hook_note": slot("첫 문장 스타일별 성과 차이를 표에서 확인해 보세요."),
        "opening_note": slot("시작 3초 화면 유형별 차이를 표에서 확인해 보세요."),
        "top_posts_why": why,
        "low_line": slot(
            "조회수가 낮았던 영상들의 시작 부분을 모았어요. 처음 3초에 왜 봐야 "
            "하는지가 보이는지 점검해 보세요."
        ),
        "fans_wants": fw,
        "opportunities": [
            slot(
                "댓글에서 반복되는 질문을 다음 영상 소재로 활용해 보세요. 질문이 "
                "많다는 건 수요가 검증됐다는 뜻이에요."
            )
        ],
        "motivation_descs": [
            {"key": "practical", "desc": "따라해서 결과를 얻으려는 댓글이에요."},
            {"key": "question", "desc": "시작 전에 조건을 확인하려는 댓글이에요."},
            {"key": "wow", "desc": "내용과 결과물에 대한 감탄·공감이에요."},
            {"key": "fan", "desc": "크리에이터를 응원하는 댓글이에요."},
        ],
        "strengths": [
            {
                "value": man(vs["max"]),
                "title": "최고 조회수",
                "desc": "가장 잘된 영상의 기록이에요.",
                "evidence_keys": ["views_stats.max"],
                "numbers_used": [],
            },
            {
                "value": man(vs["median"]),
                "title": "평소 조회수",
                "desc": "내 영상의 중간 성적이에요.",
                "evidence_keys": ["views_stats.median"],
                "numbers_used": [],
            },
            {
                "value": f"{cov['reels_with_views']}개",
                "title": "분석한 릴스",
                "desc": "이번 리포트가 살펴본 영상 수예요.",
                "evidence_keys": ["coverage"],
                "numbers_used": [],
            },
        ],
        "positioning": {
            "oneliner": "내 주제를 꾸준히 쌓아가는 계정",
            "body": "잘된 주제를 반복 검증하며 기준선을 올리는 전략이 유효해요.",
        },
        "formula_rows": [
            {
                "phase": "0~3초",
                "action": "볼 이유를 먼저 주기",
                "detail": "결과물이나 얻을 것을 화면과 말로 제시",
                "example": "잘된 영상의 첫 문장을 참고하세요",
            },
            {
                "phase": "본론",
                "action": "단계에 번호 붙이기",
                "detail": "따라하기 쉽게 자막과 함께",
                "example": "1-2-3 구조",
            },
            {
                "phase": "마지막",
                "action": "행동 요청",
                "detail": "댓글 키워드·저장 부탁",
                "example": "주제에 맞는 단어로",
            },
        ],
        # 계정 성격에 맞는 조언을 **맨 앞에** 놓고 일반 조언을 뒤로 밀어낸다.
        # 폴백도 분기해야 하는 이유: 게이트가 AI 문장을 반려하면 결국 여기가 사용자에게
        # 보이는 최종 조언이 된다(실측 2건 모두 recommendations 폴백).
        "recommendations": _audience_recs(metrics)
        + [
            {
                "title": "잘됐던 주제 다시 만들기",
                "what_to_do": "가장 잘된 영상의 주제를 최신 내용으로 다시 만들어 보세요.",
                "why": f"{post_ref(top1)}이 {man(top1.get('views'))}"
                f"{eul(man(top1.get('views')))} 기록했고, 평소 조회수 "
                f"{man(vs['median'])}보다 훨씬 높았어요.",
                "evidence_line": f"가장 잘된 영상 {man(top1.get('views'))} vs 평소 조회수 "
                f"{man(vs['median'])} (영상 {cov['reels_with_views']}개 기준)",
                "priority": "high",
                "basis": "data_observation",
                "evidence_keys": ["top_posts", "views_stats"],
                "numbers_used": [],
            },
            {
                "title": "다음 영상 목표 정하기",
                "what_to_do": f"다음 영상은 {man(vs['p75'])}{eul(man(vs['p75']))} 목표로 하고, "
                f"{man(vs['p90'])}{eul(man(vs['p90']))} 넘기면 후속편을 바로 준비해 보세요.",
                "why": f"지금 평소 조회수가 {man(vs['median'])}이라 이 정도가 현실적인 다음 목표예요.",
                "evidence_line": f"평소 {man(vs['median'])} · 잘된 편 {man(vs['p75'])} · "
                f"대박 {man(vs['p90'])}",
                "priority": "high",
                "basis": "data_observation",
                "evidence_keys": ["views_stats"],
                "numbers_used": [],
            },
            {
                "title": "올리는 시간 실험하기",
                "what_to_do": "몇 주간 올리는 시간을 하나로 고정해 보고 반응을 비교해 보세요.",
                "why": f"지금은 영상 {cov['reels_with_views']}개로 시간대별 차이를 확인하기 어려워요. "
                "같은 시간대로 몇 편 올려보면 비교가 쉬워져요.",
                "evidence_line": f"분석한 영상 {cov['reels_with_views']}개 · 평소 조회수 "
                f"{man(vs['median'])}",
                "priority": "mid",
                "basis": "experiment_suggestion",
                "evidence_keys": ["timing_hours", "coverage"],
                "numbers_used": [],
            },
            {
                "title": "낮은 영상 다시 보기",
                "what_to_do": "조회수가 낮았던 영상들의 시작 부분을 다시 보고, 처음 3초에 "
                "볼 이유가 있었는지 확인해 보세요.",
                "why": f"영상 {sum(d['counts'])}개 중 {d['counts'][0]}개가 {d['labels'][0]} "
                "조회수에 머물렀어요.",
                "evidence_line": f"{d['labels'][0]} 영상 {d['counts'][0]}개 / 전체 "
                f"{sum(d['counts'])}개 ({d['under_first_pct']}%)",
                "priority": "mid",
                "basis": "data_observation",
                "evidence_keys": ["dist"],
                "numbers_used": [],
            },
            {
                "title": "첫 마디 점검하기",
                "what_to_do": "영상 시작 2초 안에 '이걸 보면 무엇을 얻는지'를 말이나 자막으로 "
                "알려주세요.",
                "why": "처음 몇 초에 볼 이유가 없으면 시청자가 넘기기 쉬워요.",
                "evidence_line": "",
                "priority": "mid",
                "basis": "general_guide",
                "evidence_keys": [],
                "numbers_used": [],
            },
        ][: 5 - len(_audience_recs(metrics))],
        "checklist": [
            "첫 문장이 궁금하게 만들거나 필요하게 만드나요?",
            "제목이나 자막에 숫자나 결과가 있나요?",
            "댓글로 남길 한 단어가 영상 내용과 맞나요?",
            "시작 3초에 결과나 얻을 것을 먼저 보여주나요?",
            "올리는 시간이 팔로워가 활동하는 때인가요?",
            "내 계정 주제에서 벗어나지 않았나요?",
        ],
        "_fallback": True,
    }


TOP_KEY_RE = re.compile(r"^([a-z_0-9]+)")  # top3[0] → top3 (숫자 포함 필수)


def _top_keys(errors: list) -> list[str]:
    """오류 슬롯 경로들을 최상위 키로 환원 (top3[0] → top3)."""
    keys = []
    for e in errors:
        m = TOP_KEY_RE.match(e["slot"])
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


def run_gate_v3(slots: dict, metrics: dict, agg: dict, resynth_fn, log=print):
    """검증 → 실패한 슬롯만 부분 재합성 → 병합. 전체 재출력보다 빠르고 안정적."""
    meta = {"attempts": [], "fallback_slots": [], "autofixed": []}
    cur = slots
    fb_all = fallback_slots_v3(metrics, agg)
    for attempt in range(config.SYNTH_MAX_RETRY + 1):
        meta["autofixed"] += autofix_slots(cur, agg)
        v = verify_slots_v3(cur, metrics, agg)
        meta["attempts"].append({"n": attempt + 1, "errors": v["errors"]})
        if v["ok"]:
            log(f"    검증 통과 (시도 {attempt+1})")
            return cur, meta
        keys = _top_keys(v["errors"])
        log(f"    검증 실패 {len(v['errors'])}건 (시도 {attempt+1}) → {keys} 재작성 요청")
        if attempt == config.SYNTH_MAX_RETRY:
            break
        fb = "\n".join(f"- [{e['slot']}] {e['error']}" for e in v["errors"][:25])
        try:
            partial = resynth_fn(fb, keys)
        except Exception as e:  # noqa: BLE001
            # 파싱 실패 등 일시적 오류 — 남은 시도를 버리지 말고 계속 (과거: 여기서
            # break 해서 6개 슬롯이 통째로 폴백됐음)
            log(f"    재합성 호출 실패({str(e)[:60]}) — 다시 시도")
            continue
        if not partial:
            continue
        merged = dict(cur)
        for k in keys:
            if k in partial:
                merged[k] = partial[k]
        cur = merged

    # 최종까지 실패한 슬롯만 폴백으로 교체 (통과 슬롯은 살림).
    # 폴백은 코드가 결정적으로 만든 문장이라 재검증하지 않는다 — 그 안의 영어는 영상 속
    # 실제 도구명·자막에서 온 것이므로 반려 대상이 아니다(과거: 이 재검증이 전체 폴백을 유발).
    bad_keys = _top_keys(verify_slots_v3(cur, metrics, agg)["errors"])
    if bad_keys:
        log(f"    최종 실패 슬롯 {bad_keys} → 안전 문장으로 대체 (나머지 AI 문장은 유지)")
        for k in bad_keys:
            if k in fb_all:
                cur[k] = fb_all[k]
        meta["fallback_slots"] = bad_keys
        return cur, meta
    fb_slots = fallback_slots_v3(metrics, agg)
    v = verify_slots_v3(fb_slots, metrics, agg)
    if not v["ok"]:
        raise RuntimeError(f"폴백 검증 실패 — 버그: {v['errors'][:5]}")
    meta["fallback_slots"] = ["ALL"]
    return fb_slots, meta
