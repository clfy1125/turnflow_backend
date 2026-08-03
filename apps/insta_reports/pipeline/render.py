"""S8 렌더러 — 템플릿에 데이터 바인딩. 숫자=코드, AI=지정 슬롯만.

썸네일·프로필 사진은 WebP data-URI 인라인 → 리포트가 CDN 만료와 무관한
자기완결 HTML (critic HIGH 해소).
"""

import base64
import io
import json
import pathlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader
from PIL import Image

from . import config
from . import feature_schema as fs
from .metrics import man

KST = timezone(timedelta(hours=9))


# 최종 산출물은 **사용자가 내려받아 로컬에서 여는 자기완결 HTML** 이다 → 외부 스크립트 참조가
# 있으면 인터넷 없이 차트가 안 뜬다. 벤더링한 Chart.js 를 파일 안에 직접 심는다(약 200KB).
_CHARTJS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "static" / "insta_reports" / "chart.umd.min.js"
)


def _chartjs_source() -> str:
    try:
        return _CHARTJS_PATH.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 배포 누락 시 차트만 비고 나머지는 정상
        return ""


def _img_data_uri(path_or_url: str, width: int = 360) -> str:
    try:
        if str(path_or_url).startswith("http"):
            raw = requests.get(
                path_or_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
            ).content
        else:
            raw = Path(path_or_url).read_bytes()
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width > width:
            im = im.resize((width, int(im.height * width / im.width)))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=72)
        return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return ""


def _cta_msgs(cc: dict) -> tuple[str, str]:
    w, wo = cc["with"], cc["without"]
    if w["n"] < 3 or wo["n"] < 3:
        return ("영상 수가 적어 비교가 어려워요.", "영상 수가 적어 비교가 어려워요.")
    wm = (
        "잘될 때는 평소보다 훨씬 크게 터졌어요."
        if w["peak"] and w["usual"] and w["peak"] / w["usual"] >= 2.5
        else "평소와 잘될 때의 차이가 크지 않았어요."
    )
    if w["usual"] and wo["usual"] and abs(w["usual"] - wo["usual"]) / w["usual"] < 0.15:
        wom = (
            "평소 조회수는 비슷하지만, 크게 터지는 힘은 더 약했어요."
            if (w["peak"] or 0) > (wo["peak"] or 0)
            else "두 그룹의 차이가 크지 않았어요."
        )
    else:
        wom = "평소 조회수부터 차이가 났어요."
    return wm, wom


def _timing_line(m: dict) -> str:
    bs = m["best_slot"]
    if not bs["signal"]:
        return "요일·시간대별 차이가 아직 뚜렷하지 않았어요. 팔로워 활동 시간에 맞춰 꾸준히 올리는 게 우선이에요."
    dows = "·".join(bs["dows"][:3])
    return (
        f"지금까지 올린 영상 중에서는 {bs['hour_label']}({bs['hour_n']}개 기준), "
        f"그리고 {dows}에 올린 영상이 잘됐어요. 다음 게시물부터 먼저 이 시간대를 써보세요."
    )


def _top_cluster_line(top_posts: list) -> str:
    if len(top_posts) < 6:
        return ""
    months = sorted(p["date_kst"][:7] for p in top_posts)
    for i in range(len(months) - 5):
        a = datetime.strptime(months[i], "%Y-%m")
        b = datetime.strptime(months[i + 5], "%Y-%m")
        if (b.year - a.year) * 12 + b.month - a.month <= 2:
            return f"상위 6개가 {months[i][2:7].replace('-', '.')}~{months[i+5][5:7]}에 집중"
    return "상위 영상이 여러 시기에 고르게 나왔어요"


