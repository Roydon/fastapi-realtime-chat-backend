"""Prometheus metrics. Module-level so several app instances in one process share them."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

WS_CONNECTIONS = Gauge("chat_ws_connections", "Open WebSocket connections on this replica")
MESSAGES_SENT = Counter("chat_messages_sent_total", "New messages accepted")
MESSAGES_REPLAYED = Counter(
    "chat_messages_idempotent_replays_total", "Sends answered from an existing message"
)
DELIVERY_LATENCY = Histogram(
    "chat_delivery_latency_seconds",
    "Time from message creation to push on the recipient's WebSocket",
    buckets=LATENCY_BUCKETS,
)
EVENTS_PUSHED = Counter("chat_ws_events_pushed_total", "Events written to WebSockets", ["type"])
OUTBOX_BACKLOG = Gauge("chat_outbox_backlog", "Outbox rows not yet published to Redis")
OUTBOX_PUBLISHED = Counter("chat_outbox_published_total", "Outbox rows published to Redis")
OUTBOX_PUBLISH_ERRORS = Counter("chat_outbox_publish_errors_total", "Failed relay cycles")
HTTP_REQUESTS = Counter(
    "chat_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "chat_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
