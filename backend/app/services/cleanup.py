import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import repo
from app.services.events import EventBus
from app.services.locks import SessionLocks


async def purge_expired(
    sessionmaker: async_sessionmaker[AsyncSession], *, ttl_days: int, now: datetime
) -> list[uuid.UUID]:
    """Delete sessions whose last_seen_at is older than ttl_days.

    Cascades (FK ondelete="CASCADE") remove their plan versions, chat messages
    and MCP tokens. Returns the ids of the deleted sessions.
    """
    older_than = now - timedelta(days=ttl_days)
    async with sessionmaker() as db, db.begin():
        # The daily chat quota only looks one day back.
        await repo.prune_chat_usage(db, now - timedelta(days=2))
        return await repo.delete_expired_session_ids(db, older_than)


async def run_cleanup_cycle(
    sessionmaker: async_sessionmaker[AsyncSession],
    locks: SessionLocks,
    bus: EventBus,
    *,
    ttl_days: int,
    now: datetime,
) -> list[uuid.UUID]:
    """Purge expired sessions and forget their in-memory lock/event-bus entries.

    Called by the hourly background task in ``app.main`` (and directly by
    tests) so purged session ids never linger as dead ``SessionLocks``/
    ``EventBus`` entries.
    """
    purged = await purge_expired(sessionmaker, ttl_days=ttl_days, now=now)
    for session_id in purged:
        locks.forget(session_id)
        bus.forget(session_id)
    return purged
