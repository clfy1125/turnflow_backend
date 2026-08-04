"""S1-b 조회수 수집 — Apify Instagram Scraper.

**왜 필요한가**: 우리 IG 앱은 `instagram_business_manage_insights` 권한을 받지 않았다
(services.InstagramOAuthService.REQUIRED_SCOPES 참조). 즉 공식 API 로는 릴스 조회수를
가져올 수 없고, 조회수는 리포트의 척추다 → 공개 데이터 스크레이핑으로 보완한다.
댓글 원문(게시물별 최근 15개)도 이 소스가 함께 준다.

훗날 insights 권한이 승인되면 **이 모듈만 교체**하면 된다(산출물 스키마 유지).

산출물: ``APIFY_DIR/{username}.json`` — 랩 `scripts/fetch_apify.py` 와 동일 스키마.
"""

from __future__ import annotations

import json
import logging
import time

import requests
from django.conf import settings

from . import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.apify.com/v2"
DEFAULT_ACTOR = "apify~instagram-scraper"
RESULTS_LIMIT = 100
POLL_SECONDS = 10
RUN_TIMEOUT_SECONDS = 900  # 15분 — 초과 시 조회수 없이는 리포트 불가 → 실패 처리


class ApifyError(RuntimeError):
    """조회수 수집 실패. 호출부는 VIEWS_UNAVAILABLE 로 잡을 종료한다(이용 횟수 미차감)."""


def _token() -> str:
    tok = getattr(settings, "INSTA_REPORT_APIFY_API_KEY", "") or ""
    if not tok:
        raise ApifyError("APIFY_API_KEY 미설정 — 조회수를 수집할 수 없습니다.")
    return tok


def _actor() -> str:
    return getattr(settings, "INSTA_REPORT_APIFY_ACTOR", DEFAULT_ACTOR) or DEFAULT_ACTOR


def _params(extra: dict | None = None) -> dict:
    p = {"token": _token()}
    if extra:
        p.update(extra)
    return p


def start_run(session: requests.Session, run_input: dict) -> tuple[str, str]:
    r = session.post(
        f"{BASE_URL}/acts/{_actor()}/runs", params=_params(), json=run_input, timeout=60
    )
    if not r.ok:
        raise ApifyError(f"수집 시작 실패 {r.status_code}: {r.text[:300]}")
    d = (r.json() or {}).get("data") or {}
    run_id, dataset_id = d.get("id"), d.get("defaultDatasetId")
    if not run_id or not dataset_id:
        raise ApifyError(f"수집 응답에 run/dataset id 없음: {str(d)[:200]}")
    return run_id, dataset_id


def wait_run(session: requests.Session, run_id: str, *, on_tick=None) -> dict:
    start = time.monotonic()
    while True:
        r = session.get(f"{BASE_URL}/actor-runs/{run_id}", params=_params(), timeout=60)
        if not r.ok:
            raise ApifyError(f"수집 상태 조회 실패 {r.status_code}")
        d = (r.json() or {}).get("data") or {}
        status = d.get("status")
        if on_tick:
            on_tick(status, int(time.monotonic() - start))
        if status == "SUCCEEDED":
            return d
        if status in ("FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"):
            raise ApifyError(f"수집 종료 status={status}: {str(d.get('statusMessage'))[:200]}")
        if time.monotonic() - start > RUN_TIMEOUT_SECONDS:
            raise ApifyError(f"수집 타임아웃({RUN_TIMEOUT_SECONDS}s) status={status}")
        time.sleep(POLL_SECONDS)


def get_items(session: requests.Session, dataset_id: str) -> list:
    r = session.get(
        f"{BASE_URL}/datasets/{dataset_id}/items",
        params=_params({"format": "json", "clean": "true"}),
        timeout=300,
    )
    if not r.ok:
        raise ApifyError(f"수집 결과 조회 실패 {r.status_code}")
    items = r.json()
    return items if isinstance(items, list) else []


def collect(
    username: str, *, limit: int = RESULTS_LIMIT, on_tick=None, permalinks: list | None = None
) -> dict:
    """조회수 수집 → APIFY_DIR/{username}.json 기록 후 요약 반환.

    ``permalinks`` — Graph 에서 받은 **게시물 URL 목록**. 주면 프로필 대신 이 URL 들을 직접
    긁는다. ⚠️ **가능하면 항상 넘길 것.**

    왜: 프로필 URL 만 주면 액터가 **공개 프로필 그리드만** 훑어서, 그리드에 없는 릴스가
    통째로 빠진다. 실측(@yeonhada__, 2026-08-04): Graph 는 릴스 39개를 주는데 Apify 는
    9개(영상 3개)만 반환해 `NOT_ENOUGH_REELS`(3개 < 5)로 리포트가 아예 실패했다.
    같은 계정에 릴스 permalink 12개를 직접 주니 **12/12 전부 조회수**가 왔다.
    (조회수는 Graph 인사이트로는 못 받는다 — 지표 29종 전부 403, 앱 권한 미승인.)
    """
    session = requests.Session()
    targets = [u for u in (permalinks or []) if u][:limit]
    if targets:
        run_input = {
            "directUrls": targets,
            "resultsType": "posts",
            "resultsLimit": len(targets),
            "addParentData": False,
        }
    else:  # 폴백 — permalink 를 못 구한 경우에만(그리드 누락 위험을 안고 간다)
        run_input = {
            "directUrls": [f"https://www.instagram.com/{username.lstrip('@')}/"],
            "resultsType": "posts",
            "resultsLimit": limit,
            "addParentData": False,
        }
    run_id, dataset_id = start_run(session, run_input)
    logger.info("insta_report: apify run started run=%s dataset=%s", run_id, dataset_id)
    wait_run(session, run_id, on_tick=on_tick)
    items = get_items(session, dataset_id)

    # 액터가 여러 계정을 한 런에 담을 수 있으므로 소유자로 한 번 더 걸러 낸다.
    lowered = username.lstrip("@").lower()
    recs = [it for it in items if (it.get("ownerUsername") or "").lower() == lowered] or items

    doc = {
        "username": username,
        "source": "apify",
        "actor": _actor(),
        "run_id": run_id,
        "count": len(recs),
        "posts": recs,
    }
    (config.APIFY_DIR / f"{username}.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )

    videos = [r for r in recs if r.get("type") == "Video"]
    with_views = [r for r in videos if r.get("videoPlayCount") or r.get("videoViewCount")]
    if not with_views:
        raise ApifyError("조회수가 있는 영상을 찾지 못했습니다.")
    return {
        "count": len(recs),
        "videos": len(videos),
        "videos_with_views": len(with_views),
        "run_id": run_id,
    }
