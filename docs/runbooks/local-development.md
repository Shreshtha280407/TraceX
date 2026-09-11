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

### Structured-processing worker CLI (Phase 2 — Jasraj)

See `docs/architecture/structured-processing-worker.md` for the full design. The worker needs the API's internal endpoints reachable and a `WORKER_TOKEN` configured (Phase 2.4 replaced the old shared secret — see "Provisioning a worker credential" below to get one):

```bash
docker compose up -d postgres redis minio
uv run uvicorn app.main:app --reload   # or the full `docker compose up --build`
uv run python -m app.modules.access_control.worker_credentials create \
    --name structured-worker --processor fir_report_text_v1 --processor cdr_generic_v1 \
    --processor financial_transaction_generic_v1 --processor generic_tabular_v1 --processor generic_json_v1
# copy the printed token into .env as WORKER_TOKEN=<token>, then:
uv run python -m app.modules.structured_processing.worker --once
```

`--once` is the only supported mode — it claims at most one compatible queued job, processes it, submits the result, and exits. There is no daemon or polling loop; run it again to attempt another job. As of Phase 2.2 (Nipun), a real claimed job's evidence is fetched through `GET /api/v1/internal/worker-jobs/{job_id}/input` and processed for real — see the architecture doc's "Input-access boundary" section for the full history. A `DEFERRED`/`input_resolution_unavailable` result is still possible (e.g. against an older API build, or a genuine transient failure) but is no longer the expected outcome against this repository's current API.

Running its test suites specifically:

```bash
uv run pytest tests/unit/structured_processing -v         # no live infra needed
uv run pytest tests/integration/structured_processing -v  # local-file pipeline test, plus a self-skipping live-API check
```

`tests/integration/structured_processing/test_worker_live.py` self-skips (never fabricates a pass) if there's no `.env`, the live API server isn't reachable at `WORKER_API_BASE_URL`, or `WORKER_TOKEN` isn't configured — same pattern as every other `tests/integration/*` suite in this repo. When `WORKER_TOKEN` *is* set, this suite provisions a matching `worker_credentials` row itself (idempotently, by digest) the first time it runs against a given database, so no separate manual CLI step is required just to run the tests. When PostgreSQL/MinIO are also reachable, its second test (`test_full_claim_stream_parse_submit_live_pipeline`) proves the complete claim -> stream evidence -> parse -> submit path for real, using a real seeded case/user/evidence upload.

### Communication-processing worker CLI (Phase 2 — Sarthak)

See `docs/architecture/communication-processing-worker.md` for the full design. Same pattern as the structured-processing worker CLI above — a separate worker process, its own `WORKER_TOKEN`-bound credential:

```bash
docker compose up -d postgres redis minio
uv run uvicorn app.main:app --reload   # or the full `docker compose up --build`
uv run python -m app.modules.access_control.worker_credentials create \
    --name communication-worker --processor audio_metadata_v1 --processor generic_social_json_v1 \
    --processor transcript_import_v1 --processor diarization_import_v1 \
    --processor whatsapp_export_v1 --processor telegram_export_v1 --processor instagram_export_v1
# copy the printed token into .env as WORKER_TOKEN=<token>, then:
uv run python -m app.modules.communication_processing.worker --once
```

**Only two of the seven processors above are reachable through a real evidence upload today**: `audio_metadata_v1` (`source_type=audio`) and `generic_social_json_v1` (`source_type=chat`). The other five are fully supported by this worker but have no route from `evidence_lifecycle/routing.py` yet — see the architecture doc's "Routing boundary" section; they can still be exercised via a directly-constructed `WorkerJobV1` (as the unit tests do).

`--once` is the only supported mode — no daemon or polling loop; run it again to attempt another job.

Running its test suites specifically:

```bash
uv run pytest tests/unit/communication_processing -v         # no live infra needed
uv run pytest tests/integration/communication_processing -v  # full in-process pipeline test, plus a self-skipping live-API check
```

