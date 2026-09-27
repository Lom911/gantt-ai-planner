"""Mass-delete confirmations (PlanService.apply): a confirmation is bound to the exact batch
and plan version, authorized per origin (the agent loop vouches for the user's «да»; an MCP
client needs approval in the browser), and consumed once, atomically with the edit."""

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from app.domain.errors import ConfirmationRequired
from app.domain.operations import operations_adapter
from app.services.errors import NotFound
from app.services.events import EventBus
from app.services.locks import SessionLocks
from app.services.plan_service import PlanService

TODAY = date(2026, 9, 25)
MASS = [{"op": "delete_task", "id": i} for i in range(1, 8)]
OTHER_MASS = [{"op": "delete_task", "id": i} for i in range(2, 9)]


@pytest.fixture
def service(sessionmaker):
    return PlanService(sessionmaker, EventBus(), SessionLocks(), today=lambda: TODAY)


def ops(raw):
    return operations_adapter.validate_python(list(raw))


async def _rows(service, sid):
    async with service.sessionmaker() as db:
        rows = await db.execute(
            text(
                "SELECT id, origin, resolved, approved_at IS NOT NULL AS approved "
                "FROM plan_confirmations WHERE session_id = :sid ORDER BY created_at, id"
            ),
            {"sid": sid},
        )
    return [dict(r._mapping) for r in rows]


async def _refused(service, sid, batch, **kw) -> ConfirmationRequired:
    with pytest.raises(ConfirmationRequired) as exc:
        await service.apply(sid, ops(batch), **kw)
    return exc.value


def _drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_agent_confirmation_is_stored_then_consumed_once(service):
    _, sid = await service.create_session()
    refused = await _refused(service, sid, MASS, source="agent")
    details = refused.details
    assert details["origin"] == "agent" and details["count"] == 7
    assert (
        details["summary"].startswith("Удалить задачи: №1 «") and "(всего 7)" in details["summary"]
    )
    assert "«да»" in refused.message
    pending = await service.get_confirmation(sid)
    assert pending is not None and str(pending.id) == details["confirmation_id"]
    assert pending.origin == "agent" and not pending.approved and pending.count == 7

    out = await service.apply(sid, ops(MASS), source="agent", confirmed=True)
    assert len(out.state.plan.tasks) == 18
    assert [r["resolved"] for r in await _rows(service, sid)] == ["consumed"]
    assert await service.get_confirmation(sid) is None


async def test_confirmed_without_a_pending_confirmation_is_refused(service):
    _, sid = await service.create_session()
    refused = await _refused(service, sid, MASS, source="agent", confirmed=True)
    assert "confirmed=true не принят" in refused.message
    assert (await service.get_state(sid)).version == 1
    assert [r["resolved"] for r in await _rows(service, sid)] == [None]  # a fresh pending one


async def test_a_different_batch_never_rides_on_an_earlier_confirmation(service):
    _, sid = await service.create_session()
    await _refused(service, sid, MASS, source="agent")
    await _refused(service, sid, OTHER_MASS, source="agent", confirmed=True)
    assert [r["resolved"] for r in await _rows(service, sid)] == ["superseded", None]
    assert len((await service.get_state(sid)).plan.tasks) == 25


async def test_confirmation_is_bound_to_the_plan_version_it_was_asked_on(service):
    _, sid = await service.create_session()
    await _refused(service, sid, MASS, source="agent")
    edit = await service.apply(
        sid, ops([{"op": "move_task", "id": 20, "shift_days": 1}]), source="user"
    )
    assert edit.state.version == 2
    await _refused(service, sid, MASS, source="agent", confirmed=True)
    assert len((await service.get_state(sid)).plan.tasks) == 25


