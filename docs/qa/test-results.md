# Phase 1 Test Results

Actual command output from verification runs. Updated by whoever runs verification — do not hand-edit a "passing" result without having actually run the command.

## 2026-09-10 — Shreshtha — Graph Foundation and Taxonomy build

Environment: same sandbox as the Nipun build below, Python 3.12.13 (via `uv`), Docker 29.7.2. No changes to `pyproject.toml`/`uv.lock` were needed — `neo4j>=5.25.0` was already a declared Phase 1 dependency; `uv sync` resolved it to `6.3.0`. Confirmed `AsyncGraphDatabase`/`AsyncDriver`/`AsyncManagedTransaction` imports and behavior this module relies on are unchanged against that version.

```bash
$ uv sync --all-groups
Resolved 55 packages in 2ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
66 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 26 source files
```
Result: **pass**. (18 files at the start of this work → 26 after adding `app/modules/__init__.py` + the 7 files in `app/modules/graph/`.)

```bash
$ uv run pytest
140 passed, 8 skipped in 12.44s
```
Result: **pass**. The 8 skips are self-skipping live-infrastructure suites: 4 in `tests/integration/test_readiness_live.py` (pre-existing, Nipun's) and 4 in `tests/integration/graph/` (new), all requiring `docker compose up -d postgres neo4j redis minio` — confirmed for real below.

```bash
$ docker compose config
```
Result: **pass** (exit 0).

```bash
$ docker compose up -d --wait postgres neo4j redis minio
Container tracex-minio-1 Healthy
Container tracex-postgres-1 Healthy
Container tracex-neo4j-1 Healthy
Container tracex-redis-1 Healthy
```
Result: **pass**. All four required infra services reached `healthy`.

```bash
$ uv run python -m app.modules.graph.schema apply
Applied 11 schema statements:
  - case_case_id_unique
  - evidence_case_evidence_unique
  - observation_case_observation_unique
  - entity_case_entity_unique
  - event_case_event_unique
  - observation_case_id_idx
  - observation_event_time_idx
  - event_case_id_idx
  - event_event_time_idx
  - entity_case_id_idx
  - entity_entity_type_idx

$ uv run python -m app.modules.graph.schema verify
Present (11): [all 11 statement names]
All expected schema statements are present.
```
Result: **pass** (both, exit 0) — confirms the composite uniqueness constraints (Decision 2, ADR-001) are valid against the actual pinned `neo4j:5.25-community` Community Edition image, not just against documentation.

```bash
$ uv run pytest tests/integration/graph -q
....
4 passed in 0.74s
```
Result: **pass** — all 4 live graph integration tests passed for real (schema apply-twice, full-chain projection + provenance queries, repeated-projection-no-duplicates, cross-case isolation), not self-skipped. Confirmed via a direct post-test query (`MATCH (n) RETURN labels(n), count(n)`) that every test's `case_id`-scoped teardown left zero residual nodes.

```bash
$ uv run pytest   # re-run while the full stack was up
148 passed in 1.29s
```
Result: **pass**. All 8 previously-skipped tests (4 readiness, 4 graph integration) ran for real and passed.

Stack was stopped cleanly afterward: `docker compose down` (containers/network removed, named volumes preserved). Re-ran `uv run pytest` once more after teardown to confirm the suite returns to **140 passed, 8 skipped** (never fabricating a pass without real infra).

### Design decisions verified against real infrastructure, not just written down

- Composite (non-`NODE KEY`) uniqueness constraints, chosen specifically because `neo4j:5.25-community` is Community Edition (ADR-001, Decision 2) — verified applying and round-tripping successfully, twice, against that exact pinned image.
- Case isolation under a deliberately-collided `entity_id` across two cases (`tests/integration/graph/test_case_isolation.py`) — verified live, not just in the unit-level parameter-construction tests.
- Atomic dependency-deferral for `project_observation`/`project_event` (ADR-001, Decision 3) — verified in unit tests via a faked repository (`tests/unit/graph/test_projection.py`); the live suite only exercises the fully-satisfied path, since deliberately projecting an event before its entity is a unit-level concern.

## 2026-09-10 — Nipun — Phase 1 foundation build

Environment: local dev machine, Python 3.12.13 (via `uv`), Docker 29.4.1, Docker Compose v5.1.3.

```bash
$ uv sync --all-groups
Resolved 55 packages
Installed 54 packages
```
Result: **pass**.

```bash
$ uv run ruff format --check .
38 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 18 source files
```
Result: **pass**.

```bash
$ uv run pytest
80 passed, 4 skipped in 12.40s
```
Result: **pass**. The 4 skips are `tests/integration/test_readiness_live.py` — they require a running `docker compose up -d postgres neo4j redis minio` stack and self-skip per-dependency when it isn't up, by design (never fabricate a pass for infrastructure that isn't there).