DONUT_GROUPS = [
    ("감탄·공감", ["praise", "empathy"], "#34d399", "순수 호응이에요. 내용이 좋았다는 신호."),
    ("자료 요청", ["request"], "#e1306c", "받아갈 것을 원해요. 관심이 가장 높은 반응이에요."),
    (
        "DM 못 받았다는 문의",
        ["dm_not_received"],
        "#f87171",
        "약속한 자료·DM이 안 갔다는 문의예요. 팔로워 니즈가 아니라 발송이 실패했다는 신호라서 "
        "따로 분리했어요.",
    ),
    ("질문", ["question"], "#fd7e2a", "시작을 망설이는 사람들 — 다음 콘텐츠 소재이기도 해요."),
    ("후기·경험", ["testimonial"], "#6ea8fe", "직접 해본 사람들 — 가장 강한 신뢰 신호예요."),
    ("응원·팬심", ["support"], "#c86dd7", "크리에이터 자체를 좋아하는 반응이에요."),
    ("기타", ["other"], "#6d6485", "이모지·짧은 반응 등이에요."),
]
MOTIVATION_ICON = {"practical": "🛠️", "question": "🐣", "wow": "😮", "fan": "🤝"}


def render_report_v3(
    canon: dict, metrics: dict, agg: dict, slots: dict, out_name: str | None = None
) -> Path:
    from . import feature_schema as fsch

    env = Environment(loader=FileSystemLoader(str(config.TEMPLATE_PATH.parent)), autoescape=True)
    env.globals["man"] = man
    tpl = env.get_template("report_v3.html.j2")

    a, m = canon["account"], metrics
    cstats = agg["comment_stats"]
    cfilter = agg["comment_filter"]
    video_on = bool(agg.get("video_analyzed"))

    def table_rows(table, label_map, desc_map, real_key):
        """예시는 실제 채록 문구 우선, 영어·빈 값이면 범용 예시로 대체(영어 노출 차단)."""
        rows = []
        for c in table:
            key = c["key"]
            desc, generic = desc_map.get(key, ("", []))
            real = [(e.get(real_key) or "").strip() for e in c.get("examples", [])]
            real = [t for t in real if t and not fsch._is_english(t)]
            examples = [f"“{fsch.plainify(t)[:44]}”" for t in real[:2]]
            if len(examples) < 2:
                examples += generic[: 2 - len(examples)]
            rows.append(
                {
                    "label": label_map.get(key, key),
                    "desc": desc,
                    "examples": examples[:2],
                    "n": c["n"],
                    "median": c["median"],
                    "max": c["max"],
                }
            )
        return rows

    hook_rows = (
        table_rows(agg["hook_table"], fsch.HOOK_LABEL_KO, fsch.HOOK_DESC_KO, "hook_text")
        if video_on
        else []
    )
    opening_rows = (
        table_rows(
            agg["opening_table"], fsch.OPENING_LABEL_KO, fsch.OPENING_DESC_KO, "opening_desc"
        )
        if video_on
        else []
    )
    lows = [
        fsch.HOOK_LABEL_KO.get(c["key"], c["key"])
        for c in agg.get("hook_table", [])
        if c["low_sample"]
    ]
    hook_low_note = (
        "※ " + "·".join(lows) + " 스타일은 영상 수가 적어요. 경향만 참고하세요." if lows else ""
    )

    # TOP posts + why
    why_by_rank = {w["rank"]: w for w in slots.get("top_posts_why", [])}
    for w in why_by_rank.values():  # 코드 폴백 문장에 남은 업계 용어 정리
        if w.get("_fb"):
            w["why"] = fsch.plainify(w.get("why", ""))
    top_posts = []
    for p in m["top_posts"][:6]:
        w = why_by_rank.get(p["rank"], {})
        top_posts.append(
            {
                **p,
                "date_short": p["date_kst"][2:].replace("-", "."),
                "thumb_data_uri": _img_data_uri(p["thumb_local"]) if p["thumb_local"] else "",
                "why": w.get("why", ""),
            }
        )

    # 아쉬운 영상 — 시작 묘사 (피처 우선)
    lowf = {r["shortcode"]: r for r in agg.get("low_posts_features", [])}
    low_posts = []
    for p in m["low_posts"]:
        f = lowf.get(p["shortcode"])
        if f and f["hook_type"] == "none":
            od = f.get("opening_desc") or ""
            start = (
                fsch.plainify(od)
                if (od and not fsch._is_english(od))
                else "말·자막 없이 화면만 나오는 시작"
            )
        elif f and f["hook_text"]:
            start = f"“{fsch.plainify(f['hook_text'])}”"
        else:
            start = f"“{p['first_line'][:50]}”"
        low_posts.append(
            {**p, "date_short": p["date_kst"][2:].replace("-", "."), "start_desc": start}
        )

    # 도넛 + 범례
    donut_labels, donut_counts, donut_colors, donut_legend = [], [], [], []
    for label, cats, color, desc in DONUT_GROUPS:
        cnt = sum(cstats["counts"].get(c, 0) for c in cats)
        if cnt == 0:
            continue
        pct = round(cnt / max(1, cstats["n_analyzed"]) * 100)
        donut_labels.append(f"{label} {pct}%")
        donut_counts.append(cnt)
        donut_colors.append(color)
        donut_legend.append({"label": label, "pct": pct, "color": color, "desc": desc})

    # 인용 해석 (quote_id → 원문)
    id2 = {}
    for cat, qs in (cstats.get("quote_pool") or {}).items():
        for q in qs:
            id2[q["quote_id"]] = (cat, q["text"])
    fans_wants = []
    for w in slots.get("fans_wants", []):
        cat, text = id2.get(w.get("quote_id"), ("other", "(댓글 인용 없음)"))
        from .comments import CATEGORY_KO

        fans_wants.append(
            {
                "title": w.get("title", ""),
                "note": w.get("note", ""),
                "quote_text": text[:120],
                "quote_meta": f"{CATEGORY_KO.get(cat, cat)} 댓글 "
                f"{cstats['counts'].get(cat, 0)}개 중",
            }
        )

    desc_by_key = {d["key"]: d["desc"] for d in slots.get("motivation_descs", [])}
    motivations = [
        {**mv, "icon": MOTIVATION_ICON.get(mv["key"], "•"), "desc": desc_by_key.get(mv["key"], "")}
        for mv in cstats["motivations"]
    ]

    mon = m["monthly"]
    monthly_immature_note = (
        "최근 달은 아직 조회수가 쌓이는 중이라 낮게 보일 수 있어요."
        if mon["immature"] and mon["immature"][-1]
        else ""
    )
    dropped = m.get("monthly_dropped") or {}
    if dropped:
        months_ko = ", ".join(
            f"{k[:4]}년 {int(k[5:7])}월({v}개)" for k, v in sorted(dropped.items())
        )
        monthly_dropped_note = (
            f"영상이 3개 미만인 달({months_ko})은 그달을 대표하기 어려워 "
            "그래프에서 빼놨어요 — 고정해둔 옛 게시물이 섞이면 흐름이 "
            "왜곡되기 때문이에요."
        )
    elif m.get("monthly_low_sample"):
        # 하한(3개)을 넘는 달이 1개뿐이라 전 구간을 되살린 경우 — 빼면 차트가 비어 버린다.
        counts_ko = ", ".join(
            f"{mo[:4]}년 {int(mo[5:7])}월({n}개)"
            for mo, n in zip(mon["months"], mon["count"], strict=False)
        )
        monthly_dropped_note = (
            f"한 달에 올린 영상이 적어서({counts_ko}) 점 하나가 영상 1~2개일 수 있어요 — "
            "달마다의 오르내림보다 <b>전체 흐름</b>만 봐 주세요."
        )
    else:
        monthly_dropped_note = ""

    chart_data = json.dumps(
        {
            "dist_labels": m["dist"]["labels"],
            "dist": m["dist"]["counts"],
            "months": mon["months"],
            "mon_med": mon["median"],
            "mon_avg": mon["mean"],
            "mon_cnt": mon["count"],
            "cmt_labels": donut_labels,
            "cmt": donut_counts,
            "cmt_colors": donut_colors,
        },
        ensure_ascii=False,
    )

    # CSV / TXT 내보내기 (실데이터)
    rows = ["순위,날짜,유형,조회수,좋아요,댓글,첫문장스타일,제목"]
    brief = {r["shortcode"]: r for r in agg.get("video_rows_brief", [])}
    for i, p in enumerate(
        sorted([q for q in canon["posts"] if q.get("views")], key=lambda q: -q["views"]), 1
    ):
        b = brief.get(p["shortcode"], {})
        hook_ko = fsch.HOOK_LABEL_KO.get(b.get("hook_type", ""), "")
        title = p["caption_features"]["first_line"][:40].replace(",", " ").replace('"', "'")
        rows.append(
            f"{i},{p['taken_at_kst'][:10]},릴스,{p['views']},{p['likes'] or ''},"
            f"{p['comments'] or ''},{hook_ko},{title}"
        )
    csv_data = "﻿" + "\n".join(rows)
    txt_lines = [f"[내 인스타 성장 리포트 · @{a['username']}]", ""]
    txt_lines += ["■ 가장 중요한 3가지"] + [
        f"{i+1}. {t['headline']} — {t['body']}" for i, t in enumerate(slots["top3"])
    ]
    txt_lines += [
        "",
        "■ 다음 영상 조회수 목표",
        f"평소 {man(m['views_stats']['median'])} / 잘됨 {man(m['benchmark']['tiers'][2])} / "
        f"대박 {man(m['benchmark']['tiers'][3])}",
        "",
        "■ 추천",
    ]
    for i, r in enumerate(slots["recommendations"]):
        txt_lines.append(f"{i+1}. {r['title']}: {r.get('what_to_do') or r.get('body', '')}")
        if r.get("why"):
            txt_lines.append(f"   왜? {r['why']}")
        if r.get("evidence_line"):
            txt_lines.append(f"   근거: {r['evidence_line']}")
    txt_data = "\n".join(txt_lines)

    html = tpl.render(
        a=a,
        cov=m["coverage"],
        vs=m["views_stats"],
        eng=m["engagement"],
        bench=m["benchmark"],
        slots=slots,
        agg=agg,
        video_on=video_on,
        cstats=cstats,
        cfilter=cfilter,
        data_date=(m["coverage"].get("data_date") or "")[:4]
        + "."
        + (m["coverage"].get("data_date") or "    ")[4:6]
        + "."
        + (m["coverage"].get("data_date") or "      ")[6:8],
        top1_date=(m["top_posts"][0]["date_kst"][2:7].replace("-", ".") if m["top_posts"] else ""),
        # 프로필 사진: IG 서명 URL 이 만료됐으면 우리 스토리지 캐시본으로 대체한다.
        pfp_data_uri=(
            _img_data_uri(a.get("profile_picture_url", ""), width=200)
            or _img_data_uri(a.get("profile_picture_fallback_url", ""), width=200)
        ),
        hook_rows=hook_rows,
        opening_rows=opening_rows,
        hook_low_note=hook_low_note,
        top_posts=top_posts,
        low_posts=low_posts,
        donut_legend=donut_legend,
        fans_wants=fans_wants,
        motivations=motivations,
        dm_issue={
            "count": cstats.get("dm_not_received_count", 0),
            "pct": cstats.get("dm_not_received_pct", 0),
            "quotes": cstats.get("dm_not_received_quotes", []),
        },
        monthly_immature_note=monthly_immature_note,
        monthly_dropped_note=monthly_dropped_note,
        chart_data=chart_data,
        chartjs_source=_chartjs_source(),
        csv_name=f"{a['username']}_report.csv",
        csv_data=csv_data,
        txt_name=f"{a['username']}_요약.txt",
        txt_data=txt_data,
    )
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    name = out_name or f"{canon['username']}_v3_{datetime.now(KST):%Y%m%d}.html"
    out = config.REPORTS_DIR / name
    out.write_text(html, encoding="utf-8")
    return out


