import logging
import re
from datetime import date

from app.agent.llm import (
    Completed,
    LLMError,
    LLMToolCall,
    LLMTurnResult,
    LLMUsage,
    TextDelta,
)
from app.agent.loop import Agent
from app.db import repo
from app.domain.calendar import add_workdays

TODAY = date(2026, 9, 25)


async def collect(agent, sid, text):
    return [ev async for ev in agent.run_turn(sid, text)]


async def new_sid(app):
    _, sid = await app.state.service.create_session()
    return sid


async def test_turn_moves_task_and_reports(app):
    sid = await new_sid(app)
    events = await collect(app.state.agent, sid, "Перенеси задачу 1 на 2 дня")
    types = [e["type"] for e in events]
    assert "tool_started" in types and "plan_changed" in types and types[-1] == "done"
    done = events[-1]
    assert done["summary"].startswith("Изменено задач") and any(
        c["task_id"] == 1 for c in done["changes"]
    )
    assert (await app.state.service.get_state(sid)).version == 2
    assert (await app.state.service.undo(sid)).version == 1


async def test_bulk_move_is_one_undo_group(app):
    sid = await new_sid(app)
    events = await collect(app.state.agent, sid, "Сдвинь все задачи Дмитрия на 3 дня")
    assert events[-1]["type"] == "done" and events[-1]["changes"]
    assert (await app.state.service.undo(sid)).version == 1
    async with app.state.service.sessionmaker() as db:
        msgs = await repo.recent_chat_messages(db, sid, 10)
    assert all("[fake]" not in m.content for m in msgs)


async def test_bulk_shift_moves_each_task_and_project_end_exactly_n_workdays(app):
    # Regression: shifting a chain (Дмитрий owns successive tasks in the demo plan) used to
    # compound — successors moved 6 and 9 workdays and project_end moved 9 instead of 3.
    sid = await new_sid(app)
    before = (await app.state.service.get_state(sid)).scheduled
    dmitry = [t.id for t in before.tasks if t.assignee and t.assignee.startswith("Дмитри")]
    assert len(dmitry) >= 2
    events = await collect(app.state.agent, sid, "Сдвинь все задачи Дмитрия на 3 дня")
    assert events[-1]["type"] == "done"
    after = (await app.state.service.get_state(sid)).scheduled
    for tid in dmitry:
        assert after.task(tid).start == add_workdays(before.task(tid).start, 3), tid
    assert after.project_end == add_workdays(before.project_end, 3)


async def test_turn_persists_chat_and_clears_busy(app):
    sid = await new_sid(app)
    await collect(app.state.agent, sid, "привет")
    async with app.state.service.sessionmaker() as db:
        msgs = await repo.recent_chat_messages(db, sid, 10)
    assert [m.role for m in msgs] == ["user", "assistant"] and "демо-режиме" in msgs[1].content
    assert not app.state.service.locks.is_busy(sid)


async def test_iteration_cap(app):
    class Looping:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            call = LLMToolCall(id=f"x{len(messages)}", name="get_plan", input={})
            yield Completed(
                LLMTurnResult(
                    text="",
                    tool_calls=[call],
                    stop_reason="tool_use",
                    content=[{"type": "tool_use", "id": call.id, "name": "get_plan", "input": {}}],
                )
            )

    sid = await new_sid(app)
    agent = Agent(
        Looping(), app.state.tool_client, app.state.service, today=lambda: TODAY, max_iterations=3
    )
    events = await collect(agent, sid, "зациклись")
    assert events[-1]["type"] == "error" and events[-1]["code"] == "too_many_steps"
    assert not app.state.service.locks.is_busy(sid)


async def test_llm_error_is_reported(app):
    class Broken:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            raise LLMError("llm_unavailable", "LLM временно недоступна, попробуйте позже")
            yield  # pragma: no cover

    sid = await new_sid(app)
    agent = Agent(Broken(), app.state.tool_client, app.state.service, today=lambda: TODAY)
    events = await collect(agent, sid, "что-нибудь")
    assert events[-1] == {
        "type": "error",
        "code": "llm_unavailable",
        "message": "LLM временно недоступна, попробуйте позже",
    }
    assert not app.state.service.locks.is_busy(sid)


