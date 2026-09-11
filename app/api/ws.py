"""WebSocket endpoint: `/v1/ws`.

Server -> client: hello, message.new, message.delivered, message.read, typing, ping, pong, error
Client -> server: ack, read, typing, ping, pong
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, WebSocket
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.applications import Starlette
from starlette.websockets import WebSocketState

from app.api.deps import build_service
from app.auth import websocket_token
from app.config import Settings
from app.errors import AppError
from app.metrics import EVENTS_PUSHED
from app.realtime.hub import CloseRequest, Connection, Hub
from app.services.chat import ChatService

log = logging.getLogger(__name__)

router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
CLOSE_IDLE = 4408


@router.websocket("/v1/ws")
async def chat_socket(websocket: WebSocket) -> None:
    app: Starlette = websocket.app
    token, subprotocol = websocket_token(websocket)
    await websocket.accept(subprotocol=subprotocol)
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="missing token")
        return
    try:
        user_id = await app.state.token_verifier.verify(token)
    except AppError as exc:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason=exc.code)
        return

    hub: Hub = app.state.hub
    conn = Connection(websocket, user_id)
    await hub.register(conn)
    log.info("ws connected", extra={"user_id": user_id, "conn_id": conn.id})
    try:
        await SocketSession(app, conn).run()
    finally:
        await hub.unregister(conn)
        log.info("ws disconnected", extra={"user_id": user_id, "conn_id": conn.id})


class SocketSession:
    def __init__(self, app: Starlette, conn: Connection) -> None:
        self.app = app
        self.conn = conn
        self.settings: Settings = app.state.settings
        self.hub: Hub = app.state.hub
        self.last_seen = time.monotonic()
        self._peers: dict[uuid.UUID, str] = {}

    async def run(self) -> None:
        self.conn.send_nowait(
            {
                "type": "hello",
                "data": {
                    "user_id": self.conn.user_id,
                    "replica": self.settings.replica_id,
                    "heartbeat_seconds": self.settings.ws_heartbeat_seconds,
                },
            }
        )
        tasks = [
            asyncio.create_task(self._reader()),
            asyncio.create_task(self._writer()),
            asyncio.create_task(self._heartbeat()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, OSError):
                    log.warning("ws task failed", extra={"error": repr(result)})
            if self.conn.websocket.application_state == WebSocketState.CONNECTED:
                with contextlib.suppress(Exception):
                    await self.conn.websocket.close()

    # ---------------------------------------------------------------- loops

    async def _reader(self) -> None:
        ws = self.conn.websocket
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            self.last_seen = time.monotonic()
            raw = message.get("text") or (message.get("bytes") or b"").decode("utf-8", "replace")
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("event must be an object")
            except ValueError:
                self._error("bad_event", "Events must be JSON objects", None)
                continue
            await self._handle(event)

    async def _writer(self) -> None:
        ws = self.conn.websocket
        while True:
            event = await self.conn.next_event()
            if isinstance(event, CloseRequest):
                await ws.close(code=event.code, reason=event.reason)
                return
            await ws.send_text(json.dumps(event, ensure_ascii=False))
            EVENTS_PUSHED.labels(str(event.get("type", "unknown"))).inc()

    async def _heartbeat(self) -> None:
        interval = self.settings.ws_heartbeat_seconds
        while not self.conn.closing:
            await asyncio.sleep(interval)
            if time.monotonic() - self.last_seen > self.settings.ws_idle_timeout_seconds:
                self.conn.close(CLOSE_IDLE, "idle timeout")
                return
            self.conn.send_nowait({"type": "ping", "ts": time.time()})

    # ---------------------------------------------------------------- handlers

    async def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        ref = event.get("ref")
        if kind == "ping":
            self.conn.send_nowait({"type": "pong", "ref": ref})
        elif kind == "pong":
            return
        elif kind == "ack":
            await self._guard(ref, lambda svc: self._ack(svc, event))
        elif kind == "read":
            await self._guard(ref, lambda svc: self._read(svc, event))
        elif kind == "typing":
            await self._guard(ref, lambda svc: self._typing(svc, event))
        else:
            self._error("unknown_event", f"Unsupported event type: {kind!r}", ref)

    async def _guard(self, ref: Any, action: Callable[[ChatService], Awaitable[None]]) -> None:
        try:
            async with self.app.state.db.sessionmaker() as session:
                await action(build_service(self.app, session))
        except AppError as exc:
            self._error(exc.code, exc.message, ref)
        except (KeyError, TypeError, ValueError):
            self._error("bad_event", "Missing or invalid fields", ref)
        except (SQLAlchemyError, RedisError, OSError):
            log.exception("ws event failed")
            self._error("service_unavailable", "Temporarily unavailable, retry", ref)

    async def _ack(self, service: ChatService, event: dict[str, Any]) -> None:
        await service.mark_delivered(self.conn.user_id, uuid.UUID(str(event["message_id"])))

    async def _read(self, service: ChatService, event: dict[str, Any]) -> None:
        await service.mark_read(
            self.conn.user_id,
            uuid.UUID(str(event["conversation_id"])),
            uuid.UUID(str(event["up_to_message_id"])),
        )

    async def _typing(self, service: ChatService, event: dict[str, Any]) -> None:
        conversation_id = uuid.UUID(str(event["conversation_id"]))
        peer = self._peers.get(conversation_id)
        if peer is None:
            conversation = await service.get_conversation(self.conn.user_id, conversation_id)
            peer = self._peers[conversation_id] = conversation.peer_of(self.conn.user_id)
        await self.hub.publish(
            peer,
            {
                "type": "typing",
                "data": {
                    "conversation_id": str(conversation_id),
                    "user_id": self.conn.user_id,
                    "is_typing": bool(event.get("is_typing", True)),
                },
            },
        )

    def _error(self, code: str, message: str, ref: Any) -> None:
        self.conn.send_nowait(
            {"type": "error", "ref": ref, "data": {"code": code, "message": message}}
        )