def render_report(
    canon: dict, metrics: dict, agg: dict | None, slots: dict, out_name: str | None = None
) -> Path:
    env = Environment(loader=FileSystemLoader(str(config.TEMPLATE_PATH.parent)), autoescape=True)
    env.globals["man"] = man
    tpl = env.get_template(config.TEMPLATE_PATH.name)

    a = canon["account"]
    m = metrics
    video_on = bool(agg and agg.get("video_analyzed"))

    theme_by_rank = {}
    labels = {t["id"]: t["label"] for t in slots.get("account_themes", [])}
    for tp in slots.get("top_post_themes", []):
        theme_by_rank[tp["rank"]] = labels.get(tp["theme_id"], "기타")

    top_posts = []
    for p in m["top_posts"]:
        q = dict(p)
        q["theme"] = theme_by_rank.get(p["rank"], "기타")
        q["date_short"] = p["date_kst"][2:].replace("-", ".")
        q["thumb_data_uri"] = _img_data_uri(p["thumb_local"]) if p["thumb_local"] else ""
        top_posts.append(q)

    low_posts = [{**p, "date_short": p["date_kst"][2:].replace("-", ".")} for p in m["low_posts"]]

    # 영상 피처 표 (low_sample 셀은 '기타' 병합 대신 각주)
    hook_rows, opening_rows, hook_low_note, opening_msg = [], [], "", ""
    good_hooks, weak_hooks = [], []
    if video_on:
        for c in agg["hook_table"]:
            hook_rows.append(
                {
                    "label": fs.HOOK_LABEL_KO.get(c["key"], c["key"]),
                    "n": c["n"],
                    "median": c["median"],
                    "max": c["max"],
                }
            )
        lows = [
            fs.HOOK_LABEL_KO.get(c["key"], c["key"]) for c in agg["hook_table"] if c["low_sample"]
        ]
        if lows:
            hook_low_note = (
                "※ "
                + "·".join(lows)
                + f" 스타일은 영상이 {config.MIN_CELL_N}개 미만이에요. 경향만 참고하세요."
            )
        for c in agg["opening_table"]:
            opening_rows.append(
                {
                    "label": fs.OPENING_LABEL_KO.get(c["key"], c["key"]),
                    "n": c["n"],
                    "median": c["median"],
                    "max": c["max"],
                }
            )
        solid = [c for c in agg["opening_table"] if not c["low_sample"] and c["median"]]
        if len(solid) >= 2:
            best, worst = max(solid, key=lambda c: c["median"]), min(
                solid, key=lambda c: c["median"]
            )
            if best["key"] != worst["key"]:
                opening_msg = (
                    f"'{fs.OPENING_LABEL_KO[best['key']]}' 영상이 "
                    f"'{fs.OPENING_LABEL_KO[worst['key']]}' 영상보다 잘됐어요"
                    f"(평소 {man(best['median'])} vs {man(worst['median'])}, "
                    f"각 {best['n']}·{worst['n']}개)."
                )
        good_hooks = agg["good_hooks"][:4]
        weak_hooks = agg["weak_hooks"][:4]

    # 올릴 때 참고 (코드 팁)
    tip_cards = []
    t = m.get("tips", {})
    if "hashtag" in t:
        h = t["hashtag"]
        better = "적게" if h["few_median"] >= h["many_median"] else "많이"
        tip_cards.append(
            {
                "v": better,
                "l": "해시태그",
                "s": f"해시태그 3개 이하 영상 평소 {man(h['few_median'])}({h['few_n']}개), "
                f"4개 이상 {man(h['many_median'])}({h['many_n']}개)였어요.",
            }
        )
    if "caption_len_best" in t:
        b = t["caption_len_best"]
        tip_cards.append(
            {
                "v": b["range"],
                "l": "잘됐던 글 길이",
                "s": f"이 길이대 영상의 평소 조회수가 {man(b['median'])}({b['n']}개)로 높았어요.",
            }
        )
    if "carousel_like_ratio" in t:
        tip_cards.append(
            {
                "v": f"×{t['carousel_like_ratio']}",
                "l": "사진 여러 장 게시물의 좋아요",
                "s": "릴스 대비 사진 여러 장 게시물의 좋아요 비율이에요. 정보 정리형 내용은 "
                "여러 장 게시물로도 만들어보세요.",
            }
        )

    eng = dict(m["engagement"])
    cgl_ratio = eng["comment_gt_like"] / max(1, eng["n"])
    comment_gt_like_note = (
        "댓글 유도가 잘 먹히는 계정이에요"
        if cgl_ratio >= 0.4
        else "좋아요 중심으로 반응이 모이는 계정이에요"
    )

    wm, wom = _cta_msgs(m["cta_caption"])
    mon = m["monthly"]
    monthly_immature_note = (
        "최근 달은 아직 조회수가 쌓이는 중이라 낮게 보일 수 있어요."
        if mon["immature"] and mon["immature"][-1]
        else ""
    )

    chart_data = json.dumps(
        {
            "dist_labels": m["dist"]["labels"],
            "dist": m["dist"]["counts"],
            "months": mon["months"],
            "mon_med": mon["median"],
            "mon_avg": mon["mean"],
            "mon_cnt": mon["count"],
            "dn": m["timing_dow"]["labels"],
            "dow_med": m["timing_dow"]["median"],
            "dow_cnt": m["timing_dow"]["count"],
            "hb": m["timing_hours"]["labels"],
            "hr_med": m["timing_hours"]["median"],
            "hr_cnt": m["timing_hours"]["count"],
        },
        ensure_ascii=False,
    )

    html = tpl.render(
        a=a,
        cov=m["coverage"],
        vs=m["views_stats"],
        eng=eng,
        dist=m["dist"],
        bench=m["benchmark"],
        ctac=m["cta_caption"],
        slots=slots,
        agg=agg or {},
        video_on=video_on,
        data_date=(m["coverage"].get("data_date") or "")[:4]
        + "."
        + (m["coverage"].get("data_date") or "    ")[4:6]
        + "."
        + (m["coverage"].get("data_date") or "      ")[6:8],
        top1_date=(m["top_posts"][0]["date_kst"][2:7].replace("-", ".") if m["top_posts"] else ""),
        # 프로필 사진: IG 서명 URL 이 만료됐으면 우리 스토리지 캐시본으로 대체한다.
        pfp_data_uri=(
            _img_data_uri(a.get("profile_picture_url", ""), width=200)
            or _img_data_uri(a.get("profile_picture_fallback_url", ""), width=200)
        ),
        top_posts=top_posts,
        low_posts=low_posts,
        top_cluster_line=_top_cluster_line(m["top_posts"]),
        hook_rows=hook_rows,
        opening_rows=opening_rows,
        hook_low_note=hook_low_note,
        opening_msg=opening_msg,
        good_hooks=good_hooks,
        weak_hooks=weak_hooks,
        kw_rows=m.get("cta_keywords") or [],
        tip_cards=tip_cards,
        hashtags=m.get("hashtags_top") or [],
        cta_with_msg=wm,
        cta_without_msg=wom,
        timing_line=_timing_line(m),
        comment_gt_like_note=comment_gt_like_note,
        monthly_immature_note=monthly_immature_note,
        chart_data=chart_data,
        chartjs_source=_chartjs_source(),
    )
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    name = out_name or f"{canon['username']}_{datetime.now(KST):%Y%m%d}.html"
    out = config.REPORTS_DIR / name
    out.write_text(html, encoding="utf-8")
    return out
