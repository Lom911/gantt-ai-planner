"""End-to-end check that the access log carries the session id prefix (spec: privacy-safe
request logging) and never leaks the session cookie token or a query string.

Exercises the real stack (session + plan routes) rather than the synthetic ASGI scopes used
in tests/unit/test_logging.py, so this also proves AccessLogMiddleware picks up the
session_id that routes_plan's require_session dependency stashes on request.state.
"""

import logging


async def test_plan_request_logs_sid_prefix_and_never_the_cookie_or_query_string(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.access"):
        session_resp = await client.post("/api/session")
        assert session_resp.status_code == 200
        cookie_token = session_resp.cookies.get("sid")
        assert cookie_token

        plan_resp = await client.get("/api/plan", params={"secret": "x"})
        assert plan_resp.status_code == 200

    lines = [r.message for r in caplog.records if r.name == "app.access"]
    plan_lines = [line for line in lines if line.startswith("GET /api/plan 200")]
    assert len(plan_lines) == 1
    line = plan_lines[0]

    assert "/api/plan?secret=x" not in line
    assert "?secret=x" not in line
    assert "secret=x" not in line

    parts = line.split()
    sid_field = next(p for p in parts if p.startswith("sid="))
    sid_value = sid_field.removeprefix("sid=")
    assert sid_value != "-"
    assert len(sid_value) == 8
    assert all(c in "0123456789abcdef" for c in sid_value)

    for logged_line in lines:
        assert cookie_token not in logged_line


async def test_requests_refused_by_the_mcp_gate_are_logged_too(client, app, caplog):
    app.state.settings.mcp_limit_per_ip_hour = 0
    with caplog.at_level(logging.INFO, logger="app.access"):
        r = await client.post("/mcp", json={})
    assert r.status_code == 429
    lines = [rec.message for rec in caplog.records if rec.name == "app.access"]
    assert any(line.startswith("POST /mcp 429") for line in lines)