async def test_expired_confirmation_is_refused(service):
    _, sid = await service.create_session()
    await _refused(service, sid, MASS, source="agent")
    async with service.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE plan_confirmations SET expires_at = now()"))
    assert await service.get_confirmation(sid) is None  # expired counts as absent
    await _refused(service, sid, MASS, source="agent", confirmed=True)
    assert [r["resolved"] for r in await _rows(service, sid)] == ["expired", None]


async def test_repeating_the_same_batch_keeps_its_pending_confirmation(service):
    # An MCP client retrying before the user approved must not replace the request the user is
    # looking at (the approve button would then point at a superseded id).
    _, sid = await service.create_session()
    first = await _refused(service, sid, MASS, source="mcp")
    again = await _refused(service, sid, MASS, source="mcp", confirmed=True)
    assert again.details["confirmation_id"] == first.details["confirmation_id"]
    assert "ещё не подтверждено" in again.message
    assert len(await _rows(service, sid)) == 1


async def test_mcp_mass_delete_needs_approval_in_the_browser(service):
    _, sid = await service.create_session()
    queue = service.bus.subscribe(sid)
    refused = await _refused(service, sid, MASS, source="mcp")
    assert "веб-приложении" in refused.message
    [pending_event] = _drain(queue)
    cid = refused.details["confirmation_id"]
    assert pending_event == {
        "type": "confirmation_pending",
        "id": cid,
        "origin": "mcp",
        "summary": refused.details["summary"],
        "count": 7,
        "expires_at": refused.details["expires_at"],
    }

    await _refused(service, sid, MASS, source="mcp", confirmed=True)  # not approved yet
    assert _drain(queue) == []
    approved = await service.approve_confirmation(sid, uuid.UUID(cid))
    assert approved.approved
    assert _drain(queue) == [{"type": "confirmation_resolved", "id": cid, "result": "approved"}]
    assert (await service.get_confirmation(sid)).approved  # still current until consumed

    out = await service.apply(sid, ops(MASS), source="mcp", confirmed=True)
    assert len(out.state.plan.tasks) == 18
    consumed, changed = _drain(queue)
    assert consumed == {"type": "confirmation_resolved", "id": cid, "result": "consumed"}
    assert changed["type"] == "plan_changed" and changed["source"] == "mcp"


async def test_approval_does_not_carry_over_to_another_origin_or_batch(service):
    _, sid = await service.create_session()
    refused = await _refused(service, sid, MASS, source="mcp")
    await service.approve_confirmation(sid, uuid.UUID(refused.details["confirmation_id"]))
    # The agent can't use a browser approval, and a different batch can't either.
    await _refused(service, sid, MASS, source="agent", confirmed=True)
    assert len((await service.get_state(sid)).plan.tasks) == 25
    refused = await _refused(service, sid, MASS, source="mcp")
    await service.approve_confirmation(sid, uuid.UUID(refused.details["confirmation_id"]))
    await _refused(service, sid, OTHER_MASS, source="mcp", confirmed=True)
    assert len((await service.get_state(sid)).plan.tasks) == 25


async def test_only_the_current_mcp_confirmation_can_be_approved(service):
    _, sid = await service.create_session()
    agent = await _refused(service, sid, MASS, source="agent")
    with pytest.raises(NotFound):  # the agent's confirmation is the user's «да» in the chat
        await service.approve_confirmation(sid, uuid.UUID(agent.details["confirmation_id"]))
    with pytest.raises(NotFound):
        await service.approve_confirmation(sid, uuid.uuid4())
    mcp = await _refused(service, sid, MASS, source="mcp")
    with pytest.raises(NotFound):  # superseded by the MCP request
        await service.reject_confirmation(sid, uuid.UUID(agent.details["confirmation_id"]))
    _, other_sid = await service.create_session()
    with pytest.raises(NotFound):  # another session's
        await service.approve_confirmation(other_sid, uuid.UUID(mcp.details["confirmation_id"]))


