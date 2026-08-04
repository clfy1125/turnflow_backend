"""S2 지표 엔진 (결정적 코드) — metrics.json = 리포트의 유일한 숫자 원천.

전부 코드 계산. 동적 버킷/티어(범용성: 계정 규모 무관), KST 기준 시간대,
마지막 미성숙 월 플래그(가짜 하락 서술 방지).
"""

import json
import statistics as st
from datetime import UTC, datetime, timedelta, timezone

from . import config

KST = timezone(timedelta(hours=9))

NICE = [
    1_000,
    2_000,
    3_000,
    5_000,
    10_000,
    20_000,
    30_000,
    50_000,
    100_000,
    200_000,
    300_000,
    500_000,
    1_000_000,
    2_000_000,
    3_000_000,
    5_000_000,
    10_000_000,
]
DOW_KO = ["월", "화", "수", "목", "금", "토", "일"]


def man(v) -> str:
    """렌더러와 동일한 만 단위 표기."""
    if v is None:
        return "—"
    v = float(v)
    return f"{round(v/1000)/10:g}만" if v >= 10000 else f"{int(round(v)):,}"


def _nice(x: float) -> int:
    return min(NICE, key=lambda n: abs(n - x))


def _pct(xs: list, q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _round_tier(v: float) -> int:
    if v < 1_000:
        return int(round(v, -1))
    if v < 100_000:
        return int(round(v, -2))
    # 2유효숫자
    from math import floor, log10

    d = 10 ** (floor(log10(v)) - 1)
    return int(round(v / d) * d)


# ── 계정 성격 판정 ───────────────────────────────────────────────────
# ⚠️ 왜 필요한가: 이게 없으면 리포트가 **중간 규모 계정 기준의 조언 하나**만 반복한다.
#    "막 시작한 사람"에게 필요한 말과 "영상마다 터지는 대형 계정"에 필요한 말은 정반대다.
#    (실측 @jinyongjin92: 팔로워 25,934 인데 평소 조회수 41.3만 = **도달 16배**.
#     이 계정에 "조회수를 늘리세요" 는 무의미하고, 문제는 팔로워 전환이다.)
SCALE_THRESHOLDS = [
    # (키, 라벨, 평소 조회수 상한)
    ("starting", "막 시작한 단계", 1_000),
    ("growing", "성장 중", 10_000),
    ("established", "자리 잡은 단계", 100_000),
    ("large", "대형", None),
]
# 평소 조회수 / 팔로워 수 — 이 배수가 크면 팔로워 밖(탐색·추천)에서 보고 있다는 뜻.
EXPLORE_RATIO = 3.0
LOYAL_RATIO = 0.5


def _audience_profile(canon: dict, m: dict) -> dict:
    """계정 규모(scale) + 도달 방식(reach_mode). 조언 분기의 단일 소스."""
    followers = (canon.get("account") or {}).get("followers") or 0
    med = m["views_stats"]["median"]

    scale, scale_label = "large", "대형"
    for key, label, upper in SCALE_THRESHOLDS:
        if upper is None or med < upper:
            scale, scale_label = key, label
            break

    ratio = round(med / followers, 1) if followers else None
    if ratio is None:
        reach_mode, reach_label = "unknown", "팔로워 수를 확인할 수 없어요"
    elif ratio >= EXPLORE_RATIO:
        reach_mode = "explore_driven"
        reach_label = (
            f"팔로워({followers:,}명)보다 평소 조회수가 {ratio}배 많아요 — "
            "팔로워 밖(탐색·추천)에서 대부분 보고 있어요"
        )
    elif ratio <= LOYAL_RATIO:
        reach_mode = "follower_driven"
        reach_label = (
            f"평소 조회수가 팔로워({followers:,}명)의 {ratio}배예요 — "
            "새 시청자에게 퍼지지 않고 기존 팔로워 안에서만 돌고 있어요"
        )
    else:
        reach_mode = "balanced"
        reach_label = f"팔로워({followers:,}명) 규모에 맞는 조회수가 나오고 있어요"

    return {
        "followers": followers,
        "scale": scale,
        "scale_label": scale_label,
        "views_per_follower": ratio,
        "reach_mode": reach_mode,
        "reach_label": reach_label,
    }


# 반응률 분모 후보 — 계정 규모에 맞는 것을 고른다.
# ⚠️ **100 회당으로 고정하면 안 된다.** 대형 계정에서 소수 첫째자리로 반올림하면 0.0 이 된다:
#    @jinyongjin92 실측 — 댓글 886개인데 평소 조회수 41.4만이라 100회당 0.016 → **"댓글 0.0개"**.
#    사용자는 "댓글이 아예 없다" 로 읽고, 합성 AI 도 그 0.0 을 근거로 "댓글 소통이 거의 없어요"
#    라는 **사실과 반대되는 조언**을 썼다(2026-08-04 리포트에서 실제 발생).
ENGAGEMENT_BASES = (100, 1_000, 10_000, 100_000)
_BASE_LABEL = {100: "100회", 1_000: "1,000회", 10_000: "1만회", 100_000: "10만회"}


def _readable(value: float | None, base: int) -> float | None:
    """분모 환산값. 소수 첫째자리로 0.0 이 되는데 실제로 0 이 아니면 둘째자리까지 보인다
    (0 이 아닌 것을 0 으로 보여주는 게 애초의 문제였다)."""
    if value is None:
        return None
    v = value * base
    r = round(v, 1)
    return r if r > 0 or v == 0 else round(v, 2)


def _engagement_rate(like_ratio: float | None, comment_ratio: float | None) -> dict:
    """조회수 대비 좋아요·댓글 비율 → **읽히는 분모**로 환산.

    분모는 '댓글 수가 **1.0 이상**으로 보이는 가장 작은 값'을 고른다. 0.1 을 기준으로 하면
    "1,000회당 0.2개" 처럼 여전히 감이 안 오는 수가 나온다(진용진 실측). 좋아요는 항상
    댓글보다 많으므로 댓글 기준으로 잡으면 둘 다 읽힌다.
    """
    base = ENGAGEMENT_BASES[0]
    if comment_ratio:
        for b in ENGAGEMENT_BASES:
            base = b
            if comment_ratio * b >= 1.0:
                break
    return {
        "per_base": base,
        "base_label": _BASE_LABEL[base],
        "like_per_base": _readable(like_ratio, base),
        "comment_per_base": _readable(comment_ratio, base),
    }


def build_metrics(canon: dict) -> dict:
    posts = canon["posts"]
    now = datetime.now(UTC)
    reels = [p for p in posts if p["media_type"] == "reel"]
    rv = [p for p in reels if p["views"]]
    views = [p["views"] for p in rv]

    m: dict = {
        "coverage": {
            "posts_analyzed": len(posts),
            "reels_total": len(reels),
            "reels_with_views": len(rv),
            "carousels": sum(1 for p in posts if p["media_type"] == "carousel"),
            "images": sum(1 for p in posts if p["media_type"] == "image"),
            "data_date": (canon.get("fetched_at_apify") or "")[:8],
            "months_span": 0,
        },
        "views_field": canon["views_field"],
    }
    if len(rv) < config.MIN_REELS_FOR_REPORT:
        m["insufficient"] = True
        return m

    med, mean_ = st.median(views), st.mean(views)
    m["views_stats"] = {
        "total": sum(views),
        "median": int(med),
        "mean": int(mean_),
        "max": max(views),
        "min": min(views),
        "p25": int(_pct(views, 0.25)),
        "p75": int(_pct(views, 0.75)),
        "p90": int(_pct(views, 0.90)),
    }

    # ── 분포 히스토그램 (동적 버킷) ──
    e1, e2, e3 = _nice(_pct(views, 0.5)), _nice(_pct(views, 0.8)), _nice(_pct(views, 0.95))
    edges = []
    for e in (e1, e2, e3):
        if not edges or e > edges[-1]:
            edges.append(e)
    top_edge = next((n for n in NICE if n >= max(views)), NICE[-1])
    bounds = [0] + edges + [max(top_edge, edges[-1] * 2)]
    labels, counts = [], []
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        labels.append(f"{man(hi)} 미만" if i == 0 else f"{man(lo)}~{man(hi)}")
        counts.append(sum(1 for v in views if lo <= v < hi))
    counts[-1] += sum(1 for v in views if v >= bounds[-1])
    m["dist"] = {
        "labels": labels,
        "counts": counts,
        "edges": bounds,
        "under_first_pct": round(counts[0] / len(views) * 100),
    }

    # ── 월별 (마지막 미성숙 월 플래그) ──
    # ⚠️ 표본 1~2개인 달은 추세로 쓸 수 없다. 특히 고정(핀) 게시물은 최근 100개 창에
    # 몇 달 전 것이 1개만 섞여 들어와, 그 달 대표값이 되어 "급락"처럼 오독된다.
    by_month_all: dict[str, list] = {}
    for p in rv:
        by_month_all.setdefault(p["taken_at_kst"][:7], []).append(p)
    MIN_MONTH_N = 3
    by_month = {k: v for k, v in by_month_all.items() if len(v) >= MIN_MONTH_N}
    dropped = {k: len(v) for k, v in by_month_all.items() if len(v) < MIN_MONTH_N}
    # ⚠️ **선을 그리려면 점이 2개 이상** 있어야 한다. 예전 방어는 `if not by_month` 였는데,
    # 하한을 넘는 달이 딱 1개면 방어가 안 걸려 차트가 점 하나로 비었다 — 월 2~3개 올리는
    # 계정은 이 경우가 정상이다(실측: @jinyongjin92 6개월간 릴스 13개 → 3월만 통과).
    # 그때는 희소한 달까지 되살리고 low_sample 로 표시해 정직하게 보여 준다.
    low_sample = False
    if len(by_month) < 2:
        by_month, dropped = by_month_all, {}
        low_sample = len(by_month) > 1
    m["monthly_low_sample"] = low_sample
    months = sorted(by_month)
    # 데이터 기준일(수집 시각, KST). 없으면 빈 문자열 — 렌더가 항목을 아예 생략한다.
    # ⚠️ 이 값을 안 넣던 동안 리포트 부제가 "릴스 13개 기준 · **..** · 조회수는…" 으로
    #    구분점만 남아 나왔다(2026-08-04 실측). 렌더에서 문자열을 잘라 조립했기 때문.
    fetched = canon.get("fetched_at_official") or canon.get("fetched_at_apify") or ""
    m["coverage"]["data_date"] = ""
    if fetched:
        try:
            m["coverage"]["data_date"] = (
                datetime.fromisoformat(fetched).astimezone(KST).strftime("%Y.%m.%d")
            )
        except ValueError:
            pass
    m["coverage"]["months_span"] = len(months)
    all_dates = sorted(p["taken_at_kst"][:10] for p in rv)
    m["coverage"]["period_from"] = all_dates[0]
    m["coverage"]["period_to"] = all_dates[-1]
    m["monthly_dropped"] = dropped  # {월: 개수} — 표본 부족으로 그래프에서 뺀 달
    mon = {"months": months, "median": [], "mean": [], "count": [], "immature": []}
    for mo in months:
        vs = [p["views"] for p in by_month[mo]]
        young = sum(
            1 for p in by_month[mo] if (now - datetime.fromisoformat(p["taken_at_utc"])).days < 28
        )
        mon["median"].append(int(st.median(vs)))
        mon["mean"].append(int(st.mean(vs)))
        mon["count"].append(len(vs))
        mon["immature"].append(young > len(vs) / 2)
    m["monthly"] = mon

    # ── 계정 성격 (조언을 여기서 갈라야 한다) ──
    m["audience"] = _audience_profile(canon, m)

    # ── 벤치마크 티어 (분위수 라운딩, 단조 강제) ──
    tiers = [_round_tier(_pct(views, q)) for q in (0.25, 0.50, 0.75, 0.90)]
    for i in range(1, 4):
        tiers[i] = max(tiers[i], tiers[i - 1] + 1)
    m["benchmark"] = {"tiers": tiers}

    # ── 반응률 ──
    lr = [p["likes"] / p["views"] for p in rv if p["likes"] is not None]
    cr = [p["comments"] / p["views"] for p in rv if p["comments"] is not None]
    m["engagement"] = {
        **_engagement_rate(st.median(lr) if lr else None, st.median(cr) if cr else None),
        "comment_gt_like": sum(1 for p in rv if (p["comments"] or 0) > (p["likes"] or 0)),
        "n": len(rv),
    }

    # ── 캡션 CTA 유/무 비교 (캡션 프록시 — 영상 CTA 는 피처 단계에서 별도) ──
    w = [p for p in rv if p["caption_features"]["has_cta"]]
    wo = [p for p in rv if not p["caption_features"]["has_cta"]]

    def grp(g):
        if not g:
            return {"n": 0, "usual": None, "peak": None}
        vs = sorted((p["views"] for p in g), reverse=True)
        top = vs[: max(1, len(vs) // 4)]
        return {"n": len(g), "usual": int(st.median(vs)), "peak": int(st.mean(top))}

    m["cta_caption"] = {"with": grp(w), "without": grp(wo)}

    # 댓글 유도 키워드 빈도
    kw: dict[str, int] = {}
    for p in posts:
        k = p["caption_features"]["cta_keyword"]
        if k:
            kw[k] = kw.get(k, 0) + 1
    m["cta_keywords"] = sorted(([k, n] for k, n in kw.items()), key=lambda x: -x[1])[:8]

    # ── 시간대/요일 (KST) ──
    dow_med, dow_cnt = [], []
    for d in range(7):
        vs = [p["views"] for p in rv if p["kst_dow"] == d]
        dow_med.append(int(st.median(vs)) if vs else None)
        dow_cnt.append(len(vs))
    m["timing_dow"] = {"labels": DOW_KO, "median": dow_med, "count": dow_cnt}

    # 시간 버킷: 게시가 몰린 구간 1h 유지, 희소 병합 (간이: 4~6버킷)
    hours = sorted(p["kst_hour"] for p in rv)
    hb, cur, cur_cnt = [], None, 0
    min_per = max(3, len(rv) // 10)
    for h in range(24):
        cnt = sum(1 for x in hours if x == h)
        if cur is None:
            cur, cur_cnt = [h, h], cnt
        else:
            cur[1] = h
            cur_cnt += cnt
        if cur_cnt >= min_per and h < 23:
            hb.append((cur[0], cur[1]))
            cur, cur_cnt = None, 0
    if cur is not None:
        if hb and cur_cnt < min_per:
            hb[-1] = (hb[-1][0], cur[1])
        else:
            hb.append((cur[0], cur[1]))
    tb = {"labels": [], "median": [], "count": [], "ranges": []}
    for lo, hi in hb:
        vs = [p["views"] for p in rv if lo <= p["kst_hour"] <= hi]
        if not vs:
            continue
        tb["labels"].append(
            f"~{hi+1}시" if lo == 0 else (f"{lo}시" if lo == hi else f"{lo}~{hi+1}시")
        )
        tb["median"].append(int(st.median(vs)))
        tb["count"].append(len(vs))
        tb["ranges"].append([lo, hi])
    m["timing_hours"] = tb

    # 최고 슬롯 (코드 판정 — n 가드)
    best = None
    for i, med_v in enumerate(tb["median"]):
        if tb["count"][i] >= max(3, len(rv) // 15):
            if best is None or med_v > tb["median"][best]:
                best = i
    top_dows = sorted(
        (d for d in range(7) if dow_cnt[d] >= 3 and dow_med[d]), key=lambda d: -dow_med[d]
    )[:3]
    m["best_slot"] = {
        "hour_label": tb["labels"][best] if best is not None else None,
        "hour_median": tb["median"][best] if best is not None else None,
        "hour_n": tb["count"][best] if best is not None else None,
        "dows": [DOW_KO[d] for d in top_dows],
        "signal": best is not None and len(top_dows) >= 1,
    }

    # ── TOP9 / 하위5 ──
    ranked = sorted(rv, key=lambda p: -p["views"])
    m["top_posts"] = [
        {
            "rank": i + 1,
            "shortcode": p["shortcode"],
            "permalink": p["permalink"],
            "views": p["views"],
            "likes": p["likes"],
            "comments": p["comments"],
            "date_kst": p["taken_at_kst"][:10],
            "title": p["caption_features"]["first_line"][:60],
            "has_cta": p["caption_features"]["has_cta"],
            "thumb_local": p["thumb_local"],
        }
        for i, p in enumerate(ranked[:9])
    ]

    aged = [
        p
        for p in ranked
        if (now - datetime.fromisoformat(p["taken_at_utc"])).days >= config.BOTTOM_MIN_AGE_DAYS
    ]
    m["low_posts"] = [
        {
            "shortcode": p["shortcode"],
            "views": p["views"],
            "date_kst": p["taken_at_kst"][:10],
            "first_line": p["caption_features"]["first_line"][:60],
        }
        for p in sorted(aged, key=lambda p: p["views"])[:5]
    ]

    # ── 올릴 때 참고 (코드 계산 팁 통계) ──
    tips = {}
    few = [p["views"] for p in rv if p["caption_features"]["hashtag_count"] <= 3]
    many = [p["views"] for p in rv if p["caption_features"]["hashtag_count"] > 3]
    if len(few) >= 5 and len(many) >= 5:
        tips["hashtag"] = {
            "few_median": int(st.median(few)),
            "many_median": int(st.median(many)),
            "few_n": len(few),
            "many_n": len(many),
        }
    buckets = [(0, 150), (150, 300), (300, 600), (600, 99999)]
    bl = []
    for lo, hi in buckets:
        vs = [p["views"] for p in rv if lo <= p["caption_features"]["length"] < hi]
        if len(vs) >= 5:
            bl.append(
                {
                    "range": f"{lo}~{hi}자" if hi < 99999 else f"{lo}자+",
                    "median": int(st.median(vs)),
                    "n": len(vs),
                }
            )
    if bl:
        tips["caption_len_best"] = max(bl, key=lambda b: b["median"])
    car_likes = [p["likes"] for p in posts if p["media_type"] == "carousel" and p["likes"]]
    reel_likes = [p["likes"] for p in rv if p["likes"]]
    if len(car_likes) >= 5 and len(reel_likes) >= 5:
        tips["carousel_like_ratio"] = round(st.median(car_likes) / st.median(reel_likes), 1)
    m["tips"] = tips

    # 해시태그 빈도
    hcount: dict[str, int] = {}
    for p in posts:
        for h in p["caption_features"]["hashtags"]:
            hcount[h] = hcount.get(h, 0) + 1
    m["hashtags_top"] = sorted(([h, n] for h, n in hcount.items()), key=lambda x: -x[1])[:12]

    return m


def save_metrics(username: str, canon: dict) -> dict:
    m = build_metrics(canon)
    p = config.RUNS_DIR / username / "metrics.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m
