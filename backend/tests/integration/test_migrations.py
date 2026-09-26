"""`alembic upgrade head` on a scratch database, the way the deploy's migrate service runs it
(a subprocess with DATABASE_URL), since the test schema itself comes from create_all."""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import TEST_DB_URL

BACKEND = Path(__file__).resolve().parents[2]


async def _upgrade_head(url: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
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
        raise AssertionError(f"alembic upgrade still running after {timeout} s") from None
    assert proc.returncode is not None
    return proc.returncode, out.decode(errors="replace")


async def test_upgrade_runs_with_timeouts_and_commits():
    head = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head()
    name = f"planner_mig_{uuid.uuid4().hex[:8]}"
    url = make_url(TEST_DB_URL).set(database=name).render_as_string(hide_password=False)
    admin = create_async_engine(
        make_url(TEST_DB_URL).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_async_engine(url)
    try:
        code, out = await _upgrade_head(url, 60)
        assert code == 0, out
        # planner_owner has no role-level timeouts: without these a migration queued behind a
        # lock the running app holds would wait forever (values as the server reports them).
        assert "lock_timeout=10s statement_timeout=5min" in out, out
        async with scratch.connect() as conn:  # committed, not rolled back with the connection
            version = await conn.execute(text("SELECT version_num FROM alembic_version"))
            assert version.scalar_one() == head
    finally:
        await scratch.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
