"""WebSocket delivery: live push, receipts, typing, reconnect sync, cross-replica fan-out."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.servers import running_app, ws_connect, ws_recv

pytestmark = pytest.mark.asyncio


async def _send(app_url: str, sender_token: str, recipient: str, body: str) -> dict:
    import httpx

    async with httpx.AsyncClient(base_url=app_url) as c:
        resp = await c.post(
            "/v1/messages",
            json={"recipient_id": recipient, "body": body, "client_msg_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {sender_token}"},
        )
    resp.raise_for_status()
    return resp.json()


async def test_live_delivery_and_ack(settings, token_factory) -> None:
    async with running_app(settings) as srv:
        alice, bob = token_factory("alice"), token_factory("bob")
        async with ws_connect(srv.ws_url, bob) as bob_ws, ws_connect(srv.ws_url, alice) as a_ws:
            await ws_recv(bob_ws, "hello")
            await ws_recv(a_ws, "hello")

            sent = await _send(srv.base_url, alice, "bob", "live hello \U0001f680")
            event = await ws_recv(bob_ws, "message.new")
            assert event["data"]["id"] == sent["id"]
            assert event["data"]["body"] == "live hello \U0001f680"

            # sender's own devices also see the message (multi-device sync)
            own = await ws_recv(a_ws, "message.new")
            assert own["data"]["id"] == sent["id"]

            await bob_ws.send(json.dumps({"type": "ack", "message_id": sent["id"]}))
            delivered = await ws_recv(a_ws, "message.delivered")
            assert delivered["data"]["message_id"] == sent["id"]


async def test_read_receipt_over_ws(settings, token_factory) -> None:
    async with running_app(settings) as srv:
        alice, bob = token_factory("alice"), token_factory("bob")
        async with ws_connect(srv.ws_url, alice) as a_ws, ws_connect(srv.ws_url, bob) as bob_ws:
            await ws_recv(a_ws, "hello")
            await ws_recv(bob_ws, "hello")
            sent = await _send(srv.base_url, alice, "bob", "read me")
            evt = await ws_recv(bob_ws, "message.new")
            await bob_ws.send(
                json.dumps(
                    {
                        "type": "read",
                        "conversation_id": evt["data"]["conversation_id"],
                        "up_to_message_id": sent["id"],
                    }
                )
            )
            read = await ws_recv(a_ws, "message.read")
            assert sent["id"] in read["data"]["message_ids"]
            assert read["data"]["reader_id"] == "bob"


async def test_typing_indicator_is_relayed(settings, token_factory) -> None:
    async with running_app(settings) as srv:
        alice, bob = token_factory("alice"), token_factory("bob")
        async with ws_connect(srv.ws_url, alice) as a_ws, ws_connect(srv.ws_url, bob) as bob_ws:
            await ws_recv(a_ws, "hello")
            await ws_recv(bob_ws, "hello")
            await _send(srv.base_url, alice, "bob", "hi")
            evt = await ws_recv(bob_ws, "message.new")
            conv = evt["data"]["conversation_id"]
            await a_ws.send(
                json.dumps({"type": "typing", "conversation_id": conv, "is_typing": True})
            )
            typing = await ws_recv(bob_ws, "typing")
            assert typing["data"]["user_id"] == "alice"
            assert typing["data"]["is_typing"] is True


async def test_offline_recipient_syncs_missed_messages(settings, token_factory) -> None:
    """Bob is offline while Alice sends; on reconnect he catches up over REST."""
    import httpx

    async with running_app(settings) as srv:
        alice, bob = token_factory("alice"), token_factory("bob")
        for i in range(3):
            await _send(srv.base_url, alice, "bob", f"while-away-{i}")

        async with ws_connect(srv.ws_url, bob) as bob_ws:
            await ws_recv(bob_ws, "hello")
            async with httpx.AsyncClient(base_url=srv.base_url) as c:
                convs = (
                    await c.get("/v1/conversations", headers={"Authorization": f"Bearer {bob}"})
                ).json()["items"]
                assert convs[0]["unread_count"] == 3
                conv_id = convs[0]["id"]
                missed = (
                    await c.get(
                        f"/v1/conversations/{conv_id}/messages?limit=100",
                        headers={"Authorization": f"Bearer {bob}"},
                    )
                ).json()
            assert [m["body"] for m in missed["items"][::-1]] == [
                "while-away-0",
                "while-away-1",
                "while-away-2",
            ]


async def test_cross_replica_fanout(settings, token_factory) -> None:
    """Two independent app instances share one Redis: a message sent via replica A
    reaches a recipient connected to replica B."""
    replica_a = settings.model_copy(update={"replica_id": "replica-a"})
    replica_b = settings.model_copy(update={"replica_id": "replica-b"})
    async with running_app(replica_a) as srv_a, running_app(replica_b) as srv_b:
        alice, bob = token_factory("alice"), token_factory("bob")
        async with (
            ws_connect(srv_a.ws_url, alice) as a_ws,
            ws_connect(srv_b.ws_url, bob) as b_ws,
        ):
            hello_a = await ws_recv(a_ws, "hello")
            hello_b = await ws_recv(b_ws, "hello")
            assert hello_a["data"]["replica"] == "replica-a"
            assert hello_b["data"]["replica"] == "replica-b"

            sent = await _send(srv_a.base_url, alice, "bob", "across the fleet")
            got = await ws_recv(b_ws, "message.new")
            assert got["data"]["id"] == sent["id"]

            await b_ws.send(json.dumps({"type": "ack", "message_id": sent["id"]}))
            delivered = await ws_recv(a_ws, "message.delivered")
            assert delivered["data"]["message_id"] == sent["id"]


async def test_ws_rejects_missing_and_bad_token(settings, token_factory) -> None:
    import websockets

    async with running_app(settings) as srv:
        with pytest.raises(websockets.exceptions.WebSocketException):
            async with ws_connect(srv.ws_url, "") as ws:
                await ws.recv()
        with pytest.raises(websockets.exceptions.WebSocketException):
            async with ws_connect(srv.ws_url, "garbage.token.value") as ws:
                await ws.recv()


async def test_ws_ping_pong(settings, token_factory) -> None:
    async with running_app(settings) as srv:
        async with ws_connect(srv.ws_url, token_factory("alice")) as ws:
            await ws_recv(ws, "hello")
            await ws.send(json.dumps({"type": "ping", "ref": "p1"}))
            pong = await ws_recv(ws, "pong")
            assert pong["ref"] == "p1"


async def test_ws_reports_error_for_unknown_event(settings, token_factory) -> None:
    async with running_app(settings) as srv:
        async with ws_connect(srv.ws_url, token_factory("alice")) as ws:
            await ws_recv(ws, "hello")
            await ws.send(json.dumps({"type": "nonsense", "ref": "x"}))
            err = await ws_recv(ws, "error")
            assert err["data"]["code"] == "unknown_event"
