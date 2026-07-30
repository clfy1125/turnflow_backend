"""S4 피처 추출 — Gemini Flash, 1영상=1호출, 병렬, 영구 캐시.

캐시 키 = shortcode + schema_version → cache/features/{shortcode}@v{N}.json
비디오: ≤14MB 는 inline base64, 초과는 Files API. 이미지(경량 레인): inline.
비용: usageMetadata 토큰을 CostLedger 에 기록.
"""

import base64
import concurrent.futures as cf
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from . import config
from . import feature_schema as fs
from .costs import CostLedger


def _cache_path(shortcode: str) -> Path:
    return config.FEATURE_DIR / f"{shortcode}@v{fs.FEATURE_SCHEMA_VERSION}.json"


def _load_cache(shortcode: str) -> dict | None:
    p = _cache_path(shortcode)
    if not p.exists():
        return None
    try:
        env = json.loads(p.read_text(encoding="utf-8"))
        if env.get("schema_version") == fs.FEATURE_SCHEMA_VERSION and "feature" in env:
            return env
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _upload_file(path: Path, mime: str) -> str:
    """Files API resumable upload → file_uri."""
    size = path.stat().st_size
    r = requests.post(
        f"{config.GEMINI_BASE}/files",
        headers={
            "x-goog-api-key": config.GEMINI_API_KEY,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": path.name}},
        timeout=30,
    )
    r.raise_for_status()
    up_url = r.headers["X-Goog-Upload-URL"]
    r2 = requests.post(
        up_url,
        headers={
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Length": str(size),
        },
        data=path.read_bytes(),
        timeout=300,
    )
    r2.raise_for_status()
    info = r2.json()["file"]
    name, uri = info["name"], info["uri"]
    for _ in range(60):
        st = requests.get(
            f"{config.GEMINI_BASE}/{name}",
            headers={"x-goog-api-key": config.GEMINI_API_KEY},
            timeout=30,
        ).json()
        if st.get("state") == "ACTIVE":
            return uri
        if st.get("state") == "FAILED":
            raise RuntimeError(f"Files API 처리 실패: {name}")
        time.sleep(5)
    raise TimeoutError(f"Files API PROCESSING 타임아웃: {name}")


def _call_gemini(parts: list, ledger: CostLedger, note: str) -> tuple[dict, dict]:
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": fs.GEMINI_SCHEMA,
            "maxOutputTokens": 8192,
        },
    }
    backoff = 2
    for _attempt in range(5):
        r = requests.post(
            f"{config.GEMINI_BASE}/models/{config.EXTRACT_MODEL}:generateContent",
            headers={"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=300,
        )
        if r.status_code in (429, 500, 503):
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        r.raise_for_status()
        d = r.json()
        usage = d.get("usageMetadata", {})
        ledger.record_llm(
            "S4_extract",
            config.EXTRACT_MODEL,
            usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
            note=note,
        )
        text = d["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text), usage
    raise RuntimeError("Gemini 재시도 소진")


def extract_one(post: dict, ledger: CostLedger, mode: str = "video") -> dict:
    """1게시물 추출 (캐시 우선). mode=video|image. envelope 반환."""
    sc = post["shortcode"]
    cached = _load_cache(sc)
    if cached:
        ledger.record_llm(
            "S4_extract", config.EXTRACT_MODEL, 0, 0, note=f"{sc} cache-hit", cached=True
        )
        return cached

    if mode == "video":
        path = Path(post["video_local"])
        mime = "video/mp4"
        prompt = fs.EXTRACT_PROMPT
    else:
        path = Path(post["thumb_local"])
        mime = "image/jpeg"
        prompt = fs.EXTRACT_PROMPT + fs.IMAGE_PROMPT_SUFFIX + (post.get("caption") or "")[:1500]

    size = path.stat().st_size
    if size <= config.INLINE_MAX_BYTES:
        media_part = {
            "inlineData": {"mimeType": mime, "data": base64.b64encode(path.read_bytes()).decode()}
        }
    else:
        media_part = {"fileData": {"mimeType": mime, "fileUri": _upload_file(path, mime)}}

    last_errs = []
    feature = None
    for attempt in range(3):
        p = (
            prompt
            if not last_errs
            else prompt
            + "\n\n[이전 응답이 다음 검증에 실패했습니다 — 고쳐서 다시 출력하세요]\n- "
            + "\n- ".join(last_errs)
        )
        try:
            feature, usage = _call_gemini([media_part, {"text": p}], ledger, f"{sc} try{attempt+1}")
        except (json.JSONDecodeError, KeyError) as e:
            last_errs = [f"JSON 파싱 실패: {e}"]
            continue
        errs = fs.validate_feature(feature)
        if not errs:
            break
        last_errs = errs
        feature = None
    if feature is None:
        raise RuntimeError(f"{sc}: 3회 검증 실패 — {last_errs}")

    # 계산=코드: cut_pace 확정
    dur = feature["pacing"].get("video_duration_sec") or 0
    feature["pacing"]["cut_pace"] = fs.cut_pace_of(
        feature["pacing"].get("cut_count_first_10s", 0), dur
    )

    env = {
        "shortcode": sc,
        "schema_version": fs.FEATURE_SCHEMA_VERSION,
        "model": config.EXTRACT_MODEL,
        "mode": mode,
        "extracted_at": datetime.now(UTC).isoformat(),
        "media_bytes": size,
        "feature": feature,
    }
    config.FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _cache_path(sc).with_suffix(".tmp")
    tmp.write_text(json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _cache_path(sc))
    return env


def extract_sample(canon: dict, sample: dict, ledger: CostLedger, progress=None) -> dict:
    """샘플 전체 추출 (병렬). 반환: {shortcode: envelope}, 실패는 failures 에."""
    by_sc = {p["shortcode"]: p for p in canon["posts"]}
    jobs = [(by_sc[v["shortcode"]], "video") for v in sample["videos"] if v["shortcode"] in by_sc]
    jobs += [(by_sc[sc], "image") for sc in sample.get("light_images", []) if sc in by_sc]

    results, failures = {}, {}
    with cf.ThreadPoolExecutor(max_workers=config.EXTRACT_CONCURRENCY) as ex:
        futs = {
            ex.submit(extract_one, post, ledger, mode): post["shortcode"] for post, mode in jobs
        }
        done = 0
        for fut in cf.as_completed(futs):
            sc = futs[fut]
            done += 1
            try:
                results[sc] = fut.result()
            except Exception as e:  # noqa: BLE001
                failures[sc] = str(e)[:300]
            if progress:
                progress(done, len(jobs), sc)
    return {"features": results, "failures": failures}
