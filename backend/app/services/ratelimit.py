"""Limits of the costly actions, kept in Postgres: new sessions, chat messages and Excel imports
per client address (rate_counters, fixed windows) and the chat quotas (chat_messages,
chat_usage). Unlike the in-memory limiters (app.services.iplimit) they survive a restart or a
deploy, so a client can't time its bursts to them.

Each check is one `INSERT … ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count` in
the caller's transaction; a refusal raises inside it, so the rollback un-counts the refused
attempt (retrying doesn't extend a block, and a message refused by one limit doesn't use up
another). Fixed windows (the clock hour, the UTC day) are cheaper than sliding ones at the
cost of allowing up to twice the limit across a window boundary.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import repo
from app.services.errors import RateLimited

Window = Literal["hour", "day"]


def window_start(window: Window, now: datetime) -> datetime:
    now = now.astimezone(UTC)
    if window == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def count_hit(
    db: AsyncSession, action: str, client: str, window: Window, now: datetime
) -> int:
    """Count one more `action` by `client` in the current `window`; returns the count so far,
    this one included."""
    key = f"{window}:{action}:{client}"
    return await repo.bump_rate_counter(db, key, window_start(window, now))


async def enforce(
    db: AsyncSession,
    action: str,
    client: str,
    window: Window,
    limit: int,
    message: str,
    *,
    now: datetime | None = None,
) -> int:
    """Count the attempt, or raise RateLimited (the caller's rollback un-counts it)."""
    count = await count_hit(db, action, client, window, now or datetime.now(UTC))
    if count > limit:
        raise RateLimited(message)
    return count


async def check_chat_limits(
    db: AsyncSession,
    session_id: uuid.UUID,
    client: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    """All chat limits for one message, in the transaction that then stores it (under the chat
    advisory lock, so the counts can't go stale before the INSERT)."""
    now = now or datetime.now(UTC)
    per_ip_day, per_ip_hour = settings.chat_limit_per_ip_day, settings.chat_limit_per_ip_hour
    # The day first: a day-blocked address must not also use up its hourly slots.
    sent_today = await enforce(
        db,
        "chat",
        client,
        "day",
        per_ip_day,
        f"Лимит: {per_ip_day} сообщений в сутки с одного адреса. Попробуйте завтра.",
        now=now,
    )
    await enforce(
        db,
        "chat",
        client,
        "hour",
        per_ip_hour,
        f"Лимит: {per_ip_hour} сообщений в час с одного адреса. Попробуйте позже.",
        now=now,
    )
    per_hour = settings.chat_limit_per_hour
    if await repo.count_user_messages_since(db, now - timedelta(hours=1), session_id) >= per_hour:
        raise RateLimited(f"Лимит: {per_hour} сообщений в час. Попробуйте позже.")
    # App-wide quota over the session-independent ledger (see ChatUsageRow), last 24 hours.
    used = await repo.count_chat_usage_since(db, now - timedelta(days=1))
    per_day = settings.chat_limit_per_day
    if used >= per_day:
        raise RateLimited("Дневной лимит демо исчерпан. Попробуйте завтра.")
    # The last chat_daily_reserve messages of the quota are kept for addresses that have
    # written little today: a few busy (or abusive) clients can't take chat away from
    # everyone else before the day is over.
    if (
        used >= per_day - settings.chat_daily_reserve
        and sent_today - 1 >= settings.chat_reserve_per_ip_day
    ):
        raise RateLimited(
            "Дневной лимит демо почти исчерпан: оставшиеся сообщения приберегаются для тех, "
            "кто сегодня ещё почти не писал. Попробуйте завтра."
        )
