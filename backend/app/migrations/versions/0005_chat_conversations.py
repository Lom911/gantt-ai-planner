"""Chat conversations: sessions.chat_conversation_id, chat_messages.conversation_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29 12:00:00

A page load and «Очистить чат» start a new conversation; the earlier ones are kept for
«История». Each session's existing messages become its current conversation.

Backward compatible with the previous app image (a failed deploy rolls the image back without
downgrading): sessions.chat_conversation_id has a server default, and chat_messages.
conversation_id is nullable, so the old image's inserts still work (it shows every message of
the session as one chat, as before).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A volatile default is evaluated per row: every existing session gets its own id.
    op.add_column(
        "sessions",
        sa.Column(
            "chat_conversation_id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
    )
    op.add_column("chat_messages", sa.Column("conversation_id", sa.UUID(), nullable=True))
    op.execute(
        "UPDATE chat_messages m SET conversation_id = s.chat_conversation_id "
        "FROM sessions s WHERE m.session_id = s.id"
    )
    op.create_index(
        "ix_chat_session_conversation",
        "chat_messages",
        ["session_id", "conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_session_conversation", table_name="chat_messages")
    op.drop_column("chat_messages", "conversation_id")
    op.drop_column("sessions", "chat_conversation_id")