```bash
$ docker compose config
```
Result: **pass** (exit 0; also covered by `tests/integration/test_compose_config.py`, included in the 80 passed above).

```bash
$ docker compose up --build -d
```
Result: **pass**. All five containers (`api`, `postgres`, `neo4j`, `redis`, `minio`) reached `Up`/`healthy` status.

```bash
$ curl http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}          # HTTP 200

$ curl http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}   # HTTP 200

$ curl http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}   # HTTP 200
```
Result: **pass** — all three endpoints verified against the live, fully-containerized stack.

```bash
$ uv run pytest   # re-run while the full stack was up
84 passed in 0.31s
```
Result: **pass**. The 4 tests in `tests/integration/test_readiness_live.py` that self-skip without infra (see the first `pytest` run above: 80 passed, 4 skipped) ran for real against the live containers and passed — confirming genuine connectivity, not just a mocked path.

Stack was stopped cleanly afterward: `docker compose down` (containers/network removed, named volumes preserved).

### Three real bugs caught by this run (all fixed before this record)

- **Worker idempotency key regex rejected valid semantic-version processor versions.** `IDEMPOTENCY_KEY_PATTERN` originally allowed `[A-Za-z0-9_-]+` per segment, which excludes `.` — so a realistic key like `"<case_id>:<evidence_id>:cdr-parser:1.0.0"` failed validation on its own version segment. Fixed to `[A-Za-z0-9_.-]+`. Caught by `tests/contract/test_worker.py::test_valid_worker_job_parses` and 2 related tests, before any container was built.
- **Route-introspection assumption broke against this Starlette version's routing internals.** An early version of `tests/unit/test_app_startup.py` walked `app.routes` and read `.path` directly; on the installed Starlette (1.6.0), `include_router` wraps routes in an internal `_IncludedRouter` with no flattened `.path` list, so the assertion failed even though the routes worked correctly over real HTTP. Rewritten to hit the routes through the ASGI test client instead of introspecting router internals — more robust and closer to what actually matters (the routes respond).
- **Dockerfile build failed: `uv sync --locked --no-dev` (the second, post-`COPY app` sync) tried to editable-install the `tracex` project itself, which needs `README.md` — never copied into the build context, so `docker compose up --build` failed outright.** The fix (not a workaround) is that this second sync step was unnecessary: the first `uv sync --locked --no-install-project --no-dev` already installs every third-party dependency, and `uvicorn app.main:app` runs `app/` as a plain path-based package without needing the project itself pip-installed. Removed the second sync entirely.
- **`check_postgres` used `asyncpg.connect()` directly against a `postgresql+asyncpg://` DSN.** `POSTGRES_DSN` uses the SQLAlchemy-style `+asyncpg` driver suffix everywhere else in this codebase (config, Alembic), but raw `asyncpg.connect()` doesn't understand that scheme and raised `ClientConfigurationError` on every call — silently reported as `"postgres": "unavailable"` in `/readyz` even though the real Postgres container was healthy. Only surfaced once the full stack was actually run (all unit/contract tests mock this check, by design). Fixed by switching `check_postgres` to SQLAlchemy's `create_async_engine`, consistent with the DSN format used everywhere else.
- **(Environment, not a code bug) `neo4j:5-community` currently resolves to a broken build (5.26.30) whose entrypoint crash-loops (`su-exec` usage error) on this host, reproducing even with a bare `docker run` outside Compose.** Pinned `compose.yaml` to `neo4j:5.25-community`, a known-good build, and confirmed Neo4j starts and passes its health check.
