"""REST message flow: send, idempotency, validation, receipts, pagination, sync."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def send(client: AsyncClient, headers: dict[str, str], **body: object):
    payload = {"client_msg_id": str(uuid.uuid4()), **body}
    return await client.post("/v1/messages", json=payload, headers=headers)


async def test_send_and_list_conversation(client: AsyncClient, auth_headers) -> None:
    alice, bob = auth_headers("alice"), auth_headers("bob")
    resp = await send(client, alice, recipient_id="bob", body="hi bob \U0001f44b")
    assert resp.status_code == 201
    msg = resp.json()
    assert msg["status"] == "sent"
    assert msg["sender_id"] == "alice" and msg["recipient_id"] == "bob"
    assert msg["body"] == "hi bob \U0001f44b"

    convs = (await client.get("/v1/conversations", headers=bob)).json()["items"]
    assert len(convs) == 1
    assert convs[0]["peer_id"] == "alice"
    assert convs[0]["unread_count"] == 1
    assert convs[0]["last_message"]["id"] == msg["id"]


async def test_idempotent_resend_returns_same_message(client: AsyncClient, auth_headers) -> None:
    alice = auth_headers("alice")
    cmid = str(uuid.uuid4())
    first = await client.post(
        "/v1/messages",
        json={"recipient_id": "bob", "body": "once", "client_msg_id": cmid},
        headers=alice,
    )
    second = await client.post(
        "/v1/messages",
        json={"recipient_id": "bob", "body": "once", "client_msg_id": cmid},
        headers=alice,
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    msgs = (
        await client.get(
            f"/v1/conversations/{first.json()['conversation_id']}/messages", headers=alice
        )
    ).json()
    assert len(msgs["items"]) == 1


async def test_same_client_msg_id_different_body_conflicts(
    client: AsyncClient, auth_headers
) -> None:
    alice = auth_headers("alice")
    cmid = str(uuid.uuid4())
    await client.post(
        "/v1/messages",
        json={"recipient_id": "bob", "body": "original", "client_msg_id": cmid},
        headers=alice,
    )
    resp = await client.post(
        "/v1/messages",
        json={"recipient_id": "bob", "body": "changed", "client_msg_id": cmid},
        headers=alice,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "idempotency_conflict"


async def test_empty_and_overlong_bodies_rejected(client: AsyncClient, auth_headers) -> None:
    alice = auth_headers("alice")
    assert (await send(client, alice, recipient_id="bob")).status_code == 422
    assert (await send(client, alice, recipient_id="bob", body="   ")).status_code == 422
    too_long = "a" * 4001
    resp = await send(client, alice, recipient_id="bob", body=too_long)
    assert resp.status_code == 422
    assert resp.json()["code"] == "body_too_long"


async def test_cannot_message_self(client: AsyncClient, auth_headers) -> None:
    resp = await send(client, auth_headers("alice"), recipient_id="alice", body="hi me")
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_recipient"


async def test_delivered_then_read_receipts(client: AsyncClient, auth_headers) -> None:
    alice, bob = auth_headers("alice"), auth_headers("bob")
    msg = (await send(client, alice, recipient_id="bob", body="receipt me")).json()

    delivered = await client.post(f"/v1/messages/{msg['id']}/delivered", headers=bob)
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "delivered"
    assert delivered.json()["delivered_at"] is not None

    read = await client.post(
        f"/v1/conversations/{msg['conversation_id']}/read",
        json={"up_to_message_id": msg["id"]},
        headers=bob,
    )
    assert read.status_code == 200
    assert read.json()["message_ids"] == [msg["id"]]

    convs = (await client.get("/v1/conversations", headers=bob)).json()["items"]
    assert convs[0]["unread_count"] == 0


async def test_delivered_is_idempotent_and_sender_forbidden(
    client: AsyncClient, auth_headers
) -> None:
    alice, bob = auth_headers("alice"), auth_headers("bob")
    msg = (await send(client, alice, recipient_id="bob", body="x")).json()
    await client.post(f"/v1/messages/{msg['id']}/delivered", headers=bob)
    again = await client.post(f"/v1/messages/{msg['id']}/delivered", headers=bob)
    assert again.status_code == 200 and again.json()["status"] == "delivered"

    sender_try = await client.post(f"/v1/messages/{msg['id']}/delivered", headers=alice)
    assert sender_try.status_code == 403
    assert sender_try.json()["code"] == "access_denied"


async def test_read_marks_all_up_to_anchor(client: AsyncClient, auth_headers) -> None:
    alice, bob = auth_headers("alice"), auth_headers("bob")
    ids = []
    for i in range(5):
        ids.append((await send(client, alice, recipient_id="bob", body=f"m{i}")).json()["id"])
    conv = (await client.get("/v1/conversations", headers=bob)).json()["items"][0]["id"]

    read = await client.post(
        f"/v1/conversations/{conv}/read", json={"up_to_message_id": ids[2]}, headers=bob
    )
    assert set(read.json()["message_ids"]) == set(ids[:3])
    convs = (await client.get("/v1/conversations", headers=bob)).json()["items"]
    assert convs[0]["unread_count"] == 2


async def test_pagination_and_after_sync(client: AsyncClient, auth_headers) -> None:
    alice, bob = auth_headers("alice"), auth_headers("bob")
    for i in range(25):
        await send(client, alice, recipient_id="bob", body=f"msg-{i:02d}")
    conv = (await client.get("/v1/conversations", headers=bob)).json()["items"][0]["id"]

    page1 = (await client.get(f"/v1/conversations/{conv}/messages?limit=10", headers=bob)).json()
    assert len(page1["items"]) == 10 and page1["has_more"] is True
    page2 = (
        await client.get(
            f"/v1/conversations/{conv}/messages?limit=10&before={page1['next_cursor']}",
            headers=bob,
        )
    ).json()
    assert len(page2["items"]) == 10
    assert {m["id"] for m in page1["items"]} & {m["id"] for m in page2["items"]} == set()

    # catch-up sync: everything after the 5th message, oldest first
    oldest = (await client.get(f"/v1/conversations/{conv}/messages?limit=100", headers=bob)).json()[
        "items"
    ][::-1]
    cursor = oldest[4]["cursor"]
    synced = (
        await client.get(f"/v1/conversations/{conv}/messages?after={cursor}&limit=100", headers=bob)
    ).json()
    assert [m["body"] for m in synced["items"]] == [f"msg-{i:02d}" for i in range(5, 25)]


async def test_cross_user_cannot_read_conversation(client: AsyncClient, auth_headers) -> None:
    alice = auth_headers("alice")
    msg = (await send(client, alice, recipient_id="bob", body="private")).json()
    resp = await client.get(
        f"/v1/conversations/{msg['conversation_id']}/messages", headers=auth_headers("carol")
    )
    assert resp.status_code == 404


async def test_bad_cursor_returns_400(client: AsyncClient, auth_headers) -> None:
    alice = auth_headers("alice")
    msg = (await send(client, alice, recipient_id="bob", body="x")).json()
    resp = await client.get(
        f"/v1/conversations/{msg['conversation_id']}/messages?before=@@@bad@@@", headers=alice
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_cursor"


async def test_unknown_conversation_is_404(client: AsyncClient, auth_headers) -> None:
    resp = await client.get(
        f"/v1/conversations/{uuid.uuid4()}/messages", headers=auth_headers("alice")
    )
    assert resp.status_code == 404