`tests/integration/communication_processing/test_communication_worker_live.py` self-skips (never fabricates a pass) under the same conditions as `structured_processing`'s live test. It provisions its **own** dedicated worker-credential token (a fixed dev-only literal distinct from `settings.worker_token`), not a credential bound to the shared `.env` `WORKER_TOKEN` value — so running both live suites together in one `pytest -q` pass never has one suite's credential scope collide with the other's (see `docs/architecture/phase-2-decisions.md` for the full reasoning). `WORKER_TOKEN` in `.env` is still what gates whether live testing runs at all.

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

## Audio, social/chat, alias, and communication-link processing

`app/modules/communication_processing/` (see `docs/architecture/audio-social-and-communication-processing-v1.md`, `docs/architecture/multilingual-alias-candidates-v1.md`) needs no live infrastructure, Docker service, GPU, downloaded model, or network access at all — every test is a deterministic unit test against in-memory bytes/dataclasses:

```bash
uv run pytest tests/unit/communication_processing -v
uv run pytest tests/integration/communication_processing -v   # multi-processor pipeline check, no external service
```

To try it interactively:

```python
from app.modules.communication_processing.models import AudioMetadataInput
from app.modules.communication_processing.worker import process_job
# construct a WorkerJobV1 (see tests/fixtures/communication_processing/factory.py),
# then: process_job(job, AudioMetadataInput(filename="evidence.wav", data=your_wav_bytes))
```

## Video/image processing

`app/modules/media_processing/` (see `docs/runbooks/media-development.md`, `docs/architecture/media-processing-v1.md`) needs no Docker service at all — unit tests need no `ffmpeg`/`ffprobe` either (they monkeypatch `subprocess`); only the integration suite needs real `ffmpeg`/`ffprobe` on `PATH`, and self-skips cleanly if they're absent:

```bash
uv run pytest tests/unit/media_processing -v
uv run pytest tests/integration/media_processing -v   # needs ffmpeg/ffprobe; self-skips otherwise
```

See `docs/runbooks/media-development.md` for interactive usage, GPU/capability checks, and adding a real detector/tracker/OCR adapter later.

## Evidence lifecycle (upload, storage, durable job foundation)

`app/modules/evidence_lifecycle/` (see `docs/architecture/evidence-lifecycle.md`) needs PostgreSQL (evidence/job metadata), MinIO (private object storage), and Redis (durable job publish) — the same case-scoped auth as `access_control`:

```bash
docker compose up -d postgres redis minio
uv run alembic upgrade head       # applies migrations/versions/f2086e1e89f6_evidence_lifecycle_foundation.py
uv run uvicorn app.main:app --reload
```

There is still no case-CRUD API. To try the upload endpoint interactively, register/log in a user via `/api/v1/auth`, then seed a case + active membership directly (exactly what `tests/integration/evidence_lifecycle/conftest.py` and every access-control integration test already do):

```python
from app.core.config import get_settings
from app.modules.access_control.models import (
    CaseRecord,
    CaseMembershipRecord,
    CaseRole,
    CaseStatus,
    ClearanceLevel,
)
from app.modules.access_control.repository import AccessControlRepository, create_engine
# build a CaseRecord + CaseMembershipRecord for your registered user_id, then
# await repository.create_case(case); await repository.create_membership(membership)
```

Then:

```bash
curl -X POST http://localhost:8000/api/v1/cases/<case_id>/evidence \
  -H "Authorization: Bearer <access_token>" \
  -H "Idempotency-Key: <any-client-chosen-key>" \
  -F "file=@fixture.txt;type=text/plain" \
  -F "source_type=document" \
  -F "classification=unclassified"

curl http://localhost:8000/api/v1/cases/<case_id>/evidence -H "Authorization: Bearer <access_token>"
curl http://localhost:8000/api/v1/cases/<case_id>/evidence/<evidence_id> -H "Authorization: Bearer <access_token>"
curl http://localhost:8000/api/v1/cases/<case_id>/jobs/<job_id> -H "Authorization: Bearer <access_token>"
```

