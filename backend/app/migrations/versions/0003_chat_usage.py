"""chat_usage: session-independent ledger for the daily chat quota

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26 21:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_usage_created", "chat_usage", ["created_at"], unique=False)
    # Carry over today's usage so the quota doesn't reset on deploy.
    op.execute(
        "INSERT INTO chat_usage (created_at) SELECT created_at FROM chat_messages "
        "WHERE role = 'user' AND created_at >= now() - interval '1 day'"
    )


def downgrade() -> None:
    op.drop_index("ix_chat_usage_created", table_name="chat_usage")
    op.drop_table("chat_usage")
