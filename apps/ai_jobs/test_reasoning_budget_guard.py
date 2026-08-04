"""추론(reasoning) 모델 예산 소진 → "리뉴얼하니 페이지가 텅 비었다" 회귀 방지 테스트.

2026-08-04 사고 경로:
  deepseek-v4-flash 가 추론에 max_tokens 를 전부 소진 → content="" + finish_reason="length"
  → 이어받기가 빈 prefill 로 "이어서 쓰라"는 거짓 지시 → JSON 조각(blocks 누락)
  → merge_full_restyle 이 메타만 반환(blocks 키 소실) → job 은 succeeded
  → 프론트가 블록 0개인 결과를 렌더 = 빈 페이지.

여기서 잠그는 불변식:
  1. merge_full_restyle 은 **어떤 경우에도** blocks 없는(=파괴적인) 결과를 내지 않는다.
  2. content 가 빈 응답에는 이어받기를 걸지 않는다(진짜 잘림은 그대로 이어받는다).
  3. 페이지 생성 기본 예산은 PAGE_GEN_MAX_TOKENS 이고 추론 토큰이 계측된다.
"""

from __future__ import annotations

from types import SimpleNamespace

from .services import llm_client
from .services.llm_client import (
    PAGE_GEN_MAX_TOKENS,
    _complete_with_continuation,
    _extract_usage,
    call_llm,
)
from .services.style_patcher import merge_full_restyle

_META = {"title": "t", "is_public": True, "data": {}, "custom_css": ""}


def _baseline(n: int = 3) -> list[dict]:
    return [
        {
            "id": 100 + i,
            "type": "single_link",
            "order": i + 1,
            "is_enabled": True,
            "data": {"_type": "single_link", "label": f"링크{i}", "url": "https://example.com"},
            "custom_css": "",
            "schedule_enabled": False,
            "publish_at": None,
            "hide_at": None,
        }
        for i in range(n)
    ]


class TestMergeFullRestyleNeverBlockless:
    """merge_full_restyle 이 기존 블록을 통째로 날리는 결과를 내지 못하게."""

    def test_blocks_key_missing_falls_back_to_baseline(self):
        # 사고 당시 그대로: LLM 이 page 메타만 내고 blocks 를 통째로 생략.
        res = merge_full_restyle(
            existing_page_meta=_META,
            existing_blocks=_baseline(3),
            llm_response={"page": {"title": "새 제목"}},
            preserve_content=True,
        )
        assert "blocks" in res, "blocks 키가 없으면 page_applier 가 블록을 전부 지운 것과 같다"
        assert len(res["blocks"]) == 3
        assert [b["id"] for b in res["blocks"]] == [100, 101, 102]

    def test_empty_blocks_list_falls_back_to_baseline(self):
        res = merge_full_restyle(
            existing_page_meta=_META,
            existing_blocks=_baseline(2),
            llm_response={"page": {}, "blocks": []},
            preserve_content=True,
        )
        assert len(res.get("blocks") or []) == 2

    def test_all_blocks_dropped_by_validation_falls_back_to_baseline(self):
        # url 도 라벨도 없는 _new single_link 는 전부 탈락한다 → 결과 0개가 되면 되돌린다.
        res = merge_full_restyle(
            existing_page_meta=_META,
            existing_blocks=_baseline(2),
            llm_response={
                "page": {},
                "blocks": [{"_new": True, "_type": "single_link", "order": 1, "data": {}}],
            },
            preserve_content=False,
        )
        assert len(res.get("blocks") or []) == 2

    def test_legitimate_restyle_still_passes_through(self):
        # 정상 응답은 그대로 반영돼야 한다(폴백이 정상 경로를 가로채지 않게).
        base = _baseline(2)
        res = merge_full_restyle(
            existing_page_meta=_META,
            existing_blocks=base,
            llm_response={
                "page": {},
                "blocks": [{"id": 101, "_type": "single_link", "order": 1, "data": {}}],
            },
            preserve_content=True,
        )
        assert [b["id"] for b in res["blocks"]] == [101]

    def test_no_baseline_and_no_blocks_stays_empty(self):
        # 새 페이지(baseline 없음)는 되살릴 것이 없으므로 빈 blocks 로 둔다.
        res = merge_full_restyle(
            existing_page_meta=_META,
            existing_blocks=[],
            llm_response={"page": {}},
            preserve_content=False,
        )
        assert res.get("blocks") == []


def _fake_client(scripted: list[tuple[str, str]]):
    """(content, finish_reason) 시퀀스를 순서대로 돌려주는 가짜 OpenAI 클라이언트."""
    calls: list[list[dict]] = []

    def create(*, model, messages, max_tokens, temperature, extra_body=None, **kw):
        calls.append(list(messages))
        content, finish = scripted[min(len(calls) - 1, len(scripted) - 1)]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason=finish,
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=max_tokens,
                total_tokens=100 + max_tokens,
                prompt_cache_hit_tokens=0,
                prompt_cache_miss_tokens=100,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=max_tokens),
                model_dump=lambda: {},
            ),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


