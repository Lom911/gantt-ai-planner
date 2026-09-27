import asyncio
import uuid
from datetime import date

import pytest

from app.db import repo
from app.domain.errors import ConfirmationRequired, OperationError
from app.domain.operations import operations_adapter
from app.domain.seed import build_demo_plan
from app.services.errors import AgentBusy, NothingToUndo
from app.services.events import EventBus
from app.services.locks import SessionLocks
from app.services.plan_service import PlanService

TODAY = date(2026, 9, 25)


@pytest.fixture
def service(sessionmaker):
    return PlanService(
        sessionmaker, EventBus(), SessionLocks(), max_versions=50, today=lambda: TODAY
    )


def ops(*raw):
    return operations_adapter.validate_python(list(raw))


async def test_create_session_seeds_demo_plan(service):
    token, sid = await service.create_session()
    assert await service.resolve_session(token) == sid
    assert await service.resolve_session("nope") is None
    state = await service.get_state(sid)
    assert state.version == 1 and not state.can_undo and not state.can_redo
    assert state.plan == build_demo_plan(TODAY)


async def test_apply_publishes_event_and_versions(service):
    _, sid = await service.create_session()
    queue = service.bus.subscribe(sid)
    out = await service.apply(
        sid, ops({"op": "move_task", "id": 1, "shift_days": 1}), source="user"
    )
    assert out.state.version == 2 and out.state.can_undo
    event = queue.get_nowait()
    assert (
        event["type"] == "plan_changed" and event["version"] == 2 and 1 in event["changed_task_ids"]
    )


async def test_no_change_batch_creates_no_version(service):
    _, sid = await service.create_session()
    out = await service.apply(
        sid,
        ops({"op": "update_task", "id": 1, "name": "Сбор требований и приоритизация"}),
        source="user",
    )
    assert out.state.version == 1 and out.summary == "Без изменений"


async def test_undo_redo_turn_group_and_truncate(service):
    _, sid = await service.create_session()
    turn = uuid.uuid4()
    await service.apply(
        sid, ops({"op": "update_task", "id": 1, "duration": 5}), source="agent", turn_id=turn
    )
    await service.apply(
        sid, ops({"op": "update_task", "id": 2, "duration": 5}), source="agent", turn_id=turn
    )
    state = await service.undo(sid)
    assert state.version == 1 and state.can_redo
    state = await service.redo(sid)
    assert state.version == 3
    await service.undo(sid)
    await service.apply(sid, ops({"op": "update_task", "id": 3, "duration": 9}), source="user")
    state = await service.get_state(sid)
    assert state.version == 2 and not state.can_redo
    await service.undo(sid)
    with pytest.raises(NothingToUndo):
        await service.undo(sid)


async def test_invalid_batch_leaves_state(service):
    _, sid = await service.create_session()
    with pytest.raises(OperationError):
        await service.apply(sid, ops({"op": "delete_task", "id": 999}), source="user")
    assert (await service.get_state(sid)).version == 1


async def test_confirmation_required_for_mass_delete(service):
    _, sid = await service.create_session()
    batch = ops(*({"op": "delete_task", "id": i} for i in range(1, 8)))
    with pytest.raises(ConfirmationRequired):
        await service.apply(sid, batch, source="agent")
    out = await service.apply(sid, batch, source="agent", confirmed=True)
    assert len(out.state.plan.tasks) == 18


async def test_user_edit_rejected_while_agent_busy_and_mcp_waits(service):
    _, sid = await service.create_session()
    async with service.locks.agent_turn(sid):
        with pytest.raises(AgentBusy):
            await service.apply(
                sid, ops({"op": "update_task", "id": 1, "duration": 2}), source="user"
            )
        with pytest.raises(AgentBusy):
            async with service.locks.agent_turn(sid):
                pass

    async def short_turn():
        async with service.locks.agent_turn(sid):
            await asyncio.sleep(0.2)

    task = asyncio.create_task(short_turn())
    await asyncio.sleep(0.05)
    out = await service.apply(sid, ops({"op": "update_task", "id": 1, "duration": 2}), source="mcp")
    await task
    assert out.state.version == 2


