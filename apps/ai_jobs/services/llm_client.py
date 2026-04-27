"""
LiteLLM 호출 클라이언트.

LLM_URL / LLM_API_KEY 환경변수를 사용하여 OpenAI-compatible 엔드포인트에 요청.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from decouple import config
from openai import OpenAI

logger = logging.getLogger(__name__)

_LLM_URL = config("LLM_URL", default="https://llm.clfy.ai.kr")
_LLM_API_KEY = config("LLM_API_KEY", default="")


# ── DeepSeek 가격표 (USD / 1M tokens) ─────────────────────────
# v4-flash 기준. 모델별 단가가 다르면 PRICE_TABLE 에 추가하면 된다.
PRICE_TABLE: dict[str, dict[str, float]] = {
    "deepseek": {
        "input_hit": 0.028,
        "input_miss": 0.14,
        "output": 0.28,
    },
    "deepseek-v4-flash": {
        "input_hit": 0.028,
        "input_miss": 0.14,
        "output": 0.28,
    },
    # gemma-4 (자체 호스팅) — 외부 비용 없음
    "gemma-4": {"input_hit": 0.0, "input_miss": 0.0, "output": 0.0},
}


@dataclass
class LlmCallResult:
    """call_llm_with_usage 반환값. content + 토큰 사용량 + 지연시간."""

    content: str
    model: str
    elapsed_seconds: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    estimated_cost_usd: float = 0.0
    raw_usage: dict = field(default_factory=dict)


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=_LLM_URL,
        api_key=_LLM_API_KEY,
        timeout=600.0,
    )


def _extract_usage(response, model: str) -> dict:
    """OpenAI / DeepSeek usage 구조에서 캐시 통계까지 뽑아낸다."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}

    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", prompt_tokens + completion_tokens)

    # DeepSeek 전용 필드. LiteLLM 이 그대로 패스스루.
    cache_hit = getattr(usage, "prompt_cache_hit_tokens", None)
    cache_miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if cache_hit is None:
        # OpenAI 표준 cached_tokens 도 시도
        details = getattr(usage, "prompt_tokens_details", None)
        cache_hit = getattr(details, "cached_tokens", 0) if details else 0
    cache_hit = cache_hit or 0
    cache_miss = cache_miss if cache_miss is not None else max(prompt_tokens - cache_hit, 0)

    price = PRICE_TABLE.get(model, {})
    cost = (
        cache_hit / 1_000_000 * price.get("input_hit", 0.0)
        + cache_miss / 1_000_000 * price.get("input_miss", 0.0)
        + completion_tokens / 1_000_000 * price.get("output", 0.0)
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "estimated_cost_usd": cost,
        "raw_usage": usage.model_dump() if hasattr(usage, "model_dump") else dict(usage.__dict__),
    }


def call_llm(
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 8000,
    temperature: float = 0.2,
) -> str:
    """
    LLM에 메시지를 보내고 텍스트 응답만 반환한다 (기존 호출자 호환용).
    """
    return call_llm_with_usage(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    ).content


def call_llm_with_usage(
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 8000,
    temperature: float = 0.2,
) -> LlmCallResult:
    """
    LLM 호출 결과 + 토큰 사용량 + 캐시 통계 + 지연시간을 함께 반환.

    Raises:
        Exception: API 호출 실패 시
    """
    client = _get_client()

    logger.info("LLM 호출 시작: model=%s, max_tokens=%d", model, max_tokens)

    # vLLM 자체호스팅 모델은 skip_special_tokens=False 가 필요.
    # 외부 프로바이더(DeepSeek 등)는 이 옵션을 모르므로 분기.
    extra_body: dict = {}
    if model in ("gemma-4",) or model.startswith("openai/"):
        extra_body = {"skip_special_tokens": False}

    started = time.time()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        extra_body=extra_body or None,
    )
    elapsed = time.time() - started

    content = response.choices[0].message.content or ""
    usage = _extract_usage(response, model)
    logger.info(
        "LLM 응답 수신: model=%s, %d chars, in=%d (hit=%d,miss=%d), out=%d, %.2fs",
        model,
        len(content),
        usage.get("prompt_tokens", 0),
        usage.get("cache_hit_tokens", 0),
        usage.get("cache_miss_tokens", 0),
        usage.get("completion_tokens", 0),
        elapsed,
    )

    return LlmCallResult(
        content=content,
        model=model,
        elapsed_seconds=elapsed,
        **{k: v for k, v in usage.items() if k != "raw_usage"},
        raw_usage=usage.get("raw_usage", {}),
    )