async def test_rejected_confirmation_is_over(service):
    _, sid = await service.create_session()
    queue = service.bus.subscribe(sid)
    refused = await _refused(service, sid, MASS, source="mcp")
    cid = refused.details["confirmation_id"]
    _drain(queue)
    await service.reject_confirmation(sid, uuid.UUID(cid))
    assert _drain(queue) == [{"type": "confirmation_resolved", "id": cid, "result": "rejected"}]
    assert await service.get_confirmation(sid) is None
    with pytest.raises(NotFound):
        await service.approve_confirmation(sid, uuid.UUID(cid))
    await _refused(service, sid, MASS, source="mcp", confirmed=True)
    assert len((await service.get_state(sid)).plan.tasks) == 25


async def test_approving_an_expired_confirmation_reports_it_expired(service):
    _, sid = await service.create_session()
    queue = service.bus.subscribe(sid)
    refused = await _refused(service, sid, MASS, source="mcp")
    cid = refused.details["confirmation_id"]
    _drain(queue)
    async with service.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE plan_confirmations SET expires_at = now()"))
    with pytest.raises(NotFound):
        await service.approve_confirmation(sid, uuid.UUID(cid))
    assert _drain(queue) == [{"type": "confirmation_resolved", "id": cid, "result": "expired"}]
    assert [r["resolved"] for r in await _rows(service, sid)] == ["expired"]


async def test_a_new_request_supersedes_the_pending_one_and_says_so(service):
    _, sid = await service.create_session()
    queue = service.bus.subscribe(sid)
    first = await _refused(service, sid, MASS, source="mcp")
    second = await _refused(service, sid, OTHER_MASS, source="mcp")
    old, new = first.details["confirmation_id"], second.details["confirmation_id"]
    events = _drain(queue)
    assert [e["type"] for e in events] == [
        "confirmation_pending",
        "confirmation_resolved",
        "confirmation_pending",
    ]
    assert events[1] == {"type": "confirmation_resolved", "id": old, "result": "superseded"}
    assert events[2]["id"] == new
    assert [r["resolved"] for r in await _rows(service, sid)] == ["superseded", None]


async def test_discarding_ends_only_the_agents_confirmation(service):
    # A chat reply other than «да» answers the assistant's question: its pending confirmation
    # is over. An MCP request waiting for the browser is not the chat's to end.
    _, sid = await service.create_session()
    await _refused(service, sid, MASS, source="agent")
    await service.discard_confirmation(sid, origin="agent")
    assert [r["resolved"] for r in await _rows(service, sid)] == ["rejected"]
    await _refused(service, sid, MASS, source="mcp")
    await service.discard_confirmation(sid, origin="agent")
    assert (await service.get_confirmation(sid)).origin == "mcp"


async def test_ui_mass_delete_is_refused_without_storing_a_confirmation(service):
    # The web UI edits one task at a time and has no confirmation step of its own.
    _, sid = await service.create_session()
    await _refused(service, sid, MASS, source="user")
    assert await _rows(service, sid) == []


async def test_confirmation_is_consumed_atomically_with_the_edit(service):
    # A batch that fails after the confirmation matched leaves the confirmation pending.
    from app.domain.errors import OperationError

    _, sid = await service.create_session()
    bad = [*MASS, {"op": "update_task", "id": 999, "duration": 2}]
    await _refused(service, sid, bad, source="agent")
    with pytest.raises(OperationError):
        await service.apply(sid, ops(bad), source="agent", confirmed=True)
    assert [r["resolved"] for r in await _rows(service, sid)] == [None]


# --- HTTP: GET /api/plan/confirmation, POST .../{id}/approve|reject, SSE wire format ---


async def _session_id(app, client):
    return await app.state.service.resolve_session(client.cookies.get("sid"))


