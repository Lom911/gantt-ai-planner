"""External MCP endpoint over Streamable HTTP with per-session bearer tokens (spec §8).

fastmcp's own `Client` has no ASGI transport (only real-URL Streamable HTTP, SSE,
stdio and in-memory `FastMCP` transports — verified by reading
`fastmcp/client/transports/*.py`). Spinning up a real uvicorn thread proved
unnecessary: `StreamableHttpTransport` accepts an `httpx_client_factory`, so we
point it at an `httpx2.AsyncClient` (fastmcp vendors its own httpx fork,
`httpx2`) backed by `httpx2.ASGITransport(app=...)`. This drives the real
Streamable HTTP protocol (auth middleware, path routing, JSON-RPC) end to end
against the in-process app, without a real socket — the "httpx ASGITransport
fallback" the brief allows, just wired through fastmcp.Client instead of raw
JSON-RPC so we also exercise its session/protocol handling.
"""

import asyncio
from typing import Any

import httpx
import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from mcp.shared.exceptions import MCPError


def _client_factory(app: Any, extra_headers: dict[str, str] | None = None) -> Any:
    def factory(
        *, headers: dict[str, str] | None = None, auth: Any = None, timeout: Any = None, **_: Any
    ) -> httpx2.AsyncClient:
        merged = dict(headers or {})
        merged.update(extra_headers or {})
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers=merged,
            auth=auth,
            timeout=timeout,
        )

    return factory


def _mcp_client(app: Any, token: str, *, origin: str | None = None) -> Client:
    headers = {"Origin": origin} if origin else None
    transport = StreamableHttpTransport(
        "http://testserver/mcp/", auth=token, httpx_client_factory=_client_factory(app, headers)
    )
    return Client(transport)


