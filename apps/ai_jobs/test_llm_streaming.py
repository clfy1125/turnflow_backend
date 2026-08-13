"""llm_client 스트리밍 경로 회귀 테스트.

dev 는 LLM 을 Cloudflare 프록시(120초 상한) 경유로 부르는데 추론 모델은 첫 content
토큰까지 ~188초간 아무 바이트도 안 보낸다 → 비스트리밍은 항상 524 로 끊긴다.
스트리밍이 그 벽을 넘는 유일한 수단이므로, 아래가 깨지면 dev 리뉴얼이 다시 죽는다.

특히 검증하는 것:
  - 델타 조립이 비스트리밍과 **같은 반환 형태**를 유지하는지 (이어받기 로직이 분기를 모른다)
  - reasoning_content 가 본문에 섞이지 않는지 (섞이면 JSON 파싱이 깨진다)
  - usage 가 choices 빈 마지막 이벤트에서 회수되는지 (놓치면 비용집계가 조용히 0)
"""

from types import SimpleNamespace

from apps.ai_jobs.services import llm_client


def _delta_event(content=None, reasoning=None, finish_reason=None):
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=reasoning),
                finish_reason=finish_reason,
            )
        ],
    )


def _usage_event(**kw):
    """include_usage 를 켜면 마지막에 오는 choices 가 빈 usage 전용 이벤트."""
    defaults = {
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "total_tokens": 140,
        "prompt_tokens_details": SimpleNamespace(cached_tokens=90),
        "completion_tokens_details": SimpleNamespace(reasoning_tokens=30),
    }
    defaults.update(kw)
    return SimpleNamespace(usage=SimpleNamespace(**defaults), choices=[])


class _FakeClient:
    """create() 호출 인자를 기록하고 미리 정한 이벤트 목록을 흘려주는 스텁."""

    def __init__(self, events):
        self.events = events
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return iter(outer.events)

        self.chat = SimpleNamespace(completions=_Completions())


def _complete_once(client, stream=True, model="deepseek"):
    return llm_client._complete_once(
        client,
        model=model,
        messages=[{"role": "user", "content": "x"}],
        max_tokens=1000,
        temperature=0.2,
        extra_body=None,
        stream=stream,
    )


class TestStreamingAssembly:
    def test_content_deltas_are_joined_and_reasoning_dropped(self):
        client = _FakeClient(
            [
                _delta_event(reasoning="생각 1"),
                _delta_event(reasoning="생각 2"),
                _delta_event(content='{"blocks"'),
                _delta_event(content=": []}"),
                _delta_event(finish_reason="stop"),
                _usage_event(),
            ]
        )
        content, finish, usage = _complete_once(client)

        # 추론 델타가 본문에 섞이면 JSON 파싱이 깨진다.
        assert content == '{"blocks": []}'
        assert finish == "stop"
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 40
        assert usage["cache_hit_tokens"] == 90
        assert usage["cache_miss_tokens"] == 10
        assert usage["reasoning_tokens"] == 30

    def test_streaming_flags_are_sent(self):
        client = _FakeClient([_delta_event(content="ok", finish_reason="stop"), _usage_event()])
        _complete_once(client)

        (kwargs,) = client.calls
        assert kwargs["stream"] is True
        # include_usage 가 빠지면 usage 가 안 와서 비용집계가 0 이 된다.
        assert kwargs["stream_options"] == {"include_usage": True}

    def test_non_streaming_path_unchanged(self):
        """stream=False 는 기존 create() 단발 호출 그대로 — 스트림 플래그를 보내지 않는다."""
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="hello"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )

        class _Client:
            def __init__(self):
                self.calls = []
                outer = self

                class _C:
                    def create(self, **kwargs):
                        outer.calls.append(kwargs)
                        return response

                self.chat = SimpleNamespace(completions=_C())

        client = _Client()
        content, finish, usage = _complete_once(client, stream=False)

        assert (content, finish) == ("hello", "stop")
        assert usage["prompt_tokens"] == 10
        assert "stream" not in client.calls[0]

    def test_missing_usage_degrades_to_empty_not_crash(self):
        """공급자가 include_usage 를 무시해도 죽지 않고 집계만 비운다(WARNING 로그)."""
        client = _FakeClient([_delta_event(content="ok", finish_reason="stop")])
        content, finish, usage = _complete_once(client)

        assert (content, finish) == ("ok", "stop")
        assert usage == {}


