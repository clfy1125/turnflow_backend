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


def _optional_url(data: dict, key: str):
    if key not in data:
        return
    url = data[key]
    if not isinstance(url, str):
        raise ValidationError({key: f"'{key}'는 문자열이어야 합니다."})
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError({key: f"'{key}'는 유효한 URL(http/https) 이어야 합니다."})


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
    필수: headline(str)
    선택: subline(str), image_url(url), layout(str), size(str)
    """
    _require_str(data, "headline")
    _optional_str(data, "subline")
    _optional_url(data, "image_url")
    _optional_str(data, "layout")
    _optional_str(data, "size")


def _validate_contact(data: dict):
    """
    필수: country_code(str), phone(str)
    선택: label(str)
    """
    _require_str(data, "country_code")
    _require_str(data, "phone")
    _optional_str(data, "label")


def _validate_single_link(data: dict):
    """
    선택: url(valid URL or blank), label(str), thumbnail_url(url), layout(enum)
    빈 문자열 허용 — 프론트에서 편집 중 임시 저장을 위해 완화.
    """
    url = data.get("url")
    if url and isinstance(url, str) and url.strip():
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
