import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, suppress
from datetime import date
from typing import Any

from app.agent.llm import LLM, Completed, LLMError, LLMTurnResult, TextDelta
from app.agent.prompt import build_system
from app.db import repo
from app.db.models import ChatMessageRow
from app.domain.diff import diff_plans, summarize_changes
from app.domain.render import render_plan_table
from app.mcp_server.client import PlanToolClient
from app.services.plan_service import PlanService, PlanState

MUTATING_TOOLS = {"apply_operations", "undo"}
# Every iteration re-sends the whole conversation, so both knobs bound the LLM cost of a turn
# (security audit M1): a normal turn is find → apply → answer, and the full plan is already in
# the system prompt, so a huge tool result (get_plan on 500 tasks) is cut instead of re-sent.
MAX_TOOL_RESULT_CHARS = 20_000


class TooManySteps(Exception):
    pass


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
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._service = service
        self._today = today
        self._history_limit = history_limit
        self._max_iterations = max_iterations
        self._timeout = turn_timeout

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
                start = await service.get_state(session_id)
                text_parts: list[str] = []
                failure: dict[str, Any] | None = None
                try:
                    async with aclosing(
                        self._bounded(session_id, turn_id, start, history, text_parts)
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
                yield failure or {
                    "type": "done",
                    "turn_id": str(turn_id),
                    "summary": meta["summary"],
                    "changes": meta["changes"],
                }
            finally:
                service.bus.publish(session_id, {"type": "agent_status", "busy": False})

    async def _bounded(
        self,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        start: PlanState,
        history: list[ChatMessageRow],
        text_parts: list[str],
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
                async for event in self._loop(session_id, turn_id, start, history, text_parts):
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
    ) -> AsyncIterator[dict[str, Any]]:
        system = build_system(render_plan_table(start.scheduled, self._today()))
        messages = to_llm_messages(history)
        tools = await self._tools.tool_definitions()
        for _ in range(self._max_iterations):
            result: LLMTurnResult | None = None
            # Text written before a tool call and text written after it are separate
            # paragraphs; without a separator they would run together ("…план.Готово").
            needs_separator = bool("".join(text_parts).strip())
            async for ev in self._llm.stream(system=system, tools=tools, messages=messages):
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
            messages.append({"role": "assistant", "content": result.content})
            if not result.tool_calls:
                return
            tool_results: list[dict[str, Any]] = []
            for call in result.tool_calls:
                yield {"type": "tool_started", "name": call.name}
                r = await self._tools.call(
                    call.name, call.input, session_id=session_id, turn_id=turn_id
                )
                summary = r.text[:200] if r.is_error else (r.data or {}).get("summary")
                yield {
                    "type": "tool_finished",
                    "name": call.name,
                    "ok": not r.is_error,
                    "summary": summary,
                }
                if call.name in MUTATING_TOOLS and not r.is_error and r.data:
                    yield {"type": "plan_changed", "version": r.data.get("version")}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": _cap(r.text),
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
