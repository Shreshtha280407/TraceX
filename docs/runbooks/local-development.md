# Runbook: Local Development

## Phase 5 final integration note

`compose.yaml`'s `postgres` service requires `pgvector/pgvector:pg16` (not
plain `postgres:16-alpine`) since Phase 5A added `pgvector` for candidate
snapshots — `uv run alembic upgrade head` fails outright
(`extension "vector" is not available`) against a Postgres without it. If
`docker compose up -d postgres` starts an unhealthy, crash-looping
container whose logs show only repeated
`docker-entrypoint.sh: line NNN: /usr/local/bin/gosu: Success` with no
further output, this is a **corrupted local image layer** (a `0`-byte
`gosu` binary inside the pulled image), not a permissions/sandbox
restriction — confirmed by running `docker run --rm --entrypoint sh
pgvector/pgvector:pg16 -c "ls -la /usr/local/bin/gosu"` and seeing a
`0`-byte file. Fix: remove every container still referencing the image,
then force a clean re-pull:

```bash
docker rm -f <containers using pgvector/pgvector:pg16>   # docker ps -a --filter ancestor=pgvector/pgvector:pg16
docker rmi pgvector/pgvector:pg16
docker pull pgvector/pgvector:pg16
docker compose up -d postgres
```

A `docker compose up -d postgres` alone will not fix this: Docker treats
a locally-cached image as already matching its digest and will not
re-download it merely because the cached layer is corrupted.

## Phase 4 release-gate note

Use a dedicated Compose project for a release-gate run so existing local
services are untouched:

```bash
docker compose -p tracex-phase4-gate --env-file .env.example up --build -d
```

Do not treat a successful `docker compose config` or unit suite as a
containerized acceptance result. Confirm the API and each dependency are
healthy, then run the documented synthetic E2E. If Docker's build network
cannot resolve PyPI, record the exact dependency/DNS failure and leave the
gate in progress; do not substitute another user's already-running stack.

## Phase 3 final acceptance note

Run `uv run alembic upgrade head` before live worker acceptance: a stale local
database missing the Phase 3 retry-limit migration causes upload-time 500s.
For a media worker, frame OCR progress is monotonic selected-frame work;
subsequent aggregate media batches intentionally carry no incomparable progress
event. Run the full commands and known limits in `docs/qa/phase-3-acceptance.md`.

## First-time setup

```bash
git clone <repo-url>
cd TraceX
cp .env.example .env
uv sync --all-groups
```

Edit `.env` if you need non-default local values (ports already in use, etc). Never commit `.env` — it's git-ignored.

### Optional first-Provisioner browser setup

The default first-Provisioner path remains the trusted-operator CLI. For a private
deployment that needs a browser setup ceremony, temporarily set
`TRACEX_FIRST_ADMIN_SETUP_ENABLED=true` and inject a unique, randomly generated
`TRACEX_FIRST_ADMIN_SETUP_TOKEN` (at least 32 characters) through the deployment
secret store. Visit `/setup/first-admin` only while no active Provisioner
exists. The token is sent in a request header, never a URL, and the endpoint
closes permanently after the first successful Provisioner. Disable/remove the
token after setup; it is not a public signup facility.

## Running the stack

**Option A — full stack in Docker:**

```bash
docker compose up --build
```

The browser UI is served at `http://localhost:5173`; it proxies `/api/*`
same-origin to the API container. A fresh deployment with the optional
first-Provisioner setup enabled opens at `http://localhost:5173/setup/first-admin`.

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

### One-time step if you already have a `minio-data` volume from before 2026-09-25

MinIO Inc. deleted `minio/minio` from Docker Hub on 2026-09-11, then
`quay.io/minio/minio` (the stopgap this repo used) itself started refusing
anonymous pulls with `401 Unauthorized` on 2026-09-24 — both official free
distribution channels are gone. `compose.yaml` now pulls
`cgr.dev/chainguard/minio` instead (same real MinIO source, actively
maintained, verified anonymously pullable). That image runs as a non-root
user (UID `65532`), so a `minio-data` volume already populated by the old
image (which ran as root) will fail to start with `Unable to write to the
backend`. Fix it once, non-destructively — this only changes file
ownership, your existing buckets/evidence are untouched:

```bash
docker run --rm -v tracex_minio-data:/data alpine chown -R 65532:65532 /data
docker compose up -d minio
```

A brand-new environment (no pre-existing volume) needs no such step.

### Optional continuous workers (`cpu-worker`/`gpu-worker` Compose profiles)

Gap-Closure WP-8 (G15) split the previous single `workers` profile in two, so an operator can start only the workers their hardware actually supports — no worker is started by a plain `docker compose up`, both remain opt-in:

```bash
# CPU-only workers -- no GPU or model weights needed for any of these:
docker compose --profile cpu-worker up -d graph-projector intelligence-worker structured-worker communication-worker
docker compose --profile cpu-worker logs -f graph-projector intelligence-worker structured-worker communication-worker
docker compose --profile cpu-worker down

# GPU-capable worker (media/video/image detection) -- needs the bootstrapped model first:
docker compose --profile gpu-worker run --rm media-model-bootstrap
docker compose --profile gpu-worker up -d media-worker
docker compose --profile gpu-worker logs -f media-worker
docker compose --profile gpu-worker down
```

| Service | Profile | Mode | Depends on |
|---|---|---|---|
| `graph-projector` | `cpu-worker` | `--loop` (real poll loop) | `postgres`, `neo4j` directly (unchanged from before this phase) |
| `intelligence-worker` | `cpu-worker` | `--replay-loop` (real poll loop) | `postgres`, `neo4j` directly |
| `structured-worker` | `cpu-worker` | `--once` | `api` (internal worker API) |
| `communication-worker` | `cpu-worker` | `--once` | `api` (internal worker API) |
| `media-worker` | `gpu-worker` | `--loop` (real poll loop) | `api` (internal worker API) |
| `media-model-bootstrap` | `gpu-worker` | one-shot (`run`, never `up`) | none |

`structured-worker`/`communication-worker` run `--once`, not `--loop` — neither worker module has a real poll-loop mode in this phase (documented in each module's own CLI help text: "No daemon or polling mode exists"). `restart: unless-stopped` on a one-shot command is a deliberate, honestly-imperfect substitute for a real poll loop: each container restart is one more claim attempt (with Docker's own restart backoff between attempts), not a clean idle wait like `graph-projector`/`intelligence-worker`/`media-worker`'s real `--loop` modes. See `docs/qa/known-limitations.md`'s "WP-8" section.

Every worker waits for its real dependencies to be healthy and uses `restart: unless-stopped`. `media-model-bootstrap` is a one-shot command (`docker compose run`, not `up` — nothing depends on it, so `up` never starts it on its own), writing the checksum-verified detector model into the shared `media-models-data` named volume `media-worker` mounts read-only.

