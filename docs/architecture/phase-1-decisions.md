# Phase 1 Decisions

Record of the concrete choices made while building the Phase 1 foundation, and the reasoning behind each — so later contributors know what was deliberate versus what's still open.

## Technology choices

All choices follow the technology decisions specified for Phase 1 (Python 3.12 / `uv` / FastAPI / Pydantic v2 / pydantic-settings / PostgreSQL / Neo4j / Redis / MinIO / Docker Compose / pytest + pytest-asyncio + httpx / Ruff / mypy / Alembic). No deviations were needed — the repository was empty at the start of Phase 1 (only `LICENSE` and a stub `README.md` existed), so there were no pre-existing conflicting choices to reconcile.

## `uv` project layout

- Dependencies are declared directly in `pyproject.toml` under `[project.dependencies]` (runtime) and `[dependency-groups.dev]` (dev). `uv.lock` is committed.
- `python-dotenv` is pinned as an explicit dev dependency (it was already present transitively via `pydantic-settings`) because `tests/integration/test_readiness_live.py` imports it directly — direct imports get direct declarations rather than relying on another package's transitive dependency staying stable.

## Configuration: `Settings` fails fast, not cached

`app/core/config.get_settings()` builds a fresh `Settings()` on every call rather than caching it at module scope. This keeps configuration testable (tests can freely construct settings against different environments without cross-test cache pollution) at the cost of re-parsing environment variables per call — a non-issue since it's pure in-memory parsing with no I/O, and the only per-request use is `/readyz`.

`Settings` fields have no development fallback for required infrastructure (`postgres_dsn`, `neo4j_uri`, `neo4j_username`, `neo4j_password`, `redis_url`, `minio_endpoint`, `minio_access_key`, `minio_secret_key`) — a misconfigured deployment must fail loudly at startup. `.env.example` documents every required variable with safe placeholders.

## `attributes` / `stable_identifiers` use `dict[str, JsonValue]`, not `dict[str, Any]`

Pydantic v2's `JsonValue` type (a recursive union of JSON-representable types) gives contracts genuine extension-friendliness — arbitrary, evolvable attribute bags — without resorting to unconstrained `Any`, keeping `mypy --strict` meaningful on `app/contracts/`.

## `ObservationV1.extracted_entities` holds raw mentions, not `EntityV1` references

An observation is produced by extraction alone, before any entity resolution has happened (entity resolution is explicitly later-phase work). `extracted_entities` is therefore a list of `ExtractedEntityMention` (`text`, `entity_type_hint`, `attributes`) — what was literally seen in the source — not a list of `EntityV1` UUIDs. The relationship is inverted on the entity side instead: `EntityV1.created_from_observation_ids` points back at the observations an entity was resolved from. This keeps the evidence-first direction (evidence → observation → entity) intact and makes it impossible to construct an `EntityV1` that doesn't trace back to at least one observation (`created_from_observation_ids` has `min_length=1`).

## `EventV1` requires `event_time` or `time_window`

This operationalizes, as a validator rather than only as prose, the rule that events are time-bounded occurrences and TraceX never models a timeless direct entity-to-entity edge as a substitute for one. `EventV1.evidence_refs` similarly requires at least one entry: every event must cite the evidence it was derived from.

## `EvidenceRecordV1` has no separate `created_at`

The general contract rule requires `schema_version`, `case_id`, and `created_at` "where appropriate." For `EvidenceRecordV1`, `uploaded_at` already is the record's creation timestamp — the record's existence and its upload are the same event — so a second, always-identical `created_at` field would be redundant. Every other Phase 1 contract (`ObservationV1`, `EntityV1`, `EventV1`, and the worker envelopes where relevant) has a distinct creation moment from any other timestamp it carries, so they all carry an explicit `created_at`.

## `entity_type` / `event_type` / `observation_type` are plain strings

These are deliberately *not* enums in Phase 1. The detailed entity/relationship taxonomy is owned by a later phase (Shreshtha); locking these fields to an enum now would force every contributor to edit a frozen contract just to add a taxonomy value. `SourceType` (evidence/worker modality) *is* an enum, with an explicit `OTHER` escape hatch, because the modality list (document/CDR/financial/video/image/audio/chat) is fixed by the problem statement itself, not by a future taxonomy design.

