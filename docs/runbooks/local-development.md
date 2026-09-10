# Runbook: Local Development

## First-time setup

```bash
git clone <repo-url>
cd TraceX
cp .env.example .env
uv sync --all-groups
```

Edit `.env` if you need non-default local values (ports already in use, etc). Never commit `.env` — it's git-ignored.

## Running the stack

**Option A — full stack in Docker:**

```bash
docker compose up --build
```

**Option B — infra in Docker, API on host (faster iteration/reload):**

```bash
docker compose up -d postgres neo4j redis minio
uv run uvicorn app.main:app --reload
```

Stop everything:

```bash
docker compose down          # stop containers, keep volumes (data persists)
docker compose down -v       # stop containers and remove volumes (fresh state)
```

## Verifying the API is up

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8000/api/v1/meta/contracts
```

`/readyz` returns `503` until PostgreSQL, Neo4j, Redis, and MinIO are all reachable — this is expected for the first several seconds after `docker compose up` while containers pass their health checks.

## Running checks before pushing

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest
docker compose config
```

`uv run ruff format .` (without `--check`) will fix formatting in place; `uv run ruff check --fix .` fixes auto-fixable lint issues.

## Adding a dependency

```bash
uv add <package>            # runtime dependency
uv add --group dev <package>  # dev-only dependency
```

Always commit the resulting `uv.lock` change alongside `pyproject.toml`. Never hand-edit `uv.lock`.

## Graph schema (Neo4j constraints and indexes)

`app/modules/graph/schema.py` owns the graph foundation's constraints and indexes (see `docs/architecture/graph-taxonomy-v1.md` and `docs/architecture/neo4j-graph-foundation.md`). It is never run automatically by `app/main.py` on startup -- apply and verify it explicitly, with Neo4j reachable (`docker compose up -d neo4j` at minimum):

```bash
uv run python -m app.modules.graph.schema apply    # idempotent; safe to run any number of times
uv run python -m app.modules.graph.schema verify   # exits 0 if every expected constraint/index is present, 1 otherwise
```

Both read connection details from the same `Settings`/`.env` as the rest of the app.

Running the graph integration suite specifically, once schema is applied and Neo4j is up:

```bash
uv run pytest tests/integration/graph -v
```

Like `tests/integration/test_readiness_live.py`, this suite self-skips (never fabricates a pass) if there's no `.env` at the repo root, or if Neo4j specifically isn't reachable through it.

## Document/structured-data processing

`app/modules/structured_processing/` (see `docs/architecture/document-and-structured-processing-v1.md`, `docs/architecture/parser-profiles-v1.md`) needs no live infrastructure at all — every test is a deterministic unit test against in-memory or local-file bytes:

```bash
uv run pytest tests/unit/structured_processing -v
uv run pytest tests/integration/structured_processing -v   # local-file-resolver pipeline test, no external service
```

To try it interactively:

```python
from app.modules.structured_processing.models import StaticBytesResolver
from app.modules.structured_processing.worker import process_job
# construct a WorkerJobV1 + EvidenceRecordV1 (see tests/fixtures/structured_processing/factory.py),
# then: process_job(job, evidence, StaticBytesResolver(payload=your_bytes))
```

## Authentication and case-scoped access control

`app/modules/access_control/` (see `docs/architecture/access-control-v1.md`, `docs/architecture/security-boundaries-v1.md`) needs PostgreSQL (users/sessions/case data) and Redis (login/refresh rate limiting):

```bash
docker compose up -d postgres redis
uv run alembic upgrade head       # applies migrations/versions/7e8499f34f29_access_control_foundation.py
uv run uvicorn app.main:app --reload
```

Try it interactively once the API is up:

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.test","password":"a-long-enough-password","display_name":"You"}'

curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.test","password":"a-long-enough-password"}'
# -> {"access_token": "...", "refresh_token": "...", "token_type": "bearer", "expires_in": 900}

curl http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"
```

Running its test suites specifically:

```bash
uv run pytest tests/unit/access_control -v      # no live infra needed (fake in-memory repository/limiter)
uv run pytest tests/security/access_control -v  # secret-leakage + module-boundary checks
uv run pytest tests/integration/access_control -v   # needs postgres + redis; applies the migration itself
```

Like `tests/integration/test_readiness_live.py` and `tests/integration/graph/`, the integration suite self-skips (never fabricates a pass) if there's no `.env` at the repo root, or if PostgreSQL specifically isn't reachable through it — it applies `alembic upgrade head` once per test session automatically before running (in a clean subprocess, so it never picks up `tests/conftest.py`'s fake test-environment `POSTGRES_DSN` by accident).

For a multi-laptop LAN demo setup, see `docs/runbooks/lan-development.md`.

## Database migrations (Alembic)

```bash
uv run alembic revision --autogenerate -m "description"   # generate a new revision
uv run alembic upgrade head                                 # apply pending migrations
```

`migrations/env.py` reads the database URL from `app.core.config.get_settings()` — set `POSTGRES_DSN` via `.env` as usual, nothing extra to configure. `migrations/env.py` keeps `target_metadata = None` (Nipun's baseline decision); `app/modules/access_control/repository.py` defines its own SQLAlchemy Core `Table` objects rather than a project-wide declarative base, so its migration (`7e8499f34f29_access_control_foundation.py`) is hand-written, not autogenerated — see `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, Decision 6.

## Troubleshooting

- **`/readyz` stuck at 503`**: check `docker compose ps` — a service still starting (especially Neo4j, which is slower to become healthy) will show as `starting`/`unhealthy`. Check `docker compose logs <service>`.
- **Port already in use**: another process is bound to one of `8000/5432/7474/7687/6379/9000/9001`. Either stop it or change the corresponding `*_PORT` in `.env`.
- **`uv sync` fails to resolve**: confirm you're on the committed `uv.lock` (`git status`); if you intentionally changed dependencies, re-run `uv lock` then `uv sync`.
- **mypy complains about a third-party import**: check `[[tool.mypy.overrides]]` in `pyproject.toml` before adding a blanket `# type: ignore` — the library may just need `ignore_missing_imports` added to that list.
- **`AUTH_JWT_SECRET` validation error on startup**: it's required and must be ≥ 32 characters — see `.env.example` for a safe local placeholder and generate a stronger one for anything beyond a single developer's machine.
- **`alembic upgrade head` fails with a password/auth error**: confirm `POSTGRES_DSN` in `.env` matches the credentials `docker compose up -d postgres` was started with (`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`); if you changed `.env` after the Postgres container's first start, its data volume still has the old credentials — `docker compose down -v` for a fresh start, or update `.env` back to match.
