"""
LiteLLM 호출 클라이언트.

LLM_URL / LLM_API_KEY 환경변수를 사용하여 OpenAI-compatible 엔드포인트에 요청.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx
from decouple import config
from openai import OpenAI

logger = logging.getLogger(__name__)

_LLM_URL = config("LLM_URL", default="https://llm.clfy.ai.kr")
_LLM_API_KEY = config("LLM_API_KEY", default="")
# llm.clfy.ai.kr 은 Cloudflare Origin 인증서(공개 CA 체인 아님)를 제시하므로, 이 호스트를
# 직접 https 로 호출하는 개발 스택에선 TLS 검증이 실패한다(CERTIFICATE_VERIFY_FAILED).
# 개발 .env 에 `LLM_TLS_VERIFY=False` 로 이 호스트에 한해 검증을 끈다. 운영은 내부
# `http://litellm-proxy:4000`(TLS 없음)으로 호출하므로 기본 True 여도 이 분기를 타지 않는다.
_LLM_TLS_VERIFY = config("LLM_TLS_VERIFY", default=True, cast=bool)

# ── 스트리밍 (프록시 유휴 타임아웃 방어) ──────────────────────
# 개발 스택은 LLM 을 `https://llm.clfy.ai.kr`(Cloudflare 프록시) 로 부르는데, CF 는 오리진이
# 120초 안에 응답을 **시작**하지 않으면 524(origin_response_timeout) 로 끊는다. 그런데 추론
# 모델의 리뉴얼 호출은 추론에만 오래 걸린다 — 2026-08-13 실측(실제 실패 프롬프트 재현):
#   첫 이벤트 2.4s · 첫 content 토큰 187.8s(추론 28,113 토큰) · 총 204.1s 완결
# 즉 비스트리밍은 204초 동안 한 바이트도 안 나가 **구조적으로 CF 를 통과할 수 없다**.
# 스트리밍이면 추론 델타가 2.4초부터 계속 흘러 유휴 구간이 사라져 같은 프롬프트가 정상 완결된다.
#
# 운영 경로(내부 `http://litellm-proxy:4000`)는 CF 를 타지 않아 원래 이 문제가 없지만,
# 스트리밍은 프록시/LB 유휴 타임아웃 전반에 대한 방어라 양쪽 공통으로 켠다.
#
# 끄는 스위치를 둔 이유: 공급자가 `stream_options.include_usage` 를 무시하면 usage 가 비어
# 비용·토큰 집계가 0 으로 떨어진다. 그 경우 배포 없이 `LLM_STREAMING=False` 로 되돌린다.
_LLM_STREAMING = config("LLM_STREAMING", default=True, cast=bool)


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


# ── 페이지 생성/리뉴얼 출력 예산 ──────────────────────────────
# deepseek-v4-flash 는 **추론(reasoning) 모델**이고 reasoning_tokens 는 completion_tokens
# 안에 포함된다 → 추론이 max_tokens 를 먼저 먹고, 다 쓰면 content 가 빈 문자열로 온다
# (finish_reason="length"). 같은 함정을 insta_reports 가 먼저 만나 max_tokens=28000 으로
# 해결해뒀다(apps/insta_reports/pipeline/synthesize.py).
#
# 2026-08-04 실측(리뉴얼 프롬프트 ~44k 입력, 프로덕션): 같은 프롬프트의 추론량이
# 760 ~ 37,669 토큰으로 40배 넘게 요동친다. 예산별 성공률 —
#   10,000(구 기본값) 0/3 · 32,000 0/1(추론이 예산을 통째로 소진) · 40,000 2/2 · 64,000 4/4
# 실제 출력은 2~4k 토큰뿐이라 꼬리(37,669)를 덮으려면 예산이 커야 한다. max_tokens 는
# **상한이지 과금액이 아니다**(비용은 실제 생성 토큰에만 붙는다) — 크게 잡아도 손해가 없다.
# 7월까지 10,000 으로 잘 돌던 이유는 그때 추론량이 낮게만 뽑혔기 때문이고, 우리 코드·
# litellm·모델 매핑은 그대로다. 즉 이 값은 공급자 거동 변화에 대한 마진이다.
PAGE_GEN_MAX_TOKENS = 64000

# ── 출력 잘림 자동 이어받기 ───────────────────────────────────
# 단일 호출은 max_tokens 한 번에 출력 한계가 있다. 새 페이지 "완전 생성"처럼
# 출력 JSON 이 길면 finish_reason == "length" 로 잘려 파싱이 실패한다.
# (full_restyle 리뉴얼은 mode_router 의 chunk_blocks 로 입력을 쪼개 해결하지만,
#  새 생성은 쪼갤 기존 블록이 없으므로 출력을 이어받아 조립한다.)
# 잘리면 직전까지의 출력을 assistant prefill 로 되돌려주고 "이어서 출력"을 시켜
# 텍스트를 이어붙인다. 8k * (1 + 6) ≈ 최대 ~56k 토큰 출력까지 안전.
_MAX_CONTINUATIONS = 6

# 이어받기 **누적 출력 토큰** 상한.
# 횟수만으로 제한하면 예산이 큰 호출에서 곱셈이 터진다(PAGE_GEN_MAX_TOKENS 64k * 7 회 =
# 448k 출력 토큰 → celery time_limit 초과 + 비용 폭증). 반대로 횟수를 줄이면 예산이
# 작은 호출이 피해를 본다 — dm_campaign_assistant 는 max_tokens=4000 으로 답글 50개
# 같은 긴 출력을 여러 번 이어받아 조립한다(4k * 7 = 28k 로 종전과 동일하게 동작해야 함).
# 그래서 횟수(6)는 유지하고 **실제 생성량**으로 상한을 둔다 — 작은 예산 호출은 영향이
# 없고, 큰 예산 호출만 여기서 멈춘다.
_MAX_TOTAL_OUTPUT_TOKENS = 96000

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
        "reasoning_tokens",
        "estimated_cost_usd",
    ):
        acc[k] = acc.get(k, 0) + part.get(k, 0)
    # raw_usage 는 마지막 호출 것으로 덮어쓴다 (참고용).
    if part.get("raw_usage"):
        acc["raw_usage"] = part["raw_usage"]


def _complete_once(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    extra_body: dict | None,
    stream: bool,
) -> tuple[str, str | None, dict]:
    """LLM 을 한 번 호출하고 ``(content, finish_reason, usage_dict)`` 를 돌려준다.

    스트리밍/비스트리밍의 반환 형태를 여기서 통일해, 호출자는 어느 쪽인지 몰라도 된다.
    스트리밍은 델타를 이어붙이고 finish_reason·usage 는 각각 마지막으로 실린 값을 쓴다
    (``include_usage`` 를 켜면 usage 는 choices 가 빈 마지막 이벤트로 온다).
    """
    if not stream:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
        )
        choice = response.choices[0]
        return (
            choice.message.content or "",
            getattr(choice, "finish_reason", None),
            _extract_usage(response, model),
        )

    parts: list[str] = []
    finish_reason: str | None = None
    usage = None

    events = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        extra_body=extra_body,
        stream=True,
        stream_options={"include_usage": True},
    )
    for event in events:
        if getattr(event, "usage", None):
            usage = event.usage
        for choice in event.choices or []:
            delta = getattr(choice, "delta", None)
            # 추론 델타(reasoning_content)는 본문이 아니므로 버린다 — 다만 이 델타가
            # 흐르는 덕분에 프록시가 유휴로 보지 않는다(이 함수의 존재 이유).
            if delta is not None and getattr(delta, "content", None):
                parts.append(delta.content)
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

    usage_dict = _usage_to_dict(usage, model)
    if not usage_dict:
        # include_usage 를 무시하는 공급자 — 비용·토큰 집계가 조용히 0 이 된다.
        logger.warning(
            "스트리밍 응답에 usage 가 없음(model=%s) — 토큰/비용 집계가 0 으로 기록된다. "
            "집계가 중요하면 LLM_STREAMING=False 로 되돌릴 것.",
            model,
        )
    return "".join(parts), finish_reason, usage_dict


def _complete_with_continuation(
    client: OpenAI,
    model: str,
    base_messages: list[dict],
    max_tokens: int,
    temperature: float,
    stream: bool = False,
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
        content_piece, finish_reason, usage_part = _complete_once(
            client, model, messages, max_tokens, temperature, extra_body, stream
        )
        parts.append(content_piece)
        _merge_usage(merged, usage_part)

        # ── 추론 초과(빈 응답)와 진짜 잘림을 구분한다 ──────────────
        # 추론 모델은 max_tokens 를 추론으로 다 쓰면 content="" + finish_reason="length"
        # 로 온다. 이건 "잘린 출력"이 아니라 **출력이 시작조차 안 된 것**이다. 여기에
        # 이어받기를 걸면 빈 문자열을 assistant prefill 로 되돌려주며 "잘린 지점에서
        # 이어서 쓰라"는 거짓 지시를 하게 되고, 모델은 맥락 없이 JSON 조각을 내놓는다.
        # 그 조각이 blocks 를 빼먹으면 리뉴얼 결과가 블록 0개가 됐다(2026-08-04 사고:
        # 성공 표시된 리뉴얼이 빈 페이지로 보임). 실측으로 이어받기 성공률은 1~2/4 에
        # 불과했고, 재요청 문구를 고쳐도 개선되지 않았다(추론 모델은 지시로 통제 불가 —
        # reasoning_effort / enable_thinking 계열 8종이 모두 무시됨).
        # → 예산 부족이 원인이므로 이어받기로 덮지 않고 빈 응답 그대로 올려보내
        #   호출자의 재시도/실패 경로가 정직하게 동작하게 한다.
        if finish_reason == "length" and not "".join(parts).strip():
            logger.error(
                "LLM 이 추론에 max_tokens(%d) 를 전부 소진해 출력이 비었음 "
                "(model=%s, reasoning_tokens=%s). 이어받기를 하지 않고 빈 응답을 반환한다 "
                "— PAGE_GEN_MAX_TOKENS 상향이 필요한 신호.",
                max_tokens,
                model,
                merged.get("reasoning_tokens"),
            )
            break

        out_so_far = merged.get("completion_tokens", 0)
        if (
            finish_reason != "length"
            or rounds >= _MAX_CONTINUATIONS
            or out_so_far >= _MAX_TOTAL_OUTPUT_TOKENS
        ):
            if finish_reason == "length":
                logger.warning(
                    "LLM 출력이 이어받기 %d 회(누적 출력 %d 토큰) 후에도 여전히 잘림 "
                    "(model=%s). 결과 JSON 이 불완전할 수 있음 — 블록이 조용히 누락될 수 있다.",
                    rounds,
                    out_so_far,
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
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    raw_usage: dict = field(default_factory=dict)


# 단일 호출 HTTP 타임아웃. PAGE_GEN_MAX_TOKENS=64k 를 다 쓰는 최악의 경우 실측 처리량
# (~130 tok/s) 으로 ~490s 가 걸린다 — 600s 로는 마진이 없어 900s 로 둔다.
_HTTP_TIMEOUT = 900.0


def _get_client() -> OpenAI:
    kwargs = {"base_url": _LLM_URL, "api_key": _LLM_API_KEY, "timeout": _HTTP_TIMEOUT}
    if not _LLM_TLS_VERIFY:
        # CF Origin 인증서 수용(개발 전용). 운영 경로(내부 http)는 이 분기를 타지 않는다.
        kwargs["http_client"] = httpx.Client(verify=False, timeout=_HTTP_TIMEOUT)
    return OpenAI(**kwargs)


def _extract_usage(response, model: str) -> dict:
    """OpenAI / DeepSeek usage 구조에서 캐시 통계까지 뽑아낸다."""
    return _usage_to_dict(getattr(response, "usage", None), model)


def _usage_to_dict(usage, model: str) -> dict:
    """usage 객체 → 집계용 dict.

    비스트리밍은 ``response.usage``, 스트리밍은 마지막 이벤트의 ``event.usage`` 로
    같은 구조가 오므로 양쪽이 이 함수를 공유한다.
    """
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

    # 추론 모델(deepseek-v4-*)의 사고 토큰. completion_tokens 에 **포함**되며 max_tokens
    # 예산을 먹는다 — 빈 응답 원인 진단에 필수라서 별도로 뽑아 로그/누적에 싣는다.
    detail = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(detail, "reasoning_tokens", 0) if detail else 0
    reasoning_tokens = reasoning_tokens or 0

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
        "reasoning_tokens": reasoning_tokens,
        "estimated_cost_usd": cost,
        "raw_usage": usage.model_dump() if hasattr(usage, "model_dump") else dict(usage.__dict__),
    }


def call_llm(
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = PAGE_GEN_MAX_TOKENS,
    temperature: float = 0.2,
) -> str:
    """
    LLM에 메시지를 보내고 텍스트 응답만 반환한다 (기존 호출자 호환용).

    기본 예산이 ``PAGE_GEN_MAX_TOKENS`` 인 이유는 상수 주석 참고 — 이 함수의 호출자는
    ai_jobs 페이지 생성/리뉴얼 파이프라인뿐이다(tasks.py).
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
        "LLM 멀티모달 응답: model=%s, %d chars, in=%d, out=%d(reasoning=%d), %.2fs, cont=%d",
        model,
        len(content),
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("reasoning_tokens", 0),
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
    max_tokens: int = PAGE_GEN_MAX_TOKENS,
    temperature: float = 0.2,
) -> LlmCallResult:
    """
    LLM 호출 결과 + 토큰 사용량 + 캐시 통계 + 지연시간을 함께 반환.
    출력이 max_tokens 로 잘리면 자동으로 이어받아 완결된 JSON 을 조립한다.

    Raises:
        Exception: API 호출 실패 시
    """
    client = _get_client()

    logger.info(
        "LLM 호출 시작: model=%s, max_tokens=%d, stream=%s", model, max_tokens, _LLM_STREAMING
    )

    content, usage, elapsed, rounds = _complete_with_continuation(
        client,
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens,
        temperature,
        # 페이지 생성/리뉴얼은 출력·추론이 길어 프록시 유휴 타임아웃에 걸리는 유일한 경로다.
        # 멀티모달/짧은 호출(call_llm_messages_with_usage)은 비스트리밍 그대로 둔다 —
        # 이득이 없고 공급자별 스트리밍 거동 차이만 새로 떠안게 된다.
        stream=_LLM_STREAMING,
    )
    logger.info(
        "LLM 응답 수신: model=%s, %d chars, in=%d (hit=%d,miss=%d), out=%d(reasoning=%d), "
        "%.2fs, cont=%d",
        model,
        len(content),
        usage.get("prompt_tokens", 0),
        usage.get("cache_hit_tokens", 0),
        usage.get("cache_miss_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("reasoning_tokens", 0),
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
