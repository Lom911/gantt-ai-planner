"""`alembic upgrade head` on a scratch database, the way the deploy's migrate service runs it
(a subprocess with DATABASE_URL), since the test schema itself comes from create_all."""

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import make_url, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tests.integration.conftest import TEST_DB_URL

BACKEND = Path(__file__).resolve().parents[2]


async def _alembic(url: str, *args: str, timeout: float = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *args,
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise AssertionError(f"alembic {' '.join(args)} still running after {timeout} s") from None
    assert proc.returncode is not None
    return proc.returncode, out.decode(errors="replace")


@asynccontextmanager
async def _scratch_db() -> AsyncIterator[tuple[str, AsyncEngine]]:
    name = f"planner_mig_{uuid.uuid4().hex[:8]}"
    url = make_url(TEST_DB_URL).set(database=name).render_as_string(hide_password=False)
    admin = create_async_engine(
        make_url(TEST_DB_URL).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_async_engine(url)
    try:
        yield url, scratch
    finally:
        await scratch.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_upgrade_runs_with_timeouts_and_commits():
    head = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head()
    async with _scratch_db() as (url, scratch):
        code, out = await _alembic(url, "upgrade", "head")
        assert code == 0, out
        # planner_owner has no role-level timeouts: without these a migration queued behind a
        # lock the running app holds would wait forever (values as the server reports them).
        assert "lock_timeout=10s statement_timeout=5min" in out, out
        async with scratch.connect() as conn:  # committed, not rolled back with the connection
            version = await conn.execute(text("SELECT version_num FROM alembic_version"))
            assert version.scalar_one() == head


async def _one(conn: AsyncConnection, sql: str, **params: Any) -> Any:
    return (await conn.execute(text(sql), params)).scalar_one()


_TOKEN = (
    "INSERT INTO mcp_tokens (id, session_id, token_hash, prefix, expires_at, revoked_at,"
    " created_at) VALUES (gen_random_uuid(), :sid, :h, :prefix, now() + interval '5 days',"
    " CASE WHEN :revoked THEN now() - interval '3 days' END, now() - CAST(:age AS interval))"
)
_CONFIRMATION = (
    "INSERT INTO plan_confirmations (id, session_id, digest, origin, summary, task_count,"
    " expires_at) VALUES (gen_random_uuid(), :sid, 'd', 'mcp', 's', 6,"
    " now() + interval '10 minutes')"
)


async def _seed_0003(scratch: AsyncEngine, s1: uuid.UUID, s2: uuid.UUID) -> None:
    async with scratch.begin() as conn:
        for sid in (s1, s2):
            await conn.execute(
                text("INSERT INTO sessions (id, token_hash, current_version) VALUES (:id, :h, 1)"),
                {"id": sid, "h": uuid.uuid4().bytes * 2},
            )
        # Session 1 has two live tokens (what the issue/issue race could leave behind) and a
        # revoked one; session 2 has a single live token.
        tokens = [
            (s1, "old", timedelta(days=2), False),
            (s1, "new", timedelta(days=1), False),
            (s1, "gone", timedelta(days=3), True),
            (s2, "other", timedelta(hours=1), False),
        ]
        for sid, prefix, age, revoked in tokens:
            await conn.execute(
                text(_TOKEN),
                {
                    "sid": sid,
                    "h": uuid.uuid4().bytes * 2,
                    "prefix": prefix,
                    "age": age,
                    "revoked": revoked,
                },
            )
        await conn.execute(text("INSERT INTO chat_usage (created_at) VALUES (now()), (now())"))


async def test_0004_upgrades_and_downgrades_cleanly_on_existing_data():
    async with _scratch_db() as (url, scratch):
        code, out = await _alembic(url, "upgrade", "0003")
        assert code == 0, out
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        await _seed_0003(scratch, s1, s2)

        code, out = await _alembic(url, "upgrade", "head")
        assert code == 0, out
        async with scratch.connect() as conn:
            live = await conn.execute(
                text("SELECT prefix FROM mcp_tokens WHERE revoked_at IS NULL ORDER BY prefix")
            )
            assert live.scalars().all() == ["new", "other"]  # the newest one per session
            assert await _one(conn, "SELECT count(*) FROM chat_usage WHERE tokens = 0") == 2
            upsert = (
                "INSERT INTO rate_counters (key, window_start, count) VALUES ('k', :ws, 1) "
                "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_counters.count + 1 "
                "RETURNING count"
            )
            ws = datetime(2026, 9, 27, 10, tzinfo=UTC)
            assert await _one(conn, upsert, ws=ws) == 1
            assert await _one(conn, upsert, ws=ws) == 2
            await conn.commit()
        # One live token and one unresolved confirmation per session, enforced by the database.
        with pytest.raises(IntegrityError):
            async with scratch.begin() as conn:
                params = {
                    "sid": s1,
                    "h": b"x" * 32,
                    "prefix": "dup",
                    "age": timedelta(0),
                    "revoked": False,
                }
                await conn.execute(text(_TOKEN), params)
        async with scratch.begin() as conn:
            await conn.execute(text(_CONFIRMATION), {"sid": s1})
        with pytest.raises(IntegrityError):
            async with scratch.begin() as conn:
                await conn.execute(text(_CONFIRMATION), {"sid": s1})
        code, out = await _alembic(url, "check")
        assert code == 0, out  # the ORM models describe exactly the migrated schema

        code, out = await _alembic(url, "downgrade", "0003")
        assert code == 0, out
        async with scratch.connect() as conn:
            assert await _one(conn, "SELECT to_regclass('plan_confirmations')") is None
            assert await _one(conn, "SELECT to_regclass('rate_counters')") is None
            tokens_column = await _one(
                conn,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'chat_usage' AND column_name = 'tokens'",
            )
            assert tokens_column == 0
            assert await _one(conn, "SELECT count(*) FROM mcp_tokens") == 4
            assert await _one(conn, "SELECT count(*) FROM chat_usage") == 2
        code, out = await _alembic(url, "upgrade", "head")
        assert code == 0, out
