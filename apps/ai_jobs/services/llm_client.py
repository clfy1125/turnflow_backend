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


# ── 출력 잘림 자동 이어받기 ───────────────────────────────────
# 단일 호출은 max_tokens 한 번에 출력 한계가 있다. 새 페이지 "완전 생성"처럼
# 출력 JSON 이 길면 finish_reason == "length" 로 잘려 파싱이 실패한다.
# (full_restyle 리뉴얼은 mode_router 의 chunk_blocks 로 입력을 쪼개 해결하지만,
#  새 생성은 쪼갤 기존 블록이 없으므로 출력을 이어받아 조립한다.)
# 잘리면 직전까지의 출력을 assistant prefill 로 되돌려주고 "이어서 출력"을 시켜
# 텍스트를 이어붙인다. 8k * (1 + 6) ≈ 최대 ~56k 토큰 출력까지 안전.
_MAX_CONTINUATIONS = 6

_CONTINUE_INSTRUCTION = (
    "직전 응답이 토큰 한도로 중간에 잘렸습니다. "
    "잘린 바로 그 지점에서 **이어서** 나머지를 출력하세요. "
    "이미 출력한 내용을 다시 반복하지 말고, 인사말·설명·```json 같은 코드펜스 없이 "
    "남은 문자만 정확히 이어서 출력해 JSON 이 완결되게 하세요."
)


def _merge_usage(acc: dict, part: dict) -> None:
    """여러 번의 (이어받기) 호출 usage 를 acc 에 누적한다."""
    for k in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "estimated_cost_usd",
    ):
        acc[k] = acc.get(k, 0) + part.get(k, 0)
    # raw_usage 는 마지막 호출 것으로 덮어쓴다 (참고용).
    if part.get("raw_usage"):
        acc["raw_usage"] = part["raw_usage"]


def _complete_with_continuation(
    client: OpenAI,
    model: str,
    base_messages: list[dict],
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict, float, int]:
    """LLM 을 호출하되 출력이 max_tokens 로 잘리면 자동으로 이어받아 조립한다.

    Returns:
        (content, merged_usage, elapsed_seconds, continuation_rounds)
    """
    # vLLM 자체호스팅 모델은 skip_special_tokens=False 가 필요.
    # 외부 프로바이더(DeepSeek 등)는 이 옵션을 모르므로 분기.
    extra_body: dict | None = None
    if model in ("gemma-4",) or model.startswith("openai/"):
        extra_body = {"skip_special_tokens": False}

    parts: list[str] = []
    merged: dict = {}
    messages = list(base_messages)
    rounds = 0
    started = time.time()

    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
        )
        choice = response.choices[0]
        parts.append(choice.message.content or "")
        _merge_usage(merged, _extract_usage(response, model))

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason != "length" or rounds >= _MAX_CONTINUATIONS:
            if finish_reason == "length":
                logger.warning(
                    "LLM 출력이 이어받기 %d 회 후에도 여전히 잘림 (model=%s). "
                    "결과 JSON 이 불완전할 수 있음.",
                    rounds,
                    model,
                )
            break

        rounds += 1
        logger.info(
            "LLM 출력 잘림(finish_reason=length) — 이어받기 %d/%d (model=%s)",
            rounds,
            _MAX_CONTINUATIONS,
            model,
        )
        # 지금까지 누적한 출력을 assistant prefill 로 돌려주고 이어서 쓰게 한다.
        messages = list(base_messages) + [
            {"role": "assistant", "content": "".join(parts)},
            {"role": "user", "content": _CONTINUE_INSTRUCTION},
        ]

    elapsed = time.time() - started
    return "".join(parts), merged, elapsed, rounds


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
    max_tokens: int = 12000,
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


def call_llm_messages_with_usage(
    model: str,
    messages: list[dict],
    max_tokens: int = 8000,
    temperature: float = 0.2,
) -> LlmCallResult:
    """이미 빌드된 messages 배열을 그대로 LLM 에 보낸다.

    멀티모달(``content`` 가 list[dict])이나 다중 turn 호출에 사용. usage/cost
    계산은 단일 호출과 동일. 출력이 잘리면 자동으로 이어받아 조립한다.
    """
    client = _get_client()

    logger.info(
        "LLM 멀티모달 호출 시작: model=%s, msgs=%d, max_tokens=%d", model, len(messages), max_tokens
    )

    content, usage, elapsed, rounds = _complete_with_continuation(
        client, model, messages, max_tokens, temperature
    )
    logger.info(
        "LLM 멀티모달 응답: model=%s, %d chars, in=%d, out=%d, %.2fs, cont=%d",
        model,
        len(content),
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        elapsed,
        rounds,
    )

    return LlmCallResult(
        content=content,
        model=model,
        elapsed_seconds=elapsed,
        **{k: v for k, v in usage.items() if k != "raw_usage"},
        raw_usage=usage.get("raw_usage", {}),
    )


def call_llm_with_usage(
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 12000,
    temperature: float = 0.2,
) -> LlmCallResult:
    """
    LLM 호출 결과 + 토큰 사용량 + 캐시 통계 + 지연시간을 함께 반환.
    출력이 max_tokens 로 잘리면 자동으로 이어받아 완결된 JSON 을 조립한다.

    Raises:
        Exception: API 호출 실패 시
    """
    client = _get_client()

    logger.info("LLM 호출 시작: model=%s, max_tokens=%d", model, max_tokens)

    content, usage, elapsed, rounds = _complete_with_continuation(
        client,
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens,
        temperature,
    )
    logger.info(
        "LLM 응답 수신: model=%s, %d chars, in=%d (hit=%d,miss=%d), out=%d, %.2fs, cont=%d",
        model,
        len(content),
        usage.get("prompt_tokens", 0),
        usage.get("cache_hit_tokens", 0),
        usage.get("cache_miss_tokens", 0),
        usage.get("completion_tokens", 0),
        elapsed,
        rounds,
    )

    return LlmCallResult(
        content=content,
        model=model,
        elapsed_seconds=elapsed,
        **{k: v for k, v in usage.items() if k != "raw_usage"},
        raw_usage=usage.get("raw_usage", {}),
    )
