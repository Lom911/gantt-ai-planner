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
            "apply_operations", {"operations": [{"op": "move_task", "id": 1, "shift_days": 1}]}
        )
    assert not result.is_error, result.content
    assert result.structured_content["version"] == before.version + 1

    event = await asyncio.wait_for(queue.get(), timeout=2)
    assert event["type"] == "plan_changed"
    assert event["source"] == "mcp"
    assert event["version"] == before.version + 1

    after = await app.state.service.get_state(session_id)
    assert after.version == before.version + 1


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


async def test_mcp_token_requires_a_session(client):
    r = await client.post("/api/mcp-token")
    assert r.status_code == 401


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