async def test_confirmation_endpoints(app, session_client):
    service = app.state.service
    assert (await session_client.get("/api/plan/confirmation")).json() is None
    sid = await _session_id(app, session_client)
    refused = await _refused(service, sid, MASS, source="mcp")
    cid = refused.details["confirmation_id"]
    current = await session_client.get("/api/plan/confirmation")
    assert current.status_code == 200
    assert current.json() == {
        "id": cid,
        "origin": "mcp",
        "summary": refused.details["summary"],
        "count": 7,
        "expires_at": refused.details["expires_at"],
        "approved": False,
    }

    missing = await session_client.post(f"/api/plan/confirmation/{uuid.uuid4()}/approve")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    approved = await session_client.post(f"/api/plan/confirmation/{cid}/approve")
    assert approved.status_code == 200
    assert approved.json() == {**current.json(), "approved": True}
    assert (await session_client.get("/api/plan/confirmation")).json()["approved"] is True

    rejected = await session_client.post(f"/api/plan/confirmation/{cid}/reject")
    assert rejected.status_code == 204
    assert (await session_client.get("/api/plan/confirmation")).json() is None
    gone = await session_client.post(f"/api/plan/confirmation/{cid}/reject")
    assert gone.status_code == 404


async def test_confirmation_endpoints_show_the_agents_request_but_do_not_approve_it(
    app, session_client
):
    sid = await _session_id(app, session_client)
    refused = await _refused(app.state.service, sid, MASS, source="agent")
    cid = refused.details["confirmation_id"]
    assert (await session_client.get("/api/plan/confirmation")).json()["origin"] == "agent"
    r = await session_client.post(f"/api/plan/confirmation/{cid}/approve")
    assert r.status_code == 404 and "«да»" in r.json()["error"]["message"]
    assert (await session_client.post(f"/api/plan/confirmation/{cid}/reject")).status_code == 204


async def test_confirmation_endpoints_need_a_session_same_origin_and_the_mutation_limit(
    app, session_client
):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon:
        assert (await anon.get("/api/plan/confirmation")).status_code == 401
    cid = uuid.uuid4()
    r = await session_client.post(
        f"/api/plan/confirmation/{cid}/approve", headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"
    app.state.settings.mutation_limit_per_ip_hour = 0
    r = await session_client.post(f"/api/plan/confirmation/{cid}/reject")
    assert r.status_code == 429


async def test_confirmation_events_on_the_wire(app):
    # The browser listens on GET /api/events for named events; the data is the event itself.
    import json

    from app.api.routes_events import event_stream

    service = app.state.service
    _, sid = await service.create_session()
    stream = event_stream(service.bus, sid, busy=False)
    await anext(stream)  # agent_status
    refused = await _refused(service, sid, MASS, source="mcp")
    frame = await anext(stream)
    assert frame["event"] == "confirmation_pending"
    assert json.loads(frame["data"]) == {
        "type": "confirmation_pending",
        "id": refused.details["confirmation_id"],
        "origin": "mcp",
        "summary": refused.details["summary"],
        "count": 7,
        "expires_at": refused.details["expires_at"],
    }
    await service.reject_confirmation(sid, uuid.UUID(refused.details["confirmation_id"]))
    frame = await anext(stream)
    assert frame["event"] == "confirmation_resolved"
    assert json.loads(frame["data"]) == {
        "type": "confirmation_resolved",
        "id": refused.details["confirmation_id"],
        "result": "rejected",
    }
    await stream.aclose()


async def test_old_confirmations_are_pruned_by_the_cleanup(app):
    from datetime import UTC, datetime, timedelta

    from app.services.cleanup import purge_expired

    service = app.state.service
    _, sid = await service.create_session()
    await _refused(service, sid, MASS, source="agent")
    await purge_expired(service.sessionmaker, ttl_days=14, now=datetime.now(UTC))
    assert len(await _rows(service, sid)) == 1  # still fresh
    later = datetime.now(UTC) + timedelta(days=2)
    await purge_expired(service.sessionmaker, ttl_days=14, now=later)
    assert await _rows(service, sid) == []
