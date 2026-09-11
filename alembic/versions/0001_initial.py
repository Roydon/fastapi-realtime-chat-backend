"""Initial schema: conversations, messages, outbox.

Revision ID: 0001
Revises:
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

message_status = postgresql.ENUM("sent", "delivered", "read", name="message_status", create_type=False)


def upgrade() -> None:
    message_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_a", sa.String(64), nullable=False),
        sa.Column("user_b", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_a", "user_b", name="uq_conversations_pair"),
        sa.CheckConstraint("user_a < user_b", name="ck_conversations_sorted_pair"),
    )
    op.create_index("ix_conversations_user_b", "conversations", ["user_b"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_id", sa.String(64), nullable=False),
        sa.Column("recipient_id", sa.String(64), nullable=False),
        sa.Column("client_msg_id", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("attachment_key", sa.String(512), nullable=True),
        sa.Column("attachment_mime", sa.String(100), nullable=True),
        sa.Column("status", message_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("sender_id", "client_msg_id", name="uq_messages_sender_client_msg"),
        sa.CheckConstraint("body IS NOT NULL OR attachment_key IS NOT NULL", name="ck_messages_has_content"),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_messages_unread",
        "messages",
        ["recipient_id", "conversation_id"],
        postgresql_where=sa.text("status <> 'read'"),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_pending", "outbox", ["id"], postgresql_where=sa.text("published_at IS NULL"))


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("messages")
    op.drop_table("conversations")
    message_status.drop(op.get_bind(), checkfirst=True)
