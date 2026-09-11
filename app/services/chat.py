"""Message flow: send, receipts and listing.

Every state change is committed in the same transaction as the outbox rows that announce
it, so a push is emitted if and only if the change is durable.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Uuid, func, literal, or_, select, true, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import Tuple

from app.config import Settings
from app.errors import AppError, NotFoundError
from app.ids import decode_cursor, encode_cursor, uuid7
from app.metrics import MESSAGES_REPLAYED, MESSAGES_SENT
from app.models import Conversation, Message, MessageStatus, Outbox, utcnow
from app.schemas import ConversationOut, MessageOut, MessagePage, ReadResult, SendMessageRequest
from app.storage import AttachmentStorage
from app.text import grapheme_length

_NO_SYNC = {"synchronize_session": False}


def user_topic(user_id: str) -> str:
    return f"user:{user_id}"


def make_event(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": event_type, "data": data}


def _key(created_at: datetime, message_id: uuid.UUID) -> Tuple:
    return tuple_(literal(created_at, DateTime(timezone=True)), literal(message_id, Uuid()))


_MESSAGE_KEY = tuple_(Message.created_at, Message.id)


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage: AttachmentStorage,
        on_commit: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._on_commit = on_commit

    # ------------------------------------------------------------------ helpers

    def to_out(self, msg: Message) -> MessageOut:
        return MessageOut(
            id=msg.id,
            conversation_id=msg.conversation_id,
            sender_id=msg.sender_id,
            recipient_id=msg.recipient_id,
            client_msg_id=msg.client_msg_id,
            body=msg.body,
            attachment_key=msg.attachment_key,
            attachment_mime=msg.attachment_mime,
            attachment_url=(
                self._storage.presign_download(msg.attachment_key) if msg.attachment_key else None
            ),
            status=msg.status,
            created_at=msg.created_at,
            delivered_at=msg.delivered_at,
            read_at=msg.read_at,
            cursor=encode_cursor(msg.created_at, msg.id),
        )

    def enqueue(self, recipients: Sequence[str], event_type: str, data: dict[str, Any]) -> None:
        """Stage one outbox row per recipient in the current transaction."""
        for user_id in dict.fromkeys(recipients):
            self._session.add(
                Outbox(topic=user_topic(user_id), payload=make_event(event_type, data))
            )

    async def _commit(self) -> None:
        await self._session.commit()
        if self._on_commit is not None:
            self._on_commit()

    @staticmethod
    def _cursor(raw: str) -> tuple[datetime, uuid.UUID]:
        try:
            return decode_cursor(raw)
        except ValueError as exc:
            raise AppError(400, "invalid_cursor", "Malformed pagination cursor") from exc

    async def get_conversation(self, user_id: str, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self._session.get(Conversation, conversation_id)
        if conversation is None or user_id not in (conversation.user_a, conversation.user_b):
            raise NotFoundError("Conversation not found")
        return conversation

    async def _get_or_create_conversation(self, user_1: str, user_2: str) -> Conversation:
        user_a, user_b = sorted((user_1, user_2))
        await self._session.execute(
            pg_insert(Conversation)
            .values(id=uuid7(), user_a=user_a, user_b=user_b)
            .on_conflict_do_nothing(constraint="uq_conversations_pair")
        )
        result = await self._session.scalars(
            select(Conversation).where(Conversation.user_a == user_a, Conversation.user_b == user_b)
        )
        return result.one()

    async def _by_client_msg_id(self, sender_id: str, client_msg_id: str) -> Message | None:
        result = await self._session.scalars(
            select(Message).where(
                Message.sender_id == sender_id, Message.client_msg_id == client_msg_id
            )
        )
        return result.one_or_none()

    def _replay(self, existing: Message, req: SendMessageRequest) -> MessageOut:
        same = (
            existing.recipient_id == req.recipient_id
            and existing.body == req.body
            and existing.attachment_key == req.attachment_key
        )
        if not same:
            raise AppError(
                409, "idempotency_conflict", "client_msg_id was already used for another message"
            )
        MESSAGES_REPLAYED.inc()
        return self.to_out(existing)

    async def _check_attachment(self, sender_id: str, key: str) -> str:
        s = self._settings
        if not key.startswith(f"u/{sender_id}/"):
            raise AppError(422, "invalid_attachment", "attachment_key was not issued to you")
        stored = await self._storage.head(key)
        if stored is None:
            raise AppError(422, "attachment_not_found", "Upload the file before sending it")
        if stored.size > s.attachment_max_bytes:
            raise AppError(413, "payload_too_large", "Attachment exceeds the size limit")
        if stored.content_type not in s.attachment_allowed_mime:
            raise AppError(415, "unsupported_media_type", "Attachment type is not allowed")
        return stored.content_type

    # ------------------------------------------------------------------ commands

    async def send_message(
        self, sender_id: str, req: SendMessageRequest
    ) -> tuple[MessageOut, bool]:
        """Persist a message and its push events atomically. Returns (message, created)."""
        s = self._settings
        if req.recipient_id == sender_id:
            raise AppError(422, "invalid_recipient", "You cannot message yourself")
        if req.body is not None and grapheme_length(req.body) > s.message_max_graphemes:
            raise AppError(
                422, "body_too_long", f"Message body exceeds {s.message_max_graphemes} characters"
            )

        existing = await self._by_client_msg_id(sender_id, req.client_msg_id)
        if existing is not None:
            return self._replay(existing, req), False

        mime = None
        if req.attachment_key is not None:
            mime = await self._check_attachment(sender_id, req.attachment_key)

        conversation = await self._get_or_create_conversation(sender_id, req.recipient_id)
        insert = (
            pg_insert(Message)
            .values(
                id=uuid7(),
                conversation_id=conversation.id,
                sender_id=sender_id,
                recipient_id=req.recipient_id,
                client_msg_id=req.client_msg_id,
                body=req.body,
                attachment_key=req.attachment_key,
                attachment_mime=mime,
                status=MessageStatus.sent,
                created_at=utcnow(),
            )
            .on_conflict_do_nothing(constraint="uq_messages_sender_client_msg")
            .returning(Message)
        )
        message = (await self._session.scalars(insert)).one_or_none()
        if message is None:
            # A concurrent retry with the same client_msg_id committed first.
            await self._session.rollback()
            existing = await self._by_client_msg_id(sender_id, req.client_msg_id)
            if existing is None:  # pragma: no cover - requires a delete racing the retry
                raise AppError(409, "conflict", "Concurrent send, retry")
            return self._replay(existing, req), False

        out = self.to_out(message)
        self.enqueue([req.recipient_id, sender_id], "message.new", out.model_dump(mode="json"))
        await self._commit()
        MESSAGES_SENT.inc()
        return out, True

    async def mark_delivered(self, user_id: str, message_id: uuid.UUID) -> MessageOut:
        now = utcnow()
        stmt = (
            update(Message)
            .where(
                Message.id == message_id,
                Message.recipient_id == user_id,
                Message.status == MessageStatus.sent,
            )
            .values(status=MessageStatus.delivered, delivered_at=now)
            .returning(Message)
        )
        updated = (await self._session.scalars(stmt, execution_options=_NO_SYNC)).one_or_none()
        if updated is None:
            message = await self._session.get(Message, message_id)
            if message is None or user_id not in (message.sender_id, message.recipient_id):
                raise NotFoundError("Message not found")
            if message.recipient_id != user_id:
                raise AppError(403, "access_denied", "Only the recipient can confirm delivery")
            return self.to_out(message)  # already delivered or read: idempotent no-op

        self.enqueue(
            [updated.sender_id],
            "message.delivered",
            {
                "message_id": str(updated.id),
                "conversation_id": str(updated.conversation_id),
                "client_msg_id": updated.client_msg_id,
                "delivered_at": now.isoformat(),
            },
        )
        await self._commit()
        return self.to_out(updated)

    async def mark_read(
        self, user_id: str, conversation_id: uuid.UUID, up_to_message_id: uuid.UUID
    ) -> ReadResult:
        """Mark every incoming message up to and including `up_to_message_id` as read."""
        conversation = await self.get_conversation(user_id, conversation_id)
        anchor = await self._session.get(Message, up_to_message_id)
        if anchor is None or anchor.conversation_id != conversation.id:
            raise NotFoundError("Message not found in this conversation")

        now = utcnow()
        stmt = (
            update(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.recipient_id == user_id,
                Message.status != MessageStatus.read,
                _key(anchor.created_at, anchor.id) >= _MESSAGE_KEY,
            )
            .values(
                status=MessageStatus.read,
                read_at=now,
                delivered_at=func.coalesce(Message.delivered_at, now),
            )
            .returning(Message.id)
        )
        ids = list((await self._session.scalars(stmt, execution_options=_NO_SYNC)).all())
        if not ids:
            return ReadResult(conversation_id=conversation.id, message_ids=[], read_at=None)

        self.enqueue(
            [conversation.peer_of(user_id)],
            "message.read",
            {
                "conversation_id": str(conversation.id),
                "reader_id": user_id,
                "message_ids": [str(i) for i in ids],
                "read_at": now.isoformat(),
            },
        )
        await self._commit()
        return ReadResult(conversation_id=conversation.id, message_ids=ids, read_at=now)

    # ------------------------------------------------------------------ queries

    async def list_conversations(self, user_id: str, limit: int = 50) -> list[ConversationOut]:
        last_sq = (
            select(Message)
            .where(Message.conversation_id == Conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .lateral("last_message")
        )
        last = aliased(Message, last_sq)
        unread = (
            select(func.count(Message.id))
            .where(
                Message.conversation_id == Conversation.id,
                Message.recipient_id == user_id,
                Message.status != MessageStatus.read,
            )
            .correlate(Conversation)
            .scalar_subquery()
        )
        stmt = (
            select(Conversation, last, unread)
            .outerjoin(last, true())
            .where(or_(Conversation.user_a == user_id, Conversation.user_b == user_id))
            .order_by(func.coalesce(last.created_at, Conversation.created_at).desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            ConversationOut(
                id=conversation.id,
                peer_id=conversation.peer_of(user_id),
                created_at=conversation.created_at,
                last_message=self.to_out(message) if message is not None else None,
                unread_count=int(unread_count),
            )
            for conversation, message, unread_count in rows
        ]

    async def list_messages(
        self,
        user_id: str,
        conversation_id: uuid.UUID,
        *,
        limit: int,
        before: str | None = None,
        after: str | None = None,
    ) -> MessagePage:
        """`after` returns oldest-first (catch-up sync); otherwise newest-first (history)."""
        if before and after:
            raise AppError(400, "bad_request", "Use either before or after, not both")
        conversation = await self.get_conversation(user_id, conversation_id)
        stmt = select(Message).where(Message.conversation_id == conversation.id)
        if after:
            stmt = stmt.where(_key(*self._cursor(after)) < _MESSAGE_KEY).order_by(
                Message.created_at.asc(), Message.id.asc()
            )
        else:
            if before:
                stmt = stmt.where(_key(*self._cursor(before)) > _MESSAGE_KEY)
            stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc())

        rows = list((await self._session.scalars(stmt.limit(limit + 1))).all())
        has_more = len(rows) > limit
        items = [self.to_out(m) for m in rows[:limit]]
        if after:
            next_cursor: str | None = items[-1].cursor if items else after
        else:
            next_cursor = items[-1].cursor if has_more else None
        return MessagePage(items=items, next_cursor=next_cursor, has_more=has_more)
