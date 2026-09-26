import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repo
from app.services.errors import RateLimited


async def check_chat_limits(
    db: AsyncSession, session_id: uuid.UUID, *, per_hour: int, per_day: int
) -> None:
    now = datetime.now(UTC)
    if await repo.count_user_messages_since(db, now - timedelta(hours=1), session_id) >= per_hour:
        raise RateLimited(f"Лимит: {per_hour} сообщений в час. Попробуйте позже.")
    # App-wide quota over the session-independent ledger (see ChatUsageRow).
    if await repo.count_chat_usage_since(db, now - timedelta(days=1)) >= per_day:
        raise RateLimited("Дневной лимит демо исчерпан. Попробуйте завтра.")
