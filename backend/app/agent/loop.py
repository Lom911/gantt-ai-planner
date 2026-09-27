import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.agent.confirmation import is_explicit_confirmation
from app.agent.llm import LLM, Completed, LLMError, LLMTurnResult, LLMUsage, TextDelta
from app.agent.prompt import build_system
from app.db import repo
from app.db.models import ChatMessageRow
from app.domain.diff import diff_plans, summarize_changes
from app.domain.render import render_plan_table
from app.mcp_server.client import PlanToolClient
from app.services.plan_service import PlanService, PlanState

trace_logger = logging.getLogger("app.agent.trace")
_UNSAFE_LOG_CHARS = re.compile(r"[^A-Za-z0-9_-]")

MUTATING_TOOLS = {"apply_operations", "undo"}
# Every iteration re-sends the whole conversation, so both knobs bound the LLM cost of a turn
# (security audit M1): a normal turn is find → apply → answer, and the full plan is already in
# the system prompt, so a huge tool result (get_plan on 500 tasks) is cut instead of re-sent.
MAX_TOOL_RESULT_CHARS = 20_000
# The third knob: billed tokens over the turn's LLM calls (see TurnStats), a ceiling rather
# than a check after the fact: each call is sent only if its estimated input plus
# MIN_OUTPUT_TOKENS still fits in what the turn has left, and its max_tokens is capped to that.
DEFAULT_TURN_TOKEN_BUDGET = 300_000
DEFAULT_MAX_OUTPUT_TOKENS = 4096
# Room for at least a short answer or a small tool call; below it the call isn't worth sending.
MIN_OUTPUT_TOKENS = 512
# Prepended to the tool result when the loop dropped the model's confirmed=true (see _loop),
# so the model asks the user instead of retrying the same call.
CONFIRMATION_DROPPED_NOTE = (
    "[confirmed=true не принят: в текущем сообщении пользователя нет явного подтверждения. "
    "Спроси пользователя и дождись его ответа «да».]\n"
)
CONFIRMATION_ASKED_NOTE = (
    "[confirmed=true не принят: подтверждение этого удаления запрошено уже после сообщения "
    "пользователя, его «да» относилось к другому. Спроси пользователя и дождись ответа «да».]\n"
)


class TooManySteps(Exception):
    pass


class TurnBudgetExceeded(Exception):
    pass


@dataclass
class TurnStats:
    """What one turn has done and cost so far, filled in by `_loop`."""

    usage: LLMUsage = field(default_factory=LLMUsage)
    iterations: int = 0  # LLM calls made
    tools: list[str] = field(default_factory=list)  # tools run, in order
    confirm_blocked: int = 0  # confirmed=true flags dropped by the guard in _loop