**Each worker role needs its own credential — never one shared `WORKER_TOKEN`.**
An earlier version of this note said the shared `${WORKER_TOKEN:-}` passthrough
was fine to reuse across all three worker services; that was wrong; it was
never actually verified against `docker compose --profile cpu-worker up`
running several worker containers at once (only against pytest's live
suites, which each self-provision their own scoped credential in
isolation — see "Structured-processing worker CLI"/"Communication-
processing worker CLI"/"Media-processing worker CLI" below). Each worker's
`worker_credentials` row has a distinct `allowed_processor_names` scope; a
single shared token's digest can only ever match one of those rows, so the
other worker services 403 on every claim. `compose.yaml` gives each worker
service its own env var (`STRUCTURED_WORKER_TOKEN`/
`COMMUNICATION_WORKER_TOKEN`/`MEDIA_WORKER_TOKEN`), each overriding
`WORKER_TOKEN` for that one service only — provision one credential per
role via the trusted-operator CLI (see the three CLI sections below) and
set all three in `.env` before starting these services. See
`docs/architecture/worker-identity-and-security.md` and
`docs/qa/known-limitations.md` for the full writeup.

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

## Graph projection (Phase 2.5 — durable observation-to-Neo4j pipeline)

### Phase 3 mapping verification

After applying the graph schema, run the graph projector twice for the same
accepted document, CDR, or finance observation batch. `TemporalEvent` and
`SourceClaim` counts must remain unchanged, and every specialised node must
traverse through its parent `Observation` to same-case `Evidence`. The live
check self-skips when Neo4j is unavailable:

```bash
uv run pytest -q tests/integration/graph/test_phase_3_mapping_live.py
```

See `docs/architecture/graph-projection.md` for the full design. After applying the graph schema above and running the usual migration (`uv run alembic upgrade head` — includes `graph_projection_jobs`), accepted worker results automatically enqueue durable projection jobs; nothing further is needed to *create* them. To actually project queued jobs into Neo4j:

```bash
uv run python -m app.modules.graph.worker --once
```

Claims and attempts a bounded batch (`GRAPH_PROJECTION_BATCH_SIZE`, default 25), then exits — run it again (or wire it into a cron/systemd timer) to process another batch. Exit code `1` means at least one job reached a terminal `failed` state (worth investigating); `0` covers "nothing to do" and "everything succeeded or was safely left retryable."

For continuous operation instead of a cron/systemd timer (Phase 2 closeout):

```bash
uv run python -m app.modules.graph.worker --loop
```

Runs continuously until `Ctrl-C` (SIGINT) or SIGTERM, with a configurable idle-poll interval (`GRAPH_PROJECTOR_POLL_INTERVAL_SECONDS`, default 5s), bounded exponential backoff on repeated failure (capped at `GRAPH_PROJECTOR_MAX_BACKOFF_SECONDS`, default 60s), and a `GRAPH_PROJECTOR_MAX_CONSECUTIVE_FAILURES` (default 5) cutoff that stops the loop (exit `1`) rather than retrying a genuinely down Neo4j/PostgreSQL forever. Shutdown is graceful: whatever batch is already in flight finishes before the process exits. See `docs/architecture/graph-projection.md`'s "Continuous operation" section.

Inspecting job state directly (`psql`, or any PostgreSQL client):

```sql
SELECT projection_id, case_id, status, attempt, max_attempts, last_error_code, last_error_message
FROM graph_projection_jobs
WHERE status = 'failed'
ORDER BY updated_at DESC;
```

Retrying a job that's exhausted its attempts (an operator decision, not automatic): reset it back to `queued` and it becomes claimable on the next `--once` run —

```sql
UPDATE graph_projection_jobs
SET status = 'queued', attempt = 0, last_error_code = NULL, last_error_message = NULL
WHERE projection_id = '<uuid>';
```

Reading the projected graph safely (never raw Cypher, never an object URI):

```bash
curl http://localhost:8000/api/v1/cases/<case_id>/graph/observations \
  -H "Authorization: Bearer <access_token>"
```

Running the graph-projection test suites specifically:

```bash
uv run pytest tests/unit/graph tests/security/graph -v                       # no live infra needed
uv run pytest tests/integration/graph/test_outbox_repository_live.py -v      # needs postgres; self-skips otherwise
uv run pytest tests/integration/graph/test_full_pipeline_live.py -v          # needs postgres + minio + neo4j + a running API server
```

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

`tests/integration/structured_processing/test_worker_live.py` self-skips (never fabricates a pass) if there's no `.env`, the live API server isn't reachable at `WORKER_API_BASE_URL`, or `WORKER_TOKEN` isn't configured — same pattern as every other `tests/integration/*` suite in this repo. When `WORKER_TOKEN` *is* set, this suite provisions a matching `worker_credentials` row itself (idempotently, by digest) the first time it runs against a given database, so no separate manual CLI step is required just to run the tests. When PostgreSQL/MinIO are also reachable, its second test (`test_full_claim_stream_parse_submit_live_pipeline`, parametrized over `document`/`structured_tabular`/`structured_json`/`cdr`/`financial`) proves the complete claim -> stream evidence -> parse -> submit path for real, using a real seeded case/user/evidence upload.

### Real document OCR/NER and CDR/finance batch processing (Phase 3 — Jasraj)

See `docs/architecture/document-structured-processing.md` for the full design. No extra configuration is needed to process text-bearing PDFs, DOCX/TXT, or CDR/finance files — the worker CLI above already handles them via real regex/vectorized-batch extraction. Two additional, optional real capabilities need their own setup:

**Real local OCR for scanned/untrustworthy PDF pages** — needs the `tesseract-ocr` system package (already installed in this repository's `Dockerfile`; install it on the host too if running the worker outside Docker):

```bash
# Debian/Ubuntu:
sudo apt-get install tesseract-ocr tesseract-ocr-eng
```

Without it, a page that needs OCR is reported `DEFERRED` in the job's checkpoint rather than failing the whole job — trusted pages' observations are still submitted normally.

**Real local NER** (`PERSON`/`ORGANIZATION`/`LOCATION` mention extraction beyond the deterministic fallback) — bootstrap the pinned spaCy model once:

```bash
uv run python -m app.modules.structured_processing.bootstrap_ner_model
```

Downloads, checksum-verifies, and extracts `en_core_web_sm` (3.8.0, MIT-licensed) into `models/nlp/en_core_web_sm` (git-ignored) — never installed as a package, never downloaded automatically at worker runtime. Without this, `worker.py` automatically falls back to `DeterministicNerAdapter` (a fixed, no-ML gazetteer/heuristic adapter) — never a crash. Re-run any time to verify/refresh; pass `--force` to re-download even if a valid model is already present.

Running its test suites specifically:

```bash
uv run pytest tests/unit/structured_processing/test_normalization.py -v
uv run pytest tests/unit/structured_processing/test_ner.py -v                       # real-model test self-skips if not bootstrapped
uv run pytest tests/unit/structured_processing/test_relations.py -v
uv run pytest tests/unit/structured_processing/test_chunked_processing.py -v
uv run pytest tests/unit/structured_processing/test_ocr_field_match_precision.py -v -s   # self-skips if tesseract unavailable; -s prints the measured precision/recall
uv run pytest tests/unit/structured_processing/test_worker_document_batches.py -v
uv run pytest tests/unit/structured_processing/test_worker_structured_batches.py -v
```

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

**All seven processors above are reachable through a real evidence upload** (resolved in Phase 2 — `evidence_lifecycle/routing.py` added five additive `SourceType`s: `audio_transcript`, `audio_diarization`, `whatsapp_chat`, `telegram_chat`, `instagram_chat`, alongside the original `audio`/`chat`; see `docs/qa/known-limitations.md`). They can also still be exercised via a directly-constructed `WorkerJobV1` (as the unit tests do).

`--once` is the only supported mode — no daemon or polling loop; run it again to attempt another job.

Running its test suites specifically:

```bash
uv run pytest tests/unit/communication_processing -v         # no live infra needed
uv run pytest tests/integration/communication_processing -v  # full in-process pipeline test, plus a self-skipping live-API check
```

`tests/integration/communication_processing/test_communication_worker_live.py` self-skips (never fabricates a pass) under the same conditions as `structured_processing`'s live test. It provisions its **own** dedicated worker-credential token (a fixed dev-only literal distinct from `settings.worker_token`), not a credential bound to the shared `.env` `WORKER_TOKEN` value — so running both live suites together in one `pytest -q` pass never has one suite's credential scope collide with the other's (see `docs/architecture/phase-2-decisions.md` for the full reasoning). `WORKER_TOKEN` in `.env` is still what gates whether live testing runs at all.

### Real audio/chat micro-batch processing, timezone default, and mentioned-identifier/transliteration hooks (Phase 3 — Sarthak)

See `docs/architecture/communication-processing.md` for the full design. No extra configuration or bootstrap step is needed — every capability below is pure-Python/stdlib, ships with the repository, and needs no system package or model download.

**Micro-batch submission**: `worker.py --once` now submits observations through Nipun's `POST /{job_id}/observations` in bounded chunks (`COMMUNICATION_BATCH_SIZE`, default 200) instead of bundling everything into the terminal result, renewing its lease via `POST /{job_id}/renew` every 5 batches for a long-running job — the same pattern `structured_processing`'s Phase 3 worker established.

**Chat timezone default**: a naive chat timestamp with no explicit offset/`Z`/epoch signal is interpreted in `COMMUNICATION_DEFAULT_TIMEZONE` (default `Asia/Kolkata`) rather than left unresolved — WhatsApp (always naive), Telegram (when `date_unixtime` is absent), and generic-JSON exports (a naive ISO string) all use this fallback; `timestamp_source_timezone` on the resulting observation records which zone was actually used (`None` when the source's own signal was unambiguous).

**Mentioned-identifier extraction**: every chat message's text is also scanned for `phone_number`/`email_address`/`url`/`username_or_handle` via fixed, deterministic regexes (`social/identifiers.py`) — separate observations from the parent `chat_message`, never a graph edge or resolved identity.

**Sender-name transliteration candidates**: a chat message's `sender` field gets per-word `deterministic_transliteration`/`exact_normalized` candidates attached as a `sender_transliteration_candidates` observation attribute (via the pre-existing, unmodified `aliases/transliteration.py`) — review-only extraction aids, never auto-attached to any entity's aliases.

**ASR/diarization**: still no real local speech-to-text or diarization model (this module's own `test_module_safety.py` bans every practical ML toolkit for it — see `docs/architecture/phase-3-decisions.md`'s Sarthak section). `audio/asr_adapter.py`/`audio/diarization_adapter.py` now give that absence a typed, documented `Protocol` boundary (`UnavailableAsrAdapter`/`UnavailableDiarizationAdapter`, always deferring) instead of an implicit one — `worker.py`'s dispatch behavior is unchanged.

Running its new test suites specifically:

```bash
uv run pytest tests/unit/communication_processing/test_social_identifiers.py -v
uv run pytest tests/unit/communication_processing/test_social_common.py -v
uv run pytest tests/unit/communication_processing/test_asr_adapter.py tests/unit/communication_processing/test_diarization_adapter.py -v
uv run pytest tests/unit/communication_processing/test_communication_worker_orchestration.py -v
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

All seven `communication_processing` processor profiles are reachable through a real `POST /api/v1/cases/{case_id}/evidence` upload: `source_type=audio` → `audio_metadata_v1`, `chat` → `generic_social_json_v1` (both since Phase 1), and (Phase 2 routing fix) `audio_transcript` → `transcript_import_v1`, `audio_diarization` → `diarization_import_v1`, `whatsapp_chat` → `whatsapp_export_v1`, `telegram_chat` → `telegram_export_v1`, `instagram_chat` → `instagram_export_v1` — see `docs/architecture/evidence-lifecycle.md`'s routing table. This repository has no worker CLI that claims and processes these jobs yet (see `docs/qa/known-limitations.md`); the routing fix only makes the job itself creatable with the correct `processor_name`.

## Video/image processing

`app/modules/media_processing/` (see `docs/runbooks/media-development.md`, `docs/architecture/media-processing-v1.md`) needs no Docker service at all for its own pure-function unit tests — they need no `ffmpeg`/`ffprobe` either (they monkeypatch `subprocess`); only the integration suite needs real `ffmpeg`/`ffprobe` on `PATH`, and self-skips cleanly if they're absent:

```bash
uv run pytest tests/unit/media_processing -v
uv run pytest tests/integration/media_processing -v   # needs ffmpeg/ffprobe; self-skips otherwise
```

See `docs/runbooks/media-development.md` for interactive usage, GPU/capability checks, and swapping in a different detector/tracker/OCR adapter.

**Benchmark** (Phase 2 closeout): measure real, actual processing performance on a local image/video file --

```bash
uv run python -m app.modules.media_processing.benchmark path/to/file.mp4
```

Reports measured media size/duration, sampled frame count, device used (`cpu`/`cuda`), per-stage timings, throughput, and observation counts — never a fabricated or extrapolated throughput claim. Uses the real detector/OCR if bootstrapped, metadata-only otherwise (same degradation as the worker CLI). See `docs/qa/test-results.md` for actual measured runs.

### Media-processing worker CLI (Phase 2 completion — Gaurav; Phase 2 closeout — Nipun)

See `docs/architecture/media-processing-worker.md` for the full design. Same pattern as the structured-processing/communication-processing worker CLIs above — a separate worker process, its own `WORKER_TOKEN`-bound credential:

```bash
docker compose up -d postgres redis minio
uv run uvicorn app.main:app --reload   # or the full `docker compose up --build`
uv run python -m app.modules.access_control.worker_credentials create \
    --name media-worker --processor media_detection_v1 --processor media_metadata_v1
# copy the printed token into .env as WORKER_TOKEN=<token>, then bootstrap the
# real detector model asset (skip this to run metadata-only -- see below):
uv run python -m app.modules.media_processing.bootstrap_models
uv run python -m app.modules.media_processing.worker --once   # or --loop
```

Both `image` (`image/jpeg`/`image/png`) and `video` (`video/mp4`/`video/quicktime`/`video/x-matroska`) source types now route to `media_detection_v1` — see `docs/architecture/evidence-lifecycle.md`'s routing table. Real detection/OCR/tracking run only if the model asset above was bootstrapped *and* `tesseract-ocr` is installed (already true inside this repo's Docker image; install it locally otherwise) — without either, the worker degrades to metadata-only processing automatically (a structured warning is logged, never a crash); pass `--require-analysis` to fail loudly at startup instead if you want to guarantee real detection is available.

`--once` processes at most one job then exits; `--loop` (Phase 2 closeout) runs continuously until `Ctrl-C`/SIGTERM — see `docs/architecture/media-processing-worker.md`'s "Continuous operation" section for the poll/backoff/shutdown/lease-renewal policy and its `MEDIA_WORKER_*` settings.

Running its test suites specifically:

```bash
uv run pytest tests/unit/media_processing -v                 # no live infra needed
uv run pytest tests/integration/media_processing -v          # ffmpeg/ffprobe pipeline test, plus a self-skipping live-API check
```

`tests/integration/media_processing/test_media_worker_live.py` self-skips (never fabricates a pass) under the same conditions as the structured/communication-processing live tests, and provisions its own dedicated worker-credential token, decoupled from the shared `.env` `WORKER_TOKEN` value's scope the same way those two do.

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

### Generating and configuring an integrity signing key (Phase 6 — Nipun)

See `docs/architecture/phase-6-integrity.md` for the full design. Tamper-evident checkpoints need a local Ed25519 signing key — generate a dev-only one:

```bash
uv run python -m app.modules.integrity.cli generate-key
# prints one base64 line -- copy it into .env as INTEGRITY_SIGNING_KEY=...
# never into Git, .env.example, a log line, or anywhere else
```

Then set both in `.env` (see `.env.example`):

```bash
INTEGRITY_SIGNING_KEY=<the base64 line printed above>
INTEGRITY_SIGNING_KEY_ID=dev-local-ed25519-1
```

With no key configured, `build-checkpoint` (below) fails clearly
(`SigningKeyNotConfiguredError`) rather than silently skipping the
signature or crashing. There is no rotation workflow in this phase — a new
key simply changes `key_id`/the public-key fingerprint on every checkpoint
signed after the change; older checkpoints remain verifiable against their
own originally-stored public key, unaffected by a later rotation.

### Verifying an integrity checkpoint (Phase 6 — Nipun)

```bash
# Seal a contiguous, case-scoped range of already-recorded integrity events:
uv run python -m app.modules.integrity.cli build-checkpoint \
    --case-id <uuid> --start-sequence 1 --end-sequence 50

# Independently recompute and verify a checkpoint's root + signature.
# Exits 0 if ok, 1 otherwise -- composes in a script or CI gate.
uv run python -m app.modules.integrity.cli verify \
    --case-id <uuid> --checkpoint-id <uuid>

# Export a portable bundle of only public verification material + hashed
# metadata (never raw evidence content or private key material).
uv run python -m app.modules.integrity.cli export \
    --case-id <uuid> --checkpoint-id <uuid> --out bundle.json
```

No integrity events are recorded unless the evidence-lifecycle/graph
services are constructed with a real `integrity_recorder` (the default
FastAPI dependency wiring does this — see `app/modules/evidence_lifecycle
/dependencies.py`/`app/modules/graph/intelligence_worker.py`); a recording
failure never blocks the primary evidence/observation/correlation write it
follows (see `phase-6-integrity.md`'s "Producer integration seams").

### Worker retry limits and lease-ceiling (Phase 3 — Aditya)

`WORKER_JOB_MAX_ATTEMPTS` (default `5`) bounds how many times a job may be reclaimed after a lease expires before `claim_job` sweeps it to a durable terminal `failed` state (`error.code = "retry_exhausted"`) instead of leaving it reclaimable forever. `WORKER_LEASE_MAX_SECONDS` (default `3600`) caps how far `POST .../renew` may ever extend a single claim's lease, measured from the original claim time — no number of renewals can push a lease past this ceiling.

To exercise either locally without waiting a full hour or five reclaim cycles, temporarily lower both in `.env` (git-ignored, safe to edit and restore):

```bash
# in .env, temporarily:
WORKER_LEASE_SECONDS=3
WORKER_LEASE_MAX_SECONDS=8
WORKER_JOB_MAX_ATTEMPTS=2
```

Restart the API (`docker compose up --build -d` or restart the host `uvicorn` process) after editing `.env`, run through a claim → let the lease expire → reclaim → let it expire again cycle, and confirm the job lands in `failed` with `retry_exhausted` on the second lease expiry. Restore the original values and restart again when done — these are development conveniences only, never appropriate for a real deployment.

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

The user-facing `GET /api/v1/cases/<case_id>/jobs/<job_id>` reflects the result once submitted (`status`, `claimed_at`, `completed_at`, `observation_count`, `latest_progress`) — never the claim token or object URI.

### Observation-batch (partial micro-batch) submission (Phase 3)

See `docs/architecture/phase-3-decisions.md` and `docs/architecture/evidence-lifecycle.md`'s "Observation-batch ingestion" section for the full design. While a claimed job is still `running`, a worker may submit any number of partial batches before its one terminal `/result` call:

```bash
curl -X POST http://localhost:8000/api/v1/internal/worker-jobs/<job_id>/observations \
  -H "Authorization: Bearer <WORKER_TOKEN>" \
  -H "X-Claim-Token: <claim_token from the claim response>" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "v1", "job_id": "<job_id>", "case_id": "<case_id>", "evidence_id": "<evidence_id>",
    "batch_id": "page-1", "batch_sequence": 0, "idempotency_key": "page-1",
    "observations": [], "transformations": [],
    "progress": {"schema_version": "v1", "stage": "parsing", "units_total": 10, "units_completed": 1,
                 "observations_emitted": 0, "batch_sequence": 0, "message_code": "PAGE_PARSED",
                 "occurred_at": "2026-01-01T12:00:00Z"},
    "submitted_at": "2026-01-01T12:00:00Z", "is_final_batch": false
  }'
# -> {"job_id": "...", "batch_id": "page-1", "status": "accepted", "accepted_observation_count": 0,
#     "progress": {...}, "request_id": "..."}
```

An identical retry of the same `batch_id` returns `"status": "replayed"` with no duplicate rows. `is_final_batch` is bookkeeping metadata only — the worker still submits exactly one terminal `/result` as before to complete the job.

Running its test suites specifically:

```bash
uv run pytest tests/contract/test_observation_batch.py -v                     # contract validation, no infra
uv run pytest tests/unit/evidence_lifecycle -v      # no live infra needed (fakes for repository/storage/job-producer)
uv run pytest tests/security/evidence_lifecycle -v  # module-boundary static checks
uv run pytest tests/integration/evidence_lifecycle -v   # needs postgres + minio; applies the migration itself
```

Like every other `tests/integration/*` suite, the integration suite self-skips (never fabricates a pass) if there's no `.env` at the repo root, or if PostgreSQL/MinIO specifically aren't reachable through it. `tests/integration/evidence_lifecycle/test_observation_batch_live.py` additionally needs the live API server itself reachable (`docker compose up --build -d`, or `uv run uvicorn app.main:app` against `docker compose up -d postgres neo4j redis minio`) and Neo4j reachable, since it also runs the real graph projector.

## Database migrations (Alembic)

```bash
uv run alembic revision --autogenerate -m "description"   # generate a new revision
uv run alembic upgrade head                                 # apply pending migrations
```

`migrations/env.py` reads the database URL from `app.core.config.get_settings()` — set `POSTGRES_DSN` via `.env` as usual, nothing extra to configure. `migrations/env.py` keeps `target_metadata = None` (Nipun's baseline decision); `app/modules/access_control/repository.py` and `app/modules/evidence_lifecycle/repository.py` each define their own SQLAlchemy Core `Table` objects rather than a project-wide declarative base, so their migrations (`7e8499f34f29_access_control_foundation.py`, `f2086e1e89f6_evidence_lifecycle_foundation.py`, `102857ca8d1d_worker_job_claim_and_result_foundation.py`, `af5b05e61b08_structured_source_type_routing.py`, `d3f1a6c9b8e2_worker_job_retry_limits.py`) are hand-written, not autogenerated — see `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, Decision 6.

## Troubleshooting

### Phase 4 release gate

Run the release gate with a dedicated Compose project name. Do not reuse or
tear down another developer's project:

```bash
docker compose -p tracex-phase4-gate --env-file .env.example up --build -d
docker compose -p tracex-phase4-gate --env-file .env.example ps
docker compose -p tracex-phase4-gate --env-file .env.example down -v
```

If Docker reports permission denied for its socket, do not work around it by
changing socket permissions; obtain normal Docker Desktop/daemon access and
rerun the gate. A green `docker compose config` alone is not deployment
acceptance.

### Phase 4 local audio/social worker

The audio worker uses only the established worker token, claim token, input
stream, observation-batch endpoint, and terminal result endpoint. It receives
no database, Neo4j, Redis, MinIO, object-store, cloud, or model-download
credential. To enable offline ASR, install a reviewed local bridge executable
and model bundle outside this repository, then set only non-secret paths in
your uncommitted `.env`:

```bash
COMMUNICATION_AUDIO_PROFILE=deep
COMMUNICATION_ASR_COMMAND=/opt/tracex-local-asr-bridge
COMMUNICATION_ASR_MODEL_PATH=/opt/tracex-models/asr-model.bin
COMMUNICATION_DIARIZATION_COMMAND=/opt/tracex-local-diarization-bridge
COMMUNICATION_DIARIZATION_MODEL_PATH=/opt/tracex-models/diarization-model.bin
uv run python -m app.modules.communication_processing.worker --once
```

The ASR bridge must accept `--model`, `--input`, and `--language`; the
diarization bridge accepts `--model` and `--input`; both write documented
timestamped JSON to stdout. Do not put arguments, shell fragments, tokens, or
URLs in the command setting. Leave any path unset to obtain a safe deferred
raw-audio result. `--profile rapid|deep` temporarily overrides the environment
selection for one CLI invocation.

- **`/readyz` stuck at 503`**: check `docker compose ps` — a service still starting (especially Neo4j, which is slower to become healthy) will show as `starting`/`unhealthy`. Check `docker compose logs <service>`.
- **Port already in use**: another process is bound to one of `8000/5432/7474/7687/6379/9000/9001`. Either stop it or change the corresponding `*_PORT` in `.env`.
- **`uv sync` fails to resolve**: confirm you're on the committed `uv.lock` (`git status`); if you intentionally changed dependencies, re-run `uv lock` then `uv sync`.
- **mypy complains about a third-party import**: check `[[tool.mypy.overrides]]` in `pyproject.toml` before adding a blanket `# type: ignore` — the library may just need `ignore_missing_imports` added to that list.
- **`AUTH_JWT_SECRET` validation error on startup**: it's required and must be ≥ 32 characters — see `.env.example` for a safe local placeholder and generate a stronger one for anything beyond a single developer's machine.
- **`alembic upgrade head` fails with a password/auth error**: confirm `POSTGRES_DSN` in `.env` matches the credentials `docker compose up -d postgres` was started with (`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`); if you changed `.env` after the Postgres container's first start, its data volume still has the old credentials — `docker compose down -v` for a fresh start, or update `.env` back to match.

## Integrity reconciliation

Monitor the safe `integrity.event_record_failed` structured signal, then
repair one case without reading source content:

```bash
uv run python -m app.modules.integrity.reconcile_cli --case-id <case-uuid>
```

The command is bounded (`--limit`, maximum 500), case-scoped, and idempotent.
It repairs durable evidence, correlation, structured-provenance, modality-
provenance, candidate-review-decision, and hypothesis-action leaves by
reconstructing only their original safe metadata. Do not disable integrity
append-only triggers in normal operation. A PostgreSQL superuser can bypass
them, which is an explicit trust boundary. Live Compose verification was
completed in Phase 6 Part 5 — see `docs/qa/test-results.md`'s dated entry.

## Candidate review and hypothesis workflow (Phase 6 Part 5)

```bash
# List/decide a correlation candidate (case-scoped; requires an authenticated
# access token whose membership has the review permission):
curl -s http://localhost:8000/api/v1/cases/<case_id>/candidates \
    -H "Authorization: Bearer <access_token>"
curl -s -X POST http://localhost:8000/api/v1/cases/<case_id>/candidates/<candidate_id>/review \
    -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
    -d '{"decision": "accepted_by_reviewer", "rationale": "optional, protected-database-only"}'

# Propose/review a human-authored, evidence-backed hypothesis:
curl -s -X POST http://localhost:8000/api/v1/cases/<case_id>/hypotheses \
    -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
    -d '{"statement": "...", "supporting_observation_ids": ["<uuid>"]}'
curl -s -X POST http://localhost:8000/api/v1/cases/<case_id>/hypotheses/<hypothesis_id>/review \
    -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" \
    -d '{"decision": "accepted_by_reviewer"}'
```

A decision/hypothesis-review request is idempotent on an exact retry and
rejected with `409` on a conflicting one -- it never overwrites an existing
decision. `candidate_review_decisions` and `hypothesis_actions` are
append-only, exactly like `integrity_events`; there is no update/delete
path, by design, for either. See
`docs/architecture/phase-6-review-and-hypothesis.md` for the full design.

## Evaluation foundation and dataset/model manifests (Phase 7 Part 1)

See `docs/architecture/phase-7-evaluation-and-model-governance.md` for the
full design. This part introduces no new runtime service, CLI, or
environment variable -- the four frozen configs load and validate purely
in-process:

```bash
# Load and validate every frozen config against its typed contract:
uv run python -c "
from app.modules.evaluation.manifest import load_dataset_manifest
from app.modules.evaluation.catalog import load_model_candidate_catalog
from app.modules.evaluation.results import load_benchmark_metrics_spec
from app.modules.evaluation.splits import load_synthetic_case_plan
print('datasets:', len(load_dataset_manifest().datasets))
print('candidates:', len(load_model_candidate_catalog().candidates))
print('metric groups:', len(load_benchmark_metrics_spec().metric_groups))
print('case groups:', len(load_synthetic_case_plan().case_groups))
"

# Focused test run:
uv run pytest tests/unit/evaluation/ -v
```

Editing any of `configs/benchmarks/*.v1.json` by hand is safe to try --
an invalid role/owner/storage-policy value, an unsafe `local_path_placeholder`,
a candidate marked `selected`, a case appearing in two split groups, or
Operation Nightfall appearing inside a tunable case group all fail to load
with a clear pydantic `ValidationError`, not a silent acceptance. Raw
dataset content, model weights, and benchmark-run output belong under the
five paths this phase added to `.gitignore`
(`local-data/`, `model-cache/`, `benchmark-runs/`, `private-evaluation/`,
`operation-nightfall-truth/`) -- never committed, and never referenced by
anything other than a `local_path_placeholder` string in the manifest.

## Structured-data and local OCR benchmarking (Phase 7 Part 2 — Jasraj)

See `docs/architecture/phase-7-evaluation-and-model-governance.md`'s "Part
2" section for the full design. This part benchmarks exactly three Part-1
frozen datasets against their approved candidates: `fir_icdar_2023`
(document/FIR OCR, `paddleocr-ppocrv5-mobile`/`paddleocr-ppocrv5-server`),
`gomask_voice_cdr` and `ibm_amlsim` (both `existing-deterministic-parsers`,
the same deterministic CDR/finance pipeline already in production — never
an ML/fraud model). No other dataset/candidate pair is accepted.

### Benchmark CLI

Every local root comes from an explicit environment variable or CLI flag —
never a hardcoded path:

```bash
export TRACEX_BENCHMARK_DATA_ROOT=/path/to/your/local/datasets
export TRACEX_MODEL_CACHE_ROOT=/path/to/your/local/model/cache      # OCR only
export TRACEX_BENCHMARK_OUTPUT_ROOT=./benchmark-runs                # git-ignored

# CDR / finance (no model artifact needed):
uv run python -m app.modules.structured_processing.benchmark_cli \
    --dataset-id gomask_voice_cdr --candidate-id existing-deterministic-parsers

uv run python -m app.modules.structured_processing.benchmark_cli \
    --dataset-id ibm_amlsim --candidate-id existing-deterministic-parsers

# OCR (needs a verified model name/version/SHA-256 -- see the pre-flight below):
uv run python -m app.modules.structured_processing.benchmark_cli \
    --dataset-id fir_icdar_2023 --candidate-id paddleocr-ppocrv5-mobile \
    --model-name <verified-model-name> --model-version <verified-version> \
    --model-sha256 <verified-sha256>
```

Exit code `0` means `SUCCEEDED`; `1` means a truthful `UNAVAILABLE`/`FAILED`
result (missing local data/model, every row/document failed, etc.) was
still written safely to the output root; `2` means the request itself was
rejected (unknown dataset/candidate ID, an unsupported pairing, or a
missing/misconfigured local root) before any benchmark ran. A result file
never contains raw OCR text, a raw CDR/finance row, a phone number, an
account value, a narration, or a local filesystem path — only aggregate
metrics, safe metadata, and a `failure_reason_safe` string when relevant.

Unit tests still use `FakeOcrEngine` and small synthetic CDR/finance fixtures;
they never depend on external Gate B artifacts. Real Gate B inputs, model
packages, caches, derived crops, and result JSON belong under an explicitly
configured external root such as `$HOME/tracex-gateb-artifacts/jasraj`, never
inside Git or a Docker image.

### Gate B execution status (2026-09-20)

The official AMLSim sample and both approved PP-OCRv5 candidates completed
real local benchmarks. The official GoMask Voice CDR download requires an
account and credits, so that CLI run is truthfully `unavailable` and Gate B
is not fully complete. Exact source commits, artifact hashes, commands,
backend versions, aggregate results, and result JSON hashes are recorded in
`docs/qa/test-data.md` and `docs/qa/test-results.md`.

PaddleOCR 3 uses separate detector and recognizer directories:
`<model-cache>/<mobile|server>/{det,rec}`. Each directory must contain
`inference.json`, `inference.pdiparams`, and `inference.yml`. Install the
locked optional group with `uv sync --extra ocr-benchmark`. On the verified
Linux CPU host, both candidates required `enable_mkldnn=False`; the result's
`hardware_profile` records the actual PaddleOCR/PaddlePaddle versions,
variant, CPU backend, and oneDNN state.

### Original MacBook validation handoff (historical checklist)

This checklist was written before the 2026-09-20 Gate B execution. Retain it
as the safe procedure for a repeat on an Apple-Silicon MacBook; use the dated
status above and `docs/qa/test-results.md` as the current record. Before a
repeat, follow these steps in order:

1. **Verify the branch.** `git status --short && git branch --show-current
   && git log --oneline -5` — confirm you are on the exact, unmerged
   `jasraj` branch and commit this handoff refers to. Do not run against
   any other branch or a locally modified tree.
2. **Install PaddleOCR reproducibly — never `pip install paddleocr` ad
   hoc.** `uv sync --extra ocr-benchmark` installs the exact
   `paddleocr`/`paddlepaddle` versions this branch's `uv.lock` was locked
   against (`[project.optional-dependencies] ocr-benchmark` in
   `pyproject.toml`). An ad hoc `pip install paddleocr` would silently
   resolve whatever is newest on PyPI that day — a different, unpinned
   version than this branch's verified PaddleOCR 3 wiring, making a repeat
   impossible to compare. If the locked package cannot construct the engine,
   preserve the failed/unavailable result and investigate the exact API or
   backend error before changing code.
3. **Re-check licence/source status before downloading.** FIR ICDAR 2023,
   AMLSim, and PaddleOCR provenance is pinned in the benchmark manifests and
   QA docs. GoMask remains account-and-credit gated with plan-dependent terms;
   do not substitute another CDR dataset.
4. **Update only safe fields.** Add safe version/source metadata when newly
   verified; never change a candidate to `selected`, swap datasets, or alter
   a success-metric threshold after seeing a result.
5. **Download each artifact into a git-ignored local directory only** —
   under whatever path you point `TRACEX_BENCHMARK_DATA_ROOT`/
   `TRACEX_MODEL_CACHE_ROOT` at (e.g. this repo's own gitignored
   `local-data/`/`model-cache/`, or any other local path). Never commit a
   downloaded file, a raw dataset fragment, or a model weight.
6. **Compute and locally record each artifact's real SHA-256** —
   `shasum -a 256 <file>` — needed for `--model-sha256` on an OCR run and
   useful to note for the dataset files too, even though only the model
   weight's hash is required by the contract.
7. **Run the benchmark CLI commands above** with your own local
   environment variables pointing at the real downloaded data/model cache.
8. **Record the actual hardware/execution backend you observed** — do not
   assume Apple's Metal/MPS, CUDA, or any particular accelerator is in use
   just because you're on a MacBook; report whatever `hardware_profile`/
   hardware/backend PaddleOCR itself actually reports on this machine (or
   `cpu` if that's genuinely what ran). Note that `paddlepaddle`'s official
   PyPI wheels have had limited/no CUDA relevance on Apple Silicon in any
   case — CPU execution on the Mac is the expected, not a degraded, path
   unless you deliberately verify a Metal-accelerated build yourself.
9. **Send back only the safe aggregate result JSON files** (from your
   `TRACEX_BENCHMARK_OUTPUT_ROOT`) and terminal summaries — never a raw
   dataset file, image, CDR/finance row, or model weight.
10. **Leave every candidate's `selection_status` as `candidate`/
    `conditional`.** Do not mark any Part 2 candidate `selected` — Gate C
    makes that decision later, after Parts 2-4 all have comparable results.

If an artifact is genuinely unavailable, its licence terms can't be
resolved, or a candidate fails to load, run the CLI anyway and let it
produce its own truthful `UNAVAILABLE`/`FAILED` result — never substitute
a different dataset or model silently, and never report a benchmark as
having succeeded unless the CLI's own exit code and result JSON say so.

## Visual benchmark foundation (Phase 7 Part 3 — Gaurav)

See `docs/architecture/phase-7-evaluation-and-model-governance.md`'s "Part
3" section for the full design. This part benchmarks exactly three Part-1
frozen datasets against their approved candidates: `virat_ground`
(detection: `yolo11n`/`yolo11s`; tracking: `bytetrack`),
`safe_unsafe_behaviour` (detection only — additional stress-testing, no
behaviour-classification candidate exists), `ufpr_alpr` (plate-region
detection: `yolo11n`/`yolo11s`; plate OCR:
`paddleocr-lightweight-visual-text`, licence-gated). No other
dataset/candidate pair is accepted.

### Benchmark CLI

```bash
# List every approved dataset/candidate/pair this harness supports:
uv run python -m app.modules.media_processing.visual_benchmark_cli list-candidates

export TRACEX_BENCHMARK_DATA_ROOT=/path/to/your/local/datasets
export TRACEX_MODEL_CACHE_ROOT=/path/to/your/local/model/cache
export TRACEX_BENCHMARK_OUTPUT_ROOT=./benchmark-runs   # git-ignored

# Validate the request/local configuration without running anything:
uv run python -m app.modules.media_processing.visual_benchmark_cli validate \
    --dataset-id virat_ground --candidate-id yolo11n

# Run a real benchmark (needs a verified model name/version/SHA-256 --
# see the pre-flight below):
uv run python -m app.modules.media_processing.visual_benchmark_cli run \
    --dataset-id virat_ground --candidate-id yolo11n \
    --model-name <verified-model-name> --model-version <verified-version> \
    --model-sha256 <verified-sha256>
```

Exit code `0` means `succeeded`; `1` means a truthful `unavailable`/
`failed` result (missing local data/model, an unresolved licence, every
sample failed, etc.) was still written safely to the output root; `2`
means the request itself was rejected (unknown dataset/candidate ID or an
unsupported pairing) before any benchmark ran. A result file never
contains a raw video frame, plate text, face, or local filesystem path —
only aggregate metrics, safe metadata, and a `failure_reason_safe` string
when relevant.

**`virat_ground`/`yolo11n`/`yolo11s`/`bytetrack` are now
`license_status: verified_restricted_noncommercial`** after Gate B
(2026-09-20) read the real governing licences directly — `run` produces
genuine `succeeded` results for these, given a real local clip and
weights (see "Gate B execution status" below). `safe_unsafe_behaviour`
and `ufpr_alpr`/`paddleocr-lightweight-visual-text` remain
`license_status: pending_verification` — `run` still reports
`unavailable` for every combination involving them, legitimately (no
pinned source for the former; an academic access-request gate for the
latter).

The committed unit test suite for this part still runs entirely against
`Fake*` engines and small synthetic detection/tracking/OCR fixtures
(`uv run pytest tests/unit/media_processing/test_visual_benchmark_*.py
-v`), independent of what is or isn't installed on any given machine.
The integration test that runs the real CLI as a subprocess
(`tests/integration/media_processing/test_visual_benchmark_smoke.py`)
self-skips cleanly when `TRACEX_BENCHMARK_DATA_ROOT` is unset, and ran
for real (2 passed, 1 skipped) against Gate B's own downloaded VIRAT
clip.

### Reproducible install: `uv sync --extra video-benchmark`

`ultralytics`/`paddleocr`/`paddlepaddle`/`lap` are declared in
`pyproject.toml`'s `[project.optional-dependencies] video-benchmark`
group and resolved into `uv.lock` — not installed by the standard `uv
sync --all-groups` verification command. Install the exact pinned
versions via:

```bash
uv sync --extra video-benchmark
```

**Never `pip install ultralytics`/`pip install paddleocr` ad hoc** — that
would silently resolve whatever is newest on PyPI that day, a different,
unpinned version than this branch's engine wiring was written against,
making any resulting benchmark number impossible to reproduce later.

**Two side effects this install has on the rest of the repository, both
already fixed on this branch**: `ultralytics` ships a `py.typed` marker,
so `mypy` behaves differently once it's genuinely installed (see
`pyproject.toml`'s dedicated `[[tool.mypy.overrides]]` entry for
`ultralytics.*`); and `ultralytics` ships its own colliding top-level
`tests/__init__.py`, which shadows this project's own `tests` package
once installed and breaks every `from tests.fixtures... import ...`
statement repository-wide unless this project's own `tests/__init__.py`
exists (it now does). Neither requires any action from you — just be
aware if you ever see `tests.fixtures` import errors after installing a
new optional extra elsewhere in this project.

### Gate B execution status (2026-09-20)

Gate B ran directly on Shreshtha's laptop. Real host profile: Arch Linux,
kernel 7.2.4-arch1-2, x86_64, Intel Core i5-13420H (12 logical CPUs),
15 GiB RAM, Python 3.12.13 (via `uv`); no GPU (confirmed via `nvidia-smi`
reporting no driver). `virat_ground` + `yolo11n`/`yolo11s`/`bytetrack`
produced real, `succeeded` results against one small, officially
downloaded VIRAT clip and its real annotations, plus two real,
officially-released YOLO11 weight files — all outside Git under
`$HOME/tracex-gateb-artifacts/gaurav`. `ufpr_alpr` and
`safe_unsafe_behaviour` remain legitimately deferred (see
`docs/qa/known-limitations.md`). Exact source URLs, item IDs, hashes,
commands, and measured metrics are in `docs/qa/test-data.md` and
`docs/qa/test-results.md`'s dated Gate B sections — not repeated here.

Access to the VIRAT Video Dataset Usage Agreement (a genuine click-through
protection agreement) was confirmed already granted by the project owner
before any download, per this task's own "never accept an agreement on
the agent's behalf" rule. Anyone repeating this Gate B run on a different
dataset owner's behalf must independently confirm the same before
downloading anything from `data.kitware.com`.

If you repeat this on a fresh checkout, follow the same steps as the
"Original MacBook pre-flight checklist" below for `ufpr_alpr`/
`safe_unsafe_behaviour` specifically (they still need real licence
verification and a real local download); `virat_ground`'s licence is
already resolved and recorded.

### Original MacBook Gate B pre-flight checklist (historical, retained for `ufpr_alpr`/`safe_unsafe_behaviour`)

This checklist was written before the 2026-09-20 Gate B execution above.
`virat_ground`/`yolo11n`/`yolo11s`/`bytetrack` no longer need it — their
licence/source status is already resolved and recorded. Retain it as the
safe procedure for `ufpr_alpr`/`safe_unsafe_behaviour` specifically, or
for a repeat run on a different machine. Follow these steps in order:

1. **Check out the exact pushed `gaurav` commit.** `git status --short &&
   git branch --show-current && git log --oneline -5` — confirm you are
   on the exact, unmerged commit this handoff refers to. Do not run
   against any other branch or a locally modified tree.
2. **Install reproducibly.** `uv sync --extra video-benchmark` — never an
   ad hoc `pip install`. If `visual_benchmark._build_real_detector_
   engine`/`_build_real_tracker_engine`/`_build_real_visual_text_engine`'s
   wiring needs an adjustment to match the installed package's actual API
   (a real possibility — this session could not import or exercise any of
   them), fix it on this same branch and note the exact version that
   required the fix. `_build_real_tracker_engine` already wires
   Ultralytics' own bundled `BYTETracker`
   (`model.track(..., tracker='bytetrack.yaml')`'s underlying class) --
   no separate ByteTrack package is needed.
3. **Resolve and record licence/source terms before downloading
   anything**: UFPR-ALPR and Safe/Unsafe Behaviour, plus the PaddleOCR
   PP-OCRv5 mobile-lightweight release (needed only for `ufpr_alpr`'s
   plate-OCR pairing). VIRAT Ground and both Ultralytics YOLO11 releases
   are already resolved (`verified_restricted_noncommercial`, recorded
   2026-09-20) — do not re-verify unless the terms may have changed.
4. **Update only safe fields.** If verification succeeds, change the
   relevant `license_status` from `pending_verification` to `verified_
   permissive`/`verified_restricted_noncommercial` (or to a documented
   blocked state if unusable), and add safe version/source metadata —
   never split definitions, never a `selected` status, never a dataset
   swap, never a change to a success-metric threshold after seeing a
   result.
5. **Download each approved artefact into a git-ignored local directory
   only** — under whatever path you point `TRACEX_BENCHMARK_DATA_ROOT`/
   `TRACEX_MODEL_CACHE_ROOT` at (e.g. this repo's own gitignored
   `local-data/`/`model-cache/`, or any other local path). Never commit a
   downloaded file, a raw video/image, a plate crop, or a model weight.
6. **Compute and locally record each artifact's real SHA-256** —
   `shasum -a 256 <file>` — needed for `--model-sha256` on a real run.
7. **Run the benchmark CLI commands above** with your own local
   environment variables pointing at the real downloaded data/model
   cache.
8. **Record the actual hardware/execution backend you observed** — do
   not assume Apple's Metal/MPS, CUDA, or any particular accelerator is
   in use just because you're on a MacBook; report whatever
   `hardware_profile`/backend Ultralytics/PaddleOCR themselves actually
   report on this machine (or `cpu` if that's genuinely what ran).
9. **Send back only the safe aggregate result JSON files** (from your
   `TRACEX_BENCHMARK_OUTPUT_ROOT`) and terminal summaries — never a raw
   video/image file, plate crop, face, or model weight. Leave every
   candidate's `selection_status` as `candidate`/`conditional` — do not
   mark any Part 3 candidate `selected`; Gate C makes that decision later,
   after Parts 2-4 all have comparable results.

If an artifact is genuinely unavailable, its licence terms can't be
resolved, or a candidate fails to load, run the CLI anyway and let it
produce its own truthful `unavailable`/`failed` result — never substitute
a different dataset or model silently, and never report a benchmark as
having succeeded unless the CLI's own exit code and result JSON say so.

## Audio and social/chat benchmark foundation (Phase 7 Part 4 — Sarthak)

See `docs/architecture/phase-7-evaluation-and-model-governance.md`'s "Part
4" section for the full design. This part benchmarks exactly four Part-1
frozen datasets against their approved candidates: `common_voice_indic`
(ASR: `faster-whisper-small`/`faster-whisper-medium`; language ID:
`fasttext-lid176`), `ami_meeting_corpus` (VAD: `silero-vad-v6`;
diarization: `pyannote-community-local`/`deterministic-diarization-
fallback`), `vast_social_text`/`vast_2014_mixed_records` (social/chat
extraction: `existing-deterministic-social-parsers`). No other
dataset/candidate pair is accepted.

### Benchmark CLI

```bash
# List every approved dataset/candidate/pair this harness supports:
uv run python -m app.modules.communication_processing.audio_social_benchmark_cli list-candidates

export TRACEX_BENCHMARK_DATA_ROOT=/path/to/your/local/datasets
export TRACEX_MODEL_CACHE_ROOT=/path/to/your/local/model/cache   # not needed for the social baseline
export TRACEX_BENCHMARK_OUTPUT_ROOT=./benchmark-runs             # git-ignored

# Validate the request/local configuration without running anything:
uv run python -m app.modules.communication_processing.audio_social_benchmark_cli validate \
    --dataset-id common_voice_indic --candidate-id faster-whisper-small

# Run a real benchmark (ASR/VAD/diarization need a verified model name/
# version/SHA-256 -- see the pre-flight below; the social baseline needs
# neither a model cache root nor a verified artifact, since it wraps this
# project's own existing deterministic parsers):
uv run python -m app.modules.communication_processing.audio_social_benchmark_cli run \
    --dataset-id vast_social_text --candidate-id existing-deterministic-social-parsers
```

Exit code `0` means `succeeded`; `1` means a truthful `unavailable`/
`failed` result (missing local data/model, an unresolved licence, every
sample failed, etc.) was still written safely to the output root; `2`
means the request itself was rejected (unknown dataset/candidate ID or an
unsupported pairing) before any benchmark ran. A result file never
contains a raw transcript, message, participant name, phone number,
handle, speaker label, credential, Hugging Face token, evidence URI, or
local filesystem path — only aggregate metrics, safe metadata, and a
`failure_reason_safe` string when relevant. Runs as a local host process
— never through Docker Compose.

**`vast_social_text`/`vast_2014_mixed_records` +
`existing-deterministic-social-parsers` is already licence-cleared** —
unlike every ASR/VAD/diarization pair (all currently blocked by
unresolved licence status or a `conditional` selection status), this one
pair is ready to run for real the moment a local dataset directory
exists, with no optional package install and no gated-model token needed
at all. This is Part 4's most immediately actionable path for Aditya's
pre-flight.

No faster-whisper/pyannote.audio/fastText installation, real dataset, or
GPU exists on Shreshtha's laptop — every unit test for this part runs
against `Fake*Engine`s and the real (but input-only-synthetic)
deterministic social extractor (`uv run pytest tests/unit/
communication_processing/test_audio_social_benchmark_*.py -v`). The
integration test that runs the real CLI as a subprocess
(`tests/integration/communication_processing/
test_audio_social_benchmark_smoke.py`) self-skips cleanly here, since
`TRACEX_BENCHMARK_DATA_ROOT` is unset.

### Reproducible install: `uv sync --extra audio-social-benchmark`

`faster-whisper`/`pyannote-audio`/`fasttext`/`torch` are declared in
`pyproject.toml`'s `[project.optional-dependencies] audio-social-
benchmark` group and resolved into `uv.lock` — never installed by the
standard `uv sync --all-groups` verification command, and not installed
anywhere on this development machine. Aditya's Mac installs the exact
pinned versions via:

```bash
uv sync --extra audio-social-benchmark
```

**Never `pip install faster-whisper`/`pip install pyannote.audio`/
`pip install fasttext`/`pip install torch` ad hoc** — that would silently
resolve whatever is newest on PyPI that day, a different, unpinned
version than this branch's engine wiring was written against, making any
resulting benchmark number impossible to reproduce later.

`torch` was added to this extra specifically for `_build_real_vad_engine`'s
Silero VAD wiring, by explicit team decision — it remains scoped to this
one optional extra and is never a production/base dependency, and
`communication_processing`'s production ASR/diarization adapters still
import neither `torch` nor any other heavyweight ML toolkit (see
`audio/asr_adapter.py`'s docstring and `docs/qa/known-limitations.md`).

**This benchmark never downloads a model from the network itself.**
`_build_real_vad_engine` calls `torch.hub.load(..., source="local")`
against a snapshot staged entirely by Aditya beforehand — it never fetches
`snakers4/silero-vad` from GitHub, and never uses `source="github"`. The
same is true of every other real engine here: `faster-whisper`,
`pyannote.audio`, and fastText's `lid.176` are all loaded from local
paths under `TRACEX_MODEL_CACHE_ROOT` that Aditya stages manually — see
"MacBook Gate B pre-flight" below for exactly what to stage, where, and
how each artifact's SHA-256 is recorded and verified.

### Gate B execution status (2026-09-20)

Gate B ran directly on Shreshtha's laptop (no separate MacBook available).
Real host profile: Arch Linux, kernel 7.2.4-arch1-2, x86_64, Intel Core
i5-13420H (12 logical CPUs), 15 GiB RAM, Python 3.12.13 (via `uv`); no GPU
(confirmed via `torch.cuda.is_available()` returning `False` and
`nvidia-smi` reporting no driver — `torch==2.14.0+cu130` was installed
because that is the pinned resolved wheel, not because a GPU was used).
`ami_meeting_corpus` + `silero-vad-v6` produced a real, `succeeded` VAD
result against one real, fixed 20-utterance subset from one real AMI
meeting, benchmarked with a real, offline-staged `snakers4/silero-vad`
snapshot — all outside Git under `$HOME/tracex-gateb-artifacts/sarthak`.
`ami_meeting_corpus` + `deterministic-diarization-fallback` produced a
real, correctly-attributed `unavailable` result (no local raw-audio
speaker-segmentation model exists in this phase, by design).
`pyannote-community-local` remains genuinely blocked (licence/conditional
status untouched). `common_voice_indic` and `vast_social_text`/
`vast_2014_mixed_records` remain legitimately deferred (see
`docs/qa/known-limitations.md`). Exact source URLs, hashes, commands, and
measured metrics are in `docs/qa/test-data.md` and
`docs/qa/test-results.md`'s dated Gate B sections — not repeated here.

A real, narrow defect in this runbook's own pre-flight checklist below
was found and fixed during Gate B: step 4's `shasum -a 256
$TRACEX_MODEL_CACHE_ROOT/silero-vad/files/silero_vad.jit` path assumed an
outdated `snakers4/silero-vad` repository layout. A real clone (commit
`60b7ffa2`, 2026-09-17) confirms the actual current path is
`$TRACEX_MODEL_CACHE_ROOT/silero-vad/src/silero_vad/data/silero_vad.jit`
— `_SILERO_VAD_WEIGHT_RELATIVE_PATH` in `audio_social_benchmark.py` and
this runbook's own step 4 below were both corrected to match.

If you repeat this on a fresh checkout, follow the same steps as the
"Original MacBook pre-flight checklist" below for `common_voice_indic`/
`vast_social_text`/`vast_2014_mixed_records`/`pyannote-community-local`
specifically (they still need an accessible real source or an accepted
local-use agreement); `ami_meeting_corpus`'s and `silero-vad-v6`'s
licences are already resolved and recorded.

### Original MacBook Gate B pre-flight checklist (historical, retained for `common_voice_indic`/`vast_social_text`/`vast_2014_mixed_records`/`pyannote-community-local`)

This checklist was written before the 2026-09-20 Gate B execution above.
`ami_meeting_corpus`/`silero-vad-v6`/`deterministic-diarization-fallback`
no longer need it — their licence/source status is already resolved and
recorded (or, for the deterministic fallback, correctly reports
`unavailable` by design, needing no licence at all). Retain it as the
safe procedure for `common_voice_indic`/`vast_social_text`/
`vast_2014_mixed_records`/`pyannote-community-local` specifically, or for
a repeat run on a different machine. Follow these steps in order:

1. **Fetch and check out the exact pushed `sarthak` commit.** `git fetch
   && git status --short && git branch --show-current && git log
   --oneline -5` — confirm you are on the exact, unmerged commit this
   handoff refers to. Do not run against any other branch or a locally
   modified tree.
2. **Install reproducibly.** `uv sync --extra audio-social-benchmark` —
   never an ad hoc `pip install`. If `audio_social_benchmark._build_real_
   asr_engine`/`_build_real_vad_engine`/`_build_real_diarization_engine`/
   `_build_real_language_id_engine`'s wiring needs an adjustment to match
   an installed package's actual API (a real possibility — this session
   could not import or exercise any of them, including `torch`/Silero VAD
   via `torch.hub.load`), fix it on this same branch and note the exact
   version that required the fix.
3. **Resolve and record licence/source terms before any download**:
   Common Voice Indic, AMI Meeting Corpus, VAST (social text and 2014
   mixed records), the two faster-whisper release sizes, Silero VAD v6,
   pyannote.audio's community pipeline (including its local-use terms —
   `pyannote-community-local` is `conditional` specifically because these
   are not yet accepted), and fastText's `lid.176` model. `vast_social_
   text`/`vast_2014_mixed_records` are already `license_status:
   verified_permissive` in the frozen manifest — confirm this still holds
   and record the exact release/version you use, rather than re-opening
   the licence question from scratch.
4. **Stage Silero VAD as a local Torch Hub source snapshot — never let
   this benchmark fetch it itself.** `_build_real_vad_engine` refuses to
   call `torch.hub.load` against GitHub; it only accepts
   `torch.hub.load(..., source="local")` against a snapshot Aditya stages
   himself. Clone (or otherwise obtain) the exact `snakers4/silero-vad`
   revision you've verified the licence/source terms for into
   `$TRACEX_MODEL_CACHE_ROOT/silero-vad/` (a plain local checkout with
   `hubconf.py` at its root — the same layout `git clone` produces).
   Record that exact revision/tag as the `--model-version` you pass to
   the CLI. Compute `shasum -a 256
   $TRACEX_MODEL_CACHE_ROOT/silero-vad/src/silero_vad/data/silero_vad.jit`
   (confirmed against a real clone, commit `60b7ffa2`, during the
   2026-09-20 Gate B execution above — this superseded an earlier,
   incorrect `files/silero_vad.jit` assumption) and pass it as
   `--model-sha256`; a mismatch against what's actually staged blocks the
   run safely rather than loading unverified weights.
5. **Stage fastText's `lid.176` and its required faster-whisper
   transcription stage, and record both as separate verified
   artifacts.** `fasttext-lid176` classifies text, not audio, so
   `_build_real_language_id_engine` chains a `faster-whisper`
   transcription stage in front of it purely to produce text to classify
   — a completed result is rejected unless *both* are staged and
   verified, not just fastText's own model. Place fastText's model at
   `$TRACEX_MODEL_CACHE_ROOT/lid.176.ftz` and a downloaded faster-whisper
   model at `$TRACEX_MODEL_CACHE_ROOT/lid-transcription-stage/` (with its
   weight file at `lid-transcription-stage/model.bin` — adjust if the
   installed faster-whisper's actual file layout differs). Compute each
   file's SHA-256 separately and pass them as `--model-sha256` (for
   `lid.176.ftz`) and `--transcription-stage-model-sha256` (for
   `lid-transcription-stage/model.bin`), alongside
   `--transcription-stage-model-name`/`--transcription-stage-model-version`
   naming the exact faster-whisper release used as the transcription
   stage. Both hashes are verified against the actually-staged files
   before either model loads.
6. **Update only safe fields.** If verification succeeds, change the
   relevant `license_status`/`selection_status` (e.g. lifting
   `pyannote-community-local` from `conditional` to `candidate` only if
   its local-use terms are genuinely accepted), and add safe version/
   source metadata — never split definitions, never a `selected` status,
   never a dataset swap, never a change to a success-metric threshold
   after seeing a result.
7. **Download only approved datasets and weights into a git-ignored local
   directory** — under whatever path you point `TRACEX_BENCHMARK_DATA_
   ROOT`/`TRACEX_MODEL_CACHE_ROOT` at. Never commit a downloaded file, a
   raw recording, a transcript, a chat export, or a model weight.
8. **Record source/version, candidate configuration, and SHA-256
   values** — `shasum -a 256 <file>` — needed for `--model-sha256` (and,
   for `fasttext-lid176`, `--transcription-stage-model-sha256`) on a real
   ASR/VAD/diarization/language-ID run; steps 4 and 5 above already cover
   Silero VAD's and the language-ID transcription stage's specific files.
9. **Use any gated-model token only from your own local environment,
   never Git or logs.** If pyannote.audio's pipeline requires a Hugging
   Face access token, export it as your own local shell/environment
   variable (e.g. `HF_TOKEN`) — never commit it, never put it in
   `.env.example`, never print it, and never let it appear in a benchmark
   result or log line.
10. **Record the actual observed runtime backend and resource use** — do
   not assume Apple's Metal/MPS, CUDA, or any particular accelerator is
   in use just because you're on a MacBook; report whatever
   `hardware_profile`/backend faster-whisper/pyannote.audio/fastText
   themselves actually report on this machine (or `cpu` if that's
   genuinely what ran).
11. **Run approved benchmarks and return only safe aggregate results** —
   never a raw transcript, message, participant name, phone number,
   handle, speaker label, or model weight. Leave every candidate's
   `selection_status` as `candidate`/`conditional` unless step 6 above
   genuinely resolved it — do not mark any Part 4 candidate `selected`;
   a future versioned evaluation cycle makes that decision. Gate C froze
   evidence and retained the deterministic correlation baseline; it did not
   select a modality winner.

If an artifact is genuinely unavailable, its licence terms can't be
resolved, or a candidate fails to load, run the CLI anyway and let it
produce its own truthful `unavailable`/`failed` result — never substitute
a different dataset or model silently, and never report a benchmark as
having succeeded unless the CLI's own exit code and result JSON say so.

## Gate C configuration disable and recovery

The normal local setting is:

```dotenv
RELEASE_CONFIGURATION_ID=tracex-release-v1-baseline
RELEASE_CONFIGURATION_DISABLED=false
```

Only IDs in `configs/benchmarks/release-freeze.v1.json` are accepted. Never
point this setting at a file, model name, or local artifact path.

To stop relationship scoring without changing or deleting evidence, set:

```dotenv
RELEASE_CONFIGURATION_DISABLED=true
```

Restart the API/intelligence worker process so settings are reloaded. A
correlation generation attempt then fails before case observations are read
and emits the safe `evaluation.release_configuration_disabled` signal. Existing
correlations and queued durable outbox events are not deleted. Record the
operator/time/reason in the deployment's change log; no case content belongs
in that record.

After the incident is resolved, verify the committed freeze and focused tests,
restore `RELEASE_CONFIGURATION_DISABLED=false`, and restart. Do not bypass a
hash mismatch or unknown ID: restore the known committed configuration, or
create a separately reviewed/versioned release freeze.
