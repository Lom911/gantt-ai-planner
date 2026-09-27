import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = _created()


class PlanVersionRow(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("session_id", "version_no"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()


class ChatUsageRow(Base):
    """One row per accepted chat message, for the app-wide daily quota. Deliberately not tied
    to a session (no FK): chat_messages rows are cascade-deleted with their session, so a
    quota counted over them reset with «new session → chat → delete session». Rows older
    than two days are pruned by the hourly cleanup."""

    __tablename__ = "chat_usage"
    __table_args__ = (Index("ix_chat_usage_created", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _created()
    # Billed LLM tokens of the turn this message started, written when the turn ends (for the
    # ops status endpoint's tokens_today).
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_session_created", "session_id", "created_at"),
        Index("ix_chat_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class McpTokenRow(Base):
    __tablename__ = "mcp_tokens"
    __table_args__ = (
        Index("ix_mcp_tokens_session_id", "session_id"),
        # One live (non-revoked) token per session: issuing one revokes the rest, and the
        # database refuses a second one even if two issues ever raced past the advisory lock.
        Index(
            "uq_mcp_tokens_live_session",
            "session_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class PlanConfirmationRow(Base):
    """A mass deletion waiting for the user's confirmation (see PlanService.apply): the exact
    batch is pinned by `digest`, so a confirmation never carries over to a different batch.
    At most one unresolved row per session; a new one supersedes it."""

    __tablename__ = "plan_confirmations"
    __table_args__ = (
        Index("ix_plan_confirmations_session_id", "session_id"),
        Index(
            "uq_plan_confirmations_pending",
            "session_id",
            unique=True,
            postgresql_where=text("resolved IS NULL"),
        ),
        CheckConstraint("origin IN ('agent', 'mcp')", name="ck_plan_confirmations_origin"),
        CheckConstraint(
            "resolved IN ('consumed', 'rejected', 'expired', 'superseded')",
            name="ck_plan_confirmations_resolved",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)  # agent | mcp
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # consumed | rejected | expired | superseded; NULL while pending.
    resolved: Mapped[str | None] = mapped_column(Text, nullable=True)


class RateCounterRow(Base):
    """Fixed-window per-client counters for the costly actions (new sessions, chat messages,
    Excel imports): unlike the in-memory limiters they survive a restart or redeploy. Rows of
    past windows are pruned by the hourly cleanup."""

    __tablename__ = "rate_counters"
    __table_args__ = (Index("ix_rate_counters_window_start", "window_start"),)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