async def test_llm_error_after_partial_text_persists_error_not_partial(app):
    class PartialThenBroken:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            yield TextDelta("Сейчас перене")
            raise LLMError("llm_unavailable", "LLM временно недоступна, попробуйте позже")

    sid = await new_sid(app)
    agent = Agent(
        PartialThenBroken(), app.state.tool_client, app.state.service, today=lambda: TODAY
    )
    events = await collect(agent, sid, "что-нибудь")
    assert events[-1] == {
        "type": "error",
        "code": "llm_unavailable",
        "message": "LLM временно недоступна, попробуйте позже",
    }
    async with app.state.service.sessionmaker() as db:
        msgs = await repo.recent_chat_messages(db, sid, 10)
    assistant_msg = msgs[-1]
    assert assistant_msg.role == "assistant"
    assert assistant_msg.content == "LLM временно недоступна, попробуйте позже"
    assert "Сейчас перене" not in assistant_msg.content
    assert assistant_msg.meta["partial_text"] == "Сейчас перене"
    assert assistant_msg.meta["error"] == "llm_unavailable"


async def test_text_from_separate_iterations_is_separated(app):
    class TalksAroundToolCall:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            if len(messages) == 1:
                yield TextDelta("Смотрю план.")
                call = LLMToolCall(id="t1", name="get_plan", input={})
                yield Completed(
                    LLMTurnResult(
                        text="Смотрю план.",
                        tool_calls=[call],
                        stop_reason="tool_use",
                        content=[
                            {"type": "text", "text": "Смотрю план."},
                            {"type": "tool_use", "id": "t1", "name": "get_plan", "input": {}},
                        ],
                    )
                )
            else:
                yield TextDelta("Готово")
                yield TextDelta(", всё в порядке.")
                yield Completed(
                    LLMTurnResult(
                        text="Готово, всё в порядке.",
                        tool_calls=[],
                        stop_reason="end_turn",
                        content=[{"type": "text", "text": "Готово, всё в порядке."}],
                    )
                )

    sid = await new_sid(app)
    agent = Agent(
        TalksAroundToolCall(), app.state.tool_client, app.state.service, today=lambda: TODAY
    )
    events = await collect(agent, sid, "проверь план")
    streamed = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert streamed == "Смотрю план.\n\nГотово, всё в порядке."
    async with app.state.service.sessionmaker() as db:
        msgs = await repo.recent_chat_messages(db, sid, 10)
    assert msgs[-1].content == "Смотрю план.\n\nГотово, всё в порядке."


async def test_default_iteration_cap_is_small():
    # Security audit M1: every iteration re-sends the whole context — 15 of them on a big plan
    # cost millions of input tokens per turn. A normal turn needs 2-3.
    import inspect

    assert inspect.signature(Agent.__init__).parameters["max_iterations"].default <= 8


async def test_large_tool_results_are_truncated_before_going_back_to_the_llm(app):
    # Security audit M1: a 500-task get_plan result went into the context in full, every
    # iteration. The plan is already in the system prompt; the tool result is capped.
    from app.agent import loop as loop_module

    seen: list[str] = []

    class ReadsPlanTwice:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            last = messages[-1]["content"]
            if isinstance(last, list):
                seen.append(last[0]["content"])
            if len(messages) < 3:
                call = LLMToolCall(id=f"g{len(messages)}", name="get_plan", input={})
                yield Completed(
                    LLMTurnResult(
                        text="",
                        tool_calls=[call],
                        stop_reason="tool_use",
                        content=[
                            {"type": "tool_use", "id": call.id, "name": "get_plan", "input": {}}
                        ],
                    )
                )
            else:
                yield Completed(
                    LLMTurnResult(text="ok", tool_calls=[], stop_reason="end_turn", content=[])
                )

    sid = await new_sid(app)
    old = loop_module.MAX_TOOL_RESULT_CHARS
    loop_module.MAX_TOOL_RESULT_CHARS = 300
    try:
        agent = Agent(
            ReadsPlanTwice(), app.state.tool_client, app.state.service, today=lambda: TODAY
        )
        await collect(agent, sid, "покажи план")
    finally:
        loop_module.MAX_TOOL_RESULT_CHARS = old
    assert seen and all(len(s) <= 300 + 200 for s in seen)
    assert "обрезан" in seen[0]


def _tool_turn(call_id, name, args, usage=None):
    return LLMTurnResult(
        text="",
        tool_calls=[LLMToolCall(id=call_id, name=name, input=args)],
        stop_reason="tool_use",
        content=[{"type": "tool_use", "id": call_id, "name": name, "input": args}],
        usage=usage or LLMUsage(),
    )