General CSV/XLSX/JSON evidence that isn't specifically a CDR or financial export uses `source_type=structured_tabular` (CSV/XLSX) or `source_type=structured_json` (JSON) — Phase 2.3, routed to `generic_tabular_v1`/`generic_json_v1` respectively (see `docs/architecture/evidence-lifecycle.md`'s routing table):

```bash
curl -X POST http://localhost:8000/api/v1/cases/<case_id>/evidence \
  -H "Authorization: Bearer <access_token>" \
  -F "file=@records.csv;type=text/csv" \
  -F "source_type=structured_tabular" \
  -F "classification=unclassified"

curl -X POST http://localhost:8000/api/v1/cases/<case_id>/evidence \
  -H "Authorization: Bearer <access_token>" \
  -F "file=@records.json;type=application/json" \
  -F "source_type=structured_json" \
  -F "classification=unclassified"
```

A successful upload also pushes the job's canonical JSON onto a Redis list, inspectable directly:

```bash
docker compose exec redis redis-cli LRANGE tracex:jobs:document 0 -1
```

### Provisioning a worker credential (Phase 2.4)

See `docs/architecture/worker-identity-and-security.md` for the full design. There is deliberately no public API for this — a trusted-operator-only CLI is the *only* way a worker credential is ever created, rotated, listed, or revoked:

```bash
# create -- prints the plaintext token exactly once, to this terminal only
uv run python -m app.modules.access_control.worker_credentials create \
    --name my-local-worker --processor fir_report_text_v1 --processor cdr_generic_v1
# worker_id: ...
# token (shown once -- store it now, never in Git/.env.example/logs):
#   <copy this into .env as WORKER_TOKEN=...>

# list -- never prints a token or digest, safe to run/share
uv run python -m app.modules.access_control.worker_credentials list

# rotate -- issues a fresh token, immediately invalidating the old one
uv run python -m app.modules.access_control.worker_credentials rotate --worker-id <worker_id>

# revoke -- immediately and permanently denies the credential; idempotent
uv run python -m app.modules.access_control.worker_credentials revoke --worker-id <worker_id>
```

This CLI runs only where the server's own PostgreSQL configuration is already available (the same trust level as running `alembic upgrade head`) — never expose it as a network-reachable endpoint. **The printed token goes only into a local, git-ignored `.env` or a real deployment secret store — never into Git, `.env.example`, a log line, a test report, a screenshot, or an API response.** Optionally set `WORKER_CREDENTIAL_PEPPER` in `.env` for an extra server-side peppering layer on the stored digest (falls back to an unkeyed SHA-256 digest if unset — accepted in local/dev, required in production, where a missing pepper fails closed `503`).

### Worker job claim and result submission (Phase 2.1 / 2.4)

See `docs/architecture/worker-job-lifecycle.md` for the full design. The internal worker endpoints (`/api/v1/internal/worker-jobs/*`) need a real worker credential — provision one above, then set `WORKER_TOKEN` in `.env` (see `.env.example`) to that credential's token. With no worker credential resolvable at all, these endpoints fail closed with `401`; with `APP_ENV=production` and no `WORKER_CREDENTIAL_PEPPER` configured, they fail closed with `503`.

