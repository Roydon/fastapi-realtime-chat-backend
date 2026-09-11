"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str, pool_size: int = 10, max_overflow: int = 10) -> None:
        self.engine = create_async_engine(
            url, pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    db: Database = request.app.state.db
    async with db.sessionmaker() as session:
        yield session
