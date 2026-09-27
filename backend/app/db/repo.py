import uuid
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ChatMessageRow,
    ChatUsageRow,
    McpTokenRow,
    PlanConfirmationRow,
    PlanVersionRow,
    RateCounterRow,
    SessionRow,
)


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


async def lock_session_plan(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Serializes the transactions that change one session's plan (apply, undo/redo, import,
    reset) in the database itself, not just in the process (SessionLocks): with a second
    worker, two of them could both read version N and write N+1 — a unique violation or a
    lost update. Transaction-scoped: released on commit or rollback. Must be the first
    statement of the transaction, so everything it reads afterwards is already the other
    writer's committed result."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:sid, 0))"), {"sid": str(session_id)}
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


async def get_current_version_ref(
    db: AsyncSession, session_id: uuid.UUID
) -> tuple[int, int] | None:
    """(version_no, plan_versions.id) of the session's current version, in one round trip.
    The row id — never reused, unlike version_no after an undo + new edit — keys the
    in-memory plan cache in PlanService."""
    row = (
        await db.execute(
            select(PlanVersionRow.version_no, PlanVersionRow.id)
            .join(SessionRow, SessionRow.id == PlanVersionRow.session_id)
            .where(
                SessionRow.id == session_id,
                PlanVersionRow.version_no == SessionRow.current_version,
            )
        )
    ).first()
    return (row[0], row[1]) if row else None


async def get_version_snapshot(db: AsyncSession, version_id: int) -> dict[str, Any] | None:
    return await db.scalar(select(PlanVersionRow.snapshot).where(PlanVersionRow.id == version_id))


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


async def add_chat_usage(db: AsyncSession) -> int:
    row = ChatUsageRow()
    db.add(row)
    await db.flush()
    return row.id


async def count_chat_usage_since(db: AsyncSession, since: datetime) -> int:
    stmt = select(func.count()).select_from(ChatUsageRow).where(ChatUsageRow.created_at >= since)
    return int(await db.scalar(stmt) or 0)


async def prune_chat_usage(db: AsyncSession, older_than: datetime) -> None:
    await db.execute(delete(ChatUsageRow).where(ChatUsageRow.created_at < older_than))


async def bump_rate_counter(db: AsyncSession, key: str, window_start: datetime) -> int:
    """+1 on the (key, window) counter, created at 1; returns the new count. The row lock the
    upsert takes serializes concurrent hits on one key until the transaction ends."""
    stmt = (
        insert(RateCounterRow)
        .values(key=key, window_start=window_start, count=1)
        .on_conflict_do_update(
            index_elements=[RateCounterRow.key, RateCounterRow.window_start],
            set_={"count": RateCounterRow.count + 1},
        )
        .returning(RateCounterRow.count)
    )
    return int((await db.execute(stmt)).scalar_one())


async def prune_rate_counters(
    db: AsyncSession, *, hour_windows_before: datetime, day_windows_before: datetime
) -> None:
    await db.execute(
        delete(RateCounterRow).where(
            or_(
                RateCounterRow.window_start < day_windows_before,
                RateCounterRow.key.startswith("hour:")
                & (RateCounterRow.window_start < hour_windows_before),
            )
        )
    )


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
) -> tuple[McpTokenRow, datetime] | None:
    """The live token with this hash and its session's last_seen_at, in one round trip."""
    row = (
        await db.execute(
            select(McpTokenRow, SessionRow.last_seen_at)
            .join(SessionRow, SessionRow.id == McpTokenRow.session_id)
            .where(
                McpTokenRow.token_hash == token_hash,
                McpTokenRow.revoked_at.is_(None),
                McpTokenRow.expires_at > now,
            )
        )
    ).first()
    return (row[0], row[1]) if row else None


async def latest_mcp_token(db: AsyncSession, session_id: uuid.UUID) -> McpTokenRow | None:
    """The session's newest non-revoked token, expired or not (issuing one revokes the rest)."""
    return await db.scalar(
        select(McpTokenRow)
        .where(McpTokenRow.session_id == session_id, McpTokenRow.revoked_at.is_(None))
        .order_by(McpTokenRow.created_at.desc())
        .limit(1)
    )


async def lock_session_mcp_tokens(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Serializes issuing and revoking one session's MCP token (transaction-scoped, like
    lock_session_plan, and on a key of its own so it never waits for a plan edit). Without it
    two issues could both revoke before either inserted (two live tokens, or a unique violation
    now that the index forbids that), and a revoke racing an issue skipped the token being
    inserted: its UPDATE waited for the old token's row lock and never saw the new row."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('mcp-token:' || :sid, 0))"),
        {"sid": str(session_id)},
    )


async def revoke_mcp_tokens(db: AsyncSession, session_id: uuid.UUID, now: datetime) -> None:
    await db.execute(
        update(McpTokenRow)
        .where(McpTokenRow.session_id == session_id, McpTokenRow.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def touch_mcp_token(db: AsyncSession, token_id: uuid.UUID, now: datetime) -> None:
    await db.execute(update(McpTokenRow).where(McpTokenRow.id == token_id).values(last_used_at=now))


async def get_unresolved_confirmation(
    db: AsyncSession, session_id: uuid.UUID
) -> PlanConfirmationRow | None:
    """The session's unresolved confirmation (at most one: partial unique index), expired or
    not — callers decide what an expired one means."""
    return await db.scalar(
        select(PlanConfirmationRow).where(
            PlanConfirmationRow.session_id == session_id, PlanConfirmationRow.resolved.is_(None)
        )
    )


async def add_confirmation(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    digest: str,
    origin: str,
    summary: str,
    task_count: int,
    expires_at: datetime,
) -> PlanConfirmationRow:
    row = PlanConfirmationRow(
        session_id=session_id,
        digest=digest,
        origin=origin,
        summary=summary,
        task_count=task_count,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def resolve_confirmation(db: AsyncSession, confirmation_id: uuid.UUID, resolved: str) -> None:
    # A statement, not an ORM attribute change: it must reach the database before a following
    # INSERT of the next pending row, or the partial unique index would refuse that one.
    await db.execute(
        update(PlanConfirmationRow)
        .where(PlanConfirmationRow.id == confirmation_id)
        .values(resolved=resolved)
    )


async def approve_confirmation(db: AsyncSession, confirmation_id: uuid.UUID, now: datetime) -> None:
    await db.execute(
        update(PlanConfirmationRow)
        .where(PlanConfirmationRow.id == confirmation_id)
        .values(approved_at=now)
    )


async def resolve_unresolved_confirmations(
    db: AsyncSession, session_id: uuid.UUID, *, origin: str, resolved: str
) -> list[uuid.UUID]:
    result = await db.execute(
        update(PlanConfirmationRow)
        .where(
            PlanConfirmationRow.session_id == session_id,
            PlanConfirmationRow.origin == origin,
            PlanConfirmationRow.resolved.is_(None),
        )
        .values(resolved=resolved)
        .returning(PlanConfirmationRow.id)
    )
    return [row[0] for row in result.all()]


async def prune_confirmations(db: AsyncSession, expired_before: datetime) -> None:
    await db.execute(
        delete(PlanConfirmationRow).where(PlanConfirmationRow.expires_at < expired_before)
    )


async def delete_expired_session_ids(db: AsyncSession, older_than: datetime) -> list[uuid.UUID]:
    result = await db.execute(
        delete(SessionRow).where(SessionRow.last_seen_at < older_than).returning(SessionRow.id)
    )
    return [row[0] for row in result.all()]
