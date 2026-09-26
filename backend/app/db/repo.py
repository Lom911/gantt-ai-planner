import uuid
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessageRow, ChatUsageRow, McpTokenRow, PlanVersionRow, SessionRow


class VersionMeta(NamedTuple):
    version_no: int
    turn_id: uuid.UUID | None


class VersionDiff(NamedTuple):
    version_no: int
    source: str
    created_at: datetime
    summary: str
    diff: list[dict[str, Any]]


async def create_session(db: AsyncSession, token_hash: bytes) -> SessionRow:
    row = SessionRow(token_hash=token_hash, current_version=0)
    db.add(row)
    await db.flush()
    return row


async def get_session_by_token_hash(db: AsyncSession, token_hash: bytes) -> SessionRow | None:
    return await db.scalar(select(SessionRow).where(SessionRow.token_hash == token_hash))


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> SessionRow | None:
    return await db.get(SessionRow, session_id)


async def touch_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(SessionRow).where(SessionRow.id == session_id).values(last_seen_at=func.now())
    )


async def delete_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(delete(SessionRow).where(SessionRow.id == session_id))


async def add_version(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    version_no: int,
    snapshot: dict[str, Any],
    source: str,
    turn_id: uuid.UUID | None,
    summary: str,
    diff: list[dict[str, Any]],
) -> None:
    db.add(
        PlanVersionRow(
            session_id=session_id,
            version_no=version_no,
            snapshot=snapshot,
            source=source,
            turn_id=turn_id,
            summary=summary,
            diff=diff,
        )
    )
    await db.flush()


async def get_version(
    db: AsyncSession, session_id: uuid.UUID, version_no: int
) -> PlanVersionRow | None:
    return await db.scalar(
        select(PlanVersionRow).where(
            PlanVersionRow.session_id == session_id, PlanVersionRow.version_no == version_no
        )
    )


async def list_version_meta(db: AsyncSession, session_id: uuid.UUID) -> list[VersionMeta]:
    rows = await db.execute(
        select(PlanVersionRow.version_no, PlanVersionRow.turn_id)
        .where(PlanVersionRow.session_id == session_id)
        .order_by(PlanVersionRow.version_no)
    )
    return [VersionMeta(v, t) for v, t in rows.all()]


async def list_versions_upto(
    db: AsyncSession, session_id: uuid.UUID, version_no: int
) -> list[VersionDiff]:
    """Versions <= `version_no`, newest first — the raw material for task history (spec §6:
    "История задачи ... вычисляется из diff сохранённых версий")."""
    rows = await db.execute(
        select(
            PlanVersionRow.version_no,
            PlanVersionRow.source,
            PlanVersionRow.created_at,
            PlanVersionRow.summary,
            PlanVersionRow.diff,
        )
        .where(PlanVersionRow.session_id == session_id, PlanVersionRow.version_no <= version_no)
        .order_by(PlanVersionRow.version_no.desc())
    )
    return [VersionDiff(*row) for row in rows.all()]


async def delete_versions_after(db: AsyncSession, session_id: uuid.UUID, version_no: int) -> None:
    await db.execute(
        delete(PlanVersionRow).where(
            PlanVersionRow.session_id == session_id, PlanVersionRow.version_no > version_no
        )
    )


async def prune_versions(db: AsyncSession, session_id: uuid.UUID, keep: int) -> None:
    keep_from = await db.scalar(
        select(PlanVersionRow.version_no)
        .where(PlanVersionRow.session_id == session_id)
        .order_by(PlanVersionRow.version_no.desc())
        .offset(keep - 1)
        .limit(1)
    )
    if keep_from is not None:
        await db.execute(
            delete(PlanVersionRow).where(
                PlanVersionRow.session_id == session_id, PlanVersionRow.version_no < keep_from
            )
        )


async def add_chat_message(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: str,
    content: str,
    turn_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> ChatMessageRow:
    row = ChatMessageRow(
        session_id=session_id, role=role, content=content, turn_id=turn_id, meta=meta or {}
    )
    db.add(row)
    await db.flush()
    return row


async def delete_turn_messages(db: AsyncSession, session_id: uuid.UUID, turn_id: uuid.UUID) -> None:
    await db.execute(
        delete(ChatMessageRow).where(
            ChatMessageRow.session_id == session_id, ChatMessageRow.turn_id == turn_id
        )
    )


async def recent_chat_messages(
    db: AsyncSession, session_id: uuid.UUID, limit: int
) -> list[ChatMessageRow]:
    rows = await db.scalars(
        select(ChatMessageRow)
        .where(ChatMessageRow.session_id == session_id)
        .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc())
        .limit(limit)
    )
    return list(reversed(rows.all()))


async def add_chat_usage(db: AsyncSession) -> None:
    db.add(ChatUsageRow())
    await db.flush()


async def count_chat_usage_since(db: AsyncSession, since: datetime) -> int:
    stmt = select(func.count()).select_from(ChatUsageRow).where(ChatUsageRow.created_at >= since)
    return int(await db.scalar(stmt) or 0)


async def prune_chat_usage(db: AsyncSession, older_than: datetime) -> None:
    await db.execute(delete(ChatUsageRow).where(ChatUsageRow.created_at < older_than))


async def count_user_messages_since(
    db: AsyncSession, since: datetime, session_id: uuid.UUID | None = None
) -> int:
    stmt = (
        select(func.count())
        .select_from(ChatMessageRow)
        .where(ChatMessageRow.role == "user", ChatMessageRow.created_at >= since)
    )
    if session_id is not None:
        stmt = stmt.where(ChatMessageRow.session_id == session_id)
    return int(await db.scalar(stmt) or 0)


async def create_mcp_token(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    token_hash: bytes,
    prefix: str,
    expires_at: datetime,
) -> McpTokenRow:
    row = McpTokenRow(
        session_id=session_id, token_hash=token_hash, prefix=prefix, expires_at=expires_at
    )
    db.add(row)
    await db.flush()
    return row


async def get_active_mcp_token(
    db: AsyncSession, token_hash: bytes, now: datetime
) -> McpTokenRow | None:
    return await db.scalar(
        select(McpTokenRow).where(
            McpTokenRow.token_hash == token_hash,
            McpTokenRow.revoked_at.is_(None),
            McpTokenRow.expires_at > now,
        )
    )


async def revoke_mcp_tokens(db: AsyncSession, session_id: uuid.UUID, now: datetime) -> None:
    await db.execute(
        update(McpTokenRow)
        .where(McpTokenRow.session_id == session_id, McpTokenRow.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def touch_mcp_token(db: AsyncSession, token_id: uuid.UUID, now: datetime) -> None:
    await db.execute(update(McpTokenRow).where(McpTokenRow.id == token_id).values(last_used_at=now))


async def delete_expired_session_ids(db: AsyncSession, older_than: datetime) -> list[uuid.UUID]:
    result = await db.execute(
        delete(SessionRow).where(SessionRow.last_seen_at < older_than).returning(SessionRow.id)
    )
    return [row[0] for row in result.all()]
