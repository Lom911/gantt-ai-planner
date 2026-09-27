"""GET /api/ops/status: one authenticated JSON document for external alerting (there is no
metrics backend): traffic and errors of the last 15 minutes, LLM spend, disk, backups."""

from pydantic import SecretStr
from sqlalchemy import text

from app.agent.llm import Completed, LLMToolCall, LLMTurnResult, LLMUsage
from app.agent.loop import Agent
from app.config import Settings
from tests.integration.conftest import TODAY

TOKEN = "ops-" + "x" * 40


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_status_is_hidden_unless_a_token_is_configured(app, client):
    assert app.state.settings.ops_token is None
    r = await client.get("/api/ops/status", headers=_auth())
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    app.state.settings.ops_token = SecretStr("  \n")  # an empty secret file counts as unset
    assert (await client.get("/api/ops/status", headers=_auth())).status_code == 404


async def test_status_needs_the_bearer_token(app, client):
    app.state.settings.ops_token = SecretStr(TOKEN + "\n")  # as read from a secret file
    for headers in ({}, _auth("wrong"), {"Authorization": f"Basic {TOKEN}"}, _auth("")):
        r = await client.get("/api/ops/status", headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert (await client.get("/api/ops/status", headers=_auth())).status_code == 200


async def test_status_document(app, client, tmp_path):
    cfg = app.state.settings
    cfg.ops_token = SecretStr(TOKEN)
    status_file = tmp_path / "backup-status"
    status_file.write_text(
        "timestamp=2026-09-27T03:15:04Z\nstatus=ok\nlast_ok=2026-09-27T03:15:04Z\n"
    )
    cfg.backup_status_file = str(status_file)
    app.state.request_metrics._samples.clear()
    for _ in range(3):
        assert (await client.get("/api/meta")).status_code == 200
    assert (await client.get("/api/nope")).status_code == 404
    for _ in range(2):
        assert (await client.get("/healthz")).status_code == 200  # not counted
    async with app.state.sessionmaker() as db, db.begin():
        await db.execute(
            text(
                "INSERT INTO chat_usage (created_at, tokens) VALUES "
                "(now(), 1200), (now() - interval '2 hours', 300), (now() - interval '2 days', 999)"
            )
        )

    r = await client.get("/api/ops/status", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "window_minutes",
        "requests",
        "errors_5xx",
        "error_rate",
        "p95_ms",
        "tokens_today",
        "chat_messages_today",
        "disk_free_ratio",
        "backup",
    }
    assert body["window_minutes"] == 15
    assert body["requests"] == 4 and body["errors_5xx"] == 0 and body["error_rate"] == 0.0
    assert body["p95_ms"] > 0
    assert body["tokens_today"] == 1500 and body["chat_messages_today"] == 2
    assert 0 < body["disk_free_ratio"] < 1
    assert body["backup"]["status"] == "ok"
    assert body["backup"]["last_ok"] == "2026-09-27T03:15:04Z"
    assert isinstance(body["backup"]["age_hours"], float)
    # The monitor's own polling doesn't count either.
    again = (await client.get("/api/ops/status", headers=_auth())).json()
    assert again["requests"] == 4


async def test_backup_status_file_default_and_unknown(app, client, tmp_path):
    assert Settings().backup_status_file == "/var/lib/gantt-planner/backup-status"
    app.state.settings.ops_token = SecretStr(TOKEN)
    app.state.settings.backup_status_file = str(tmp_path / "missing")
    body = (await client.get("/api/ops/status", headers=_auth())).json()
    assert body["backup"] == {"status": "unknown", "last_ok": None, "age_hours": None}


def test_ops_token_is_read_from_the_secrets_dir(tmp_path):
    (tmp_path / "ops_token").write_text("from-file\n")
    cfg = Settings(_secrets_dir=str(tmp_path))
    assert cfg.ops_token is not None and cfg.ops_token.get_secret_value().strip() == "from-file"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert Settings(_secrets_dir=str(empty)).ops_token is None


class _SpendsTokens:
    """One get_plan call, then an answer; each call billed 1000 + 50 + 7 + 3 tokens."""

    usage = LLMUsage(
        input_tokens=1000,
        output_tokens=50,
        cache_creation_input_tokens=7,
        cache_read_input_tokens=3,
    )

    async def stream(self, *, system, tools, messages, max_tokens=None):
        if isinstance(messages[-1]["content"], list):
            result = LLMTurnResult(
                text="ok", tool_calls=[], stop_reason="end_turn", content=[], usage=self.usage
            )
        else:
            call = LLMToolCall(id="g1", name="get_plan", input={})
            result = LLMTurnResult(
                text="",
                tool_calls=[call],
                stop_reason="tool_use",
                content=[{"type": "tool_use", "id": "g1", "name": "get_plan", "input": {}}],
                usage=self.usage,
            )
        yield Completed(result)


async def test_chat_turn_stores_its_billed_tokens(app, session_client):
    app.state.agent = Agent(
        _SpendsTokens(), app.state.tool_client, app.state.service, today=lambda: TODAY
    )
    async with session_client.stream("POST", "/api/chat", json={"message": "покажи план"}) as r:
        assert r.status_code == 200
        await r.aread()
    async with app.state.sessionmaker() as db:
        tokens = (await db.execute(text("SELECT tokens FROM chat_usage"))).scalars().all()
    assert tokens == [2 * 1060]


async def test_tokens_are_stored_when_the_turn_fails_too(app):
    from app.agent.llm import LLMError

    class SpendsThenFails(_SpendsTokens):
        async def stream(self, *, system, tools, messages, max_tokens=None):
            if isinstance(messages[-1]["content"], list):
                raise LLMError("llm_unavailable", "LLM временно недоступна")
            async for event in super().stream(system=system, tools=tools, messages=messages):
                yield event

    from app.db import repo

    _, sid = await app.state.service.create_session()
    async with app.state.sessionmaker() as db, db.begin():
        usage_id = await repo.add_chat_usage(db)
    agent = Agent(SpendsThenFails(), app.state.tool_client, app.state.service, today=lambda: TODAY)
    events = [ev async for ev in agent.run_turn(sid, "покажи план", usage_id=usage_id)]
    assert events[-1]["code"] == "llm_unavailable"
    async with app.state.sessionmaker() as db:
        tokens = (await db.execute(text("SELECT tokens FROM chat_usage"))).scalar_one()
    assert tokens == 1060


async def test_tokens_are_stored_when_the_client_goes_away(app):
    # On disconnect sse-starlette cancels the response's task group: the turn is torn down
    # inside a cancelled scope, where an unshielded database write would be cancelled too.
    import asyncio
    from contextlib import aclosing

    import anyio

    from app.db import repo

    tool_done = asyncio.Event()

    class SpendsThenHangs(_SpendsTokens):
        async def stream(self, *, system, tools, messages, max_tokens=None):
            if isinstance(messages[-1]["content"], list):
                await asyncio.Event().wait()  # the second call never returns
            async for event in super().stream(system=system, tools=tools, messages=messages):
                yield event

    _, sid = await app.state.service.create_session()
    async with app.state.sessionmaker() as db, db.begin():
        usage_id = await repo.add_chat_usage(db)
    agent = Agent(SpendsThenHangs(), app.state.tool_client, app.state.service, today=lambda: TODAY)

    async def consume() -> None:
        async with aclosing(agent.run_turn(sid, "покажи план", usage_id=usage_id)) as turn:
            async for event in turn:
                if event["type"] == "tool_finished":
                    tool_done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await tool_done.wait()
        tg.cancel_scope.cancel()
    async with app.state.sessionmaker() as db:
        tokens = (await db.execute(text("SELECT tokens FROM chat_usage"))).scalar_one()
    assert tokens == 1060
    assert not app.state.service.locks.is_busy(sid)
