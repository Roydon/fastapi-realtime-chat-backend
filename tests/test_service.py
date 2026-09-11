"""ChatService unit-level tests that exercise branches the HTTP tests do not reach."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.errors import AppError, NotFoundError
from app.schemas import SendMessageRequest
from app.services.chat import ChatService
from app.storage import AttachmentStorage

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def service(settings: Settings):
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    storage = AttachmentStorage(settings)
    await storage.ensure_bucket()
    async with maker() as session:
        yield ChatService(session, settings, storage)
    await engine.dispose()


def _req(**kw: object) -> SendMessageRequest:
    return SendMessageRequest(client_msg_id=str(uuid.uuid4()), **kw)


async def test_send_creates_conversation_once(service: ChatService) -> None:
    a, _ = await service.send_message("alice", _req(recipient_id="bob", body="1"))
    b, _ = await service.send_message("bob", _req(recipient_id="alice", body="2"))
    assert a.conversation_id == b.conversation_id


async def test_list_conversations_orders_by_recent_activity(service: ChatService) -> None:
    await service.send_message("alice", _req(recipient_id="bob", body="hi bob"))
    await service.send_message("alice", _req(recipient_id="carol", body="hi carol"))
    await service.send_message("carol", _req(recipient_id="alice", body="reply"))
    convs = await service.list_conversations("alice")
    assert [c.peer_id for c in convs] == ["carol", "bob"]
    assert convs[0].last_message is not None
    assert convs[0].last_message.body == "reply"


async def test_mark_read_no_unread_is_noop(service: ChatService) -> None:
    msg, _ = await service.send_message("alice", _req(recipient_id="bob", body="x"))
    first = await service.mark_read("bob", msg.conversation_id, msg.id)
    assert first.message_ids == [msg.id]
    again = await service.mark_read("bob", msg.conversation_id, msg.id)
    assert again.message_ids == []
    assert again.read_at is None


async def test_mark_read_requires_membership(service: ChatService) -> None:
    msg, _ = await service.send_message("alice", _req(recipient_id="bob", body="x"))
    with pytest.raises(NotFoundError):
        await service.mark_read("mallory", msg.conversation_id, msg.id)


async def test_mark_read_anchor_must_be_in_conversation(service: ChatService) -> None:
    msg, _ = await service.send_message("alice", _req(recipient_id="bob", body="x"))
    other, _ = await service.send_message("alice", _req(recipient_id="carol", body="y"))
    with pytest.raises(NotFoundError):
        await service.mark_read("bob", msg.conversation_id, other.id)


async def test_delivered_unknown_message(service: ChatService) -> None:
    with pytest.raises(NotFoundError):
        await service.mark_delivered("bob", uuid.uuid4())


async def test_list_messages_before_and_after_are_exclusive(service: ChatService) -> None:
    msg, _ = await service.send_message("alice", _req(recipient_id="bob", body="x"))
    with pytest.raises(AppError, match="either before or after"):
        await service.list_messages("alice", msg.conversation_id, limit=10, before="a", after="b")


async def test_replay_returns_same_message_object(service: ChatService) -> None:
    cmid = str(uuid.uuid4())
    first, created1 = await service.send_message(
        "alice", SendMessageRequest(client_msg_id=cmid, recipient_id="bob", body="dup")
    )
    second, created2 = await service.send_message(
        "alice", SendMessageRequest(client_msg_id=cmid, recipient_id="bob", body="dup")
    )
    assert created1 is True and created2 is False
    assert first.id == second.id
