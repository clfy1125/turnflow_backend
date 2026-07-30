"""AI 캐시 브릿지 — DB(영구) ↔ 런 디렉터리 파일(파이프라인이 기대하는 형태).

랩 파이프라인(`extract.py`/`comments.py`)은 캐시를 **파일**로 읽고 쓴다. 그 코드를 그대로
이식하기 위해, 실행 전에 DB 캐시를 파일로 풀어 두고(warm) 실행 후 새로 생긴 파일을
DB 로 거둔다(flush). 파이프라인 코드는 캐시가 어디서 왔는지 몰라도 된다.

효과: 같은 계정 재분석 시 Gemini 추출비 $0.21~0.27 → $0 (캐시 히트).
"""

from __future__ import annotations

import hashlib
import json
import logging

from ..models import IGVideoFeature, ReportAiCache
from . import config
from . import feature_schema as fs

logger = logging.getLogger(__name__)

COMMENT_CLASS_KIND = "comment_class"


# ── 영상 피처 캐시 ────────────────────────────────────────────────────
def warm_features(external_account_id: str, shortcodes: list[str]) -> int:
    """DB → FEATURE_DIR/{shortcode}@v{N}.json. 반환: 풀어 놓은 건수."""
    if not shortcodes:
        return 0
    rows = IGVideoFeature.objects.filter(
        external_account_id=external_account_id,
        shortcode__in=list(shortcodes),
        schema_version=fs.FEATURE_SCHEMA_VERSION,
    )
    n = 0
    for row in rows:
        path = config.FEATURE_DIR / f"{row.shortcode}@v{fs.FEATURE_SCHEMA_VERSION}.json"
        try:
            path.write_text(json.dumps(row.features_json, ensure_ascii=False), encoding="utf-8")
            n += 1
        except OSError:  # pragma: no cover - 디스크 문제면 그냥 캐시 미스로 진행
            logger.warning("insta_report: feature cache warm failed sc=%s", row.shortcode)
    return n


def flush_features(external_account_id: str) -> int:
    """FEATURE_DIR 의 피처 envelope → DB upsert. 반환: 저장/갱신 건수."""
    n = 0
    suffix = f"@v{fs.FEATURE_SCHEMA_VERSION}.json"
    for path in config.FEATURE_DIR.glob(f"*{suffix}"):
        if path.name.startswith("comments_"):
            continue
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sc = env.get("shortcode")
        if not sc or "feature" not in env:
            continue
        IGVideoFeature.objects.update_or_create(
            external_account_id=external_account_id,
            shortcode=sc,
            schema_version=fs.FEATURE_SCHEMA_VERSION,
            defaults={
                "features_json": env,
                "model_name": env.get("model", "")[:100],
            },
        )
        n += 1
    return n


# ── 댓글 분류 캐시 ────────────────────────────────────────────────────
def _comment_cache_key(external_account_id: str, username: str, version: int) -> str:
    raw = f"{external_account_id}:{username}:v{version}"
    return hashlib.sha256(raw.encode()).hexdigest()


def warm_comment_classes(external_account_id: str, username: str, version: int) -> int:
    key = _comment_cache_key(external_account_id, username, version)
    row = ReportAiCache.objects.filter(cache_key=key).first()
    if not row:
        return 0
    path = config.FEATURE_DIR / f"comments_{username}@v{version}.json"
    try:
        path.write_text(json.dumps(row.payload_json, ensure_ascii=False), encoding="utf-8")
    except OSError:  # pragma: no cover
        return 0
    return len(row.payload_json or {})


def flush_comment_classes(external_account_id: str, username: str, version: int) -> int:
    path = config.FEATURE_DIR / f"comments_{username}@v{version}.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    ReportAiCache.objects.update_or_create(
        cache_key=_comment_cache_key(external_account_id, username, version),
        defaults={
            "kind": COMMENT_CLASS_KIND,
            "version": version,
            "payload_json": payload,
        },
    )
    return len(payload)
