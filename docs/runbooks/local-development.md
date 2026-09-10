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

## Database migrations (Alembic)

Phase 1 ships only the baseline revision (no domain tables). To add a new migration once domain models exist in a later phase:

```bash
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
```

`migrations/env.py` reads the database URL from `app.core.config.get_settings()` — set `POSTGRES_DSN` via `.env` as usual, nothing extra to configure.

## Troubleshooting

- **`/readyz` stuck at 503`**: check `docker compose ps` — a service still starting (especially Neo4j, which is slower to become healthy) will show as `starting`/`unhealthy`. Check `docker compose logs <service>`.
- **Port already in use**: another process is bound to one of `8000/5432/7474/7687/6379/9000/9001`. Either stop it or change the corresponding `*_PORT` in `.env`.
- **`uv sync` fails to resolve**: confirm you're on the committed `uv.lock` (`git status`); if you intentionally changed dependencies, re-run `uv lock` then `uv sync`.
- **mypy complains about a third-party import**: check `[[tool.mypy.overrides]]` in `pyproject.toml` before adding a blanket `# type: ignore` — the library may just need `ignore_missing_imports` added to that list.
