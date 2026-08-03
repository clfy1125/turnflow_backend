"""S3 영상 샘플러 — 층화 30개 (상10 경과일보정 + 하10 + 중간5 + 최신5).

PLAN §4 확정 규칙: score = views / min(age_days, 28), 상위 후보는 게시 7일+,
하위 후보는 14일+, 층 dedup 우선순위(상>하>중>최신), 영상 파일 있는 것만.
+ 캐러셀/이미지 경량 분석 후보(좋아요 상위, 기획서 표4-1).
"""

import json
from datetime import UTC, datetime

from . import config


def build_sample(canon: dict, *, require_local: bool = False) -> dict:
    """층화 샘플 선정.

    ``require_local`` — 후보를 "이미 내려받은 파일이 있는 것"으로 제한할지.
    ⚠️ **기본값 False 여야 한다.** 백엔드 파이프라인은 이 함수를 **다운로드 전에** 호출하고
    다운로드는 여기서 고른 것만 받는다(`media.download_for_run(…, sample)`). True 로 두면
    파일이 아직 없어 후보가 0개가 되고, 영상 분석 입력이 통째로 비어 리포트가
    `EXTRACT_FAILED` 로 죽는다 — 2026-08-03 운영 실측(`videos_requested: 0`).
    FAKE 모드는 샘플링 전에 자리표시자 파일을 써 두기 때문에 이 결함을 가렸다.
    랩처럼 "전량 다운로드 → 샘플링" 순서로 쓸 때만 True 를 준다.
    """
    now = datetime.now(UTC)
    reels = [
        p
        for p in canon["posts"]
        if p["media_type"] == "reel" and p["views"] and (p["video_local"] or not require_local)
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

    # 캐러셀/이미지 경량 후보 (좋아요 상위). 썸네일도 이 선정 결과를 보고 내려받으므로
    # require_local 을 강제하면 여기도 0개가 된다(위 docstring 참고).
    car = [
        p
        for p in canon["posts"]
        if p["media_type"] in ("carousel", "image")
        and p["likes"]
        and (p["thumb_local"] or not require_local)
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
