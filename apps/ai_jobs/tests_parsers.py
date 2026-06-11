"""extract_json 견고성 테스트 — 코드펜스/설명텍스트/trailing comma/truncation 복구."""

from __future__ import annotations

import pytest

from .services.parsers import extract_json


def test_plain_json():
    assert extract_json('{"a": 1, "b": [1,2]}') == {"a": 1, "b": [1, 2]}


def test_code_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_leading_and_trailing_prose():
    raw = '설명입니다.\n{"blocks": [{"x": 1}]}\n끝.'
    assert extract_json(raw) == {"blocks": [{"x": 1}]}


def test_trailing_comma_repaired():
    assert extract_json('{"a": 1, "b": [1, 2,], }') == {"a": 1, "b": [1, 2]}


def test_brace_in_string_not_counted():
    out = extract_json('{"t": "a } b { c", "n": 2}')
    assert out == {"t": "a } b { c", "n": 2}


def test_truncated_object_recovered():
    # 길이 초과로 잘린 응답 — 열린 구조를 닫아 최대한 복구.
    raw = '{"title": "x", "blocks": [{"id": 1, "label": "안녕"}, {"id": 2, "label": "잘린'
    out = extract_json(raw)
    assert isinstance(out, dict)
    assert out["title"] == "x"
    assert isinstance(out["blocks"], list) and out["blocks"][0]["id"] == 1


def test_truncated_midstring_recovered():
    raw = '{"a": 1, "b": "끊긴 문자열 중간에서'
    out = extract_json(raw)
    assert out["a"] == 1


def test_garbage_raises():
    with pytest.raises(ValueError):
        extract_json("이건 그냥 텍스트입니다 JSON 없음")
    with pytest.raises(ValueError):
        extract_json("")
