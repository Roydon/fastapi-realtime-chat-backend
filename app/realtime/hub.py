"""Per-replica WebSocket registry bridged to Redis pub/sub.

Each user has a channel `chat:user:<id>`. A replica subscribes to a user's channel while at
least one of that user's devices is connected to it, so any replica can publish an event
and whichever replica holds the recipient's socket delivers it. No sticky sessions needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from app.metrics import DELIVERY_LATENCY, WS_CONNECTIONS

log = logging.getLogger(__name__)

CHANNEL_PREFIX = "chat:"
MAX_PENDING_EVENTS = 1000


def user_channel(user_id: str) -> str:
    return f"{CHANNEL_PREFIX}user:{user_id}"


def topic_channel(topic: str) -> str:
    return f"{CHANNEL_PREFIX}{topic}"


@dataclass(frozen=True)
class CloseRequest:
    code: int
    reason: str


class Connection:
    """One WebSocket. Events are queued so a slow client never blocks fan-out."""

    def __init__(self, websocket: WebSocket, user_id: str) -> None:
        self.websocket = websocket
        self.user_id = user_id
        self.id = uuid.uuid4().hex[:12]
        self.closing = False
        self._queue: asyncio.Queue[dict[str, Any] | CloseRequest] = asyncio.Queue()

    def send_nowait(self, event: dict[str, Any]) -> None:
        if self.closing:
            return
        if self._queue.qsize() >= MAX_PENDING_EVENTS:
            log.warning("slow consumer, closing", extra={"user_id": self.user_id})
            self.close(1013, "client too slow, reconnect and sync")
            return
        self._queue.put_nowait(event)

    def close(self, code: int, reason: str) -> None:
        if not self.closing:
            self.closing = True
            self._queue.put_nowait(CloseRequest(code, reason))

    async def next_event(self) -> dict[str, Any] | CloseRequest:
        return await self._queue.get()


class Hub:
    def __init__(self, redis: Redis, replica_id: str) -> None:
        self._redis = redis
        self._control_channel = f"{CHANNEL_PREFIX}replica:{replica_id}"
        self._local: dict[str, set[Connection]] = defaultdict(set)
        self._requests: asyncio.Queue[tuple[str, str, asyncio.Future[None]]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="hub-pubsub")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.connected.wait(), timeout=5)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for conns in list(self._local.values()):
            for conn in conns:
                conn.close(1012, "server restarting")

    # ---------------------------------------------------------------- registry

    def local_users(self) -> list[str]:
        return list(self._local)

    async def register(self, conn: Connection) -> None:
        conns = self._local[conn.user_id]
        first = not conns
        conns.add(conn)
        WS_CONNECTIONS.inc()
        if first:
            await self._request("subscribe", user_channel(conn.user_id))

    async def unregister(self, conn: Connection) -> None:
        conns = self._local.get(conn.user_id)
        if conns is None or conn not in conns:
            return
        conns.discard(conn)
        WS_CONNECTIONS.dec()
        if not conns:
            del self._local[conn.user_id]
            await self._request("unsubscribe", user_channel(conn.user_id))

    async def publish(self, user_id: str, event: dict[str, Any]) -> None:
        """Publish an ephemeral event (for example typing) directly, bypassing the outbox."""
        await self._redis.publish(user_channel(user_id), json.dumps(event, ensure_ascii=False))

    async def _request(self, op: str, channel: str) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._requests.put_nowait((op, channel, future))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(future), timeout=5)

    # ---------------------------------------------------------------- pub/sub loop

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            try:
                channels = [user_channel(u) for u in self._local]
                await pubsub.subscribe(self._control_channel, *channels)
                self.connected.set()
                backoff = 0.5
                while True:
                    await self._apply_requests(pubsub)
                    message = await pubsub.get_message(timeout=0.05)
                    if message is not None:
                        self._dispatch(message)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                self.connected.clear()
                log.warning("redis pub/sub lost, resubscribing", extra={"error": str(exc)})
                self._release_waiters()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _apply_requests(self, pubsub: PubSub) -> None:
        while not self._requests.empty():
            op, channel, future = self._requests.get_nowait()
            try:
                if op == "subscribe":
                    await pubsub.subscribe(channel)
                else:
                    await pubsub.unsubscribe(channel)
            finally:
                if not future.done():
                    future.set_result(None)

    def _release_waiters(self) -> None:
        # The reconnect path resubscribes from `_local`, so pending requests are moot.
        while not self._requests.empty():
            _, _, future = self._requests.get_nowait()
            if not future.done():
                future.set_result(None)

    def _dispatch(self, message: dict[str, Any]) -> None:
        channel = message.get("channel")
        prefix = user_channel("")
        if not isinstance(channel, str) or not channel.startswith(prefix):
            return
        user_id = channel[len(prefix) :]
        conns = self._local.get(user_id)
        if not conns:
            return
        try:
            event = json.loads(message["data"])
        except (TypeError, ValueError):
            log.warning("dropping malformed event", extra={"channel": channel})
            return
        self._observe_latency(user_id, event)
        for conn in list(conns):
            conn.send_nowait(event)

    @staticmethod
    def _observe_latency(user_id: str, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        if event.get("type") != "message.new" or data.get("recipient_id") != user_id:
            return
        with contextlib.suppress(KeyError, TypeError, ValueError):
            created = datetime.fromisoformat(data["created_at"]).timestamp()
            DELIVERY_LATENCY.observe(max(0.0, time.time() - created))
