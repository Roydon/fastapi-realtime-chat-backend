"""Database schema."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.ids import uuid7

USER_ID_LENGTH = 64


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MessageStatus(enum.StrEnum):
    sent = "sent"
    delivered = "delivered"
    read = "read"


class Conversation(Base):
    """A 1:1 conversation. The pair is stored sorted (user_a < user_b) so it is unique."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_a: Mapped[str] = mapped_column(String(USER_ID_LENGTH))
    user_b: Mapped[str] = mapped_column(String(USER_ID_LENGTH))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_a", "user_b", name="uq_conversations_pair"),
        CheckConstraint("user_a < user_b", name="ck_conversations_sorted_pair"),
        Index("ix_conversations_user_b", "user_b"),
    )

    def peer_of(self, user_id: str) -> str:
        return self.user_b if user_id == self.user_a else self.user_a


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    sender_id: Mapped[str] = mapped_column(String(USER_ID_LENGTH))
    recipient_id: Mapped[str] = mapped_column(String(USER_ID_LENGTH))
    client_msg_id: Mapped[str] = mapped_column(String(64))
    body: Mapped[str | None] = mapped_column(Text)
    attachment_key: Mapped[str | None] = mapped_column(String(512))
    attachment_mime: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status"), default=MessageStatus.sent
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Idempotent sends: a client retry with the same client_msg_id never duplicates.
        UniqueConstraint("sender_id", "client_msg_id", name="uq_messages_sender_client_msg"),
        CheckConstraint(
            "body IS NOT NULL OR attachment_key IS NOT NULL", name="ck_messages_has_content"
        ),
        Index(
            "ix_messages_conversation_created",
            "conversation_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_messages_unread",
            "recipient_id",
            "conversation_id",
            postgresql_where=text("status <> 'read'"),
        ),
    )


class Outbox(Base):
    """Transactional outbox: events are written in the same transaction as the state change
    and published to Redis afterwards, so the database and live pushes never diverge."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_outbox_pending", "id", postgresql_where=text("published_at IS NULL")),
    )
