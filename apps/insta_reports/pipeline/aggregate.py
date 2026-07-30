"""S5 조인·집계 — 피처 × 성과 교차표 + 합성 입력(aggregates.json).

표본 < MIN_CELL_N 셀은 low_sample 플래그(기획서 4.4 — 합성이 단정에 못 쓰게).
derived.ratios/pcts 는 검증 화이트리스트와 합성 입력에 동시 사용(제공 숫자만 원칙).
"""

import json
import statistics as st

from . import config
from . import feature_schema as fs


def _cell(rows):
    vs = [r["views"] for r in rows]
    return {
        "n": len(rows),
        "median": int(st.median(vs)) if vs else None,
        "max": max(vs) if vs else None,
        "low_sample": len(rows) < config.MIN_CELL_N,
    }


def build_aggregates(canon: dict, metrics: dict, extraction: dict, sample: dict) -> dict:
    by_sc = {p["shortcode"]: p for p in canon["posts"]}
    feats = extraction["features"]

    video_rows = []
    for sc, env in feats.items():
        p = by_sc.get(sc)
        if not p or env["mode"] != "video" or not p.get("views"):
            continue
        f = env["feature"]
        video_rows.append(
            {
                "shortcode": sc,
                "views": p["views"],
                "likes": p["likes"],
                "comments": p["comments"],
                "hook_type": f["hook"]["type"] if f["hook"]["source"] != "none" else "none",
                "hook_text": f["hook"]["text_verbatim"],
                "opening": f["opening"]["screen_type"],
                "opening_desc": f["opening"]["visual_description"][:60],
                "value_2s": f["opening"]["value_shown_in_2s"],
                "cta_types": f["cta"]["types"],
                "cta_keyword": f["cta"]["comment_keyword_verbatim"],
                "structure": f["structure"]["type"],
                "cut_pace": f["pacing"]["cut_pace"],
                "voice": f["audio"]["has_voiceover"],
                "subtitles": f["audio"]["has_subtitles"],
                "is_promotional": f["commercial"]["is_promotional"],
                "topics": f["topic_keywords"],
                "stratum": next(
                    (v["stratum"] for v in sample["videos"] if v["shortcode"] == sc), "?"
                ),
            }
        )

    def table(key_fn, keys):
        out = []
        for k in keys:
            rows = [r for r in video_rows if key_fn(r) == k]
            if rows:
                out.append(
                    {
                        "key": k,
                        **_cell(rows),
                        "examples": sorted(rows, key=lambda r: -r["views"])[:2],
                    }
                )
        return sorted(out, key=lambda c: -(c["max"] or 0))

    hook_table = table(lambda r: r["hook_type"], fs.HOOK_TYPES + ["none"])
    opening_table = table(lambda r: r["opening"], fs.OPENING_TYPES)

    cta_v = [r for r in video_rows if "none" not in r["cta_types"]]
    cta_n = [r for r in video_rows if "none" in r["cta_types"]]
    promo = [r for r in video_rows if r["is_promotional"]]
    nonpromo = [r for r in video_rows if not r["is_promotional"]]
    v2s_y = [r for r in video_rows if r["value_2s"]]
    v2s_n = [r for r in video_rows if not r["value_2s"]]

    agg = {
        "video_analyzed": len(video_rows),
        "video_total": metrics["coverage"]["reels_with_views"],
        "images_analyzed": sum(1 for e in feats.values() if e["mode"] == "image"),
        "failures": len(extraction.get("failures", {})),
        "hook_table": hook_table,
        "opening_table": opening_table,
        "cta_video": {"with": _cell(cta_v), "without": _cell(cta_n)},
        "value_2s": {"yes": _cell(v2s_y), "no": _cell(v2s_n)},
        "promotional": {"promo": _cell(promo), "non_promo": _cell(nonpromo)},
        "structure_table": table(lambda r: r["structure"], fs.STRUCTURE_TYPES),
        "pace_table": table(lambda r: r["cut_pace"], ["slow", "medium", "fast"]),
        "good_hooks": [
            {"text": r["hook_text"], "views": r["views"]}
            for r in sorted(video_rows, key=lambda r: -r["views"])[:4]
            if r["hook_text"]
        ],
        "weak_hooks": [
            {"text": r["hook_text"], "views": r["views"]}
            for r in sorted(video_rows, key=lambda r: r["views"])[:4]
            if r["hook_text"]
        ],
        "low_posts_features": [
            {
                "shortcode": r["shortcode"],
                "views": r["views"],
                "hook_type": r["hook_type"],
                "opening": r["opening"],
                "hook_text": r["hook_text"][:60],
                "opening_desc": r["opening_desc"],
            }
            for r in sorted(video_rows, key=lambda r: r["views"])[:8]
        ],
        "topics_pool": sorted({t for r in video_rows for t in r["topics"]}),
        "video_rows_brief": [
            {
                "shortcode": r["shortcode"],
                "views": r["views"],
                "hook_type": r["hook_type"],
                "opening": r["opening"],
                "topics": r["topics"],
                "stratum": r["stratum"],
                "promotional": r["is_promotional"],
            }
            for r in video_rows
        ],
    }

    # ── derived: 합성이 인용 가능한 사전 계산 비율/배수 전량 ──
    ratios, pcts = [], []

    def add_ratio(key, a, b):
        if a and b:
            ratios.append({"key": key, "value": round(a / b, 1)})

    ht = {c["key"]: c for c in hook_table if not c["low_sample"]}
    for k1 in ht:
        for k2 in ht:
            if k1 != k2:
                add_ratio(f"hook.{k1}.median/hook.{k2}.median", ht[k1]["median"], ht[k2]["median"])
    ot = {c["key"]: c for c in opening_table if not c["low_sample"]}
    for k1 in ot:
        for k2 in ot:
            if k1 != k2:
                add_ratio(
                    f"opening.{k1}.median/opening.{k2}.median", ot[k1]["median"], ot[k2]["median"]
                )
    vs_ = metrics["views_stats"]
    add_ratio("views.max/views.median", vs_["max"], vs_["median"])
    add_ratio("views.mean/views.median", vs_["mean"], vs_["median"])
    cc = metrics["cta_caption"]
    if cc["with"]["n"] and cc["without"]["n"]:
        add_ratio("cta.with.peak/cta.without.peak", cc["with"]["peak"], cc["without"]["peak"])
        add_ratio("cta.with.usual/cta.without.usual", cc["with"]["usual"], cc["without"]["usual"])
        add_ratio("cta.with.peak/cta.with.usual", cc["with"]["peak"], cc["with"]["usual"])
    cv = agg["cta_video"]
    if cv["with"]["n"] and cv["without"]["n"] and cv["with"]["median"] and cv["without"]["median"]:
        add_ratio(
            "cta_video.with.median/cta_video.without.median",
            cv["with"]["median"],
            cv["without"]["median"],
        )
    if v2s_y and v2s_n:
        add_ratio(
            "value2s.yes.median/value2s.no.median",
            agg["value_2s"]["yes"]["median"],
            agg["value_2s"]["no"]["median"],
        )
    if promo and nonpromo:
        add_ratio(
            "promo.median/nonpromo.median",
            agg["promotional"]["promo"]["median"],
            agg["promotional"]["non_promo"]["median"],
        )

    d = metrics["dist"]
    for i, c in enumerate(d["counts"]):
        pcts.append(
            {"key": f"dist.counts[{i}]/total", "value": round(c / max(1, sum(d["counts"])) * 100)}
        )
    agg["derived"] = {"ratios": ratios, "pcts": pcts}
    return agg