async def test_replace_and_reset_are_undoable_and_add_chat_note(service):
    _, sid = await service.create_session()
    plan = build_demo_plan(TODAY)
    plan.tasks = plan.tasks[:3]
    plan.dependencies = [
        d for d in plan.dependencies if d.predecessor_id <= 3 and d.successor_id <= 3
    ]
    state = await service.replace(
        sid, plan, source="import", summary="Импорт", chat_note="Загружен план «x.xlsx», задач: 3"
    )
    assert len(state.plan.tasks) == 3
    async with service.sessionmaker() as db:
        msgs = await repo.recent_chat_messages(db, sid, 5)
    assert msgs[-1].role == "system" and "x.xlsx" in msgs[-1].content
    assert len((await service.reset(sid)).plan.tasks) == 25
    assert len((await service.undo(sid)).plan.tasks) == 3


async def test_versions_are_pruned(sessionmaker):
    service = PlanService(
        sessionmaker, EventBus(), SessionLocks(), max_versions=3, today=lambda: TODAY
    )
    _, sid = await service.create_session()
    for d in range(2, 7):
        await service.apply(sid, ops({"op": "update_task", "id": 1, "duration": d}), source="user")
    assert (await service.get_state(sid)).version == 6
    await service.undo(sid)
    await service.undo(sid)
    with pytest.raises(NothingToUndo):
        await service.undo(sid)


async def test_mutation_or_import_over_the_plan_size_cap_is_rejected(sessionmaker):
    # Security audit M2: every version stores a full snapshot (up to max_versions of them).
    from app.services.errors import PlanTooLarge
    from app.services.plan_service import plan_json_size

    demo_size = plan_json_size(build_demo_plan(TODAY))
    service = PlanService(
        sessionmaker,
        EventBus(),
        SessionLocks(),
        max_plan_bytes=demo_size + 300,
        today=lambda: TODAY,
    )
    _, sid = await service.create_session()
    queue = service.bus.subscribe(sid)
    out = await service.apply(
        sid, ops({"op": "update_task", "id": 1, "description": "x" * 100}), source="user"
    )
    assert out.state.version == 2  # still under the cap
    queue.get_nowait()
    with pytest.raises(PlanTooLarge) as exc:
        await service.apply(
            sid, ops({"op": "update_task", "id": 1, "description": "я" * 300}), source="agent"
        )
    assert exc.value.code == "plan_too_large" and "слишком большим" in exc.value.message
    big = build_demo_plan(TODAY)
    big.tasks[0].description = "я" * 300  # 600 bytes in UTF-8
    with pytest.raises(PlanTooLarge):
        await service.replace(sid, big, source="import", summary="Импорт")
    state = await service.get_state(sid)
    assert state.version == 2 and state.plan.tasks[0].description == "x" * 100
    assert queue.empty()  # nothing stored, nothing announced


def _worker(sessionmaker):
    # A second uvicorn worker = its own PlanService with its own in-process SessionLocks: only
    # the database can serialize it with the others.
    return PlanService(sessionmaker, EventBus(), SessionLocks(), today=lambda: TODAY)


async def _warm_pool(sessionmaker):
    """Two pooled connections ready, so neither racer is delayed by opening one (which lets
    the other finish first and hides the race)."""
    from sqlalchemy import text

    dbs = [sessionmaker(), sessionmaker()]
    await asyncio.gather(*(db.execute(text("SELECT 1")) for db in dbs))
    for db in dbs:
        await db.close()


async def test_concurrent_applies_from_two_workers_get_consecutive_versions(sessionmaker):
    a, b = _worker(sessionmaker), _worker(sessionmaker)
    _, sid = await a.create_session()
    await _warm_pool(sessionmaker)
    out_a, out_b = await asyncio.gather(
        a.apply(sid, ops({"op": "update_task", "id": 1, "duration": 5}), source="user"),
        b.apply(sid, ops({"op": "update_task", "id": 2, "duration": 6}), source="user"),
    )
    assert sorted([out_a.state.version, out_b.state.version]) == [2, 3]
    state = await a.get_state(sid)
    assert state.version == 3
    # No lost update: the later batch was applied on top of the earlier one.
    assert state.plan.tasks[0].duration == 5 and state.plan.tasks[1].duration == 6


