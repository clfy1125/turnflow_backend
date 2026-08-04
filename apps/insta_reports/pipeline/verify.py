"""S7 검증 게이트 (결정적 코드) — 생성 서술의 숫자 전수 대조 + 금칙 + 구조 검증.

critic 수정 반영:
- 날짜(YY.MM) 배제 정규식에 후행 단위 lookahead → "21.3만"이 날짜로 오인되던 구멍 봉합
- "N개월" 기간 배제 ("6개월"→"6개" 오토큰화 방지)
- 한글 수량어(절반·두 배·수십만 등) 검출 시 fail
검증 실패 → 사유 첨부 재합성(최대 SYNTH_MAX_RETRY회) → 그래도 실패 시 슬롯 폴백.
"""

import re

from . import config
from .metrics import man

# ── 한국어 숫자 토큰화 (순서대로 소비) ──────────────────────────────
RE_EXCLUDE = [
    re.compile(r"\d{4}년"),
    re.compile(r"\d{4}[-.]\d{1,2}"),
    re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)"),
    # YY.MM 날짜 — 단, 뒤에 단위가 붙으면 숫자다 (critic HIGH 수정)
    re.compile(r"(?<!\d)2\d\.(0?[1-9]|1[0-2])(~\d{1,2})?(?!\s*(만|천|배|%|회|개|명|\d))"),
    re.compile(r"\d+\s*개월"),  # 기간 (critic 수정)
    re.compile(r"\d{1,2}(~\d{1,2})?월"),
    re.compile(r"\d{1,2}(~\d{1,2})?시"),
    re.compile(r"\d{1,2}:\d{2}"),
    re.compile(r"\d{1,2}일(?![가-힣])"),
    re.compile(r"\d+\s*초"),
    re.compile(r"조회\s*\d+\s*회당|\d+\s*회당"),  # "조회 100회당" = 단위 표현
    re.compile(r"\d+(?:\.\d+)?\s*만?\s*기준"),  # "조회수 1만 기준" = 단위 표현
    re.compile(r"\d+(?:\s*~\s*\d+)?\s*자(?![가-힣])"),  # "150~300자" 글자 수 단위
]
RE_MAN = re.compile(r"(\d+(?:\.\d+)?)\s*만")
RE_CHEON = re.compile(r"(\d+(?:\.\d+)?)\s*천")
RE_BAE = re.compile(r"(\d+(?:\.\d+)?)\s*배")
RE_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(%|퍼센트)")
RE_COMMA = re.compile(r"\d{1,3}(?:,\d{3})+")
RE_COUNT = re.compile(r"(\d+)\s*(개(?![월국조])|회|명|번|건|편)")
RE_INT = re.compile(r"(?<![\d.,])(\d+)(?![\d.,%가-힣])")

HANGUL_QUANT = re.compile(r"(절반|두\s*배|세\s*배|네\s*배|수십만|수백만|수만|과반|대부분의\s*영상)")
FORBIDDEN = re.compile(
    r"(섀도밴|쉐도우밴|shadow\s*ban|알고리즘[^.]{0,8}(처벌|제한|억제|밀어주지)|"
    r"계정[^.]{0,6}(제재|정지|블락)|노출[^.]{0,6}(막혔|제한)|유령\s*계정|"
    r"도달률|저장수|시청\s*시간|완주율|팔로우\s*전환|턴플로우|turnflow)",
    re.I,
)
CAUSAL = re.compile(r"(때문에|때문이에요|덕분에|원인은)")
# 인과 단정 문장을 면책하는 헤지 (좁게 유지)
HEDGE = re.compile(r"(있을 수|수도 있|확실하진|섞여|단정)")
# "헤지 필수 슬롯" 충족 판정 (넓게 — 관찰 어법 포함)
HEDGE_REQ = re.compile(
    r"(수 있|수도 있|확실하|섞여|단정|어려워|어렵|보여요|나타났|함께|겹치|겹쳐|맞물|추정|가능성|보였어요)"
)
HEDGE_HINT = (
    "'~영향도 섞여 있을 수 있어요' / '~와 함께 나타났어요' / "
    "'~때문인지는 단정하기 어려워요' 같은 표현 중 하나를 문장에 포함하세요"
)