def _final_turn(text, usage=None):
    return LLMTurnResult(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        content=[{"type": "text", "text": text}],
        usage=usage or LLMUsage(),
    )


class WorstCaseBiller:
    """Bills every call its full estimated input and every output token it was allowed, and
    keeps asking for get_plan: the most a turn can cost within the loop's own accounting."""

    def __init__(self):
        self.calls = []  # (estimated input, max_tokens) per call

    async def stream(self, *, system, tools, messages, max_tokens=None):
        from app.agent.loop import estimate_input_tokens

        estimate = estimate_input_tokens(system, tools, messages)
        self.calls.append((estimate, max_tokens))
        usage = LLMUsage(input_tokens=estimate, output_tokens=max_tokens)
        yield Completed(_tool_turn(f"g{len(messages)}", "get_plan", {}, usage))


async def test_turn_token_budget_is_a_ceiling(app):
    # Security audit M1 follow-up: the budget used to be checked only after a call returned,
    # so the call that crossed it was already paid for (and could be arbitrarily large). Now a
    # call is sent only if its estimated input plus a minimum answer still fits, and its
    # max_tokens is capped to what is left.
    from app.agent.loop import MIN_OUTPUT_TOKENS

    def agent_for(llm, **kw):
        return Agent(
            llm,
            app.state.tool_client,
            app.state.service,
            today=lambda: TODAY,
            max_output_tokens=2000,
            **kw,
        )

    probe = WorstCaseBiller()  # the same turn with room to spare: what each call would cost
    await collect(agent_for(probe, max_iterations=2), await new_sid(app), "покажи план")
    (first, _), (second, _) = probe.calls
    budget = first + 2000 + second + 1500  # room for a full first call and a capped second one

    llm = WorstCaseBiller()
    sid = await new_sid(app)
    events = await collect(agent_for(llm, turn_token_budget=budget), sid, "покажи план")
    assert events[-1]["type"] == "error" and events[-1]["code"] == "turn_budget_exceeded"
    assert "сузьте" in events[-1]["message"]
    assert llm.calls == [(first, 2000), (second, 1500)]  # the second call's answer was capped
    assert sum(estimate + cap for estimate, cap in llm.calls) <= budget
    assert all(cap >= MIN_OUTPUT_TOKENS for _, cap in llm.calls)
    assert not app.state.service.locks.is_busy(sid)


async def test_turn_that_cannot_afford_even_one_call_sends_nothing(app):
    llm = WorstCaseBiller()
    agent = Agent(
        llm, app.state.tool_client, app.state.service, today=lambda: TODAY, turn_token_budget=500
    )
    events = await collect(agent, await new_sid(app), "покажи план")
    assert events[-1]["code"] == "turn_budget_exceeded"
    assert llm.calls == []


async def test_billed_usage_over_the_budget_still_stops_before_tools(app):
    # The estimate is a heuristic: if a response turns out to have cost more than the budget
    # allows, its tool calls are not run (no half-done edit behind an error message).
    calls = []

    class UnderEstimated:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            calls.append(max_tokens)
            op = {"op": "update_task", "id": 1, "duration": 9}
            usage = LLMUsage(input_tokens=90_000, output_tokens=20_000)
            yield Completed(_tool_turn("a1", "apply_operations", {"operations": [op]}, usage))

    sid = await new_sid(app)
    agent = Agent(
        UnderEstimated(),
        app.state.tool_client,
        app.state.service,
        today=lambda: TODAY,
        turn_token_budget=100_000,
    )
    events = await collect(agent, sid, "поменяй что-нибудь")
    assert events[-1]["code"] == "turn_budget_exceeded"
    assert len(calls) == 1
    assert [e for e in events if e["type"] == "tool_started"] == []
    assert (await app.state.service.get_state(sid)).version == 1


async def test_a_final_answer_over_the_budget_still_completes_the_turn(app):
    class OneExpensiveAnswer:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            yield TextDelta("Готово.")
            yield Completed(_final_turn("Готово.", LLMUsage(input_tokens=200_000)))

    sid = await new_sid(app)
    agent = Agent(
        OneExpensiveAnswer(),
        app.state.tool_client,
        app.state.service,
        today=lambda: TODAY,
        turn_token_budget=100_000,
    )
    events = await collect(agent, sid, "привет")
    assert events[-1]["type"] == "done"