async def test_concurrent_undo_and_apply_from_two_workers_do_not_interleave(sessionmaker):
    a, b = _worker(sessionmaker), _worker(sessionmaker)
    _, sid = await a.create_session()
    await a.apply(sid, ops({"op": "update_task", "id": 1, "duration": 5}), source="user")
    await _warm_pool(sessionmaker)
    await asyncio.gather(
        a.undo(sid),
        b.apply(sid, ops({"op": "update_task", "id": 2, "duration": 6}), source="user"),
    )
    state = await a.get_state(sid)
    # Either order is fine, but each step must have seen the other's committed result.
    if state.plan.tasks[1].duration == 6:  # undo first (-> v1), then apply (-> v2)
        assert state.version == 2 and state.plan.tasks[0].duration == 3
    else:  # apply first (-> v3), then undo (-> v2)
        assert state.version == 2 and state.plan.tasks[0].duration == 5
    async with a.sessionmaker() as db:
        meta = await repo.list_version_meta(db, sid)
    assert [m.version_no for m in meta] == list(range(1, len(meta) + 1))


async def test_delete_session_forgets_lock_and_bus_entries(service):
    _, sid = await service.create_session()
    service.bus.subscribe(sid)
    service.locks.lock(sid)
    assert sid in service.locks._locks
    assert sid in service.bus._subs

    await service.delete_session(sid)

    assert sid not in service.locks._locks
    assert sid not in service.bus._subs
    async with service.sessionmaker() as db:
        assert await repo.get_session(db, sid) is None


async def test_set_project_start_without_task_moves_still_creates_version(service):
    from app.domain.models import Plan

    _, sid = await service.create_session()
    await service.replace(
        sid, Plan(project_start=date(2026, 9, 21)), source="import", summary="Пустой план"
    )
    queue = service.bus.subscribe(sid)
    out = await service.apply(
        sid, ops({"op": "set_project_start", "date": "2026-10-05"}), source="user"
    )
    assert out.changes == []
    assert out.state.version == 3 and out.state.can_undo
    assert out.state.plan.project_start == date(2026, 10, 5)
    assert out.summary == "Старт проекта перенесён на 05.10.2026"
    assert queue.get_nowait()["version"] == 3
    assert (await service.get_state(sid)).plan.project_start == date(2026, 10, 5)


async def test_set_project_start_that_moves_tasks_mentions_both(service):
    _, sid = await service.create_session()
    out = await service.apply(
        sid, ops({"op": "set_project_start", "date": "2026-10-05"}), source="user"
    )
    assert out.changes
    assert out.summary.startswith("Изменено задач")
    assert out.summary.endswith("; старт проекта перенесён на 05.10.2026")


async def test_stale_expected_version_is_rejected(service):
    from app.services.errors import VersionConflict

    _, sid = await service.create_session()
    await service.apply(sid, ops({"op": "move_task", "id": 1, "shift_days": 1}), source="agent")
    with pytest.raises(VersionConflict) as exc:
        await service.apply(
            sid,
            ops({"op": "update_task", "id": 1, "name": "Старое имя"}),
            source="user",
            expected_version=1,
        )
    assert exc.value.details == {"expected_version": 1, "current_version": 2}
    assert (await service.get_state(sid)).plan.tasks[0].name != "Старое имя"
    with pytest.raises(VersionConflict):
        await service.undo(sid, expected_version=1)
    # Matching version passes; None skips the check.
    out = await service.apply(
        sid, ops({"op": "update_task", "id": 1, "name": "Новое"}), source="user", expected_version=2
    )
    assert out.state.version == 3
    assert (await service.undo(sid, expected_version=3)).version == 2
    assert (await service.redo(sid)).version == 3
    # An import (replace) is checked the same way, under the plan lock.
    with pytest.raises(VersionConflict):
        await service.replace(
            sid, build_demo_plan(TODAY), source="import", summary="Импорт", expected_version=2
        )
    assert (await service.get_state(sid)).version == 3
    replaced = await service.replace(
        sid, build_demo_plan(TODAY), source="import", summary="Импорт", expected_version=3
    )
    assert replaced.version == 4