class TestEmptyContentDoesNotContinue:
    """추론 초과(빈 응답)와 진짜 잘림을 구분한다."""

    def test_empty_content_with_length_does_not_continue(self):
        client, calls = _fake_client([("", "length")])
        content, usage, _elapsed, rounds = _complete_with_continuation(
            client, "deepseek", [{"role": "user", "content": "x"}], 10000, 0.2
        )
        assert content == ""
        assert rounds == 0, "빈 응답에 이어받기를 걸면 거짓 prefill 로 JSON 조각이 나온다"
        assert len(calls) == 1, f"추가 호출이 발생했다: {len(calls)}"

    def test_whitespace_only_content_also_treated_as_empty(self):
        client, calls = _fake_client([("   \n  ", "length")])
        _content, _usage, _elapsed, rounds = _complete_with_continuation(
            client, "deepseek", [{"role": "user", "content": "x"}], 10000, 0.2
        )
        assert rounds == 0
        assert len(calls) == 1

    def test_real_truncation_still_continues(self):
        # content 가 있는 잘림 = 진짜 truncation → 이어받기 유지(새-페이지 생성 경로).
        client, calls = _fake_client([('{"page":{"ti', "length"), ('tle":"x"}}', "stop")])
        content, _usage, _elapsed, rounds = _complete_with_continuation(
            client, "deepseek", [{"role": "user", "content": "x"}], 10000, 0.2
        )
        assert content == '{"page":{"title":"x"}}'
        assert rounds == 1
        assert len(calls) == 2
        # 이어받기 2번째 호출엔 지금까지의 출력이 assistant prefill 로 들어간다.
        assert calls[1][-2]["role"] == "assistant"
        assert calls[1][-2]["content"] == '{"page":{"ti'

    def test_continuation_is_bounded_by_round_count(self):
        # 예산이 작으면(4k) 종전대로 6회까지 이어받는다 — dm_campaign_assistant 의
        # "답글 50개" 처럼 긴 출력을 조립하는 경로가 이 횟수에 의존한다.
        client, calls = _fake_client([("chunk", "length")])
        _content, _usage, _elapsed, rounds = _complete_with_continuation(
            client, "deepseek", [{"role": "user", "content": "x"}], 4000, 0.2
        )
        assert rounds == llm_client._MAX_CONTINUATIONS
        assert len(calls) == llm_client._MAX_CONTINUATIONS + 1

    def test_continuation_is_bounded_by_total_output_tokens(self):
        # 예산이 크면(64k) 횟수보다 누적 출력 상한이 먼저 걸려 곱셈 폭주를 막는다.
        # (_fake_client 는 호출마다 completion_tokens=max_tokens 를 보고한다.)
        client, calls = _fake_client([("chunk", "length")])
        _content, usage, _elapsed, rounds = _complete_with_continuation(
            client, "deepseek", [{"role": "user", "content": "x"}], PAGE_GEN_MAX_TOKENS, 0.2
        )
        assert rounds < llm_client._MAX_CONTINUATIONS, "누적 토큰 상한이 먼저 걸려야 한다"
        assert usage["completion_tokens"] >= llm_client._MAX_TOTAL_OUTPUT_TOKENS
        # 64k * 2 = 128k >= 96k → 2 회 호출에서 멈춘다.
        assert len(calls) == 2


class TestBudgetAndReasoningMetering:
    def test_page_gen_budget_covers_observed_reasoning_tail(self):
        # 실측 추론량 꼬리는 37,669 토큰. 출력 여지까지 포함해 충분해야 한다.
        assert PAGE_GEN_MAX_TOKENS >= 48000

    def test_call_llm_defaults_to_page_gen_budget(self, monkeypatch):
        seen: dict = {}

        def fake(**kw):
            seen.update(kw)
            return SimpleNamespace(content="{}")

        monkeypatch.setattr(llm_client, "call_llm_with_usage", fake)
        call_llm(model="deepseek", system_prompt="s", user_prompt="u")
        assert seen["max_tokens"] == PAGE_GEN_MAX_TOKENS

    def test_reasoning_tokens_extracted(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=500,
                total_tokens=510,
                prompt_cache_hit_tokens=0,
                prompt_cache_miss_tokens=10,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=480),
                model_dump=lambda: {},
            )
        )
        assert _extract_usage(resp, "deepseek")["reasoning_tokens"] == 480

    def test_reasoning_tokens_absent_is_zero_not_crash(self):
        # gemma 등 비추론 모델은 이 필드가 없다.
        resp = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
                model_dump=lambda: {},
            )
        )
        assert _extract_usage(resp, "gemma-4")["reasoning_tokens"] == 0
