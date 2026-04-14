"""
LLM 응답에서 JSON을 추출하고 파싱.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json(raw_text: str) -> dict:
    """
    LLM 응답 텍스트에서 JSON 객체를 추출한다.

    1차: ```json ... ``` 코드 블록에서 추출
    2차: 코드 블록 없이 { ... } 직접 추출
    3차: 실패 시 ValueError

    Returns:
        파싱된 dict
    Raises:
        ValueError: JSON 추출/파싱 실패
    """
    # 1) 코드 블록에서 추출
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw_text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            logger.warning("코드 블록 JSON 파싱 실패: %s", e)

    # 2) 가장 바깥쪽 { ... } 추출
    match = re.search(r"\{[\s\S]*\}", raw_text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            logger.warning("Raw JSON 파싱 실패: %s", e)

    raise ValueError(f"LLM 응답에서 유효한 JSON을 찾을 수 없습니다. (응답 길이: {len(raw_text)})")
