"""Request context: request id propagation, access log, HTTP metrics, last-resort 500s."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import error_response
from app.logging import request_id_var
from app.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("app.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
            else:
                await self._http(scope, receive, send, request_id)
        finally:
            request_id_var.reset(token)

    async def _http(self, scope: Scope, receive: Receive, send: Send, request_id: str) -> None:
        start = time.perf_counter()
        status = 500
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error")
            if started:
                raise
            await error_response(500, "internal_error", "Internal server error")(
                scope, receive, send_wrapper
            )
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            method = scope["method"]
            HTTP_REQUESTS.labels(method, route_path, str(status)).inc()
            HTTP_LATENCY.labels(method, route_path).observe(elapsed)
            level = logging.DEBUG if scope["path"] in _QUIET_PATHS else logging.INFO
            log.log(
                level,
                "request",
                extra={
                    "method": method,
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