def estimate_input_tokens(
    system: list[dict[str, Any]], tools: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> int:
    """Input tokens a request will be billed for, estimated before sending it: a third of the
    characters of its JSON (system blocks, tool definitions, messages), rounded up. No
    tokenizer ships offline and counting via the API would cost a request per call. Russian
    prose runs at roughly 3-4 characters per token and the JSON punctuation counted here adds
    more, so this errs high for chat and plan text; long digit runs (dates, ids) tokenize
    denser, which is why the loop still checks the billed usage after every call."""
    chars = len(json.dumps([system, tools, messages], ensure_ascii=False))
    return max(1, math.ceil(chars / 3))


def _trace(
    session_id: uuid.UUID, turn_id: uuid.UUID, stats: TurnStats, outcome: str, seconds: float
) -> None:
    """One line per agent turn (roadmap «трейсинг вызовов LLM»): what it did and cost, for
    cost and latency tracking. Like the access log: session id prefix only, never the
    message text, the plan or the client address."""
    u = stats.usage
    # Tool names come from the model's output: keep them to a safe charset for the log line.
    tools = ",".join(_UNSAFE_LOG_CHARS.sub("?", name)[:64] for name in stats.tools) or "-"
    trace_logger.info(
        "agent_turn sid=%s turn=%s outcome=%s iterations=%d tools=%s tokens_in=%d tokens_out=%d"
        " cache_write=%d cache_read=%d tokens_total=%d confirm_blocked=%d duration_ms=%d",
        str(session_id)[:8],
        turn_id,
        outcome,
        stats.iterations,
        tools,
        u.input_tokens,
        u.output_tokens,
        u.cache_creation_input_tokens,
        u.cache_read_input_tokens,
        u.total,
        stats.confirm_blocked,
        round(seconds * 1000),
    )


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: PlanToolClient,
        service: PlanService,
        *,
        today: Callable[[], date],
        history_limit: int = 20,
        max_iterations: int = 8,
        turn_timeout: float = 180.0,
        turn_token_budget: int = DEFAULT_TURN_TOKEN_BUDGET,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._service = service
        self._today = today
        self._history_limit = history_limit
        self._max_iterations = max_iterations
        self._timeout = turn_timeout
        self._turn_token_budget = turn_token_budget
        self._max_output_tokens = max_output_tokens

    async def run_turn(
        self, session_id: uuid.UUID, user_text: str, *, turn_id: uuid.UUID | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run one agent turn for `user_text`.

        If `turn_id` is given, the caller has *already* persisted the user's chat
        message under that turn id (e.g. routes_chat.py does this inside the same
        DB transaction/advisory lock it uses to check the rate limit, so the
        check-then-insert is atomic) — this method must not insert it again.
        Direct callers that omit `turn_id` (e.g. tests, MCP-triggered turns) keep
        the old self-contained behavior: a fresh turn id is generated and the
        message is saved here.
        """
        service = self._service
        message_already_saved = turn_id is not None
        turn_id = turn_id or uuid.uuid4()
        async with service.locks.agent_turn(session_id):
            started = time.monotonic()
            stats = TurnStats()
            outcome = "aborted"  # client gone mid-stream, or an unexpected exception
            service.bus.publish(session_id, {"type": "agent_status", "busy": True})
            try:
                async with service.sessionmaker() as db, db.begin():
                    if not message_already_saved:
                        await repo.add_chat_message(
                            db,
                            session_id=session_id,
                            role="user",
                            content=user_text,
                            turn_id=turn_id,
                        )
                    history = await repo.recent_chat_messages(db, session_id, self._history_limit)
                user_confirmed = is_explicit_confirmation(user_text)
                if not user_confirmed:
                    # Any reply other than «да» answers the assistant's question: a mass deletion
                    # it asked about is off, and a later «да» to something else can't revive it.
                    await service.discard_confirmation(session_id, origin="agent")
                start = await service.get_state(session_id)
                text_parts: list[str] = []
                failure: dict[str, Any] | None = None
                try:
                    async with aclosing(
                        self._bounded(
                            session_id,
                            turn_id,
                            start,
                            history,
                            text_parts,
                            stats,
                            user_confirmed=user_confirmed,
                        )
                    ) as bounded:
                        async for event in bounded:
                            yield event
                except LLMError as exc:
                    failure = {"type": "error", "code": exc.code, "message": exc.message}
                except TooManySteps:
                    failure = {
                        "type": "error",
                        "code": "too_many_steps",
                        "message": (
                            "Слишком много шагов за один запрос, попробуйте разбить его на части"
                        ),
                    }
                except TurnBudgetExceeded:
                    failure = {
                        "type": "error",
                        "code": "turn_budget_exceeded",
                        "message": "Ход слишком большой для одного запроса — сузьте его",
                    }
                except TimeoutError:
                    failure = {
                        "type": "error",
                        "code": "timeout",
                        "message": "Агент не уложился по времени, попробуйте ещё раз",
                    }
                end = await service.get_state(session_id)
                changes = (
                    diff_plans(start.scheduled, end.scheduled)
                    if end.version != start.version
                    else []
                )
                meta: dict[str, Any] = {
                    "summary": summarize_changes(changes),
                    "changes": [c.model_dump() for c in changes[:200]],
                }
                partial_text = "".join(text_parts).strip()
                if failure:
                    meta["error"] = failure["code"]
                    if partial_text:
                        # Keep the partial stream for diagnostics, but never persist it as
                        # the chat bubble — the user must see the actual failure reason,
                        # not a sentence truncated mid-word by the error.
                        meta["partial_text"] = partial_text
                    text = failure["message"]
                else:
                    text = partial_text or "Готово."
                async with service.sessionmaker() as db, db.begin():
                    await repo.add_chat_message(
                        db,
                        session_id=session_id,
                        role="assistant",
                        content=text,
                        turn_id=turn_id,
                        meta=meta,
                    )
                outcome = failure["code"] if failure else "done"
                yield failure or {
                    "type": "done",
                    "turn_id": str(turn_id),
                    "summary": meta["summary"],
                    "changes": meta["changes"],
                }
            finally:
                service.bus.publish(session_id, {"type": "agent_status", "busy": False})
                _trace(session_id, turn_id, stats, outcome, time.monotonic() - started)

    async def _bounded(
        self,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        start: PlanState,
        history: list[ChatMessageRow],
        text_parts: list[str],
        stats: TurnStats,
        *,
        user_confirmed: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Runs `_loop` in a background task and forwards its events, bounding the whole
        turn to `self._timeout` seconds via a wall-clock deadline.

        A plain `async with asyncio.timeout(...):` wrapped around a loop that `yield`s
        (as the brief's reference code does) is unsafe here: while this generator is
        suspended at a `yield`, the owning task may be awaiting unrelated code elsewhere
        (e.g. the SSE writer), and the timeout's internal deadline callback would cancel
        *that* task at *that* unrelated await point once the deadline passes - not the
        LLM/tool call it was meant to bound. Running the real work in its own task and
        only ever cancelling that specific task avoids the mismatch.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        done = object()

        async def pump() -> None:
            try:
                async for event in self._loop(
                    session_id,
                    turn_id,
                    start,
                    history,
                    text_parts,
                    stats,
                    user_confirmed=user_confirmed,
                ):
                    await queue.put(event)
            except Exception as exc:  # forwarded to the consumer below, not swallowed
                await queue.put(exc)
            else:
                await queue.put(done)

        task = asyncio.ensure_future(pump())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                item = await asyncio.wait_for(queue.get(), remaining)
                if item is done:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            if not task.done():
                task.cancel()
            with suppress(BaseException):
                await task

    async def _loop(
        self,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        start: PlanState,
        history: list[ChatMessageRow],
        text_parts: list[str],
        stats: TurnStats,
        *,
        user_confirmed: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        system = build_system(render_plan_table(start.scheduled, self._today()))
        messages = to_llm_messages(history)
        tools = await self._tools.tool_definitions()
        # Set once the server asked for a confirmation during this turn: the user's message
        # predates that question, so it can't be the answer to it.
        asked_this_turn = False
        for _ in range(self._max_iterations):
            result: LLMTurnResult | None = None
            # Text written before a tool call and text written after it are separate
            # paragraphs; without a separator they would run together ("…план.Готово").
            needs_separator = bool("".join(text_parts).strip())
            room = (
                self._turn_token_budget
                - stats.usage.total
                - estimate_input_tokens(system, tools, messages)
            )
            if room < min(MIN_OUTPUT_TOKENS, self._max_output_tokens):
                raise TurnBudgetExceeded()
            stats.iterations += 1
            async for ev in self._llm.stream(
                system=system,
                tools=tools,
                messages=messages,
                max_tokens=min(self._max_output_tokens, room),
            ):
                if isinstance(ev, TextDelta):
                    if needs_separator and ev.text.strip():
                        needs_separator = False
                        text_parts.append("\n\n")
                        yield {"type": "text_delta", "text": "\n\n"}
                    text_parts.append(ev.text)
                    yield {"type": "text_delta", "text": ev.text}
                elif isinstance(ev, Completed):
                    result = ev.result
            if result is None:
                raise LLMError("llm_unavailable", "Пустой ответ LLM")
            stats.usage += result.usage
            messages.append({"role": "assistant", "content": result.content})
            if not result.tool_calls:
                return
            if stats.usage.total > self._turn_token_budget:
                # The input estimate undershot (see estimate_input_tokens). A final answer is
                # kept whatever it cost, but an over-budget response doesn't get its tools run
                # (a half-done edit behind an error message).
                raise TurnBudgetExceeded()
            tool_results: list[dict[str, Any]] = []
            for call in result.tool_calls:
                stats.tools.append(call.name)
                yield {"type": "tool_started", "name": call.name}
                args = call.input
                # Security audit L3: the confirmed=true that reaches the server is this loop
                # vouching that the user's message of THIS turn is an exact «да» answering a
                # confirmation asked before it (PlanService._gate_confirmation trusts it for
                # the agent). A flag the model set on its own (text in the plan can talk it into
                # that) is dropped, so the server answers confirmation_required again. External
                # MCP clients don't go through here: they need the user's approval in the app.
                blocked_note = None
                if call.name == "apply_operations" and args.get("confirmed", False) is not False:
                    if not user_confirmed:
                        blocked_note = CONFIRMATION_DROPPED_NOTE
                    elif asked_this_turn:
                        blocked_note = CONFIRMATION_ASKED_NOTE
                if blocked_note:
                    args = {**args, "confirmed": False}
                    stats.confirm_blocked += 1
                r = await self._tools.call(call.name, args, session_id=session_id, turn_id=turn_id)
                if call.name == "apply_operations" and r.is_error:
                    asked_this_turn |= r.text.startswith("confirmation_required")
                summary = r.text[:200] if r.is_error else (r.data or {}).get("summary")
                yield {
                    "type": "tool_finished",
                    "name": call.name,
                    "ok": not r.is_error,
                    "summary": summary,
                }
                if call.name in MUTATING_TOOLS and not r.is_error and r.data:
                    yield {"type": "plan_changed", "version": r.data.get("version")}
                content = _cap(r.text)
                if blocked_note and r.is_error:
                    content = blocked_note + content
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": content,
                        "is_error": r.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        raise TooManySteps()


def _cap(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    cut = len(text) - MAX_TOOL_RESULT_CHARS
    return (
        text[:MAX_TOOL_RESULT_CHARS]
        + f"\n…[результат обрезан на {cut} символов: план целиком есть в системном промпте;"
        " для деталей используй find_tasks или get_task]"
    )


def to_llm_messages(history: list[ChatMessageRow]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for m in history:
        role = "assistant" if m.role == "assistant" else "user"
        content = f"[Событие приложения] {m.content}" if m.role == "system" else m.content
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": role, "content": content})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged
