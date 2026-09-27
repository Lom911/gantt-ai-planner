"""plan_confirmations, rate_counters, one live MCP token per session, chat_usage.tokens

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27 12:00:00

Backward compatible with the previous app image (a failed deploy rolls the image back without
downgrading): the new tables are unused by it, chat_usage.tokens has a default, and the
partial unique index only turns the old issue/issue race into an error instead of a second
live token.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_confirmations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved", sa.Text(), nullable=True),
        sa.CheckConstraint("origin IN ('agent', 'mcp')", name="ck_plan_confirmations_origin"),
        sa.CheckConstraint(
            "resolved IN ('consumed', 'rejected', 'expired', 'superseded')",
            name="ck_plan_confirmations_resolved",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plan_confirmations_session_id", "plan_confirmations", ["session_id"], unique=False
    )
    op.create_index(
        "uq_plan_confirmations_pending",
        "plan_confirmations",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("resolved IS NULL"),
    )

    # Before the index can exist, sessions that the old issue/issue race left with several live
    # tokens keep only the newest. Writes to mcp_tokens wait until this transaction commits, so
    # the running app can't slip a second live token in between.
    op.execute("LOCK TABLE mcp_tokens IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        """
        UPDATE mcp_tokens t SET revoked_at = now()
        WHERE t.revoked_at IS NULL
          AND t.id <> (
            SELECT t2.id FROM mcp_tokens t2
            WHERE t2.session_id = t.session_id AND t2.revoked_at IS NULL
            ORDER BY t2.created_at DESC, t2.id DESC
            LIMIT 1
          )
        """
    )
    op.create_index(
        "uq_mcp_tokens_live_session",
        "mcp_tokens",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "rate_counters",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start"),
    )
    op.create_index(
        "ix_rate_counters_window_start", "rate_counters", ["window_start"], unique=False
    )

    # A constant default: no table rewrite (PostgreSQL 11+), existing rows read as 0.
    op.add_column(
        "chat_usage", sa.Column("tokens", sa.Integer(), server_default="0", nullable=False)
    )


def downgrade() -> None:
    # Tokens revoked by upgrade() stay revoked: they were duplicates the app never meant to keep.
    op.drop_column("chat_usage", "tokens")
    op.drop_index("ix_rate_counters_window_start", table_name="rate_counters")
    op.drop_table("rate_counters")
    op.drop_index("uq_mcp_tokens_live_session", table_name="mcp_tokens")
    op.drop_index("uq_plan_confirmations_pending", table_name="plan_confirmations")
    op.drop_index("ix_plan_confirmations_session_id", table_name="plan_confirmations")
    op.drop_table("plan_confirmations")
