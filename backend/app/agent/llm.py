import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from pydantic import SecretStr

from app.config import Settings

UNAVAILABLE = "LLM временно недоступна, попробуйте позже"

logger = logging.getLogger("app.agent.llm")


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMUsage:
    """Tokens one LLM call was billed for (the fake LLM reports zeros)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        # Cache reads cost a fraction of an input token, but they are still billed.
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


@dataclass(frozen=True)
class LLMTurnResult:
    text: str
    tool_calls: list[LLMToolCall]
    stop_reason: str
    content: list[dict[str, Any]] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    result: LLMTurnResult


LLMEvent = TextDelta | Completed


class LLMError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LLM(Protocol):
    def stream(
        self,
        *,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """`max_tokens`: output cap for this call (the agent loop's remaining turn budget);
        None means the LLM's configured maximum."""
        ...


class AnthropicLLM:
    def __init__(
        self,
        api_key: str | None,
        model: str,
        max_tokens: int,
        *,
        auth_token: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        supports_cache_control: bool = True,
    ) -> None:
        # Anthropic-Messages-compatible gateways (OpenRouter) authenticate with a plain
        # bearer token rather than the anthropic SDK's usual `x-api-key` header, so the
        # SDK's `auth_token=` (mutually exclusive with `api_key=`) is used for those —
        # see anthropic.AsyncAnthropic's credential resolution order.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            auth_token=auth_token,
            base_url=base_url,
            default_headers=default_headers,
            max_retries=2,
            timeout=anthropic.Timeout(60.0, connect=5.0),
        )
        self._model = model
        self._max_tokens = max_tokens
        self._supports_cache_control = supports_cache_control

    async def stream(
        self,
        *,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        cached = (
            [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]
            if tools and self._supports_cache_control
            else list(tools)
        )
        try:
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=min(max_tokens, self._max_tokens) if max_tokens else self._max_tokens,
                system=system,  # type: ignore[arg-type]
                tools=cached,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield TextDelta(event.text)
                final = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise LLMError(
                "llm_rate_limited", "Превышен лимит запросов к LLM, попробуйте через минуту"
            ) from exc
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        ) as exc:
            raise LLMError("llm_unavailable", UNAVAILABLE) from exc
        content = [block.model_dump(exclude_none=True) for block in final.content]
        calls = [
            LLMToolCall(b.id, b.name, dict(b.input)) for b in final.content if b.type == "tool_use"
        ]
        text = "".join(b.text for b in final.content if b.type == "text")
        yield Completed(
            LLMTurnResult(
                text=text,
                tool_calls=calls,
                stop_reason=final.stop_reason or "",
                content=content,
                usage=_usage(final.usage),
            )
        )


def _usage(usage: anthropic.types.Usage | None) -> LLMUsage:
    # The cache counters are optional in the API (and a compatible gateway may omit them).
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
    )


OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_KEY_PREFIX = "sk-or-"


def _secret_value(key: SecretStr | None) -> str:
    return key.get_secret_value().strip() if key is not None else ""


def _openrouter_key(settings: Settings) -> str:
    """The OpenRouter bearer key `make_llm` would use, or "" if none is configured.

    Prefers `openrouter_api_key`; falls back to `anthropic_api_key` only when that key
    itself looks like an OpenRouter key (the owner pasted it into the wrong secret) —
    covers both an explicit `llm_provider="openrouter"` and the anthropic-provider
    auto-detect in `resolve_llm_mode`.
    """
    key = _secret_value(settings.openrouter_api_key)
    if key:
        return key
    fallback = _secret_value(settings.anthropic_api_key)
    return fallback if fallback.startswith(OPENROUTER_KEY_PREFIX) else ""


