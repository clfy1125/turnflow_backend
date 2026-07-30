"""S3 영상 샘플러 — 층화 30개 (상10 경과일보정 + 하10 + 중간5 + 최신5).

PLAN §4 확정 규칙: score = views / min(age_days, 28), 상위 후보는 게시 7일+,
하위 후보는 14일+, 층 dedup 우선순위(상>하>중>최신), 영상 파일 있는 것만.
+ 캐러셀/이미지 경량 분석 후보(좋아요 상위, 기획서 표4-1).
"""

import json
from datetime import UTC, datetime

from . import config


def build_sample(canon: dict) -> dict:
    now = datetime.now(UTC)
    reels = [
        p for p in canon["posts"] if p["media_type"] == "reel" and p["views"] and p["video_local"]
    ]

    def age(p):
        return max(1, (now - datetime.fromisoformat(p["taken_at_utc"])).days)

    for p in reels:
        p["_age"] = age(p)
        p["_score"] = p["views"] / min(p["_age"], config.MATURITY_WINDOW_DAYS)

    chosen: dict[str, str] = {}  # shortcode -> stratum

    def take(cands, n, stratum):
        got = 0
        for p in cands:
            if got >= n:
                break
            if p["shortcode"] not in chosen:
                chosen[p["shortcode"]] = stratum
                got += 1

    top_c = sorted(
        (p for p in reels if p["_age"] >= config.TOP_MIN_AGE_DAYS), key=lambda p: -p["_score"]
    )
    take(top_c, config.SAMPLE_TOP, "top")

    bot_c = sorted(
        (p for p in reels if p["_age"] >= config.BOTTOM_MIN_AGE_DAYS), key=lambda p: p["views"]
    )
    take(bot_c, config.SAMPLE_BOTTOM, "bottom")

    ranked = sorted(reels, key=lambda p: -p["views"])
    n = len(ranked)
    mid_c = ranked[max(0, int(n * 0.4)) : int(n * 0.6) + 1]
    take(mid_c, config.SAMPLE_MID, "mid")

    recent_c = sorted(reels, key=lambda p: p["taken_at_utc"], reverse=True)
    take(recent_c, config.SAMPLE_RECENT, "recent")

    # 소형 계정: 30 미만이면 전량
    if len(reels) <= (
        config.SAMPLE_TOP + config.SAMPLE_BOTTOM + config.SAMPLE_MID + config.SAMPLE_RECENT
    ):
        for p in reels:
            chosen.setdefault(p["shortcode"], "all")

    # 캐러셀/이미지 경량 후보 (좋아요 상위, 썸네일 파일 있는 것)
    car = [
        p
        for p in canon["posts"]
        if p["media_type"] in ("carousel", "image") and p["thumb_local"] and p["likes"]
    ]
    car = sorted(car, key=lambda p: -p["likes"])[: config.CAROUSEL_LIGHT_MAX]

    sample = {
        "username": canon["username"],
        "created_at": datetime.now(UTC).isoformat(),
        "videos": [{"shortcode": sc, "stratum": stt} for sc, stt in chosen.items()],
        "light_images": [p["shortcode"] for p in car],
        "reels_with_views": len(reels),
    }
    config.SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    (config.SAMPLE_DIR / f"{canon['username']}.json").write_text(
        json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return sample
