"""Uniform error responses: every error body is `{code, message, request_id}`."""

from __future__ import annotations

import logging
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import request_id_var

log = logging.getLogger(__name__)

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "access_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(404, "not_found", message)


def error_body(code: str, message: str) -> dict[str, str | None]:
    return {"code": code, "message": message, "request_id": request_id_var.get()}


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(code, message))


async def _app_error(_: Request, raw: Exception) -> JSONResponse:
    exc = cast(AppError, raw)
    return error_response(exc.status_code, exc.code, exc.message)


async def _http_error(_: Request, raw: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, raw)
    code = _STATUS_CODES.get(exc.status_code, "error")
    return error_response(exc.status_code, code, str(exc.detail))


async def _validation_error(_: Request, raw: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, raw)
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return error_response(422, "validation_error", "; ".join(parts) or "Invalid request")


async def _dependency_down(_: Request, exc: Exception) -> JSONResponse:
    log.error("dependency unavailable", exc_info=exc)
    return error_response(503, "service_unavailable", "A backing service is unavailable, retry")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(OperationalError, _dependency_down)
    app.add_exception_handler(DBAPIError, _dependency_down)
    app.add_exception_handler(RedisError, _dependency_down)
    app.add_exception_handler(ConnectionError, _dependency_down)
