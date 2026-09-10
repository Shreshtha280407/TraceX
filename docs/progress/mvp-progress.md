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

## Later phases (not started)

Owned by other contributors, building on the frozen Phase 1 contracts:

- Source extractors (documents, CDR, financial, video, image, audio, chat) → `ObservationV1` producers.
- Entity resolution and merge review workflow.
- Neo4j graph projection.
- Cross-modal correlation, candidate scoring, hypothesis engine.
- Authentication, RBAC/ABAC enforcement.
- Merkle checkpointing and signatures.
- Frontend.
