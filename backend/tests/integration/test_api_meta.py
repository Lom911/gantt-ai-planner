"""GET /api/meta: no session required, reports whether the agent is running the
real Anthropic LLM or the fake demo mode, so the frontend can show a badge.

The blank-key -> ERROR log case is already covered thoroughly at the unit level
in tests/unit/test_make_llm.py; here we only check what /api/meta reports, since
asserting on logs through create_app()'s configure_logging(force=True) reset is
unrelated plumbing, not what this endpoint is for.
"""

import httpx
from asgi_lifespan import LifespanManager
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import MAX_TASKS
from app.main import create_app
from tests.integration.conftest import TEST_DB_URL, TODAY


async def test_meta_reports_fake_mode_with_no_model_by_default(client):
    r = await client.get("/api/meta")
    assert r.status_code == 200
    assert r.json() == {"llm_mode": "fake", "model": None, "max_tasks": MAX_TASKS}


async def test_meta_reports_the_plan_task_limit(client):
    # The UI disables «Добавить задачу» at this many tasks instead of letting the user fill in
    # a form the server would refuse; it must be the same number the domain enforces.
    r = await client.get("/api/meta")
    assert r.json()["max_tasks"] == MAX_TASKS == 500


async def test_meta_requires_no_session(client):
    # No /api/session call at all — /api/meta must not 401.
    r = await client.get("/api/meta")
    assert r.status_code == 200


async def test_meta_reports_anthropic_mode_and_model_when_key_is_configured(sessionmaker):
    settings = Settings(
        database_url=TEST_DB_URL,
        public_origin="http://testserver",
        llm_provider="anthropic",
        anthropic_api_key=SecretStr("sk-ant-" + "a" * 40),
        llm_model="claude-sonnet-5",
    )
    application = create_app(settings, sessionmaker=sessionmaker, today=lambda: TODAY)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as anthropic_client:
            r = await anthropic_client.get("/api/meta")
    assert r.status_code == 200
    assert r.json() == {
        "llm_mode": "anthropic",
        "model": "claude-sonnet-5",
        "max_tasks": MAX_TASKS,
    }


async def test_meta_reports_fake_mode_when_key_is_blank(sessionmaker):
    settings = Settings(
        database_url=TEST_DB_URL,
        public_origin="http://testserver",
        llm_provider="anthropic",
        anthropic_api_key=SecretStr(""),
    )
    application = create_app(settings, sessionmaker=sessionmaker, today=lambda: TODAY)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as fake_client:
            r = await fake_client.get("/api/meta")
    assert r.status_code == 200
    assert r.json() == {"llm_mode": "fake", "model": None, "max_tasks": MAX_TASKS}
