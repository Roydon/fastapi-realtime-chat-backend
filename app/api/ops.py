"""Liveness, readiness and Prometheus metrics."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["ops"])

CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/healthz", summary="Liveness: the process is serving requests")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness: PostgreSQL and Redis are reachable")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, str] = {}

    async def check(name: str, probe: Any) -> None:
        try:
            await asyncio.wait_for(probe, timeout=CHECK_TIMEOUT_SECONDS)
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {type(exc).__name__}"

    await asyncio.gather(check("database", state.db.ping()), check("redis", state.redis.ping()))
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {
            "status": "ok" if ready else "unavailable",
            "replica": state.settings.replica_id,
            "checks": checks,
        },
        status_code=200 if ready else 503,
    )


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