## Readiness checks are dependency-injected, not hardcoded

`app/dependencies/services.py` exposes `get_health_checks` as a FastAPI dependency returning a `dict[str, Callable[[], Awaitable[None]]]`. `GET /readyz` calls whatever it's handed. Tests override `get_health_checks` via `app.dependency_overrides` to simulate healthy/unhealthy infrastructure without touching real services — this is what makes scenarios 13/14 (`/readyz` 200 vs 503) unit-testable without Docker.

## `/readyz` never echoes exception detail

`_probe()` in `app/api/health.py` catches any exception from a dependency check, logs only the dependency name and exception *type* (not its message) via `structlog`, and returns a bare boolean to the caller. The HTTP response body only ever contains `{"postgres": "ok" | "unavailable", ...}` — never a driver's exception text, which for some drivers can itself contain connection strings. See `tests/security/test_no_secret_leakage.py`.

## `check_postgres` uses SQLAlchemy's async engine, not raw `asyncpg`

`POSTGRES_DSN` uses the SQLAlchemy-style `postgresql+asyncpg://` scheme everywhere in this codebase (config, Alembic). Raw `asyncpg.connect()` doesn't understand the `+asyncpg` driver suffix and rejects that DSN outright — this was caught only once the full Docker Compose stack was actually run (`ClientConfigurationError`; every unit/contract test mocks this check by design, so it couldn't surface there). `check_postgres` now opens a `create_async_engine(...)` connection and runs `SELECT 1` through it, matching the DSN format used by every other consumer.

## `neo4j` image pinned to `5.25-community`, not the floating `5-community` tag

`neo4j:5-community` currently resolves to a build (5.26.30) whose container entrypoint crash-loops on this host — reproducible with a bare `docker run neo4j:5-community` outside Compose entirely, so it's an upstream image issue, not a `compose.yaml` misconfiguration. `5.25-community` starts and passes its health check cleanly. Revisit this pin if a later `5-community` patch fixes the entrypoint.

## MinIO health check runs in a thread

`minio-py` is a synchronous client with no async variant. `check_minio` offloads `client.bucket_exists(...)` to `asyncio.to_thread` so a slow/unreachable MinIO doesn't block the event loop for other concurrent requests.

## Alembic baseline with no domain metadata

`migrations/env.py` sets `target_metadata = None` and reads the database URL from `app.core.config.get_settings()` at runtime rather than hardcoding it in `alembic.ini` — so no connection string is ever committed. A single empty baseline revision (`migrations/versions/3e8cbaa07711_baseline.py`) establishes Alembic's version tracking; it contains no schema operations, since Phase 1 defines no domain tables.

## Compose: host-facing `.env` vs. in-network `api` environment

`.env.example` defaults (`POSTGRES_DSN`, `NEO4J_URI`, `REDIS_URL`, `MINIO_ENDPOINT`) point at `localhost` with the host-exposed ports, matching the "infra-only + run the API on your host" workflow (`docker compose up -d postgres neo4j redis minio` + `uv run uvicorn ...`). The `api` service in `compose.yaml` explicitly overrides those same variables to use in-network service DNS names (`postgres`, `neo4j`, `redis`, `minio`) instead, so the same `.env` file's credential fragments (`POSTGRES_USER`, `POSTGRES_PASSWORD`, ...) work for both the containerized and host-run API without maintaining two separate env files.

## Open questions for team review

- Final entity/relationship taxonomy (owned by Shreshtha) will likely add fields to `EntityV1`/`EventV1` under a `V2` schema version once frozen.
- Evidence `classification` levels (`unclassified`/`restricted`/`confidential`/`secret`) are a placeholder label set; actual RBAC/ABAC policy per level is later-phase and unowned as of Phase 1.
- `DerivedArtifact` (worker byproducts like thumbnails/transcripts) is defined but has no consumer yet — first real use should confirm the field set is sufficient.
