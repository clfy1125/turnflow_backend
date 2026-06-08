"""
블록 타입별 최소 데이터 스키마 검증.

BLOCK_SCHEMA 딕셔너리에 타입을 키로, 검증 함수를 값으로 추가하면
새로운 블록 타입을 DB 마이그레이션 없이 확장할 수 있습니다.
"""

from urllib.parse import urlparse

from rest_framework.exceptions import ValidationError

# ────────────────────────────────────────────
# 공통 유틸
# ────────────────────────────────────────────


def _require_str(data: dict, key: str):
    val = data.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValidationError({key: f"'{key}'는 비어 있지 않은 문자열이어야 합니다."})


def _optional_str(data: dict, key: str):
    if key in data and not isinstance(data[key], str):
        raise ValidationError({key: f"'{key}'는 문자열이어야 합니다."})


def _normalize_url(url: str) -> str | None:
    """
    URL 문자열을 정규화한다.

    - 앞뒤 공백 제거. 빈 문자열은 그대로 허용("" 반환).
    - http/https 스킴이 이미 있으면 그대로 사용.
    - 스킴이 없으면 도메인으로 간주해 ``https://`` 를 자동 부착.
      단, host에 점(.)이 없거나 공백이 있으면 URL이 아닌 것으로 본다.

    유효한 http/https URL로 만들 수 없으면 ``None`` 을 반환한다.
    """
    stripped = url.strip()
    if not stripped:
        return ""  # 빈 값 허용 (편집 중 임시 저장)

    parsed = urlparse(stripped)
    if parsed.scheme:
        # 이미 스킴이 있으면 http/https 만 허용 (기존 동작 유지)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return stripped
        return None

    # 스킴이 없으면 도메인 형태일 때만 https:// 자동 부착
    candidate = "https://" + stripped.lstrip("/")
    host = urlparse(candidate).netloc
    if not host or " " in host or "." not in host:
        return None
    return candidate


def _optional_url(data: dict, key: str):
    if key not in data:
        return
    url = data[key]
    if url is None:
        return  # null 은 미설정으로 간주 (허용)
    if not isinstance(url, str):
        raise ValidationError({key: f"'{key}'는 문자열이어야 합니다."})
    normalized = _normalize_url(url)
    if normalized is None:
        raise ValidationError({key: f"'{key}'는 유효한 URL(http/https) 이어야 합니다."})
    data[key] = normalized  # 스킴 자동 보정된 값으로 치환


def _require_url(data: dict, key: str):
    _require_str(data, key)
    _optional_url(data, key)


def _optional_enum(data: dict, key: str, choices: list):
    if key in data and data[key] not in choices:
        raise ValidationError({key: f"'{key}'는 {choices} 중 하나여야 합니다."})


# ────────────────────────────────────────────
# 타입별 검증 함수
# ────────────────────────────────────────────


def _validate_profile(data: dict):
    """
    선택: headline(str), subline(str), image_url(url), layout(str), size(str)
    빈 문자열 허용 — 편집 중 임시 저장을 위해 완화.
    """
    _optional_str(data, "headline")
    _optional_str(data, "subline")
    _optional_url(data, "image_url")
    _optional_str(data, "layout")
    _optional_str(data, "size")


def _validate_contact(data: dict):
    """
    선택: country_code(str), phone(str), label(str)
    빈 문자열 허용 — 편집 중 임시 저장을 위해 완화.
    """
    _optional_str(data, "country_code")
    _optional_str(data, "phone")
    _optional_str(data, "label")


def _validate_single_link(data: dict):
    """
    선택: url(valid URL or blank), label(str), thumbnail_url(url), layout(enum)
    빈 문자열 허용 — 프론트에서 편집 중 임시 저장을 위해 완화.
    """
    _optional_url(data, "url")
    _optional_str(data, "label")
    _optional_url(data, "thumbnail_url")
    _optional_enum(data, "layout", ["small", "medium", "large"])


# ────────────────────────────────────────────
# 확장 포인트: 새 타입은 여기에만 추가
# ────────────────────────────────────────────

BLOCK_SCHEMA: dict[str, callable] = {
    "profile": _validate_profile,
    "contact": _validate_contact,
    "single_link": _validate_single_link,
}


def validate_block_data(block_type: str, data):
    """
    1. data는 dict(object)여야 한다.
    2. 타입별 최소 스키마 검증 수행.
    """
    if not isinstance(data, dict):
        raise ValidationError({"data": "data는 JSON object(dict) 여야 합니다."})

    validator = BLOCK_SCHEMA.get(block_type)
    if validator is None:
        # 서버가 모르는 타입은 허용하지 않음 (serializer choices에서 먼저 걸리지만 방어 코드)
        raise ValidationError({"type": f"지원하지 않는 블록 타입입니다: {block_type}"})

    validator(data)
