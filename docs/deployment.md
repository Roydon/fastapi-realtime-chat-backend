# Deployment

## Kubernetes

Manifests live in [`deploy/k8s/base`](../deploy/k8s/base) (kustomize). They assume:

* An existing Postgres 16 and Redis 7 you point `DATABASE_URL` / `REDIS_URL` at (RDS +
  ElastiCache, Cloud SQL + Memorystore, or in-cluster StatefulSets - not included here,
  since production topology and backup policy are yours to choose).
* An S3-compatible bucket for attachments (`S3_*` vars).
* Your real identity provider: set `AUTH_MODE=jwks`, `JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`.

```bash
kubectl create secret generic chat-api-secrets -n chat-backend \
  --from-literal=JWT_SECRET=... --from-literal=S3_ACCESS_KEY=... --from-literal=S3_SECRET_KEY=...
kubectl apply -k deploy/k8s/base
```

### Why this design needs no sticky sessions

A WebSocket connection is pinned to whichever pod accepted it, but the sender and
recipient can be on different pods (or different regions of an HPA-scaled fleet). Delivery
does not rely on the two sockets sharing a process:

1. A write commits the message and an outbox row in the same transaction.
2. Every pod's outbox relay publishes newly-committed rows to Redis pub/sub, on a channel
   per recipient user id.
3. Whichever pod holds that user's live socket (if any) is subscribed to their channel and
   pushes the event. No pod-to-pod calls, no session affinity, no shared in-memory state.

So the `chat-api` Service is a plain ClusterIP/round-robin, the Deployment has no
`sessionAffinity`, and the HPA can scale up or down without draining sockets to a specific
node first (the `preStop` sleep just lets in-flight sends finish).

### Scaling notes

* Increase `maxReplicas` in `hpa.yaml` and, if you scrape metrics, switch the HPA to
  `chat_ws_connections` from `/metrics` - CPU under-reacts to many idle-but-open sockets.
* `outbox_batch_size` / `outbox_poll_interval_seconds` trade relay latency for DB load;
  the defaults (200 rows / 0.5s poll, woken immediately on local commits) hold up to
  several thousand messages/sec per pod - see [README Results](../README.md#results).
* Redis is a single logical pub/sub bus. For very large fleets, shard by hashing the
  recipient id across several Redis instances (channel naming already supports this: the
  hub only needs to resolve `user:<id>` to the same shard on every pod).

## AWS reference architecture

```
Route53 -> ALB (TLS, WebSocket-aware) -> ECS Fargate service / EKS Deployment (this app)
                                              |            |            |
                                          RDS Postgres  ElastiCache  S3 bucket
                                          (Multi-AZ)      Redis      (attachments)
```

* **Compute**: ECS Fargate (simplest) or EKS with the manifests above. Either way, run
  the container as-is; `alembic upgrade head` runs as an ECS one-off task or the
  Kubernetes `initContainer` already in `deployment.yaml`.
* **Load balancer**: ALB in `HTTP2`/`WebSockets` mode, idle timeout raised to >= 3600s to
  match `proxy_read_timeout` in the local nginx config so long-lived sockets survive.
* **Database**: RDS for PostgreSQL 16, Multi-AZ. `messages` and `outbox` are append-heavy;
  size storage IOPS accordingly and enable automated backups.
* **Cache/bus**: ElastiCache for Redis, cluster mode disabled is fine at moderate scale
  (pub/sub, not data storage - AOF/RDB persistence is not required).
* **Attachments**: S3 with a bucket policy restricting `PutObject` to presigned requests
  (no public writes) and a lifecycle rule to expire abandoned uploads.
* **Secrets**: AWS Secrets Manager or SSM Parameter Store, injected via ECS task
  definition secrets or the External Secrets Operator on EKS - not hardcoded env vars.
* **Auth**: point `JWKS_URL` at your IdP (Cognito, Auth0, an internal SSO) - see
  [README "Integrating your existing auth"](../README.md#integrating-your-existing-auth).

## Environment variable reference

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | - | `postgresql+asyncpg://...` |
| `REDIS_URL` | - | `redis://...` |
| `AUTH_MODE` | `hs256` | `hs256` or `jwks` |
| `JWT_SECRET` | - | required when `AUTH_MODE=hs256`, >= 32 chars |
| `JWKS_URL` | - | required when `AUTH_MODE=jwks` |
| `JWT_ALGORITHMS` | `RS256` | comma-separated, jwks mode |
| `JWT_ISSUER` / `JWT_AUDIENCE` | unset | verified when set |
| `JWT_USER_CLAIM` | `sub` | claim holding the user id |
| `S3_ENDPOINT_URL` | unset | unset = real AWS S3 |
| `S3_PUBLIC_ENDPOINT_URL` | unset | host browsers use for presigned URLs, if different |
| `S3_BUCKET` / `S3_REGION` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | - | attachment storage |
| `ATTACHMENT_MAX_BYTES` | `5242880` | 5 MB |
| `ATTACHMENT_ALLOWED_MIME` | images + pdf | comma-separated |
| `MESSAGE_MAX_GRAPHEMES` | `4000` | counted in user-perceived characters |
| `OUTBOX_POLL_INTERVAL_SECONDS` / `OUTBOX_BATCH_SIZE` | `0.5` / `200` | relay tuning |
| `WS_HEARTBEAT_SECONDS` / `WS_IDLE_TIMEOUT_SECONDS` | `20` / `60` | WebSocket keepalive |
| `DEMO_ENABLED` | `false` | serves `/demo/` and `/v1/demo/token` - dev only |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | structured logging |
