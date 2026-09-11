# TraceX

TraceX is an evidence-first criminal-network intelligence backend built for SIH 2026 (Problem Statement 26189). It ingests heterogeneous investigative material — documents, CDRs, financial records, surveillance video, images, audio, and social/chat exports — turns it into provenance-rich observations, and assembles a temporal, auditable graph of entities and events that investigators can review without losing sight of the underlying evidence.

## Scope

Phase 1 (**Foundation and Frozen Contracts**) delivered the backend skeleton:

- A minimal FastAPI service with health, readiness, and contract-metadata endpoints.
- Independently startable local infrastructure (PostgreSQL, Neo4j, Redis, MinIO) via Docker Compose.
- Versioned, frozen shared contracts (`EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, worker job/result envelopes) that every later module builds against.
- Deterministic ID and canonical serialization helpers.
- Authentication and case-scoped RBAC/ABAC (`app/modules/access_control/`).
- Document/structured-data, audio/social/chat, and video/image processing foundations (`structured_processing`/`communication_processing`/`media_processing`) — deterministic extraction logic, not yet wired to a real ingestion pipeline.
- Graph projection and case-scoped query foundation (`app/modules/graph/`) — built; wired to a real, durable pipeline as of Phase 2.5 (below) for `Evidence`/`Observation`/`EntityMention` specifically.

Phase 2 (`app/modules/evidence_lifecycle/`) adds the real, case-scoped **evidence lifecycle**: authenticated upload → streamed SHA-256 hashing → private MinIO storage → immutable PostgreSQL metadata → a durable `WorkerJobV1` job record → best-effort Redis dispatch. See `docs/architecture/evidence-lifecycle.md`.

Phase 2.1 (in progress, same module) adds the **worker claim and result-submission** half of the lifecycle: a fail-closed internal API for a future worker to claim exactly one queued job (via `FOR UPDATE SKIP LOCKED` + a one-time claim token) and durably submit its `WorkerResultV1`/`ObservationV1`s, atomically transitioning the job to a terminal state. See `docs/architecture/worker-job-lifecycle.md`.

Jasraj's Phase 2 structured-processing worker (`app/modules/structured_processing/worker.py`) is the first real consumer of that internal API: a one-shot CLI (`uv run python -m app.modules.structured_processing.worker --once`) that claims a compatible job, streams its evidence through Phase 2.2's claim-token-bound worker-input endpoint, parses it (FIR/document text, CDR, financial, generic tabular/JSON), and submits a canonical `WorkerResultV1` — still not a daemon. See `docs/architecture/structured-processing-worker.md`.

Phase 2.2 (Nipun, same module) adds the **worker evidence-delivery** endpoint that closes the gap the paragraph above used to defer on: `GET /api/v1/internal/worker-jobs/{job_id}/input` streams a claimed job's evidence bytes through the API itself — never an object key, bucket, MinIO endpoint, or credential — scoped to the exact job and claim token a worker holds. See `docs/architecture/evidence-lifecycle.md`'s "Worker evidence-delivery" section.

Phase 2.3 (Nipun, same module) adds two explicit evidence source types, `structured_tabular` (CSV/XLSX) and `structured_json` (JSON), for general structured data that isn't specifically a CDR or financial export — routed deterministically to Jasraj's existing `generic_tabular_v1`/`generic_json_v1` fallback profiles, which a real upload could not reach before this. An additive, backward-compatible `SourceType` extension — every existing source type is unchanged. See `docs/architecture/evidence-lifecycle.md`'s routing table.

Aditya's Phase 2 worker-identity hardening (`app/modules/access_control/worker_credentials.py`) replaces the temporary shared secret every earlier worker-integration phase above used with revocable, per-worker service identities: a trusted-operator-only CLI provisions/rotates/revokes a `WorkerCredentialRecord` per worker (digest-only storage, plaintext token shown once), `/claim` now enforces per-worker processor scoping, and `/result`/`/input` now require the caller to *be* the worker identity currently bound to that job, not just hold its claim token. Closes the case-access-denial audit gap flagged since Phase 2 along the way. See `docs/architecture/worker-identity-and-security.md`.

Sarthak's Phase 2 communication-processing worker (`app/modules/communication_processing/worker.py`) is a second real consumer of the internal worker API, following Jasraj's structured-processing worker's exact pattern: a one-shot CLI (`uv run python -m app.modules.communication_processing.worker --once`) that claims a compatible job, streams its evidence through the same claim-token-bound, SHA-256-verified worker-input endpoint, parses it (WAV technical metadata, externally-produced transcript/diarization interchange, WhatsApp/Telegram/Instagram/generic-JSON social exports), and submits a canonical `WorkerResultV1` — still not a daemon, and still authenticated via Aditya's per-worker-credential system with no new authentication path. All seven of its processor profiles are now reachable through a real evidence upload (a follow-up routing fix — see below); originally only two were. See `docs/architecture/communication-processing-worker.md`.

Phase 2.5 (Shreshtha, `app/modules/graph/`) closes the "graph projection exists but nothing calls it" gap for `Evidence`/`Observation` specifically: every canonical `ObservationV1` a worker result accepts now durably enqueues a PostgreSQL-backed projection job (in the same transaction as the result itself), a bounded `uv run python -m app.modules.graph.worker --once` CLI claims and projects those jobs into a case-scoped Neo4j graph with safe retry/lease semantics, and a new evidence-local `EntityMention` node (never a resolved entity) captures each observation's raw extracted-entity mentions. A new case-scoped, paginated read endpoint (`GET /api/v1/cases/{case_id}/graph/observations`) exposes the result safely. A Neo4j outage never discards an accepted worker result — PostgreSQL remains authoritative and Neo4j fully rebuildable. See `docs/architecture/graph-projection.md`.

A follow-up routing fix (Shreshtha, `evidence_lifecycle/routing.py`) closed the gap Sarthak's build above reported but explicitly did not fix itself (shared routing was outside that task's scope to change unilaterally): five new, additive `SourceType` values (`audio_transcript`, `audio_diarization`, `whatsapp_chat`, `telegram_chat`, `instagram_chat`) route real uploads to `transcript_import_v1`/`diarization_import_v1`/`whatsapp_export_v1`/`telegram_export_v1`/`instagram_export_v1`, verified live end to end (upload → the real `communication_processing` worker claim/input/result → a persisted `ObservationV1` → graph projection) for all five. See `docs/architecture/phase-2-decisions.md`.

Explicitly **not** implemented anywhere in this repository yet: a continuous worker daemon/consumer loop for any of the one-shot `--once` CLIs, real OCR/ASR, automatic entity resolution or identity merging, cross-modal correlation, a human-review workflow, Merkle checkpointing or signatures, case CRUD, worker login/logout or OAuth/OIDC/SAML/mTLS, MFA/SSO, or any frontend. See `CLAUDE.md`, `docs/architecture/phase-1-decisions.md`, `docs/architecture/phase-2-decisions.md`, `docs/architecture/worker-job-lifecycle.md`, `docs/architecture/worker-identity-and-security.md`, `docs/architecture/graph-projection.md`, and `docs/qa/known-limitations.md` for the full non-goal list.

**No sensitive or real production evidence is used anywhere in this repository.** All fixtures and test data are synthetic (see `docs/qa/test-data.md`).

## Prerequisites

- Git
- Docker and Docker Compose (Compose v2, i.e. `docker compose`, not `docker-compose`)
- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

This project uses `uv` exclusively for dependency management — never `pip` directly.

## Setup

```bash
git clone <repo-url>
cd TraceX
cp .env.example .env      # edit if you need non-default local values
uv sync --all-groups
```

## Running the API

Full stack (API + all infrastructure, in Docker):

```bash
docker compose up --build
```

Infrastructure only (run the API on your host for faster iteration):

```bash
docker compose up -d postgres neo4j redis minio
uv run uvicorn app.main:app --reload
```

Once running, the API is available at:

- `GET http://localhost:8000/healthz` — liveness
- `GET http://localhost:8000/readyz` — readiness (checks PostgreSQL, Neo4j, Redis, MinIO)
- `GET http://localhost:8000/api/v1/meta/contracts` — supported contract versions
- `POST/GET http://localhost:8000/api/v1/cases/{case_id}/evidence`, `GET .../evidence/{evidence_id}`, `GET .../jobs/{job_id}` — case-scoped evidence upload/status (see `docs/architecture/evidence-lifecycle.md`)
- `POST http://localhost:8000/api/v1/internal/worker-jobs/claim`, `GET .../worker-jobs/{job_id}/input`, `POST .../worker-jobs/{job_id}/result` — internal worker claim/input-stream/result endpoints, gated by a per-worker `WORKER_TOKEN` credential + a per-job claim token (see `docs/architecture/worker-identity-and-security.md`, `docs/architecture/worker-job-lifecycle.md`, `docs/architecture/evidence-lifecycle.md`)
- `GET http://localhost:8000/api/v1/cases/{case_id}/graph/observations` — safe, paginated, case-scoped projected observation graph (see `docs/architecture/graph-projection.md`)
- `GET http://localhost:8000/docs` — interactive OpenAPI docs

Exposed development ports: API `8000`, PostgreSQL `5432`, Neo4j `7474` (HTTP browser) / `7687` (Bolt), Redis `6379`, MinIO `9000` (API) / `9001` (console).

## Development commands

```bash
uv sync --all-groups          # install runtime + dev dependencies
uv run ruff format --check .  # formatting check
uv run ruff check .           # lint
uv run mypy app                # type-check the app package
uv run pytest                 # run the test suite
docker compose config         # validate compose syntax
```

## Repository layout

- `app/` — FastAPI application: API routes, core infra (config, errors, IDs, canonical serialization), versioned contracts, and `app/modules/` (access control, evidence lifecycle, graph, structured/communication/media processing).
- `tests/` — unit, contract, integration, security, and e2e tests, plus shared fixtures.
- `docs/` — architecture decisions, contract reference, QA tracking, progress log, and runbooks.
- `migrations/` — Alembic baseline plus additive domain migrations (`access_control`, `evidence_lifecycle`, worker job claim/result).
- `compose.yaml`, `Dockerfile` — local reproducible infrastructure.

## Contributing

See `CLAUDE.md` for the rules this repository is built and maintained under (frozen-contract policy, module boundaries, required tests/docs per change).
