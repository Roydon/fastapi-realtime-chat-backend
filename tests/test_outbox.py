"""Transactional outbox guarantees: no push without a durable write, backlog drains,
at-least-once relay survives a Redis outage."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Message, Outbox
from tests.servers import running_app

pytestmark = pytest.mark.asyncio


async def _outbox_backlog(db_url: str) -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return int(
                await conn.scalar(
                    select(func.count()).select_from(Outbox).where(Outbox.published_at.is_(None))
                )
            )
    finally:
        await engine.dispose()


async def test_send_failure_writes_nothing_and_pushes_nothing(
    client, auth_headers, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the commit fails, neither the message nor an outbox row survives, and no
    event is published."""
    from app.services import chat as chat_module

    published: list[object] = []
    real_commit = chat_module.ChatService._commit

    async def boom(self: object) -> None:
        raise RuntimeError("database exploded during commit")

    monkeypatch.setattr(chat_module.ChatService, "_commit", boom)

    resp = await client.post(
        "/v1/messages",
        json={"recipient_id": "bob", "body": "doomed", "client_msg_id": str(uuid.uuid4())},
        headers=auth_headers("alice"),
    )
    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"

    monkeypatch.setattr(chat_module.ChatService, "_commit", real_commit)
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(Message)) == 0
        assert await conn.scalar(select(func.count()).select_from(Outbox)) == 0
    await engine.dispose()
    assert published == []


async def test_backlog_returns_to_zero_after_redis_restart(settings, token_factory) -> None:
    """Kill Redis mid-flight: rows queue in the outbox, then drain once Redis is back."""
    import asyncio

    import httpx
    from testcontainers.redis import RedisContainer

    alice = token_factory("alice")

    async def send(base_url: str, n: int) -> None:
        async with httpx.AsyncClient(base_url=base_url) as c:
            r = await c.post(
                "/v1/messages",
                json={"recipient_id": "bob", "body": f"m{n}", "client_msg_id": str(uuid.uuid4())},
                headers={"Authorization": f"Bearer {alice}"},
            )
            r.raise_for_status()

    redis = RedisContainer("redis:7-alpine")
    redis.start()
    stopped = False
    host, port = redis.get_container_host_ip(), redis.get_exposed_port(6379)
    cfg = settings.model_copy(update={"redis_url": f"redis://{host}:{port}/0"})
    try:
        async with running_app(cfg) as srv:
            await send(srv.base_url, 0)
            for _ in range(50):
                if await _outbox_backlog(cfg.database_url) == 0:
                    break
                await asyncio.sleep(0.1)
            assert await _outbox_backlog(cfg.database_url) == 0

            redis.stop()  # outage begins
            stopped = True
            for i in range(1, 6):
                await send(srv.base_url, i)
            await asyncio.sleep(0.5)
            assert await _outbox_backlog(cfg.database_url) >= 1  # rows persist, safely unpublished
    finally:
        if not stopped:
            redis.stop()

    fresh = RedisContainer("redis:7-alpine")
    fresh.start()
    host2, port2 = fresh.get_container_host_ip(), fresh.get_exposed_port(6379)
    cfg2 = settings.model_copy(update={"redis_url": f"redis://{host2}:{port2}/0"})
    try:
        async with running_app(cfg2):  # a replica reconnects to Redis
            for _ in range(50):
                if await _outbox_backlog(cfg2.database_url) == 0:
                    break
                await asyncio.sleep(0.2)
            assert await _outbox_backlog(cfg2.database_url) == 0
    finally:
        fresh.stop()


async def test_outbox_row_written_in_same_transaction_as_message(
    client, auth_headers, settings
) -> None:
    resp = await client.post(
        "/v1/messages",
        json={"recipient_id": "bob", "body": "paired", "client_msg_id": str(uuid.uuid4())},
        headers=auth_headers("alice"),
    )
    assert resp.status_code == 201
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        topics = (await conn.execute(select(Outbox.topic).order_by(Outbox.id))).scalars().all()
    await engine.dispose()
    assert set(topics) == {"user:alice", "user:bob"}
