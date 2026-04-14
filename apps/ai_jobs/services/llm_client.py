"""
LiteLLM 호출 클라이언트.

LLM_URL / LLM_API_KEY 환경변수를 사용하여 OpenAI-compatible 엔드포인트에 요청.
"""

import logging

from decouple import config
from openai import OpenAI

logger = logging.getLogger(__name__)

_LLM_URL = config("LLM_URL", default="https://llm.clfy.ai.kr")
_LLM_API_KEY = config("LLM_API_KEY", default="")


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=_LLM_URL,
        api_key=_LLM_API_KEY,
        timeout=600.0,
    )


def call_llm(
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 8000,
    temperature: float = 0.2,
) -> str:
    """
    LLM에 메시지를 보내고 텍스트 응답을 반환한다.

    Raises:
        Exception: API 호출 실패 시
    """
    client = _get_client()

    logger.info("LLM 호출 시작: model=%s, max_tokens=%d", model, max_tokens)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        extra_body={"skip_special_tokens": False},
    )

    content = response.choices[0].message.content or ""
    logger.info("LLM 응답 수신: %d chars", len(content))
    return content
