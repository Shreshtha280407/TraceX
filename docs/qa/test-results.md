# Phase 1 Test Results

Actual command output from verification runs. Updated by whoever runs verification — do not hand-edit a "passing" result without having actually run the command.

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
