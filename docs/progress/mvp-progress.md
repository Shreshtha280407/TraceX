# MVP Progress

Team: Nipun (Phase 1 — core foundation), Shreshtha, Aditya, Jasraj, Gaurav, Sarthak (later-phase modules building on the Phase 1 contracts).

## Phase 1 — Nipun core foundation: Complete

All acceptance criteria below are verified as of 2026-09-10 — see `docs/qa/test-results.md` for actual command output, including the full-stack `docker compose up --build` run with all five services healthy and all three endpoints hit live.

### Delivered

- [x] `pyproject.toml` / `uv.lock` — Python 3.12, `uv`-managed, Ruff + mypy configured.
- [x] FastAPI app (`app/main.py`) with `/healthz`, `/readyz`, `/api/v1/meta/contracts`.
- [x] Frozen Phase 1 contracts: `EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, `WorkerJobV1`, `WorkerResultV1`, `WorkerProgressV1` (`app/contracts/`).
- [x] Deterministic ID helper (`app/core/ids.py`) and canonical serialization (`app/core/canonical.py`).
- [x] Typed configuration (`app/core/config.py`) — fails fast on missing/invalid required settings.
- [x] Safe error envelope + request-correlation-ID middleware (`app/core/errors.py`).
- [x] `compose.yaml` + `Dockerfile` — api/postgres/neo4j/redis/minio, named volumes, health checks.
- [x] Alembic baseline (`migrations/`), no domain tables.
- [x] Contract, unit, security, e2e, and self-skipping integration tests (`tests/`).
- [x] `.github/workflows/ci.yml` running the required verification commands.
- [x] Documentation set: README, CLAUDE.md, `docs/architecture/*`, `docs/qa/*`, this file, `docs/runbooks/local-development.md`.

- [x] Full-stack `docker compose up --build` verified: all five services reached `healthy`/`running`, and `/healthz`, `/readyz`, `/api/v1/meta/contracts` all responded correctly against the live stack. `/readyz` reported all four real dependencies `"ok"`.

### Outstanding for team review

- [ ] Open questions in `docs/architecture/phase-1-decisions.md` (taxonomy ownership, classification/RBAC levels, `DerivedArtifact` shape) — not blockers for building against the frozen contracts, but worth a team pass.

## Shreshtha Phase 1 — Graph Foundation and Taxonomy: In progress

Verification is complete as of 2026-09-10 (see `docs/qa/test-results.md` for full command output, including live projection/query/schema/isolation tests run against the Compose Neo4j instance). Marked **in progress**, not complete, pending the team-review items below.

### Delivered

- [x] `docs/architecture/graph-taxonomy-v1.md` — proposed node/relationship taxonomy, recommended entity/event type values, identity-safety rules. Does not modify any frozen `V1` contract.
- [x] `app/modules/graph/` — `errors.py`, `models.py`, `schema.py`, `repository.py`, `projection.py`, `queries.py`. Idempotent, case-scoped, allow-listed-property projection of `EvidenceRecordV1`/`ObservationV1`/`EntityV1`/`EventV1`; safe, bounded, case-scoped read queries; explicit (never automatic) Neo4j schema management via `uv run python -m app.modules.graph.schema apply|verify`.
- [x] `docs/architecture/neo4j-graph-foundation.md`, `docs/decisions/ADR-001-graph-projection-and-case-isolation.md` — module structure, property-storage policy, and the design decisions behind case isolation, dependency ordering/deferral, and the deferred `SUPPORTS` relationship.
- [x] `tests/unit/graph/`, `tests/integration/graph/` (self-skipping without live Neo4j, same pattern as `tests/integration/test_readiness_live.py`), `tests/fixtures/graph/` — all 12 required unit-test scenarios and all 6 required integration-test scenarios from the task brief, verified passing both self-skipped and live against the Compose Neo4j container.
- [x] QA entries `GRAPH-SCHEMA-001`, `GRAPH-PROJECTION-001`, `GRAPH-PROJECTION-002`, `GRAPH-ISOLATION-001`, `GRAPH-PROVENANCE-001`, `GRAPH-QUERY-001`, `GRAPH-INTEGRATION-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/runbooks/local-development.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md` updated.

### Outstanding for team review

- [ ] `SUPPORTS` (`Observation` -> `Event`) is schema-defined but not populated — neither `ObservationV1` nor `EventV1` carries the observation-to-event link needed (ADR-001, Decision 4). Needs a decision once a later phase builds events from observations.
- [ ] The "entities must be projected before the events that reference them" ordering constraint (ADR-001, Decision 3) needs confirming against whatever later-phase orchestrator actually calls this projection code.
- [ ] No API endpoint, worker, or orchestrator calls `app/modules/graph/projection.py` yet — there is no ingestion pipeline to call it from in this phase.

## Later phases (not started)

Owned by other contributors, building on the frozen Phase 1 contracts and the graph foundation above:

- Source extractors (documents, CDR, financial, video, image, audio, chat) → `ObservationV1` producers.
- Entity resolution and merge review workflow; candidate identity links.
- Cross-modal correlation, candidate scoring, hypothesis engine.
- Graph analytics (centrality, community detection, motifs).
- Authentication, RBAC/ABAC enforcement.
- Merkle checkpointing and signatures.
- Frontend.
