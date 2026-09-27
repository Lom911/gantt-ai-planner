import asyncio
import json
import uuid
from datetime import date

from app.db import repo
from app.mcp_server.client import PlanToolClient
from app.mcp_server.server import build_mcp


async def new_sid(app):
    _, sid = await app.state.service.create_session()
    return sid


async def test_tool_definitions_are_anthropic_shaped(app):
    defs = await app.state.tool_client.tool_definitions()
    assert {d["name"] for d in defs} == {
        "get_plan",
        "find_tasks",
        "get_task",
        "get_resource_load",
        "apply_operations",
        "undo",
    }
    apply = next(d for d in defs if d["name"] == "apply_operations")
    assert apply["input_schema"]["type"] == "object"
    assert "operations" in apply["input_schema"]["properties"]
    assert "session_id" not in json.dumps(defs)
    assert apply["input_schema"]["properties"]["operations"]["maxItems"] == 200
    # Both kinds of caller learn how a mass deletion gets confirmed.
    assert "«да»" in apply["description"] and "веб-приложении" in apply["description"]


async def test_get_plan_and_find_tasks(app):
    sid = await new_sid(app)
    tools = app.state.tool_client
    r = await tools.call("get_plan", {}, session_id=sid, turn_id=None)
    assert not r.is_error and "Следующий свободный id: 26" in r.text
    assert r.text.startswith("Версия плана: 1\n")
    r = await tools.call("find_tasks", {"assignee": "дмитрий"}, session_id=sid, turn_id=None)
    found = r.data["result"]
    assert not r.is_error and 11 in [t["id"] for t in found]


async def test_apply_operations_tags_agent_turn_and_undo(app):
    sid = await new_sid(app)
    tools = app.state.tool_client
    turn = uuid.uuid4()
    ops = {"operations": [{"op": "move_task", "id": 1, "shift_days": 2}]}
    r = await tools.call("apply_operations", ops, session_id=sid, turn_id=turn)
    assert not r.is_error, r.text
    assert r.data["version"] == 2 and r.data["summary"].startswith("Изменено задач")
    async with app.state.service.sessionmaker() as db:
        meta = await repo.list_version_meta(db, sid)
    assert meta[-1].turn_id == turn
    r = await tools.call("undo", {}, session_id=sid, turn_id=turn)
    assert not r.is_error and r.data["version"] == 1


async def test_domain_errors_become_tool_errors(app):
    sid = await new_sid(app)
    tools = app.state.tool_client
    r = await tools.call(
        "apply_operations",
        {"operations": [{"op": "delete_task", "id": 999}]},
        session_id=sid,
        turn_id=None,
    )
    assert r.is_error and "invalid_operation" in r.text
    batch = [{"op": "delete_task", "id": i} for i in range(1, 8)]
    r = await tools.call("apply_operations", {"operations": batch}, session_id=sid, turn_id=None)
    assert r.is_error and "confirmation_required" in r.text


async def test_oversized_batch_is_tool_error(app):
    sid = await new_sid(app)
    batch = [{"op": "update_task", "id": 1, "duration": 2}] * 201
    r = await app.state.tool_client.call(
        "apply_operations", {"operations": batch}, session_id=sid, turn_id=None
    )
    assert r.is_error and "не больше 200 операций" in r.text


async def test_sessions_are_isolated_under_concurrency(app):
    a, b = await new_sid(app), await new_sid(app)
    tools = app.state.tool_client
    del_a = {"operations": [{"op": "delete_task", "id": 25}]}
    del_b = {"operations": [{"op": "delete_task", "id": 24}]}
    await asyncio.gather(
        tools.call("apply_operations", del_a, session_id=a, turn_id=None),
        tools.call("apply_operations", del_b, session_id=b, turn_id=None),
    )
    ta = {t.id for t in (await app.state.service.get_state(a)).plan.tasks}
    tb = {t.id for t in (await app.state.service.get_state(b)).plan.tasks}
    assert 25 not in ta and 24 in ta and 24 not in tb and 25 in tb


async def test_missing_session_is_tool_error(app):
    mcp = build_mcp(app.state.service, today=lambda: date(2026, 9, 25))
    async with PlanToolClient(mcp) as client:
        r = await client.call("get_plan", {}, session_id=None, turn_id=None)
    assert r.is_error and "сесси" in r.text


async def test_resource_load_groups_assignees_case_insensitively(app):
    sid = await new_sid(app)
    tools = app.state.tool_client
    ops = {
        "operations": [
            {"op": "add_task", "name": "A", "duration": 3, "assignee": "Зоя Ёлкина"},
            {"op": "add_task", "name": "B", "duration": 3, "assignee": "зоя ёлкина "},
        ]
    }
    r = await tools.call("apply_operations", ops, session_id=sid, turn_id=None)
    assert not r.is_error, r.text
    a, b = r.data["created_task_ids"]
    r = await tools.call("get_resource_load", {"assignee": "ёлкина"}, session_id=sid, turn_id=None)
    assert not r.is_error, r.text
    people = r.data["result"]
    assert len(people) == 1
    assert {t["id"] for t in people[0]["tasks"]} == {a, b}
    assert people[0]["conflicts"] == [[a, b]]


async def test_in_process_agent_may_omit_expected_version_but_it_is_checked_if_given(app):
    # The agent's turn already holds the session (and its tools run under the plan lock), so the
    # version is optional there; a wrong one is still refused.
    sid = await new_sid(app)
    tools = app.state.tool_client
    turn = uuid.uuid4()
    move = [{"op": "move_task", "id": 1, "shift_days": 1}]
    r = await tools.call("apply_operations", {"operations": move}, session_id=sid, turn_id=turn)
    assert not r.is_error and r.data["version"] == 2
    r = await tools.call(
        "apply_operations",
        {"operations": move, "expected_version": 1},
        session_id=sid,
        turn_id=turn,
    )
    assert r.is_error and "version_conflict" in r.text
