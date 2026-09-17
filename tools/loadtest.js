// k6 WebSocket load test: ramps to a target concurrency, has each virtual user send a
// message and wait for it to be delivered back over its own socket, and reports p95
// delivery latency.
//
//   k6 run tools/loadtest.js
//   k6 run -e BASE_URL=http://localhost:8080 -e TARGET_VUS=500 tools/loadtest.js
//
// Each VU authenticates as its own synthetic user (loadtest-<vu>) and messages a fixed
// peer, so p95 reflects real send -> persist -> outbox -> Redis -> WebSocket round trips,
// not a warmed cache. Every VU/peer pair is its own conversation.
//
// Tokens are minted here with the shared HS256 secret rather than fetched from
// /v1/demo/token: that endpoint only issues tokens for the two demo users, so it cannot
// represent N distinct concurrent users. JWT_SECRET must match the server's.

import ws from "k6/ws";
import http from "k6/http";
import crypto from "k6/crypto";
import encoding from "k6/encoding";
import { check } from "k6";
import { Trend, Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const WS_URL = BASE_URL.replace(/^http/, "ws");
const TARGET_VUS = Number(__ENV.TARGET_VUS || 500);
const HOLD_DURATION = __ENV.HOLD_DURATION || "60s";
const JWT_SECRET = __ENV.JWT_SECRET || "local-dev-only-secret-change-me-0123456789abcdef";

const deliveryLatency = new Trend("chat_delivery_latency_ms", true);
const messagesSent = new Counter("chat_messages_sent");
const messagesDelivered = new Counter("chat_messages_delivered");
const wsErrors = new Counter("chat_ws_errors");

export const options = {
  scenarios: {
    ramping: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "20s", target: Math.min(50, TARGET_VUS) },
        { duration: "30s", target: Math.min(200, TARGET_VUS) },
        { duration: "40s", target: TARGET_VUS },
        { duration: HOLD_DURATION, target: TARGET_VUS },
        { duration: "15s", target: 0 },
      ],
    },
  },
  thresholds: {
    chat_delivery_latency_ms: ["p(95)<1000"],
    ws_connecting: ["p(95)<1000"],
    // A run where sends fail or sockets error is not a valid measurement, so fail loudly
    // instead of reporting a latency computed from a handful of lucky iterations.
    checks: ["rate>0.99"],
    chat_ws_errors: ["count<10"],
  },
};

function mintToken(sub) {
  const header = encoding.b64encode(JSON.stringify({ alg: "HS256", typ: "JWT" }), "rawurl");
  const now = Math.floor(Date.now() / 1000);
  const claims = { sub: sub, iat: now, exp: now + 3600 };
  const payload = encoding.b64encode(JSON.stringify(claims), "rawurl");
  const signingInput = `${header}.${payload}`;
  const signature = crypto.hmac("sha256", JWT_SECRET, signingInput, "base64rawurl");
  return `${signingInput}.${signature}`;
}

export default function () {
  const self = `loadtest-${__VU}`;
  const peer = "loadtest-peer";
  const myToken = mintToken(self);

  const url = `${WS_URL}/v1/ws?token=${myToken}`;
  ws.connect(url, {}, (socket) => {
    let sentAt = 0;
    let clientMsgId = "";
    let closing = false;

    socket.on("open", () => {
      socket.setTimeout(() => {
        clientMsgId = `${self}-${__ITER}-${Date.now()}`;
        sentAt = Date.now();
        const res = http.post(
          `${BASE_URL}/v1/messages`,
          JSON.stringify({ recipient_id: peer, body: "load test ping", client_msg_id: clientMsgId }),
          { headers: { Authorization: `Bearer ${myToken}`, "Content-Type": "application/json" } }
        );
        check(res, { "send accepted": (r) => r.status === 201 || r.status === 200 });
        messagesSent.add(1);
      }, 50);
      // Latency is measured on our own sent message being fanned back to us as the
      // sender (the API pushes to sender + recipient), so one socket covers the round trip.
      socket.setTimeout(() => {
        closing = true;
        socket.close();
      }, 5000);
    });

    socket.on("message", (data) => {
      const event = JSON.parse(data);
      if (event.type === "message.new" && event.data.client_msg_id === clientMsgId) {
        deliveryLatency.add(Date.now() - sentAt);
        messagesDelivered.add(1);
        // Acking after the close timeout fired is a race in this script, not a server
        // problem, so skip it rather than manufacture an error.
        if (!closing) {
          socket.send(JSON.stringify({ type: "ack", message_id: event.data.id }));
        }
      }
    });

    // Count errors rather than swallowing them: a silent handler here previously hid the
    // fact that every socket was failing auth and the run measured nothing.
    socket.on("error", (e) => {
      const msg = e && e.error ? e.error() : String(e);
      if (closing && msg.indexOf("close sent") !== -1) return;
      wsErrors.add(1);
      console.error(`ws error: ${msg}`);
    });
  });
}
