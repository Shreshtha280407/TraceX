# TraceX

TraceX is an evidence-first criminal-network intelligence backend built for SIH 2026 (Problem Statement 26189). It ingests heterogeneous investigative material — documents, CDRs, financial records, surveillance video, images, audio, and social/chat exports — turns it into provenance-rich observations, and assembles a temporal, auditable graph of entities and events that investigators can review without losing sight of the underlying evidence.

## Phase 1 scope

This repository is currently at **Phase 1: Foundation and Frozen Contracts**. Phase 1 is backend-only and deliberately narrow:

- A minimal FastAPI service with health, readiness, and contract-metadata endpoints.
- Independently startable local infrastructure (PostgreSQL, Neo4j, Redis, MinIO) via Docker Compose.
- Versioned, frozen shared contracts (`EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, worker job/result envelopes) that every later module builds against.
- Deterministic ID and canonical serialization helpers.
- Contract, configuration, and startup smoke tests.

Phase 1 explicitly does **not** implement: source extractors (OCR/ASR/video/CDR/financial parsers), entity resolution, graph projection logic, authentication or RBAC/ABAC enforcement, Merkle checkpointing or signatures, ML models, a full ingestion pipeline, or any frontend. See `docs/architecture/phase-1-decisions.md` and `CLAUDE.md` for the full non-goal list.

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

- `app/` — FastAPI application: API routes, core infra (config, errors, IDs, canonical serialization), and versioned contracts.
- `tests/` — unit, contract, integration, security, and e2e tests, plus shared fixtures.
- `docs/` — architecture decisions, contract reference, QA tracking, progress log, and runbooks.
- `migrations/` — Alembic baseline (no domain tables in Phase 1).
- `compose.yaml`, `Dockerfile` — local reproducible infrastructure.

## Contributing

See `CLAUDE.md` for the rules this repository is built and maintained under (frozen-contract policy, module boundaries, required tests/docs per change).
