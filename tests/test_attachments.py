"""Attachment presign: size and MIME enforcement, ownership, end-to-end upload."""

from __future__ import annotations

import io
import uuid

import httpx
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
    b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def test_presign_rejects_disallowed_mime(client: AsyncClient, auth_headers) -> None:
    resp = await client.post(
        "/v1/attachments/presign",
        json={"filename": "notes.txt", "content_type": "text/plain", "size": 10},
        headers=auth_headers("alice"),
    )
    assert resp.status_code == 415
    assert resp.json()["code"] == "unsupported_media_type"


async def test_presign_rejects_oversize(client: AsyncClient, auth_headers) -> None:
    resp = await client.post(
        "/v1/attachments/presign",
        json={"filename": "big.png", "content_type": "image/png", "size": 6 * 1024 * 1024},
        headers=auth_headers("alice"),
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "payload_too_large"


async def test_upload_and_send_attachment(client: AsyncClient, auth_headers) -> None:
    alice, bob = auth_headers("alice"), auth_headers("bob")
    presign = (
        await client.post(
            "/v1/attachments/presign",
            json={"filename": "hi.png", "content_type": "image/png", "size": len(PNG_1PX)},
            headers=alice,
        )
    ).json()
    assert presign["key"].startswith("u/alice/")

    async with httpx.AsyncClient() as raw:
        files = {"file": ("hi.png", io.BytesIO(PNG_1PX), "image/png")}
        up = await raw.post(presign["url"], data=presign["fields"], files=files)
    assert up.status_code in (201, 204)

    msg = (
        await client.post(
            "/v1/messages",
            json={
                "recipient_id": "bob",
                "attachment_key": presign["key"],
                "client_msg_id": str(uuid.uuid4()),
            },
            headers=alice,
        )
    ).json()
    assert msg["attachment_mime"] == "image/png"
    assert msg["attachment_url"] and presign["key"] in msg["attachment_url"]

    got = (await client.get("/v1/conversations", headers=bob)).json()["items"][0]
    assert got["last_message"]["attachment_url"] is not None


async def test_storage_enforces_size_limit_on_upload(client: AsyncClient, auth_headers) -> None:
    """The presigned POST policy itself rejects a body over the limit."""
    presign = (
        await client.post(
            "/v1/attachments/presign",
            json={"filename": "a.png", "content_type": "image/png", "size": 1024},
            headers=auth_headers("alice"),
        )
    ).json()
    oversize = b"x" * (5 * 1024 * 1024 + 1)
    async with httpx.AsyncClient() as raw:
        up = await raw.post(
            presign["url"],
            data=presign["fields"],
            files={"file": ("a.png", io.BytesIO(oversize), "image/png")},
        )
    assert up.status_code == 400  # EntityTooLarge


async def test_cannot_send_attachment_key_of_another_user(
    client: AsyncClient, auth_headers
) -> None:
    presign = (
        await client.post(
            "/v1/attachments/presign",
            json={"filename": "a.png", "content_type": "image/png", "size": len(PNG_1PX)},
            headers=auth_headers("alice"),
        )
    ).json()
    resp = await client.post(
        "/v1/messages",
        json={
            "recipient_id": "alice",
            "attachment_key": presign["key"],
            "client_msg_id": str(uuid.uuid4()),
        },
        headers=auth_headers("bob"),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_attachment"


async def test_send_with_unuploaded_key_is_422(client: AsyncClient, auth_headers) -> None:
    resp = await client.post(
        "/v1/messages",
        json={
            "recipient_id": "bob",
            "attachment_key": "u/alice/never-uploaded.png",
            "client_msg_id": str(uuid.uuid4()),
        },
        headers=auth_headers("alice"),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "attachment_not_found"
