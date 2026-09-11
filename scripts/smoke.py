"""Manual cross-replica smoke test against a running stack (`make up` first).

Alice connects through replica 1, Bob through replica 2. Alice sends a message; we assert
Bob receives `message.new` over his socket and Alice gets `message.delivered` after Bob acks.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
import uuid

import websockets

BASE = "http://localhost:8080"


def token(user: str) -> str:
    with urllib.request.urlopen(f"{BASE}/v1/demo/token?user={user}", timeout=5) as resp:
        return json.load(resp)["access_token"]


async def recv_type(ws: websockets.WebSocketClientProtocol, wanted: str, timeout: float = 5.0):
    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await ws.recv())
            if event.get("type") == wanted:
                return event


async def main() -> int:
    alice, bob = token("alice"), token("bob")
    async with (
        websockets.connect(f"ws://localhost:8080/r1/v1/ws?token={alice}") as a_ws,
        websockets.connect(f"ws://localhost:8080/r2/v1/ws?token={bob}") as b_ws,
    ):
        await recv_type(a_ws, "hello")
        hello_b = await recv_type(b_ws, "hello")
        print(f"alice on r1, bob on {hello_b['data']['replica']}")

        body = {
            "recipient_id": "bob",
            "body": "hello from the smoke test \U0001f44b",
            "client_msg_id": str(uuid.uuid4()),
        }
        req = urllib.request.Request(
            f"{BASE}/v1/messages",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {alice}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            sent = json.load(resp)
        print(f"sent {sent['id']} status={sent['status']}")

        got = await recv_type(b_ws, "message.new")
        assert got["data"]["id"] == sent["id"], "bob did not receive the message"
        print("bob received message.new")

        await b_ws.send(json.dumps({"type": "ack", "message_id": sent["id"]}))
        delivered = await recv_type(a_ws, "message.delivered")
        assert delivered["data"]["message_id"] == sent["id"]
        print("alice received message.delivered -> cross-replica fan-out OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