async def _issue_token(session_client: httpx.AsyncClient) -> str:
    r = await session_client.post("/api/mcp-token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("mcp_")
    assert body["url"] == "http://testserver/mcp"
    assert "claude mcp add" in body["claude_code_command"]
    assert body["claude_desktop_config"]["mcpServers"]["planner"]["url"] == "http://testserver/mcp"
    return str(body["token"])


async def test_apply_operations_over_http_increments_version_and_publishes_mcp_event(
    app, session_client
):
    token = await _issue_token(session_client)
    sid_cookie = session_client.cookies.get("sid")
    session_id = await app.state.service.resolve_session(sid_cookie)
    queue = app.state.service.bus.subscribe(session_id)

    before = await app.state.service.get_state(session_id)

    async with _mcp_client(app, token) as client:
        result = await client.call_tool(
            "apply_operations",
            {
                "operations": [{"op": "move_task", "id": 1, "shift_days": 1}],
                "expected_version": before.version,
            },
        )
    assert not result.is_error, result.content
    assert result.structured_content["version"] == before.version + 1

    event = await asyncio.wait_for(queue.get(), timeout=2)
    assert event["type"] == "plan_changed"
    assert event["source"] == "mcp"
    assert event["version"] == before.version + 1

    after = await app.state.service.get_state(session_id)
    assert after.version == before.version + 1


async def test_external_mcp_mass_delete_needs_approval_in_the_browser(app, session_client):
    # An external client's own confirmed=true proves nothing (the model behind it can be talked
    # into setting it): the user approves the exact batch in the web app, then the client's
    # confirmed=true of that batch runs it.
    token = await _issue_token(session_client)
    session_id = await app.state.service.resolve_session(session_client.cookies.get("sid"))
    queue = app.state.service.bus.subscribe(session_id)
    ops = [{"op": "delete_task", "id": i} for i in range(1, 7)]
    async with _mcp_client(app, token) as client:
        refused = await client.call_tool(
            "apply_operations", {"operations": ops, "expected_version": 1}, raise_on_error=False
        )
        assert refused.is_error and "confirmation_required" in refused.content[0].text
        assert "веб-приложении" in refused.content[0].text
        early = await client.call_tool(
            "apply_operations",
            {"operations": ops, "confirmed": True, "expected_version": 1},
            raise_on_error=False,
        )
        assert early.is_error and "ещё не подтверждено" in early.content[0].text

        pending = (await session_client.get("/api/plan/confirmation")).json()
        assert pending["origin"] == "mcp" and pending["count"] == 6 and not pending["approved"]
        event = queue.get_nowait()
        assert event["type"] == "confirmation_pending" and event["id"] == pending["id"]
        approve = await session_client.post(f"/api/plan/confirmation/{pending['id']}/approve")
        assert approve.status_code == 200

        confirmed = await client.call_tool(
            "apply_operations", {"operations": ops, "confirmed": True, "expected_version": 1}
        )
    assert not confirmed.is_error and confirmed.structured_content["version"] == 2
    assert len((await app.state.service.get_state(session_id)).plan.tasks) == 19


async def test_bad_token_is_rejected(app, session_client):
    await _issue_token(session_client)  # a valid token exists, but we use a bogus one
    with pytest.raises(MCPError):  # fastmcp wraps the HTTP 401 as an MCPError
        async with _mcp_client(app, "mcp_" + "x" * 43) as client:
            await client.call_tool("get_plan", {})


async def test_revoked_token_is_rejected(app, session_client):
    token = await _issue_token(session_client)
    r = await session_client.delete("/api/mcp-token")
    assert r.status_code == 204

    with pytest.raises(MCPError):
        async with _mcp_client(app, token) as client:
            await client.call_tool("get_plan", {})


async def test_issuing_a_new_token_revokes_the_previous_one(app, session_client):
    old_token = await _issue_token(session_client)
    new_token = await _issue_token(session_client)
    assert new_token != old_token

    with pytest.raises(MCPError):
        async with _mcp_client(app, old_token) as client:
            await client.call_tool("get_plan", {})

    async with _mcp_client(app, new_token) as client:
        result = await client.call_tool("get_plan", {})
    assert not result.is_error


async def test_cross_origin_request_is_rejected_before_reaching_mcp(app, session_client):
    token = await _issue_token(session_client)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
        r = await raw.post(
            "/mcp/",
            headers={
                "Origin": "https://evil.example",
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "bad_origin"


async def _raw_mcp_post(app: Any, **headers: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
        return await raw.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **headers,
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )


async def test_mcp_requests_are_limited_per_ip_authenticated_or_not(app):
    # Security audit L5: every unauthenticated attempt costs a token lookup in the database.
    app.state.settings.mcp_limit_per_ip_hour = 2
    statuses = [(await _raw_mcp_post(app)).status_code for _ in range(2)]
    assert statuses == [401, 401]
    r = await _raw_mcp_post(app, Authorization="Bearer mcp_" + "x" * 43)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert "MCP" in r.json()["error"]["message"]


async def test_mcp_limit_uses_the_proxy_appended_address(app):
    app.state.settings.mcp_limit_per_ip_hour = 1
    app.state.settings.trust_proxy = True
    assert (await _raw_mcp_post(app, **{"X-Forwarded-For": "203.0.113.1"})).status_code == 401
    assert (await _raw_mcp_post(app, **{"X-Forwarded-For": "203.0.113.1"})).status_code == 429
    assert (await _raw_mcp_post(app, **{"X-Forwarded-For": "203.0.113.2"})).status_code == 401


async def test_mcp_limit_does_not_touch_other_paths(session_client, app):
    app.state.settings.mcp_limit_per_ip_hour = 0
    assert (await session_client.get("/api/plan")).status_code == 200


async def test_mcp_token_requires_a_session(client):
    r = await client.post("/api/mcp-token")
    assert r.status_code == 401
    assert (await client.get("/api/mcp-token")).status_code == 401


NO_TOKEN = {
    "active": False,
    "prefix": None,
    "created_at": None,
    "expires_at": None,
    "last_used_at": None,
}


async def test_mcp_token_status_shows_activity_but_never_the_token(app, session_client):
    assert (await session_client.get("/api/mcp-token")).json() == NO_TOKEN
    token = await _issue_token(session_client)
    body = (await session_client.get("/api/mcp-token")).json()
    assert body["active"] is True
    assert body["prefix"] == token[:12] and token not in str(body)
    assert body["created_at"] and body["expires_at"] and body["last_used_at"] is None
    async with _mcp_client(app, token) as client:
        await client.call_tool("get_plan", {})
    assert (await session_client.get("/api/mcp-token")).json()["last_used_at"] is not None
    assert (await session_client.delete("/api/mcp-token")).status_code == 204
    assert (await session_client.get("/api/mcp-token")).json() == NO_TOKEN


async def test_expired_mcp_token_is_reported_inactive(app, session_client):
    from sqlalchemy import text

    token = await _issue_token(session_client)
    async with app.state.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE mcp_tokens SET expires_at = now() - interval '1 minute'"))
    body = (await session_client.get("/api/mcp-token")).json()
    assert body["active"] is False
    assert body["prefix"] == token[:12] and body["expires_at"]


async def test_bare_post_mcp_without_trailing_slash_returns_200_not_307(app, session_client):
    """McpOriginGate rewrites a bare `/mcp` to `/mcp/` before fastmcp's router sees it, so a
    client that (correctly, per the MCP HTTP spec) POSTs to `/mcp` without a trailing slash
    never hits fastmcp's own 307 redirect — which MCP HTTP clients don't reliably follow.
    """
    token = await _issue_token(session_client)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as raw:
        r = await raw.post(
            "/mcp",
            headers={
                "Origin": "http://testserver",
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert r.status_code == 200
    assert r.json()["result"]["tools"]


async def test_mcp_activity_keeps_the_session_alive(app, session_client):
    # Security audit: a user working only through an MCP client must not lose the plan to the
    # idle-session cleanup — using the token counts as activity on its session.
    from sqlalchemy import text

    token = await _issue_token(session_client)
    async with app.state.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE sessions SET last_seen_at = now() - interval '13 days'"))
    async with _mcp_client(app, token) as client:
        await client.call_tool("get_plan", {})
    async with app.state.sessionmaker() as db:
        age = (await db.execute(text("SELECT now() - last_seen_at FROM sessions"))).scalar_one()
    assert age.total_seconds() < 60


async def _activity_ages(app: Any) -> tuple[float, float]:
    from sqlalchemy import text

    async with app.state.sessionmaker() as db:
        token_age = (
            await db.execute(text("SELECT now() - last_used_at FROM mcp_tokens"))
        ).scalar_one()
        session_age = (
            await db.execute(text("SELECT now() - last_seen_at FROM sessions"))
        ).scalar_one()
    return token_age.total_seconds(), session_age.total_seconds()


async def test_mcp_activity_is_written_at_most_every_ten_minutes(app, session_client):
    # Every MCP request verifies the token; rewriting last_used_at and last_seen_at on each one
    # is a WAL write per request for timestamps that only matter at the scale of days.
    from sqlalchemy import text

    token = await _issue_token(session_client)
    async with app.state.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE mcp_tokens SET last_used_at = now() - interval '1 minute'"))
        await db.execute(text("UPDATE sessions SET last_seen_at = now() - interval '1 minute'"))
    async with _mcp_client(app, token) as client:
        await client.call_tool("get_plan", {})
    token_age, session_age = await _activity_ages(app)
    assert token_age >= 50 and session_age >= 50  # fresh enough: left alone
    async with app.state.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE mcp_tokens SET last_used_at = now() - interval '11 minutes'"))
        await db.execute(text("UPDATE sessions SET last_seen_at = now() - interval '11 minutes'"))
    async with _mcp_client(app, token) as client:
        await client.call_tool("get_plan", {})
    token_age, session_age = await _activity_ages(app)
    assert token_age < 10 and session_age < 10  # stale: touched


async def _live_token_prefixes(app: Any) -> list[str]:
    from sqlalchemy import text

    async with app.state.sessionmaker() as db:
        rows = await db.execute(text("SELECT prefix FROM mcp_tokens WHERE revoked_at IS NULL"))
    return list(rows.scalars().all())


def _slow_revoke(monkeypatch: Any) -> asyncio.Event:
    """Widen the window between «revoke the old tokens» and «insert the new one», so two
    unserialized requests would both revoke before either inserts. The returned event is set
    once a revoke has run (its transaction still open)."""
    from app.db import repo

    real = repo.revoke_mcp_tokens
    revoking = asyncio.Event()

    async def slow(db: Any, session_id: Any, now: Any) -> None:
        await real(db, session_id, now)
        revoking.set()
        await asyncio.sleep(0.5)

    monkeypatch.setattr(repo, "revoke_mcp_tokens", slow)
    return revoking


async def _warm_pool(app: Any, n: int = 4) -> None:
    """Pooled connections ready, so no racer is delayed by opening one (which lets the other
    finish first and hides the race)."""
    from sqlalchemy import text

    dbs = [app.state.sessionmaker() for _ in range(n)]
    await asyncio.gather(*(db.execute(text("SELECT 1")) for db in dbs))
    for db in dbs:
        await db.close()


async def test_concurrent_token_issues_leave_exactly_one_live_token(
    app, session_client, monkeypatch
):
    await _warm_pool(app)
    _slow_revoke(monkeypatch)
    responses = await asyncio.gather(*(session_client.post("/api/mcp-token") for _ in range(3)))
    assert [r.status_code for r in responses] == [200, 200, 200]
    live = await _live_token_prefixes(app)
    assert len(live) == 1
    assert live[0] in {r.json()["token"][:12] for r in responses}


async def test_revoke_during_an_issue_also_revokes_the_new_token(app, session_client, monkeypatch):
    # Unserialized, the revoke's UPDATE waited on the old token's row lock, then skipped the
    # token the issue had just inserted (not in its snapshot): «Отключить» answered 204 while
    # a live token remained.
    await _issue_token(session_client)
    await _warm_pool(app)
    revoking = _slow_revoke(monkeypatch)
    issue = asyncio.create_task(session_client.post("/api/mcp-token"))
    await revoking.wait()  # the issue is inside its transaction
    revoked = await session_client.delete("/api/mcp-token")
    issued = await issue
    assert issued.status_code == 200 and revoked.status_code == 204
    assert await _live_token_prefixes(app) == []


async def test_external_edits_must_say_which_version_they_build_on(app, session_client):
    # Optimistic concurrency for external clients: without expected_version an edit computed
    # on a stale get_plan would silently overwrite what the user did in the browser meanwhile.
    token = await _issue_token(session_client)
    move = [{"op": "move_task", "id": 1, "shift_days": 1}]
    async with _mcp_client(app, token) as client:
        plan = await client.call_tool("get_plan", {})
        assert "Версия плана: 1" in plan.content[0].text
        task = await client.call_tool("get_task", {"id": 1})
        assert task.structured_content["version"] == 1

        missing = await client.call_tool(
            "apply_operations", {"operations": move}, raise_on_error=False
        )
        assert missing.is_error and "expected_version" in missing.content[0].text
        assert "get_plan" in missing.content[0].text
        ok = await client.call_tool("apply_operations", {"operations": move, "expected_version": 1})
        assert ok.structured_content["version"] == 2
        stale = await client.call_tool(
            "apply_operations", {"operations": move, "expected_version": 1}, raise_on_error=False
        )
        assert stale.is_error and "version_conflict" in stale.content[0].text
        assert "текущая 2" in stale.content[0].text

        no_version_undo = await client.call_tool("undo", {}, raise_on_error=False)
        assert no_version_undo.is_error and "expected_version" in no_version_undo.content[0].text
        stale_undo = await client.call_tool("undo", {"expected_version": 1}, raise_on_error=False)
        assert stale_undo.is_error and "version_conflict" in stale_undo.content[0].text
        undone = await client.call_tool("undo", {"expected_version": 2})
        assert undone.structured_content["version"] == 1
