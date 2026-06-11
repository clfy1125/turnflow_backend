"""
LLM 응답에서 JSON을 추출하고 파싱.

생성 모델이 만드는 JSON 은 (1) 코드펜스로 감싸거나, (2) 앞뒤 설명 텍스트가 붙거나,
(3) 길이 초과로 **잘리거나**(truncated), (4) trailing comma 같은 사소한 문법 오류를
내기도 한다. 단순 ``json.loads`` 는 이런 경우 전부 실패한다. 풍부한 새-페이지 생성처럼
출력이 길수록 깨질 확률이 커서, 여기서는 **brace 인식 추출 + 경미한 자동 복구**로
최대한 살린다.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)


def _strip_trailing_commas(s: str) -> str:
    """``,}`` / ``,]`` (배열·객체 끝 trailing comma) 제거."""
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _balanced_object(text: str) -> str | None:
    """첫 ``{`` 부터 **문자열/이스케이프를 인식**하며 균형 잡힌 최상위 객체를 잘라낸다.

    - 문자열 안의 ``{`` ``}`` 는 깊이에 안 셈(따옴표 상태 추적).
    - 깊이가 0 으로 돌아오면 거기서 끝(뒤 설명 텍스트 무시).
    - 끝까지 가도 안 닫히면(truncated) 열린 ``{``/``[`` 를 역순으로 닫아 복구 시도.
    """
    start = text.find("{")
    if start == -1:
        return None
    stack: list[str] = []
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return text[start : i + 1]  # 균형 완성

    # 끝까지 안 닫힘 = truncated. 잘린 꼬리를 정리하고 열린 구조를 닫는다.
    frag = text[start:]
    if in_str:  # 문자열 도중에 잘림 → 닫아준다
        frag += '"'
    # 마지막 완결 토큰 뒤(따옴표/괄호/숫자/true/false/null)까지만 남기고 잘린 partial 제거
    m = re.search(r'[\s\S]*["}\]\d eltu]', frag)
    if m:
        frag = frag[: m.end()]
    frag = frag.rstrip().rstrip(",")
    closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
    return frag + closers


def extract_json(raw_text: str) -> dict:
    """LLM 응답 텍스트에서 JSON 객체(dict)를 추출한다.

    순서: ① 코드펜스 내부 → ② brace 인식 균형 추출(+truncation 복구) → ③ 가장 바깥 ``{..}``.
    각 후보에 대해 원본 / trailing-comma 제거본을 모두 ``json.loads`` 시도.

    Raises:
        ValueError: 모든 시도 실패.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("LLM 응답이 비어 있습니다.")

    candidates: list[str] = []

    # 1) 코드펜스 내부
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw_text)
    if m:
        candidates.append(m.group(1))

    # 2) brace 인식 균형 추출(truncation 복구 포함) — 가장 견고
    balanced = _balanced_object(raw_text)
    if balanced:
        candidates.append(balanced)

    # 3) 가장 바깥 { ... } (greedy) — 폴백
    m = re.search(r"\{[\s\S]*\}", raw_text)
    if m:
        candidates.append(m.group(0))

    for cand in candidates:
        for variant in (cand, _strip_trailing_commas(cand)):
            try:
                obj = json.loads(variant)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

    raise ValueError(f"LLM 응답에서 유효한 JSON을 찾을 수 없습니다. (응답 길이: {len(raw_text)})")