def _chips_of(f: dict) -> list[str]:
    """게시물 피처 → 허용 칩 어휘 (코드 파생 — AI 는 이 안에서만 선택)."""
    chips = []
    ht = f["hook"]["type"]
    if f["hook"]["source"] != "none" and ht not in ("other",):
        chips.append(fs.HOOK_LABEL_KO.get(ht, ht) + " 시작")
    op = f["opening"]["screen_type"]
    if op == "result_showcase":
        chips.append("결과물 먼저")
    elif op == "talking_face":
        chips.append("얼굴 등장")
    cta = f["cta"]["types"]
    if "comment_keyword" in cta or "comment_open" in cta:
        chips.append("댓글 유도")
    if "save" in cta:
        chips.append("저장 유도")
    if "follow" in cta:
        chips.append("팔로우 유도")
    st = f["structure"]["type"]
    if st == "tutorial_steps":
        chips.append("따라하기 구조")
    if st == "before_after":
        chips.append("비포/애프터")
    if f["audio"]["has_subtitles"]:
        chips.append("자막 있음")
    if f["opening"].get("value_shown_in_2s"):
        chips.append("얻을 것 먼저 제시")
    return chips[:6]


def build_v3_extras(metrics: dict, extraction: dict, cstats: dict, cfilter: dict) -> dict:
    """v3 템플릿 전용 집계: top 게시물 칩·훅 원문·댓글 통계."""
    feats = extraction["features"]
    top_posts_meta = []
    for p in metrics["top_posts"][:6]:
        env = feats.get(p["shortcode"])
        item = {
            "rank": p["rank"],
            "shortcode": p["shortcode"],
            "has_features": bool(env),
            "views": p["views"],
            "comments": p["comments"],
            "likes": p["likes"],
        }
        if env:
            f = env["feature"]
            item["allowed_chips"] = _chips_of(f)
            # "왜 잘됐나"를 날카롭게 쓰려면 첫 3초의 실제 내용이 필요하다.
            item["first_words"] = f["hook"]["text_verbatim"][:100]
            item["first_screen"] = f["opening"]["visual_description"][:80]
            item["value_shown_in_2s"] = f["opening"]["value_shown_in_2s"]
            item["on_screen_text"] = f["opening"]["on_screen_text_verbatim"][:80]
            item["segments"] = [
                {"start": s["start_sec"], "role": s["role"], "label": s["label"][:40]}
                for s in (f["structure"]["segments"] or [])[:6]
            ]
            item["structure_type"] = f["structure"]["type"]
            item["cut_count_first_10s"] = f["pacing"]["cut_count_first_10s"]
            item["cta_quotes"] = [q["quote_verbatim"][:60] for q in (f["cta"]["quotes"] or [])[:2]]
            item["cta_keyword"] = f["cta"]["comment_keyword_verbatim"]
            item["transcript_short"] = f["audio"]["transcript_short"][:300]
            item["topics"] = f["topic_keywords"]
        else:
            item["allowed_chips"] = ["댓글 유도"] if p["has_cta"] else []
            item["first_words"] = ""
        top_posts_meta.append(item)
    return {
        "top_posts_meta": top_posts_meta,
        "comment_stats": cstats,
        "comment_filter": cfilter["stats"],
    }


def save_aggregates(username: str, agg: dict) -> None:
    p = config.RUNS_DIR / username / "aggregates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(agg, ensure_ascii=False, indent=1), encoding="utf-8")
