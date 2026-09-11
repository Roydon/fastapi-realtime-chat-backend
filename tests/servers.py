"""Run the app under a real uvicorn server so WebSocket tests use a real transport."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import uvicorn
import websockets

from app.config import Settings
from app.main import create_app
from tests.conftest import free_port


@dataclass
class RunningApp:
    base_url: str
    ws_url: str


@contextlib.asynccontextmanager
async def running_app(settings: Settings) -> AsyncIterator[RunningApp]:
    port = free_port()
    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover
            raise RuntimeError("server did not start")
        yield RunningApp(f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        await task


async def ws_recv(conn: object, wanted: str, timeout: float = 5.0) -> dict:
    import json

    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await conn.recv())  # type: ignore[attr-defined]
            if event.get("type") == wanted:
                return event


def ws_connect(url: str, token: str, *, path: str = "/v1/ws"):
    return websockets.connect(f"{url}{path}?token={token}")
