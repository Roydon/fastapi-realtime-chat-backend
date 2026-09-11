// k6 WebSocket load test: ramps to a target concurrency, has each virtual user send a
// message and wait for delivery, and reports p95 delivery latency.
//
//   k6 run tools/loadtest.js
//   k6 run -e BASE_URL=http://localhost:8080 -e TARGET_VUS=500 tools/loadtest.js
//
// Each VU authenticates as its own synthetic user (loadtest-<vu>-<iter>) and messages a
// fixed peer (loadtest-peer), so p95 reflects real send -> persist -> outbox -> Redis ->
// WebSocket round trips, not a warmed cache.

import ws from "k6/ws";
import http from "k6/http";
import { check } from "k6";
import { Trend, Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const WS_URL = BASE_URL.replace(/^http/, "ws");
const TARGET_VUS = Number(__ENV.TARGET_VUS || 500);
const HOLD_DURATION = __ENV.HOLD_DURATION || "60s";

const deliveryLatency = new Trend("chat_delivery_latency_ms", true);
const messagesSent = new Counter("chat_messages_sent");
const messagesDelivered = new Counter("chat_messages_delivered");

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
  },
};

function token(user) {
  const res = http.get(`${BASE_URL}/v1/demo/token?user=${user}`);
  check(res, { "got demo token": (r) => r.status === 200 });
  return res.json("access_token");
}

export default function () {
  const self = `loadtest-${__VU}`;
  const peer = "loadtest-peer";
  const myToken = token(self);

  const url = `${WS_URL}/v1/ws?token=${myToken}`;
  ws.connect(url, {}, (socket) => {
    let sentAt = 0;
    let clientMsgId = "";

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
      // Every VU also plays "peer" for others' messages implicitly via message.new,
      // but latency is only measured on our own sent message being fanned back to us
      // as the sender (the API always pushes to sender + recipient).
      socket.setTimeout(() => socket.close(), 5000);
    });

    socket.on("message", (data) => {
      const event = JSON.parse(data);
      if (event.type === "message.new" && event.data.client_msg_id === clientMsgId) {
        deliveryLatency.add(Date.now() - sentAt);
        messagesDelivered.add(1);
        socket.send(JSON.stringify({ type: "ack", message_id: event.data.id }));
      }
    });

    socket.on("error", () => {});
  });
}
