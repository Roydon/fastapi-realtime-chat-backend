"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app import __doc__ as description
from app.api import ops, v1, ws
from app.auth import TokenVerifier
from app.config import Settings, get_settings
from app.db import Database
from app.errors import install_error_handlers
from app.logging import configure_logging
from app.middleware import RequestContextMiddleware
from app.realtime.hub import Hub
from app.realtime.relay import OutboxRelay
from app.storage import AttachmentStorage

log = logging.getLogger(__name__)

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url, settings.db_pool_size, settings.db_max_overflow)
        redis = Redis.from_url(settings.redis_url, decode_responses=True, health_check_interval=30)
        hub = Hub(redis, settings.replica_id)
        relay = OutboxRelay(
            db,
            redis,
            poll_interval=settings.outbox_poll_interval_seconds,
            batch_size=settings.outbox_batch_size,
        )
        app.state.db, app.state.redis, app.state.hub, app.state.relay = db, redis, hub, relay
        await hub.start()
        await relay.start()
        log.info("started", extra={"replica": settings.replica_id, "auth_mode": settings.auth_mode})
        try:
            yield
        finally:
            await relay.stop()
            await hub.stop()
            await redis.aclose()
            await db.dispose()

    app = FastAPI(
        title="Realtime Chat API",
        version="0.1.0",
        description=description or "",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.token_verifier = TokenVerifier(settings)
    app.state.storage = AttachmentStorage(settings)

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(ops.router)
    app.include_router(v1.router)
    app.include_router(ws.router)

    if settings.demo_enabled and settings.auth_mode == "hs256":
        app.include_router(v1.demo_router)
        if DEMO_DIR.is_dir():
            app.mount("/demo", StaticFiles(directory=DEMO_DIR, html=True), name="demo")

            @app.get("/", include_in_schema=False)
            async def root() -> RedirectResponse:
                return RedirectResponse("/demo/")

    return app
