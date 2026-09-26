from datetime import date

from app.agent.llm import Completed, LLMError, LLMToolCall, LLMTurnResult, TextDelta
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
        async def stream(self, *, system, tools, messages):
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
        async def stream(self, *, system, tools, messages):
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
        async def stream(self, *, system, tools, messages):
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
        async def stream(self, *, system, tools, messages):
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
        async def stream(self, *, system, tools, messages):
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
