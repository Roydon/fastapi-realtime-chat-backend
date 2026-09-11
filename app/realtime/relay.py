"""Outbox relay: publishes committed outbox rows to Redis and marks them published.

* Runs on every replica. `FOR UPDATE SKIP LOCKED` lets replicas share the work without
  double-publishing a row.
* Wakes immediately when this replica commits a change (`notify`), and polls as a safety
  net for rows written by replicas that crashed before publishing, or during a Redis outage.
* Delivery is at-least-once: if the commit after publishing fails, the batch is published
  again. Clients de-duplicate by message id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from redis.asyncio import Redis
from sqlalchemy import delete, func, select, text, update

from app.db import Database
from app.metrics import OUTBOX_BACKLOG, OUTBOX_PUBLISH_ERRORS, OUTBOX_PUBLISHED
from app.models import Outbox
from app.realtime.hub import topic_channel

log = logging.getLogger(__name__)

BACKLOG_REFRESH_SECONDS = 5.0
RETENTION_SWEEP_SECONDS = 60.0


class OutboxRelay:
    def __init__(
        self,
        db: Database,
        redis: Redis,
        poll_interval: float = 0.5,
        batch_size: int = 200,
        retention: str = "1 hour",
    ) -> None:
        self._db = db
        self._redis = redis
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._retention = retention
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def notify(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="outbox-relay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def publish_batch(self) -> int:
        async with self._db.sessionmaker() as session, session.begin():
            rows = (
                await session.execute(
                    select(Outbox.id, Outbox.topic, Outbox.payload)
                    .where(Outbox.published_at.is_(None))
                    .order_by(Outbox.id)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return 0
            async with self._redis.pipeline(transaction=False) as pipe:
                for row in rows:
                    pipe.publish(topic_channel(row.topic), json.dumps(row.payload))
                await pipe.execute()
            await session.execute(
                update(Outbox)
                .where(Outbox.id.in_([row.id for row in rows]))
                .values(published_at=func.now())
            )
        OUTBOX_PUBLISHED.inc(len(rows))
        return len(rows)

    async def backlog(self) -> int:
        async with self._db.sessionmaker() as session:
            count = await session.scalar(
                select(func.count()).select_from(Outbox).where(Outbox.published_at.is_(None))
            )
        return int(count or 0)

    async def _sweep(self) -> None:
        async with self._db.sessionmaker() as session, session.begin():
            await session.execute(
                delete(Outbox).where(
                    Outbox.published_at < func.now() - text(f"interval '{self._retention}'")
                )
            )

    async def _run(self) -> None:
        delay = self._poll_interval
        next_backlog = next_sweep = 0.0
        while True:
            self._wake.clear()
            try:
                published = await self.publish_batch()
                now = time.monotonic()
                if now >= next_backlog:
                    OUTBOX_BACKLOG.set(await self.backlog())
                    next_backlog = now + BACKLOG_REFRESH_SECONDS
                if now >= next_sweep:
                    await self._sweep()
                    next_sweep = now + RETENTION_SWEEP_SECONDS
                delay = self._poll_interval
                if published >= self._batch_size:
                    continue  # more rows are waiting
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                OUTBOX_PUBLISH_ERRORS.inc()
                log.warning("outbox relay cycle failed", extra={"error": repr(exc)})
                next_backlog = 0.0
                delay = min(delay * 2, 5.0)
                await asyncio.sleep(delay)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