async def test_max_output_tokens_comes_from_settings(app):
    assert app.state.agent._max_output_tokens == app.state.settings.llm_max_tokens


async def test_turn_token_budget_comes_from_settings(app):
    assert app.state.settings.llm_turn_token_budget == 300_000
    assert app.state.agent._turn_token_budget == app.state.settings.llm_turn_token_budget


def _trace_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "app.agent.trace"]


async def test_each_turn_logs_one_trace_line_without_content(app, caplog):
    sid = await new_sid(app)
    with caplog.at_level(logging.INFO, logger="app.agent.trace"):
        events = await collect(app.state.agent, sid, "Сдвинь все задачи Дмитрия на 3 дня")
    [line] = _trace_lines(caplog)
    assert line.startswith("agent_turn ")
    assert f"sid={str(sid)[:8]} " in line and str(sid) not in line
    assert f"turn={events[-1]['turn_id']} " in line
    assert " outcome=done " in line
    assert " iterations=3 " in line
    assert " tools=find_tasks,apply_operations " in line
    assert " tokens_in=0 tokens_out=0 cache_write=0 cache_read=0 tokens_total=0 " in line
    assert re.search(r" duration_ms=\d+$", line)
    # No message text, no plan content (names, people), no address.
    assert "Сдвин" not in line and "Дмитри" not in line and "127.0.0.1" not in line


async def test_trace_line_sums_tokens_and_names_the_error(app, caplog):
    class Looping:
        async def stream(self, *, system, tools, messages, max_tokens=None):
            usage = LLMUsage(input_tokens=100, output_tokens=5, cache_read_input_tokens=1000)
            yield Completed(_tool_turn(f"x{len(messages)}", "get_plan", {}, usage))

    sid = await new_sid(app)
    agent = Agent(
        Looping(), app.state.tool_client, app.state.service, today=lambda: TODAY, max_iterations=2
    )
    with caplog.at_level(logging.INFO, logger="app.agent.trace"):
        await collect(agent, sid, "зациклись")
    [line] = _trace_lines(caplog)
    assert " outcome=too_many_steps " in line and " iterations=2 " in line
    assert " tools=get_plan,get_plan " in line
    assert " tokens_in=200 tokens_out=10 cache_write=0 cache_read=2000 tokens_total=2210 " in line


async def test_trace_line_is_logged_for_a_turn_without_tools(app, caplog):
    sid = await new_sid(app)
    with caplog.at_level(logging.INFO, logger="app.agent.trace"):
        await collect(app.state.agent, sid, "привет")
    [line] = _trace_lines(caplog)
    assert " outcome=done iterations=1 tools=- " in line


class MassDeleter:
    """A model talked into deleting tasks 1-6 with confirmed=true right away (security audit
    L3: e.g. by an instruction hidden in a task description), then answering."""

    def __init__(self):
        self.tool_results = []

    async def stream(self, *, system, tools, messages, max_tokens=None):
        last = messages[-1]["content"]
        if isinstance(last, list):
            self.tool_results.append(last[0]["content"])
            yield Completed(_final_turn("Подтвердите удаление задач 1–6."))
        else:
            ops = [{"op": "delete_task", "id": i} for i in range(1, 7)]
            args = {"operations": ops, "confirmed": True}
            yield Completed(_tool_turn(f"d{len(messages)}", "apply_operations", args))


async def test_model_cannot_confirm_a_mass_delete_without_the_users_yes(app):
    llm = MassDeleter()
    agent = Agent(llm, app.state.tool_client, app.state.service, today=lambda: TODAY)
    sid = await new_sid(app)
    events = await collect(agent, sid, "Удали задачи 1, 2, 3, 4, 5, 6")
    assert events[-1]["type"] == "done"
    assert [e["ok"] for e in events if e["type"] == "tool_finished"] == [False]
    # The server asked for confirmation again, and the model is told why its flag was dropped.
    assert "confirmation_required" in llm.tool_results[0]
    assert "подтверждения" in llm.tool_results[0]
    state = await app.state.service.get_state(sid)
    assert state.version == 1 and len(state.plan.tasks) == 25

    events = await collect(agent, sid, "да нет, не надо")
    assert len((await app.state.service.get_state(sid)).plan.tasks) == 25
    # Only the exact reply the assistant asks for confirms: not «да» with anything added.
    events = await collect(agent, sid, "Да, удаляй")
    assert [e["ok"] for e in events if e["type"] == "tool_finished"] == [False]
    assert len((await app.state.service.get_state(sid)).plan.tasks) == 25

    events = await collect(agent, sid, "Да!")
    assert [e["ok"] for e in events if e["type"] == "tool_finished"] == [True]
    state = await app.state.service.get_state(sid)
    assert state.version == 2 and len(state.plan.tasks) == 19
    assert await app.state.service.get_confirmation(sid) is None  # consumed


