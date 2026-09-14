# Runbook: LAN Development (Local Network Demo Setup)

## Phase 4 release-gate prerequisite

The Phase 4 release gate must first pass on one isolated local Compose project
with synthetic evidence only. A LAN deployment, GPU worker, or model asset is
not a substitute for that gate. Do not point LAN workers at PostgreSQL, Neo4j,
Redis, or MinIO; they publish only through the authenticated coordinator API.

For a small team (e.g. three laptops on the same Wi-Fi/LAN) that wants to demo or jointly test TraceX without every laptop running its own full stack. **This is a demo/development configuration, not a production deployment** — see the warning at the end.

## Topology

```text
┌─────────────────────────────┐        LAN (same Wi-Fi / switch)
│  Host laptop                │
│  - PostgreSQL   (local only)│◄──────────────┐
│  - Neo4j        (local only)│                │
│  - Redis        (local only)│                │
│  - MinIO        (local only)│                │
│  - API :8000    (LAN-visible)│◄──┐            │
└──────────────┬───────────────┘   │            │
               │ HTTP               │            │
       ┌───────┴───────┐    ┌──────┴──────┐  (host loopback only)
       │ Laptop 2       │    │ Laptop 3     │
       │ curl / browser │    │ curl / browser│
       └────────────────┘    └───────────────┘
```

**One designated host laptop runs the full stack** (`docker compose up --build`, or infra-in-Docker + API on host per `docs/runbooks/local-development.md`). **The other laptops only ever talk to the host's API port** over the LAN — never to PostgreSQL, Neo4j, Redis, or MinIO directly. Nothing in `compose.yaml` was changed to make this work: `postgres`/`neo4j`/`redis`/`minio` already bind their host ports to all interfaces via Docker's default port publishing, same as before this phase, but this runbook's instructions and firewall step below are what actually keep them LAN-inaccessible in practice (see the warning below about `0.0.0.0` bindings).

## Setup on the host laptop

1. Follow `docs/runbooks/local-development.md`'s first-time setup (`cp .env.example .env`, `uv sync --all-groups`).
2. Find the host's LAN IP address:
   ```bash
   # Linux
   ip addr show | grep "inet " | grep -v 127.0.0.1
   # macOS
   ipconfig getifaddr en0
   ```
   Note the address (e.g. `192.168.1.42`) — this is what the other laptops will use.
3. Start the stack:
   ```bash
   docker compose up --build
   ```
4. **Firewall: open the API port only.**
   ```bash
   # Linux (ufw)
   sudo ufw allow 8000/tcp comment "TraceX API - LAN demo only"
   # macOS: System Settings -> Network -> Firewall -> allow incoming for the process on :8000
   ```
   Do **not** open 5432 (PostgreSQL), 7687/7474 (Neo4j), 6379 (Redis), or 9000/9001 (MinIO) on the firewall. Docker Compose still binds those ports on the host by default (see the warning below) — the firewall rule above is what actually prevents LAN access to them, since the host's OS firewall sits in front of Docker's port publishing for traffic arriving from other machines.

## Accessing the API from another laptop on the same LAN

```bash
curl http://192.168.1.42:8000/healthz
curl http://192.168.1.42:8000/readyz
curl -X POST http://192.168.1.42:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"teammate@example.test","password":"a-real-password-here","display_name":"Teammate"}'
```

Replace `192.168.1.42` with the host's actual LAN IP. `/healthz` should return `200` immediately; `/readyz` returns `503` until PostgreSQL/Neo4j/Redis/MinIO all report healthy on the host (same behavior as local development, just reached over the LAN instead of `localhost`).

## What must never be shared over chat, Slack, email, or committed to Git

- The host's `.env` file (contains `POSTGRES_PASSWORD`, `AUTH_JWT_SECRET`, `NEO4J_PASSWORD`, `MINIO_*` credentials).
- The value of `AUTH_JWT_SECRET` specifically — anyone with it can forge access tokens for any user.
- Database or MinIO passwords, in any form (screenshot, pasted text, etc).

If a teammate needs to run their own host instead of connecting to yours, they run their own `cp .env.example .env` and get their own locally-generated dev-only values — never a copy of yours.

## Firewall / exposure reminder

Docker Compose's default port publishing (`"${POSTGRES_PORT:-5432}:5432"` etc. in `compose.yaml`) binds to `0.0.0.0` on the host, meaning PostgreSQL/Neo4j/Redis/MinIO are reachable from the LAN by IP:port **unless the host OS firewall blocks them** — Compose itself does not restrict this. This phase deliberately did not change `compose.yaml`'s port bindings (e.g. to `127.0.0.1:5432:5432`) to "fix" this, per the instruction to avoid broad changes to `compose.yaml` for LAN convenience — the firewall step above is the documented, minimal way to keep those services LAN-inaccessible with zero risk of breaking the existing single-host Docker networking that Nipun's/Shreshtha's/Jasraj's work already depends on. **If your host's firewall is disabled or misconfigured, your database/graph/cache/object-storage ports are exposed to your entire LAN.** Verify the firewall rule is active (`sudo ufw status` / check firewall settings) before treating this setup as safe even for a demo.

## Verifying LAN reachability

From a laptop other than the host:

```bash
curl -sf http://<host-lan-ip>:8000/healthz && echo "API reachable"
curl -s http://<host-lan-ip>:5432 --connect-timeout 2 || echo "postgres correctly unreachable"
```

The second command should fail to connect (confirming the firewall is doing its job) — if it succeeds, stop and fix the firewall rule before continuing any demo.

## TLS-ready worker control plane

For a secure LAN deployment, terminate TLS at the API or a reverse proxy and
set `WORKER_SECURE_TRANSPORT_REQUIRED=true`. If TLS terminates at a proxy, set
`WORKER_TRUSTED_PROXY_IPS` only to that proxy's immediate IP; the proxy must
overwrite `X-Forwarded-Proto`. Certificates and private keys are operator
managed and must never be committed. Local demo HTTP requires the explicit
`false` setting and is unsuitable for sensitive evidence.

Workers use only the API and must never receive PostgreSQL, Neo4j, Redis,
MinIO, object-store, or object URI credentials.

Before any LAN Phase 4 demonstration, first complete the dedicated local
Compose release gate in `docs/architecture/phase-4-integration-release-gate.md`.
Its synthetic-only flow validates lease/replay and graph-outbox behavior before
any worker connects over the LAN; it does not authorize real evidence use.

## This is not production deployment

This setup has no TLS (plain HTTP over the LAN), no reverse proxy, no production secret manager, and dev-only credentials in a plaintext `.env` file on one laptop. It is suitable only for a trusted, temporary, same-room/same-Wi-Fi development or demo session — never for anything internet-facing, never for real evidentiary data, and never left running unattended on a shared network.