class TestStreamingWithContinuation:
    def test_truncated_output_is_continued(self):
        """finish_reason=length + 본문 있음 → 스트리밍에서도 이어받기가 동작한다."""
        rounds = {"n": 0}

        class _Client:
            def __init__(self):
                class _C:
                    def create(self, **kwargs):
                        rounds["n"] += 1
                        if rounds["n"] == 1:
                            return iter(
                                [_delta_event(content='{"a"'), _delta_event(finish_reason="length")]
                            )
                        return iter([_delta_event(content=": 1}", finish_reason="stop")])

                self.chat = SimpleNamespace(completions=_C())

        content, usage, elapsed, n = llm_client._complete_with_continuation(
            _Client(),
            "deepseek",
            [{"role": "user", "content": "x"}],
            max_tokens=1000,
            temperature=0.2,
            stream=True,
        )
        assert content == '{"a": 1}'
        assert n == 1

    def test_reasoning_exhausted_budget_does_not_continue(self):
        """추론이 예산을 다 먹어 본문이 비면 이어받기를 걸지 않는다(빈 페이지 사고 방어).

        비스트리밍에서 확립된 이 가드가 스트리밍에서도 살아있어야 한다.
        """
        calls = {"n": 0}

        class _Client:
            def __init__(self):
                class _C:
                    def create(self, **kwargs):
                        calls["n"] += 1
                        return iter(
                            [
                                _delta_event(reasoning="끝없는 생각"),
                                _delta_event(finish_reason="length"),
                            ]
                        )

                self.chat = SimpleNamespace(completions=_C())

        content, usage, elapsed, n = llm_client._complete_with_continuation(
            _Client(),
            "deepseek",
            [{"role": "user", "content": "x"}],
            max_tokens=1000,
            temperature=0.2,
            stream=True,
        )
        assert content == ""
        assert n == 0
        assert calls["n"] == 1  # 이어받기 재호출 없음


def test_page_gen_path_uses_streaming_flag(monkeypatch):
    """call_llm_with_usage 가 _LLM_STREAMING 을 그대로 내려보내는지."""
    seen = {}

    def fake_cwc(client, model, messages, max_tokens, temperature, stream=False):
        seen["stream"] = stream
        return "{}", {}, 1.0, 0

    monkeypatch.setattr(llm_client, "_get_client", lambda: object())
    monkeypatch.setattr(llm_client, "_complete_with_continuation", fake_cwc)

    for flag in (True, False):
        monkeypatch.setattr(llm_client, "_LLM_STREAMING", flag)
        llm_client.call_llm_with_usage(model="deepseek", system_prompt="s", user_prompt="u")
        assert seen["stream"] is flag


def test_multimodal_path_stays_non_streaming(monkeypatch):
    """비전/스팸/DM어시스트 경로는 스트리밍으로 바꾸지 않는다 — 공급자 거동 리스크만 늘어난다."""
    seen = {}

    def fake_cwc(client, model, messages, max_tokens, temperature, stream=False):
        seen["stream"] = stream
        return "{}", {}, 1.0, 0

    monkeypatch.setattr(llm_client, "_get_client", lambda: object())
    monkeypatch.setattr(llm_client, "_complete_with_continuation", fake_cwc)
    monkeypatch.setattr(llm_client, "_LLM_STREAMING", True)

    llm_client.call_llm_messages_with_usage(
        model="gemma-4", messages=[{"role": "user", "content": "x"}]
    )
    assert seen["stream"] is False


def test_streaming_is_on_by_default():
    """기본값이 False 로 뒤집히면 dev 리뉴얼이 조용히 다시 죽는다."""
    assert llm_client._LLM_STREAMING is True
