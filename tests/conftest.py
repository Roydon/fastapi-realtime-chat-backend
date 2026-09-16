"""Shared fixtures. Real Postgres, Redis and MinIO run in throwaway containers
(testcontainers) so the tests exercise the same code paths as production."""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.minio import MinioContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from alembic import command
from app.config import Settings
from app.main import create_app
from app.storage import AttachmentStorage
from app.tokens import mint_token

# Podman ships no Docker daemon for Ryuk to talk back to; disable the reaper.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
TEST_SECRET = "test-secret-value-that-is-long-enough-000"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def backends() -> Iterator[dict[str, str]]:
    pg = PostgresContainer("postgres:16-alpine", driver="asyncpg")
    redis = RedisContainer("redis:7-alpine")
    # quay.io, not Docker Hub: minio/minio on Docker Hub is no longer anonymously pullable.
    minio = MinioContainer("quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z")
    pg.start()
    redis.start()
    minio.start()
    try:
        async_url = pg.get_connection_url()
        cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "alembic"))
        cfg.attributes["database_url"] = async_url
        cfg.attributes["skip_logging"] = True
        command.upgrade(cfg, "head")
        yield {
            "database_url": async_url,
            "redis_url": (
                f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"
            ),
            "s3_endpoint_url": f"http://{minio.get_config()['endpoint']}",
            "s3_access_key": minio.get_config()["access_key"],
            "s3_secret_key": minio.get_config()["secret_key"],
        }
    finally:
        minio.stop()
        redis.stop()
        pg.stop()


@pytest.fixture
def settings(backends: dict[str, str]) -> Settings:
    return Settings(
        replica_id="test-1",
        auth_mode="hs256",
        jwt_secret=TEST_SECRET,
        demo_enabled=True,
        database_url=backends["database_url"],
        redis_url=backends["redis_url"],
        s3_endpoint_url=backends["s3_endpoint_url"],
        s3_public_endpoint_url=backends["s3_endpoint_url"],
        s3_access_key=backends["s3_access_key"],
        s3_secret_key=backends["s3_secret_key"],
        s3_bucket="chat-attachments",
        outbox_poll_interval_seconds=0.1,
        ws_heartbeat_seconds=1.0,
        ws_idle_timeout_seconds=5.0,
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean(backends: dict[str, str]) -> AsyncIterator[None]:
    engine = create_async_engine(backends["database_url"])
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE messages, conversations, outbox RESTART IDENTITY CASCADE")
        )
    await engine.dispose()
    yield


@pytest_asyncio.fixture
async def make_app(settings: Settings):
    stacks: list[AsyncExitStack] = []

    async def factory(**overrides: object):
        cfg = settings.model_copy(update=overrides) if overrides else settings
        await AttachmentStorage(cfg).ensure_bucket()
        application = create_app(cfg)
        stack = AsyncExitStack()
        await stack.enter_async_context(application.router.lifespan_context(application))
        stacks.append(stack)
        return application

    try:
        yield factory
    finally:
        for stack in reversed(stacks):
            await stack.aclose()


@pytest_asyncio.fixture
async def app(make_app):
    return await make_app()


@pytest_asyncio.fixture
async def client(app: object) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def token_factory(settings: Settings):
    def make(user_id: str, **kw: object) -> str:
        return mint_token(user_id, settings.jwt_secret, user_claim=settings.jwt_user_claim, **kw)

    return make


@pytest.fixture
def auth_headers(token_factory):
    def make(user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token_factory(user_id)}"}

    return make