def tokenize_numbers(text: str) -> list[dict]:
    """서술에서 검증 대상 숫자 토큰 추출."""
    s = text
    for rx in RE_EXCLUDE:
        s = rx.sub(" ", s)
    tokens = []

    def eat(rx, unit, conv):
        nonlocal s

        def rep(mt):
            tokens.append({"raw": mt.group(0), "unit": unit, "value": conv(mt)})
            return " "

        s = rx.sub(rep, s)

    eat(RE_MAN, "만", lambda mt: float(mt.group(1)) * 10_000)
    eat(RE_CHEON, "천", lambda mt: float(mt.group(1)) * 1_000)
    eat(RE_BAE, "배", lambda mt: float(mt.group(1)))
    eat(RE_PCT, "%", lambda mt: float(mt.group(1)))
    eat(RE_COMMA, "raw", lambda mt: float(mt.group(0).replace(",", "")))
    eat(RE_COUNT, "count", lambda mt: float(mt.group(1)))
    # 잔여 정수: 13+ 만 검증 (1~12 는 서수·목록 면제)
    for mt in RE_INT.finditer(s):
        v = float(mt.group(1))
        if v >= 13:
            tokens.append({"raw": mt.group(0), "unit": "raw", "value": v})
    return tokens


def _walk_numbers(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_numbers(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_numbers(v, f"{path}[{i}]")
    elif isinstance(obj, int | float) and not isinstance(obj, bool):
        yield path, float(obj)


def build_whitelist(metrics: dict, agg: dict) -> dict:
    """{category: set(values)} — W1 원값 + W2 표기변환 + W3 배수 + W4 백분율 + W5 카운트."""
    raws, mans, counts = set(), set(), set()
    for _path, v in list(_walk_numbers(metrics)) + list(_walk_numbers(agg)):
        raws.add(v)
        if v >= 10_000:
            mans.add(round(v / 1000) / 10 * 10_000)  # man() 반올림과 동일
            mans.add(round(v, -3))
            mans.add(round(v, -4))
        elif v >= 100:
            mans.add(round(v, -2))
        if v == int(v) and 0 <= v <= 2000:
            counts.add(v)
    baes = {r["value"] for r in agg.get("derived", {}).get("ratios", [])}
    pcts = {p["value"] for p in agg.get("derived", {}).get("pcts", [])}
    # engagement 비율(1.6개 등)은 카운트/원값으로도 등장
    eng = metrics.get("engagement") or {}
    for k in ("like_per_base", "comment_per_base"):
        v = eng.get(k)
        if v is not None:
            raws.add(float(v))
            counts.add(float(v))
    # ⚠️ **우리 리포트가 직접 쓰는 반응률 분모** — "조회 1만회당 댓글 N개"(engagement.per_base).
    # 분모는 지표 '값' 으로도 등장하지만(per_base) 명시적으로 넣어 둔다. 실측(2026-08-04):
    # 이게 없어 재작성 4회 중 3·4회차의 반려 사유가 **'100회' 단 2건**이었고 추천이 폴백됐다.
    if eng:
        counts.add(float(eng.get("per_base") or 100))
        counts.add(100.0)  # 분모가 커져도 사용자가 100 기준으로 되물을 수 있다
    return {"raw": raws, "man": mans, "bae": baes, "pct": pcts, "count": counts}


# 배수·백분율 산수 검증에 쓸 지표 값의 상한 — 조합 수를 O(n²) 로 보되 n 을 제한한다.
_DERIVABLE_MAX_OPERANDS = 400


def _derivable(value: float, wl: dict, *, as_pct: bool) -> bool:
    """허용된 두 지표의 **나눗셈으로 실제 나오는 값**인지.

    왜 필요한가: 모델은 "A가 B의 6.4배" 처럼 스스로 나눗셈을 한다. 화이트리스트는
    `agg.derived.ratios` 에 **미리 계산해 둔** 배수만 담고 있어서, 두 수가 모두 허용된
    지표인데도 "지표에 없는 숫자" 로 반려됐다(2026-08-04 실측: 재작성 4회의 주 사유).
    둘 다 실제 지표에서 온 값이라면 그 비율은 **환각이 아니라 맞는 산수**다.
    반올림 표기(41.3만 ÷ 2.6만)를 감안해 허용 오차를 넉넉히 둔다.
    """
    if value <= 0:
        return False
    nums = sorted({n for n in wl["raw"] | wl["man"] if n > 0})
    if len(nums) > _DERIVABLE_MAX_OPERANDS:  # 큰 값 위주로 잘라낸다(비율의 분자·분모는 대개 큰 값)
        nums = nums[-_DERIVABLE_MAX_OPERANDS:]
    target = value / 100 if as_pct else value
    tol = max(0.05, target * 0.03)  # 표기 반올림 누적분
    for a in nums:
        for b in nums:
            if b and abs(a / b - target) <= tol:
                return True
    return False


def _match(tok: dict, wl: dict) -> bool:
    v, u = tok["value"], tok["unit"]

    def close(target_set, tol):
        return any(abs(v - t) <= tol for t in target_set)

    if u == "만":
        return close(wl["man"] | wl["raw"], max(1000, v * 0.015))
    if u == "천":
        return close(wl["man"] | wl["raw"], max(100, v * 0.02))
    if u == "배":
        return close(wl["bae"], 0.05) or close(wl["raw"], 0.05) or _derivable(v, wl, as_pct=False)
    if u == "%":
        return close(wl["pct"], 1.0) or close(wl["raw"], 1.0) or _derivable(v, wl, as_pct=True)
    if u == "count":
        return close(wl["count"] | wl["raw"], 0.05)
    return close(wl["raw"] | wl["man"], max(0.5, v * 0.01))


def _texts_of(slots: dict):
    """(slot_path, text) 나열."""
    for i, t in enumerate(slots.get("top3", [])):
        yield f"top3[{i}]", f"{t.get('headline','')} {t.get('body','')}"
    for k in ("monthly_observation", "low_line", "hook_note", "kw_recommendation"):
        if slots.get(k):
            yield k, slots[k].get("text", "")
    for i, r in enumerate(slots.get("recommendations", [])):
        yield f"recommendations[{i}]", f"{r.get('title','')} {r.get('body','')}"
    for i, c in enumerate(slots.get("checklist", [])):
        yield f"checklist[{i}]", c


def verify_slots(slots: dict, metrics: dict, agg: dict) -> dict:
    """{ok: bool, errors: [{slot, error}]}"""
    errors = []
    wl = build_whitelist(metrics, agg)

    # 구조 검증
    if len(slots.get("top3", [])) != 3:
        errors.append({"slot": "top3", "error": "정확히 3개여야 함"})
    recs = slots.get("recommendations", [])
    if not (4 <= len(recs) <= 6):
        errors.append({"slot": "recommendations", "error": "4~6개여야 함"})
    if sum(1 for r in recs if r.get("basis") == "data_observation") < 2:
        errors.append({"slot": "recommendations", "error": "data_observation 최소 2개 필요"})
    for i, r in enumerate(recs):
        if r.get("basis") not in ("data_observation", "experiment_suggestion", "general_guide"):
            errors.append({"slot": f"recommendations[{i}]", "error": "basis 값 위반"})
        if r.get("basis") == "general_guide" and tokenize_numbers(r.get("body", "")):
            errors.append(
                {"slot": f"recommendations[{i}]", "error": "general_guide 는 숫자 인용 금지"}
            )
    cl = slots.get("checklist", [])
    if not (6 <= len(cl) <= 8):
        errors.append({"slot": "checklist", "error": "6~8개여야 함"})
    for i, c in enumerate(cl):
        if not re.search(r"(나요|까요|가요)\?$", c.strip()):
            errors.append({"slot": f"checklist[{i}]", "error": "'~나요?/~까요?' 질문형이어야 함"})

    # 테마 검증
    themes = {t["id"] for t in slots.get("account_themes", [])}
    if not (3 <= len(themes) <= 5):
        errors.append({"slot": "account_themes", "error": "테마 3~5개여야 함"})
    for t in slots.get("account_themes", []):
        if len(t.get("label", "")) > 12:
            errors.append({"slot": "account_themes", "error": f"라벨 10자 초과: {t['label']}"})
    ranks_needed = {tp["rank"] for tp in metrics_top_ranks(metrics)}
    ranks_given = {tp["rank"] for tp in slots.get("top_post_themes", [])}
    if not ranks_needed.issubset(ranks_given):
        errors.append(
            {
                "slot": "top_post_themes",
                "error": f"모든 top_posts rank 커버 필요 (누락: {sorted(ranks_needed-ranks_given)})",
            }
        )
    for tp in slots.get("top_post_themes", []):
        if tp.get("theme_id") not in themes:
            errors.append(
                {"slot": "top_post_themes", "error": f"미정의 theme_id {tp.get('theme_id')}"}
            )
    if len({tp["theme_id"] for tp in slots.get("top_post_themes", [])}) > 4:
        errors.append({"slot": "top_post_themes", "error": "distinct 테마 ≤4"})

    # 텍스트 검증
    for path, text in _texts_of(slots):
        if not text:
            continue
        m = HANGUL_QUANT.search(text)
        if m:
            errors.append({"slot": path, "error": f"한글 수량어 금지: '{m.group(0)}' → 숫자로"})
        m = FORBIDDEN.search(text)
        if m:
            errors.append({"slot": path, "error": f"금칙어: '{m.group(0)}'"})
        for sent in re.split(r"(?<=[.요다])\s+", text):
            if CAUSAL.search(sent) and not HEDGE.search(sent):
                errors.append({"slot": path, "error": f"인과 단정(헤지 없음): '{sent[:40]}…'"})
        for tok in tokenize_numbers(text):
            if not _match(tok, wl):
                errors.append(
                    {
                        "slot": path,
                        "error": f"숫자 '{tok['raw']}'(={tok['value']:g}) 가 지표에 없음 — "
                        f"입력 JSON 값만 사용",
                    }
                )
        if re.search(r"[\U0001F300-\U0001FAFF]", text):
            errors.append({"slot": path, "error": "이모지 금지"})

    # 헤지 필수 슬롯 — 반려 대신 코드 자동 부착(결정적 안전 문구, 재시도 낭비 방지)
    mo_slot = slots.get("monthly_observation") or {}
    mo = mo_slot.get("text", "")
    if mo and not HEDGE_REQ.search(mo):
        mo_slot["text"] = mo.rstrip() + " 다만 주제나 올린 시기 영향도 섞여 있을 수 있어요."
        mo_slot["_auto_hedged"] = True

    return {"ok": not errors, "errors": errors}


def metrics_top_ranks(metrics: dict):
    return [{"rank": p["rank"]} for p in metrics.get("top_posts", [])]


# ── 폴백 (검증 탈락·합성 실패 시 안전 문장 — 숫자만 끼움, 항상 통과 설계) ──
def fallback_slots(metrics: dict, agg: dict) -> dict:
    vs = metrics["views_stats"]
    cov = metrics["coverage"]
    d = metrics["dist"]
    top1 = (metrics.get("top_posts") or [{}])[0]
    themes = [
        {"id": "t1", "label": "주요 콘텐츠"},
        {"id": "t2", "label": "기타"},
        {"id": "t3", "label": "정보·팁"},
    ]
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
                f"{d['labels'][0]}이에요.",
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
        "monthly_observation": {
            "text": "이 시기 변화의 원인은 숫자만으로 단정하기 어려워요. "
            "다음 영상에서 첫 문장을 바꿔 올려보고 반응을 비교해 보세요.",
            "evidence_keys": ["monthly"],
            "numbers_used": [],
        },
        "low_line": {
            "text": "조회수가 낮았던 영상들의 첫 문장을 모았어요. 처음 3초에 왜 봐야 하는지가 "
            "보이는지 점검해 보세요.",
            "evidence_keys": [],
            "numbers_used": [],
        },
        "hook_note": {
            "text": "첫 문장 스타일별 성과 차이를 표에서 확인해 보세요.",
            "evidence_keys": ["hook_table"],
            "numbers_used": [],
        },
        "kw_recommendation": {
            "text": "영상 내용에 맞는 댓글 단어를 골라 번갈아 사용해 보세요. 어떤 주제에 "
            "관심이 많은지 구분하기 쉬워져요.",
            "evidence_keys": ["cta_keywords"],
            "numbers_used": [],
        },
        "account_themes": themes,
        "top_post_themes": [
            {"rank": p["rank"], "theme_id": "t1"} for p in metrics.get("top_posts", [])
        ],
        "recommendations": [
            {
                "title": "잘됐던 주제 다시 만들기",
                "body": f"가장 잘된 영상({man(top1.get('views'))})의 주제를 최신 내용으로 다시 만들어 보세요.",
                "priority": "high",
                "basis": "data_observation",
                "evidence_keys": ["top_posts"],
                "numbers_used": [],
            },
            {
                "title": "다음 영상 목표 정하기",
                "body": f"다음 영상은 {man(vs['p75'])}를 목표로 해보세요. {man(vs['p90'])}을 넘기면 "
                f"후속편을 준비해 보세요.",
                "priority": "high",
                "basis": "data_observation",
                "evidence_keys": ["views_stats"],
                "numbers_used": [],
            },
            {
                "title": "올리는 시간 실험하기",
                "body": "몇 주간 시간대를 고정해 올리고 반응을 비교해 보세요.",
                "priority": "mid",
                "basis": "experiment_suggestion",
                "evidence_keys": ["timing_hours"],
                "numbers_used": [],
            },
            {
                "title": "첫 문장 점검하기",
                "body": "영상 시작 1.5초 안에 계속 볼 이유를 주세요. 결과나 얻을 것을 먼저 보여주는 게 좋아요.",
                "priority": "mid",
                "basis": "general_guide",
                "evidence_keys": [],
                "numbers_used": [],
            },
        ],
        "checklist": [
            "첫 문장이 궁금하게 만들거나 필요하게 만드나요?",
            "제목이나 자막에 숫자나 결과가 있나요?",
            "댓글로 남길 한 단어가 영상 내용과 맞나요?",
            "시작 3초에 결과나 얻을 것을 먼저 보여주나요?",
            "해시태그를 너무 많이 넣지 않았나요?",
            "올리는 시간이 팔로워가 활동하는 때인가요?",
        ],
        "_fallback": True,
    }


def run_gate(slots: dict, metrics: dict, agg: dict, resynth_fn, log=print) -> tuple[dict, dict]:
    """검증→재합성 루프. 반환 (확정 슬롯, gate_meta)."""
    meta = {"attempts": [], "fallback_slots": []}
    cur = slots
    for attempt in range(config.SYNTH_MAX_RETRY):
        v = verify_slots(cur, metrics, agg)
        meta["attempts"].append({"n": attempt + 1, "errors": v["errors"]})
        if v["ok"]:
            log(f"    검증 통과 (시도 {attempt+1})")
            return cur, meta
        log(f"    검증 실패 {len(v['errors'])}건 (시도 {attempt+1}) → 재합성")
        fb = "\n".join(f"- [{e['slot']}] {e['error']}" for e in v["errors"][:20])
        try:
            cur = resynth_fn(fb)
        except Exception as e:  # noqa: BLE001
            log(f"    재합성 호출 실패: {e}")
            break
    log("    최종 실패 → 전 슬롯 폴백 사용")
    fb_slots = fallback_slots(metrics, agg)
    v = verify_slots(fb_slots, metrics, agg)
    if not v["ok"]:
        raise RuntimeError(f"폴백 슬롯이 검증 실패 — 파이프라인 버그: {v['errors'][:5]}")
    meta["fallback_slots"] = ["ALL"]
    return fb_slots, meta
