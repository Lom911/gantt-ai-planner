"""Read-path performance (load test on the production VPS: ~20 req/s per core, most of it
per-request DB round trips plus re-parsing and re-scheduling the current snapshot)."""

import asyncio
from datetime import timedelta

from sqlalchemy import text

from app.db import repo
from app.domain.operations import UpdateTask


async def _new_session(service):
    token, sid = await service.create_session()
    return token, sid


async def test_repeated_reads_do_not_reschedule(app, monkeypatch):
    service = app.state.service
    _, sid = await _new_session(service)
    calls = 0
    import app.services.plan_service as ps

    real = ps.schedule

    def counting(plan):
        nonlocal calls
        calls += 1
        return real(plan)

    monkeypatch.setattr(ps, "schedule", counting)
    first = await service.get_state(sid)
    for _ in range(5):
        again = await service.get_state(sid)
        assert again.scheduled == first.scheduled and again.version == first.version
    assert calls <= 1


async def test_cache_is_keyed_by_stored_row_not_version_number(app):
    # After undo, the next edit reuses the version NUMBER of the discarded redo branch; a cache
    # keyed by (session, number) would then serve the discarded plan.
    service = app.state.service
    _, sid = await _new_session(service)
    await service.apply(sid, [UpdateTask(op="update_task", id=1, duration=7)], source="user")
    v2 = await service.get_state(sid)
    assert v2.plan.tasks[0].duration == 7
    await service.undo(sid)
    await service.apply(sid, [UpdateTask(op="update_task", id=1, duration=9)], source="user")
    reused = await service.get_state(sid)
    assert reused.version == v2.version  # same number…
    assert reused.plan.tasks[0].duration == 9  # …different plan


async def test_editing_does_not_mutate_the_cached_plan(app):
    service = app.state.service
    _, sid = await _new_session(service)
    before = await service.get_state(sid)
    duration = before.plan.tasks[0].duration
    await service.apply(
        sid, [UpdateTask(op="update_task", id=1, duration=duration + 3)], source="user"
    )
    assert before.plan.tasks[0].duration == duration


async def test_last_seen_is_not_rewritten_on_every_request(app):
    service = app.state.service
    token, _ = await _new_session(service)
    async with service.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE sessions SET last_seen_at = now() - interval '1 minute'"))
    await service.resolve_session(token)
    async with service.sessionmaker() as db:
        age = (await db.execute(text("SELECT now() - last_seen_at FROM sessions"))).scalar_one()
    assert age >= timedelta(seconds=50)  # fresh enough: left alone
    async with service.sessionmaker() as db, db.begin():
        await db.execute(text("UPDATE sessions SET last_seen_at = now() - interval '2 hours'"))
    await service.resolve_session(token)
    async with service.sessionmaker() as db:
        age = (await db.execute(text("SELECT now() - last_seen_at FROM sessions"))).scalar_one()
    assert age < timedelta(seconds=10)  # stale: touched


async def test_concurrent_cold_reads_keep_the_byte_counter_exact(app, monkeypatch):
    # Every cold reader misses the cache before any of them has stored the plan, then each one
    # stores it: replacing the entry must not count its bytes again, or the counter drifts up
    # and the cache evicts far below its real budget.
    service = app.state.service
    service._plan_cache.clear()
    service._plan_cache_bytes = 0
    _, sid = await _new_session(service)
    real_snapshot = repo.get_version_snapshot

    async def slow_snapshot(db, version_id):
        await asyncio.sleep(0.05)  # all readers are past the cache lookup by now
        return await real_snapshot(db, version_id)

    monkeypatch.setattr(repo, "get_version_snapshot", slow_snapshot)
    # Pooled connections ready, so no reader is held back by opening one.
    dbs = [service.sessionmaker() for _ in range(8)]
    await asyncio.gather(*(db.execute(text("SELECT 1")) for db in dbs))
    for db in dbs:
        await db.close()
    states = await asyncio.gather(*(service.get_state(sid) for _ in range(8)))
    assert len({s.version for s in states}) == 1
    assert len(service._plan_cache) == 1
    assert service._plan_cache_bytes == sum(e[2] for e in service._plan_cache.values())
