"""Health, readiness, metrics, OpenAPI and the uniform error schema."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_reports_backends(client: AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "redis": "ok"}


async def test_readyz_unavailable_when_redis_down(make_app) -> None:
    app = await make_app(redis_url="redis://127.0.0.1:1/0")
    from httpx import ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",  # type: ignore[arg-type]
    ) as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"].startswith("error")


async def test_metrics_endpoint_exposes_prometheus(client: AsyncClient, auth_headers) -> None:
    import uuid

    await client.post(
        "/v1/messages",
        json={"recipient_id": "bob", "body": "metric", "client_msg_id": str(uuid.uuid4())},
        headers=auth_headers("alice"),
    )
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "chat_messages_sent_total" in resp.text
    assert "chat_outbox_backlog" in resp.text


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"X-Request-ID": "test-req-123"})
    assert resp.headers["x-request-id"] == "test-req-123"


async def test_unknown_route_uses_error_schema(client: AsyncClient) -> None:
    resp = await client.get("/v1/nope")
    assert resp.status_code == 404
    assert set(resp.json()) == {"code", "message", "request_id"}


async def test_openapi_and_docs(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert schema["info"]["title"] == "Realtime Chat API"
    assert "/v1/messages" in schema["paths"]
    assert (await client.get("/docs")).status_code == 200


async def test_demo_token_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/v1/demo/token", params={"user": "alice"})
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "alice"
    assert (await client.get("/v1/demo/token", params={"user": "mallory"})).status_code == 422
