import os
from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.db.models  # register tables
from app.config import Settings
from app.db.base import Base
from app.main import create_app

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://planner:planner@localhost:55432/planner_test"
)

TODAY = date(2026, 9, 25)


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessionmaker(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE sessions, plan_versions, chat_messages, mcp_tokens, chat_usage CASCADE")
        )
    yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=TEST_DB_URL, public_origin="http://testserver", llm_provider="fake"
    )


@pytest.fixture
async def app(settings, sessionmaker):
    application = create_app(settings, sessionmaker=sessionmaker, today=lambda: TODAY)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def session_client(client):
    r = await client.post("/api/session")
    assert r.status_code == 200
    return client