```bash
# claim one eligible job for a processor this worker is scoped to
# (a processor_name outside --processor at creation time is rejected 403)
curl -X POST http://localhost:8000/api/v1/internal/worker-jobs/claim \
  -H "Authorization: Bearer <WORKER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"}'
# -> {"job": {...WorkerJobV1...}, "claim_token": "...", "lease_expires_at": "..."} or
#    {"job": null, "claim_token": null, "lease_expires_at": null} if nothing is eligible

# stream the claimed job's evidence bytes (Phase 2.2 -- see "Worker evidence
# delivery" in docs/architecture/evidence-lifecycle.md); only works while the
# job is still `running`, has an unexpired lease, AND this is the same worker
# identity that claimed it
curl http://localhost:8000/api/v1/internal/worker-jobs/<job_id>/input \
  -H "Authorization: Bearer <WORKER_TOKEN>" \
  -H "X-Claim-Token: <claim_token from the claim response>" \
  -o downloaded_evidence

# submit a result for the claimed job (a WorkerResultV1 JSON body, the claim token in a header)
curl -X POST http://localhost:8000/api/v1/internal/worker-jobs/<job_id>/result \
  -H "Authorization: Bearer <WORKER_TOKEN>" \
  -H "X-Claim-Token: <claim_token from the claim response>" \
  -H "Content-Type: application/json" \
  -d '{"schema_version": "v1", "job_id": "<job_id>", "case_id": "<case_id>", "evidence_id": "<evidence_id>", "status": "succeeded", "observations": [], "derived_artifacts": [], "checkpoint": null, "error": null, "completed_at": "2026-01-01T12:00:00Z"}'
```

The user-facing `GET /api/v1/cases/<case_id>/jobs/<job_id>` reflects the result once submitted (`status`, `claimed_at`, `completed_at`, `observation_count`) — never the claim token or object URI.

Running its test suites specifically:

```bash
uv run pytest tests/unit/evidence_lifecycle -v      # no live infra needed (fakes for repository/storage/job-producer)
uv run pytest tests/security/evidence_lifecycle -v  # module-boundary static checks
uv run pytest tests/integration/evidence_lifecycle -v   # needs postgres + minio; applies the migration itself
```

Like every other `tests/integration/*` suite, the integration suite self-skips (never fabricates a pass) if there's no `.env` at the repo root, or if PostgreSQL/MinIO specifically aren't reachable through it.

## Database migrations (Alembic)

```bash
uv run alembic revision --autogenerate -m "description"   # generate a new revision
uv run alembic upgrade head                                 # apply pending migrations
```

`migrations/env.py` reads the database URL from `app.core.config.get_settings()` — set `POSTGRES_DSN` via `.env` as usual, nothing extra to configure. `migrations/env.py` keeps `target_metadata = None` (Nipun's baseline decision); `app/modules/access_control/repository.py` and `app/modules/evidence_lifecycle/repository.py` each define their own SQLAlchemy Core `Table` objects rather than a project-wide declarative base, so their migrations (`7e8499f34f29_access_control_foundation.py`, `f2086e1e89f6_evidence_lifecycle_foundation.py`, `102857ca8d1d_worker_job_claim_and_result_foundation.py`, `af5b05e61b08_structured_source_type_routing.py`) are hand-written, not autogenerated — see `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, Decision 6.

## Troubleshooting

- **`/readyz` stuck at 503`**: check `docker compose ps` — a service still starting (especially Neo4j, which is slower to become healthy) will show as `starting`/`unhealthy`. Check `docker compose logs <service>`.
- **Port already in use**: another process is bound to one of `8000/5432/7474/7687/6379/9000/9001`. Either stop it or change the corresponding `*_PORT` in `.env`.
- **`uv sync` fails to resolve**: confirm you're on the committed `uv.lock` (`git status`); if you intentionally changed dependencies, re-run `uv lock` then `uv sync`.
- **mypy complains about a third-party import**: check `[[tool.mypy.overrides]]` in `pyproject.toml` before adding a blanket `# type: ignore` — the library may just need `ignore_missing_imports` added to that list.
- **`AUTH_JWT_SECRET` validation error on startup**: it's required and must be ≥ 32 characters — see `.env.example` for a safe local placeholder and generate a stronger one for anything beyond a single developer's machine.
- **`alembic upgrade head` fails with a password/auth error**: confirm `POSTGRES_DSN` in `.env` matches the credentials `docker compose up -d postgres` was started with (`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`); if you changed `.env` after the Postgres container's first start, its data volume still has the old credentials — `docker compose down -v` for a fresh start, or update `.env` back to match.