def resolve_llm_mode(settings: Settings) -> str:
    """Which LLM `make_llm` will actually construct: "anthropic", "openrouter" or "fake".

    Shared with `GET /api/meta` so the UI can show a "Демо-режим без LLM"
    badge without duplicating the blank-key check.
    """
    if settings.llm_provider == "openrouter":
        return "openrouter" if _openrouter_key(settings) else "fake"
    if settings.llm_provider == "anthropic":
        key = _secret_value(settings.anthropic_api_key)
        if key.startswith(OPENROUTER_KEY_PREFIX):
            # The owner pasted an OpenRouter key into ANTHROPIC_API_KEY — still usable,
            # just via OpenRouter's Anthropic-Messages-compatible endpoint.
            return "openrouter"
        return "anthropic" if key else "fake"
    return "fake"  # llm_provider == "fake"


def resolve_llm_model(settings: Settings) -> str:
    """The model id `make_llm` will actually request, after the OpenRouter mapping.

    `llm_model` only ever stores the bare Anthropic-style name (e.g.
    "claude-sonnet-5"); when the effective provider is OpenRouter and the operator
    hasn't already supplied a namespaced id, this maps it to OpenRouter's
    "anthropic/<model>" form.
    """
    model = settings.llm_model
    if resolve_llm_mode(settings) == "openrouter" and "/" not in model:
        return f"anthropic/{model}"
    return model


def resolve_llm_base_url(settings: Settings) -> str | None:
    if settings.llm_base_url:
        return settings.llm_base_url
    if resolve_llm_mode(settings) == "openrouter":
        return OPENROUTER_BASE_URL
    return None


def make_llm(settings: Settings) -> LLM:
    mode = resolve_llm_mode(settings)
    if mode == "openrouter":
        key = _openrouter_key(settings)
        assert key  # guaranteed by resolve_llm_mode
        if settings.llm_provider == "anthropic":
            # Logged once per call (make_llm runs once at app startup) — never the key
            # value itself. This is a valid, expected setup (an OpenRouter key stored in
            # ANTHROPIC_API_KEY), not a misconfiguration, so WARNING rather than the
            # ERROR used for the blank-key demo fallback below.
            logger.warning(
                "ANTHROPIC_API_KEY похож на ключ OpenRouter (префикс sk-or-), "
                "используется OpenRouter вместо Anthropic API"
            )
        return AnthropicLLM(
            None,
            resolve_llm_model(settings),
            settings.llm_max_tokens,
            auth_token=key,
            base_url=resolve_llm_base_url(settings),
            default_headers={
                "HTTP-Referer": settings.public_origin,
                "X-Title": "Gantt AI Planner",
            },
            # Verified live (2026-09-26, model anthropic/claude-sonnet-5): OpenRouter's
            # Anthropic-Messages-compatible endpoint accepts `cache_control` on tool
            # definitions without error and completes the request normally, so it is
            # left enabled here rather than made conditional. `supports_cache_control`
            # stays a constructor option in case a future gateway does reject it.
        )
    if mode == "anthropic":
        assert settings.anthropic_api_key is not None  # guaranteed by resolve_llm_mode
        return AnthropicLLM(
            settings.anthropic_api_key.get_secret_value(),
            resolve_llm_model(settings),
            settings.llm_max_tokens,
        )
    # mode == "fake": either llm_provider == "fake" (no logging - not misconfiguration),
    # or the configured provider's key is blank/missing. The production secret file is
    # created empty by bootstrap.sh until the owner fills it in, so a blank key on
    # llm_provider in {"anthropic", "openrouter"} is a misconfigured production
    # deployment silently running demo mode, not a routine condition — log it as an
    # error (never the key value itself), not just a warning, so it's not lost in
    # normal startup noise.
    if settings.llm_provider == "anthropic":
        logger.error("ANTHROPIC_API_KEY не задан, используется демо-режим без LLM")
    elif settings.llm_provider == "openrouter":
        logger.error("OPENROUTER_API_KEY не задан, используется демо-режим без LLM")

    from app.agent.fake import FakeLLM

    return FakeLLM()
