# Phase 1 — case and job foundation

Phase 1 implements the control plane only. It preserves authorised upload bytes, stores their digest and metadata, queues a durable import job, publishes durable event records, and lets a worker verify the preserved source. Parsing, canonical facts, graph construction, findings, and ML remain out of scope until later phases.

## Components and boundaries

| Component | Behaviour |
| --- | --- |
| FastAPI control plane | Creates cases/memberships; accepts a case-scoped multipart upload and returns `202` with IDs immediately. |
| PostgreSQL control store | Stores users, cases, memberships, source metadata, jobs, leases, event sequence records, and audit records. |
| Local evidence vault | Streams bytes to a staging file while hashing, `fsync`s, then atomically moves to `case_id/sha256/original`. Existing hash targets are verified, never overwritten. |
| Worker | Claims/reclaims an expired lease and performs Phase 1 source hash/size verification only. It does not parse a file in the request handler or worker. |

The `X-TraceX-Actor` header is a local-development identity adapter so the case-isolation contract is testable. It is not production authentication; replace it with the approved identity provider before deployment. Case authorization is server-side on every case/job/event route, and non-members receive `404` rather than case existence disclosure.

## Local run

```bash
docker compose up -d postgres
uv sync --extra dev
TRACEX_DATABASE_URL=postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5432/tracex \
  uv run python -c 'from app.db import init_database; init_database()'
TRACEX_DATABASE_URL=postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5432/tracex \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
TRACEX_DATABASE_URL=postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5432/tracex \
  uv run python -m workers.runner --once
```

The default database URL points to that local PostgreSQL service. Set `TRACEX_EVIDENCE_ROOT` to a dedicated encrypted volume in deployment; it defaults to `var/evidence`. Use only filesystem paths configured by the server, never client-provided paths.

## API contract

| Route | Contract |
| --- | --- |
| `POST /v1/cases` | Creates a case and makes the caller its `case_lead`. |
| `POST /v1/cases/{case_id}/members` | Case lead adds/updates analyst or reviewer membership. |
| `POST /v1/cases/{case_id}/imports` | Multipart field `file` plus required `Idempotency-Key`; accepts `.csv`, `.json`, `.ndjson`, `.xml`; returns `202`. |
| `GET /v1/jobs/{job_id}` | Returns durable state, stage, exact byte/row counters, lease/retry fields, and errors. |
| `GET /v1/cases/{case_id}/events` | SSE replay after `Last-Event-ID` (or `after`); `follow=true` emits heartbeats. |
| `GET /v1/healthz` / `GET /v1/readyz` | Liveness checks database/vault; readiness additionally requires a fresh worker heartbeat. |

Job states are `queued -> running -> completed` in this phase; `running/checkpointed` jobs with expired leases are reclaimable. Failures are durable (`failed`) with an error code. No completion percentage is emitted: only durable state and exact counters.

## Exit evidence

`tests/unit/test_phase_one.py` creates Case A, uploads a tiny authorised CSV, verifies immutable bytes/hash, simulates a worker crash/reclaim, verifies completion after a new worker session, replays events, proves idempotency, and proves a Case B actor cannot see the job. Run `make phase-one`.
