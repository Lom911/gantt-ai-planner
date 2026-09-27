"""Token usage surfaced from an LLM call into LLMTurnResult (security audit M1: the per-turn
budget is enforced on real usage). No network: the anthropic client is replaced with a stub
that streams one text event and returns a prepared final Message."""

from types import SimpleNamespace
from typing import Any

import anthropic
from anthropic.types import Message, TextBlock, Usage

from app.agent.fake import FakeLLM
from app.agent.llm import AnthropicLLM, Completed, LLMUsage


class _Stream:
    def __init__(self, final: Message) -> None:
        self._final = final

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def _events(self) -> Any:
        yield SimpleNamespace(type="text", text="Готово")

    def __aiter__(self) -> Any:
        return self._events()

    async def get_final_message(self) -> Message:
        return self._final


def _llm_returning(usage: Usage) -> AnthropicLLM:
    final = Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-sonnet-5",
        content=[TextBlock(type="text", text="Готово")],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=usage,
    )
    llm = AnthropicLLM("sk-ant-test", "claude-sonnet-5", 100)
    llm._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(stream=lambda **_: _Stream(final))
    )
    return llm


async def _result(llm: Any) -> Any:
    events = [ev async for ev in llm.stream(system=[], tools=[], messages=[])]
    assert isinstance(events[-1], Completed)
    return events[-1].result


async def test_anthropic_usage_reaches_the_turn_result() -> None:
    usage = Usage(
        input_tokens=11, output_tokens=7, cache_creation_input_tokens=5, cache_read_input_tokens=3
    )
    result = await _result(_llm_returning(usage))
    assert result.usage == LLMUsage(
        input_tokens=11, output_tokens=7, cache_creation_input_tokens=5, cache_read_input_tokens=3
    )
    assert result.usage.total == 26  # cache reads are cheaper, but still billed


async def test_missing_cache_counters_count_as_zero() -> None:
    result = await _result(_llm_returning(Usage(input_tokens=4, output_tokens=2)))
    assert result.usage == LLMUsage(input_tokens=4, output_tokens=2)


async def test_fake_llm_reports_zero_usage() -> None:
    messages = [{"role": "user", "content": "привет"}]
    events = [ev async for ev in FakeLLM().stream(system=[], tools=[], messages=messages)]
    assert events[-1].result.usage == LLMUsage()


def test_usage_adds_up() -> None:
    a = LLMUsage(input_tokens=1, output_tokens=2, cache_creation_input_tokens=3)
    b = LLMUsage(output_tokens=10, cache_read_input_tokens=20)
    assert a + b == LLMUsage(
        input_tokens=1, output_tokens=12, cache_creation_input_tokens=3, cache_read_input_tokens=20
    )


def test_sdk_client_is_real_by_default() -> None:
    # The stub above replaces a real client instance; guard against the attribute moving.
    assert isinstance(AnthropicLLM("sk-ant-test", "m", 1)._client, anthropic.AsyncAnthropic)


async def test_anthropic_call_uses_the_per_call_output_cap() -> None:
    captured: list[dict[str, Any]] = []
    final = Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-sonnet-5",
        content=[TextBlock(type="text", text="Готово")],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    llm = AnthropicLLM("sk-ant-test", "claude-sonnet-5", 100)

    def stream(**kwargs: Any) -> _Stream:
        captured.append(kwargs)
        return _Stream(final)

    llm._client = SimpleNamespace(messages=SimpleNamespace(stream=stream))  # type: ignore[assignment]
    [ev async for ev in llm.stream(system=[], tools=[], messages=[], max_tokens=40)]
    [ev async for ev in llm.stream(system=[], tools=[], messages=[], max_tokens=4000)]
    [ev async for ev in llm.stream(system=[], tools=[], messages=[])]
    # Never above the configured llm_max_tokens; the configured value when no cap is given.
    assert [c["max_tokens"] for c in captured] == [40, 100, 100]


def test_input_estimate_is_a_third_of_the_request_characters() -> None:
    from app.agent.loop import estimate_input_tokens

    system = [{"type": "text", "text": "я" * 300}]
    estimate = estimate_input_tokens(system, [], [{"role": "user", "content": "x" * 300}])
    # 600 characters of text plus the JSON around them, a third of that, rounded up.
    assert 200 < estimate < 240
    assert estimate_input_tokens([], [], []) >= 1
