"""오프라인/개발 모드 — 외부 호출 0으로 S1~S8 전 구간을 통과시킨다.

용도 2가지:
  1) **프론트 통합** — 15분을 기다리지 않고 진행률/완료/PDF 다운로드를 검증
     (`INSTA_REPORT_FAKE_MODE=True`, dev·local 전용. prod 에서 켜면 안 됨)
  2) **테스트** — Apify/Gemini/DeepSeek 없이 파이프라인 회귀 검증

가짜인 것은 **수집 원본과 AI 응답뿐**이다. 정규화·지표·집계·검증·렌더는 실코드를 탄다
(= 렌더 계약이 깨지면 테스트가 잡는다). 계정별로 시드가 고정돼 결과가 재현된다.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta, timezone

from . import config
from . import feature_schema as fs

KST = timezone(timedelta(hours=9))

_CAPTIONS = [
    "이거 모르면 손해예요\n\n댓글에 '자료' 남겨주시면 정리해서 보내드릴게요 #정보 #꿀팁",
    "3가지만 바꿨는데 결과가 달라졌어요\n\n자세한 건 프로필 링크에서 확인하세요",
    "많이들 물어보신 부분 정리했어요\n\n저장해두고 보세요 #저장필수",
    "이렇게 하면 훨씬 빨라집니다\n\n댓글에 '가이드' 적어주세요",
    "실제로 해본 후기예요\n\n#후기 #실전",
    "왜 아무도 이걸 안 알려줄까요?",
]
_COMMENTS = [
    ("자료", "request"),
    ("자료요", "request"),
    ("가이드", "request"),
    ("와 대박이네요", "praise"),
    ("이거 진짜 유용해요", "praise"),
    ("저도 그랬어요 완전 공감", "empathy"),
    ("항상 잘 보고 있어요", "support"),
    ("따라해봤는데 진짜 됐어요", "testimonial"),
    ("혹시 초보도 할 수 있나요?", "question"),
    ("DM 이 안 왔어요 확인 부탁드려요", "dm_not_received"),
    ("🔥", "other"),
    ("👍", "other"),
]
# 같은 말이 반복 댓글로 걸리지 않도록 붙이는 꼬리말(빈 문자열 포함).
_COMMENT_TAILS = [
    "",
    " 감사합니다",
    " 진짜요?",
    " 저장했어요",
    " 오늘도 잘 보고 갑니다",
    " 다음 편도 부탁드려요",
    "!!",
]


def _rng(seed_key: str) -> random.Random:
    return random.Random(f"insta_report_fake::{seed_key}")


def write_sources(
    username: str,
    *,
    external_account_id: str = "fake_ig",
    followers: int = 12_345,
    n_posts: int = 46,
) -> dict:
    """합성 수집 원본 2종을 런 디렉터리에 기록 (공식 + Apify)."""
    rng = _rng(username)
    now = datetime.now(UTC).replace(microsecond=0)

    official_posts, apify_posts = [], []
    for i in range(n_posts):
        sc = f"FAKE{abs(hash((username, i))) % 10**8:08d}"
        # 최근 5개월에 고르게(월당 8~10개) 배치 → 월별 그래프 표본 하한(3) 충족
        days_ago = 3 + int(i * 4.2)
        ts = now - timedelta(days=days_ago, hours=rng.randint(0, 20))
        is_video = i % 6 != 5  # 6개 중 5개는 릴스
        caption = _CAPTIONS[i % len(_CAPTIONS)]
        # 롱테일 분포(소수의 대박 + 다수의 평범) — 히스토그램/벤치마크가 의미 있게 나오도록
        base = rng.randint(900, 6_000)
        views = base * (40 if i == 0 else 8 if i < 4 else 1)
        likes = max(3, int(views * rng.uniform(0.01, 0.05)))
        n_comments = rng.randint(0, 14)

        official_posts.append(
            {
                "id": f"fake_media_{i}",
                "timestamp": ts.isoformat().replace("+00:00", "+0000"),
                "media_type": "VIDEO" if is_video else "CAROUSEL_ALBUM",
                "media_product_type": "REELS" if is_video else "FEED",
                "caption": caption,
                "permalink": f"https://www.instagram.com/{'reel' if is_video else 'p'}/{sc}/",
                "comments_count": n_comments,
                "like_count": likes,
                "media_url": "",  # 다운로드 스킵(가짜 모드는 미디어 없음)
                "thumbnail_url": "",
            }
        )
        comments = []
        for j in range(n_comments):
            text, _cat = _COMMENTS[(i + j) % len(_COMMENTS)]
            # 반복 댓글 필터(REPEAT_THRESHOLD)에 전멸하지 않도록 꼬리말로 변주를 준다
            # — 실제 계정에서도 같은 말이 조금씩 다르게 달린다.
            tail = _COMMENT_TAILS[(i * 3 + j) % len(_COMMENT_TAILS)]
            comments.append(
                {
                    "id": f"{sc}:{j}",
                    "text": f"{text}{tail}",
                    "ownerUsername": f"follower_{(i + j) % 37}",
                    "likesCount": rng.randint(0, 5),
                }
            )
        apify_posts.append(
            {
                "shortCode": sc,
                "url": f"https://www.instagram.com/{'reel' if is_video else 'p'}/{sc}/",
                "type": "Video" if is_video else "Sidecar",
                "timestamp": ts.isoformat(),
                "videoPlayCount": views if is_video else 0,
                "videoViewCount": 0,
                "likesCount": likes,
                "commentsCount": n_comments,
                "latestComments": comments,
            }
        )

    (config.RAW_DIR / f"{username}.json").write_text(
        json.dumps(
            {
                "username": username,
                "external_account_id": external_account_id,
                "fetched_at": now.isoformat(),
                "fields_fallback_used": False,
                "account_meta": {
                    "username": username,
                    "name": f"{username} (개발용 더미)",
                    "biography": "개발 검증용 합성 데이터입니다.",
                    "followers_count": followers,
                    "media_count": n_posts,
                    "profile_picture_url": "",
                },
                "post_count": len(official_posts),
                "posts": official_posts,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (config.APIFY_DIR / f"{username}.json").write_text(
        json.dumps(
            {
                "username": username,
                "source": "fake",
                "actor": "fake",
                "count": len(apify_posts),
                "posts": apify_posts,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 샘플러는 "영상 파일이 있는" 릴스만 후보로 삼는다(sampler.build_sample). 가짜 모드는
    # 실제 미디어를 받지 않으므로 자리표시 파일을 만들어 둔다 — 추출도 가짜라 내용은 무의미하고,
    # 렌더의 썸네일 인라인은 열기 실패 시 빈 문자열로 폴백한다.
    for post in official_posts:
        sc = post["permalink"].rstrip("/").rsplit("/", 1)[-1]
        if post["media_type"] == "VIDEO":
            (config.MEDIA_DIR / f"{sc}.mp4").write_bytes(b"\0" * 16)
        (config.MEDIA_DIR / f"{sc}_thumb.jpg").write_bytes(b"\0" * 16)

    return {
        "post_count": len(official_posts),
        "followers_count": followers,
        "media_count": n_posts,
        "name": f"{username} (개발용 더미)",
    }


def _fake_feature(sc: str, seed: int) -> dict:
    rng = _rng(f"{sc}:{seed}")
    hook = fs.HOOK_TYPES[seed % len(fs.HOOK_TYPES)]
    opening = fs.OPENING_TYPES[seed % len(fs.OPENING_TYPES)]
    duration = rng.choice([12.0, 18.0, 26.0, 41.0])
    has_kw = seed % 3 == 0
    feature = {
        "hook": {
            "text_verbatim": "이거 모르면 손해예요",
            "source": "spoken",
            "type": hook,
        },
        "opening": {
            "screen_type": opening,
            "visual_description": "사람이 화면을 보며 말하기 시작하는 장면이에요.",
            "shows_face": seed % 2 == 0,
            "on_screen_text_verbatim": "3초만 보세요",
            "value_shown_in_2s": seed % 2 == 1,
        },
        "cta": {
            "types": ["comment_keyword"] if has_kw else ["none"],
            "comment_keyword_verbatim": "자료" if has_kw else "",
            "quotes": (
                [{"timestamp_sec": 8.0, "quote_verbatim": "댓글에 '자료' 남겨주세요"}]
                if has_kw
                else []
            ),
        },
        "structure": {
            "type": fs.STRUCTURE_TYPES[seed % len(fs.STRUCTURE_TYPES)],
            "segments": [
                {"start_sec": 0.0, "end_sec": 3.0, "role": fs.SEGMENT_ROLES[0], "label": "도입부"},
                {
                    "start_sec": 3.0,
                    "end_sec": duration,
                    "role": fs.SEGMENT_ROLES[1],
                    "label": "본문 설명",
                },
            ],
        },
        "pacing": {"cut_count_first_10s": rng.randint(1, 8), "video_duration_sec": duration},
        "audio": {
            "has_voiceover": True,
            "has_subtitles": seed % 2 == 0,
            "has_bgm": True,
            "transcript_short": "짧은 설명 음성이 들려요.",
        },
        "commercial": {
            "is_promotional": seed % 7 == 0,
            "brands_or_products": [],
            "price_mentioned": False,
        },
        "topic_keywords": ["꿀팁", "정보정리"],
        "uncertainty_notes": "",
    }
    feature["pacing"]["cut_pace"] = fs.cut_pace_of(
        feature["pacing"]["cut_count_first_10s"], duration
    )
    return feature


def fake_extraction(canon: dict, sample: dict) -> dict:
    """AI 추출 결과 대체 — 스키마·검증을 실제로 통과하는 envelope 묶음."""
    features = {}
    for idx, v in enumerate(sample.get("videos") or []):
        sc = v["shortcode"]
        features[sc] = {
            "shortcode": sc,
            "schema_version": fs.FEATURE_SCHEMA_VERSION,
            "model": "fake",
            "mode": "video",
            "extracted_at": datetime.now(UTC).isoformat(),
            "media_bytes": 0,
            "feature": _fake_feature(sc, idx),
        }
    return {"features": features, "failures": {}}


def fake_comment_classes(pool: list) -> dict:
    """댓글 분류 대체 — 합성 댓글의 앞부분(원문 템플릿)으로 분류."""
    out = {}
    for c in pool:
        text = (c.get("text") or "").strip()
        category = "other"
        for base, cat in _COMMENTS:
            if text.startswith(base):
                category = cat
                break
        out[c["id"]] = category
    return out