def _tool_rounds_this_turn(messages):
    rounds = 0
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], str):
            break
        rounds += m["role"] == "user"
    return rounds


class RetryingDeleter:
    """Deletes tasks 1-6 with confirmed=true and, when refused, tries once more in the same
    turn before answering."""

    def __init__(self):
        self.tool_results = []

    async def stream(self, *, system, tools, messages, max_tokens=None):
        last = messages[-1]["content"]
        if isinstance(last, list):
            self.tool_results.append(last[0]["content"])
        if _tool_rounds_this_turn(messages) < 2:
            ops = [{"op": "delete_task", "id": i} for i in range(1, 7)]
            args = {"operations": ops, "confirmed": True}
            yield Completed(_tool_turn(f"d{len(messages)}", "apply_operations", args))
        else:
            yield Completed(_final_turn("Подтвердите удаление задач 1–6."))


async def test_a_yes_cannot_confirm_a_deletion_asked_after_it(app, caplog):
    # The user's «да» answered something else; a model that asks for a mass deletion in the
    # same turn must not be able to confirm it with that «да» right away.
    llm = RetryingDeleter()
    agent = Agent(llm, app.state.tool_client, app.state.service, today=lambda: TODAY)
    sid = await new_sid(app)
    with caplog.at_level(logging.INFO, logger="app.agent.trace"):
        events = await collect(agent, sid, "да")
    assert [e["ok"] for e in events if e["type"] == "tool_finished"] == [False, False]
    assert len((await app.state.service.get_state(sid)).plan.tasks) == 25
    assert "confirmation_required" in llm.tool_results[1]
    assert "после сообщения пользователя" in llm.tool_results[1]
    [line] = _trace_lines(caplog)
    assert " confirm_blocked=1 " in line
    # The request stays pending, so the user's next «да» does confirm it.
    events = await collect(agent, sid, "да")
    oks = [e["ok"] for e in events if e["type"] == "tool_finished"]
    assert oks[0] is True  # (the model's retry then fails: those tasks are gone)
    assert len((await app.state.service.get_state(sid)).plan.tasks) == 19


async def test_a_reply_other_than_yes_ends_the_pending_confirmation(app):
    sid = await new_sid(app)
    deleter = Agent(MassDeleter(), app.state.tool_client, app.state.service, today=lambda: TODAY)
    await collect(deleter, sid, "Удали задачи 1, 2, 3, 4, 5, 6")
    assert (await app.state.service.get_confirmation(sid)).origin == "agent"
    await collect(app.state.agent, sid, "покажи план")  # answers the question with something else
    assert await app.state.service.get_confirmation(sid) is None
    # A later «да» (to whatever) can't pick the old request up.
    events = await collect(deleter, sid, "да")
    assert [e["ok"] for e in events if e["type"] == "tool_finished"] == [False]
    assert len((await app.state.service.get_state(sid)).plan.tasks) == 25


async def test_confirmation_guard_is_counted_in_the_trace(app, caplog):
    agent = Agent(MassDeleter(), app.state.tool_client, app.state.service, today=lambda: TODAY)
    sid = await new_sid(app)
    with caplog.at_level(logging.INFO, logger="app.agent.trace"):
        await collect(agent, sid, "Удали задачи 1, 2, 3, 4, 5, 6")
    [line] = _trace_lines(caplog)
    assert " confirm_blocked=1 " in line


async def test_trace_line_is_logged_when_the_client_goes_away(app, caplog):
    sid = await new_sid(app)
    with caplog.at_level(logging.INFO, logger="app.agent.trace"):
        turn = app.state.agent.run_turn(sid, "Перенеси задачу 1 на 2 дня")
        await anext(turn)
        await turn.aclose()
    [line] = _trace_lines(caplog)
    assert " outcome=aborted " in line
    assert not app.state.service.locks.is_busy(sid)
