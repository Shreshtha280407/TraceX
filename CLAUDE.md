# CLAUDE.md

Guidance for any AI agent (or human) working in this repository.

## Project purpose

TraceX is an evidence-first criminal-network intelligence backend (SIH 2026, PS 26189). It ingests documents, CDRs, financial records, surveillance video, images, audio, and social/chat exports; converts them into provenance-rich observations; builds a temporal Neo4j graph of entities and events; and preserves auditability and integrity throughout.

## Current phase: Phase 1 — Foundation and Frozen Contracts

This repository is backend-only in Phase 1. Do **not** implement, in this phase:

- Frontend of any kind.
- Real source extractors: OCR, ASR/transcription, video/image analysis, CDR/financial parsers, social/chat parsers.
- Entity resolution, merging, or deduplication logic.
- Neo4j graph projection logic, cross-modal correlation, candidate scoring, or the hypothesis engine.
- Authentication (login/logout), RBAC/ABAC enforcement.
- Case CRUD business workflows.
- Actual file uploads, MinIO evidence writes, or actual evidence hashing.
- Merkle roots, Ed25519 signatures, or any audit-chain implementation.
- Database migrations beyond the Alembic baseline.
- ML training/evaluation of any kind.

Interfaces and placeholders exist only where required to make the frozen contracts and startup foundation coherent — not as speculative scaffolding for future phases.

## `uv` and Docker rules

- Use `uv` for all dependency management. Never use `pip` directly.
- `uv sync --all-groups`, `uv run <tool>` for every command (ruff, mypy, pytest, alembic, uvicorn).
- Commit `uv.lock`. Regenerate it via `uv sync`/`uv add`, never hand-edit it.
- Infrastructure runs via Docker Compose (`compose.yaml`). Named volumes for PostgreSQL, Neo4j, and MinIO. One internal network. Health checks on every infra service.
- Never commit real secrets. `.env` is git-ignored; only `.env.example` (with safe placeholders) is committed.

## Shared-contract rule

`app/contracts/` holds the frozen Phase 1 interface (`EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, `WorkerJobV1`, `WorkerResultV1`, `WorkerProgressV1`) that every other module — ingestion, graph, correlation, hypothesis engine, frontend — builds against. Treat these as a versioned public API:

- Do not change a `V1` contract's required fields, types, or validation rules in a way that breaks existing valid payloads. Add a new version (`V2`) instead, and update `CONTRACT_VERSIONS` in `app/contracts/__init__.py` and `docs/architecture/contracts.md` accordingly.
- Any change to `app/contracts/`, `app/core/ids.py`, or `app/core/canonical.py` needs a corresponding update to `docs/architecture/contracts.md` and the relevant contract tests in `tests/contract/`.
- A future worker's only contract is `WorkerJobV1` in, `WorkerResultV1` out. Workers must not require direct PostgreSQL, Neo4j, or MinIO credentials.

## No automatic identity merge, guilt conclusion, or case-unscoped retrieval

These are hard boundaries for every phase of this project, not just Phase 1:

- Never silently merge two entities into one identity. Entity resolution is an explicit, reviewable, later-phase operation — not something inferred and auto-applied.
- Confidence scores (`extraction_confidence`, `EventV1.confidence`) describe extraction/statement quality only. Never present or treat them as a probability of guilt or culpability.
- Every retrieval, query, or aggregation must stay scoped to a single `case_id`. Do not build or suggest cross-case retrieval — case isolation is a core evidentiary-integrity requirement of this system.

## Module boundaries

- Do not edit another contributor's in-progress module without being asked. If a task requires touching code outside the area you were asked to work on, stop and report the conflict rather than resolving it unilaterally.
- `app/modules/` is reserved for later-phase feature modules; don't add real implementations there in Phase 1.

## Required tests and documentation per change

- New or changed contract fields/rules: add/update tests in `tests/contract/`, and update `docs/architecture/contracts.md` and `docs/qa/test-matrix.md`.
- New API endpoints: add tests in `tests/unit/` (mocked dependencies) and, where relevant, `tests/integration/` (real infra, self-skipping when infra isn't available).
- Config changes: update `.env.example` and `docs/runbooks/local-development.md`.
- Before calling any task done: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy app`, `uv run pytest`, and `docker compose config` must all pass. Don't report a test as passing unless you actually ran it.
- Update `docs/progress/mvp-progress.md` when a phase or milestone changes status.

## Git discipline

Do not commit, push, create a pull request, alter git history, or stage files unless the user explicitly asks for that specific action in that message. Preparing a diff is not the same as being asked to commit it.
