# Phase 1 Test Results

Actual command output from verification runs. Updated by whoever runs verification — do not hand-edit a "passing" result without having actually run the command.

## 2026-09-11 — Nipun — Phase 2: Evidence Lifecycle and Durable Processing Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.4.1 / Compose v5.1.3. Branch `nipun`, clean tree, not diverged from `origin/main`, confirmed via `git status --short`/`git branch --show-current`/`git fetch origin` before starting. Inspected and reused, rather than duplicated: `app.modules.access_control.dependencies.require_case_action`/`require_evidence_read`, `record_audit_event`, the hand-written-`sa.Table`/Alembic convention, the `asyncio.to_thread`-wrapped MinIO client-construction pattern, and `structured_processing`'s `SourceResolver` protocol shape (satisfied structurally, no cross-module import).

```bash
$ uv add python-multipart
Resolved 66 packages in 1.22s
 + python-multipart==0.0.32
```
Result: **pass**. The only new dependency (FastAPI requires it for `File`/`Form` multipart parsing) — confirmed genuinely absent first via `uv run python -c "import multipart"` failing with `ModuleNotFoundError`.

```bash
$ uv sync --all-groups
```
Result: **pass**.

```bash
$ uv run ruff format --check .
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 117 source files
```
Result: **pass**, all three. (One real mypy finding fixed before this record: `MinioObjectStorage.put_object`'s `data: IO[bytes]` parameter didn't satisfy `asyncio.to_thread`'s `BinaryIO` expectation for `minio.Minio.put_object` — changed the `ObjectStorage` protocol and both implementations to `BinaryIO` throughout `storage.py`.)

```bash
$ uv run pytest -q
948 passed in 12.97s
```
Result: **pass**, no regressions. 65 new tests this build: `tests/unit/evidence_lifecycle/` (29), `tests/security/evidence_lifecycle/` (34), `tests/integration/evidence_lifecycle/` (2, run live — see below). 883 total collected before this build (864 passed + 19 skipped, Gaurav's entry below) + 65 = 948; this run had `.env` present and live infra up (see below), so every previously-self-skipping suite across every other module also ran live instead of skipping, which is why `0 skipped` here rather than the expected `19` — not a regression, a side effect of infra being up for this build's own live verification.

```bash
$ docker compose config
```
Result: **pass** (exit 0) — validates the new `MAX_EVIDENCE_BYTES` env passthrough on the `api` service alongside the existing ones.

### Docker build: transient sandbox network flakiness (not a code defect)

```bash
$ docker compose up --build -d
...
#12 25.05   × Failed to download `opencv-python-headless==5.0.0.93`
#12 25.05   ├─▶ dns error / failed to lookup address information: Try again
```
Retried twice more (once after confirming `docker ps`/a synthetic `docker build` both had working outbound network): failed a second time on the same package, then a third time on `asyncpg` instead — three different pre-existing dependencies (unrelated to this task's own code, already present before this build), each failing ~24s into `uv sync` inside the BuildKit build network with an intermittent DNS resolution failure against `files.pythonhosted.org`. A direct `docker run alpine wget https://files.pythonhosted.org/` from the same daemon succeeded once and failed once across two attempts, confirming this is environment-level flakiness in this sandbox's Docker networking, not a dependency-resolution or code problem. Used the documented "Option B" workflow instead (infra in Docker, API on host) to complete live verification without waiting on a fresh image build — see below for the full live smoke test.

**Resolved**: the user re-ran `docker compose up --build -d` afterward (in the background, ~153s for `uv sync` alone this time — a fourth package, `neo4j`, hit the same transient DNS failure mid-attempt before it eventually succeeded), and it completed clean:

```bash
$ docker compose ps
tracex-api-1        Up   (running)
tracex-minio-1      Up   (healthy)
tracex-neo4j-1      Up   (healthy)
tracex-postgres-1   Up   (healthy)
tracex-redis-1      Up   (healthy)

$ curl .../healthz    # {"status":"ok","service":"tracex-api","version":"0.1.0"}                              200
$ curl .../readyz     # {"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}} 200
$ curl .../api/v1/meta/contracts                                                                                200
```
Result: **pass** — the full containerized stack, including the `api` image, builds and runs correctly. Confirms the earlier failures were exactly what they looked like (sandbox network flakiness across four different unrelated packages, never the same one twice), not a defect in this task's `Dockerfile`/`compose.yaml`/dependency changes. `docs/qa/known-limitations.md` and `docs/progress/mvp-progress.md` updated to close this out.

```bash
$ docker compose up -d postgres neo4j redis minio
```
Result: **pass** — all four reached `healthy` (confirmed via polled `docker compose ps`).

```bash
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 3e8cbaa07711, baseline
INFO  [alembic.runtime.migration] Running upgrade 3e8cbaa07711 -> 7e8499f34f29, access control foundation
INFO  [alembic.runtime.migration] Running upgrade 7e8499f34f29 -> f2086e1e89f6, evidence lifecycle foundation
```
Result: **pass** — the full chain applies cleanly against a genuinely fresh database, including the new revision. (The local `.env` predated the `AUTH_JWT_*`/`MAX_EVIDENCE_BYTES` settings entirely — a pre-existing gap unrelated to this task — so the missing required-setting block from `.env.example` was appended to the local, git-ignored `.env` before this could run.)

```bash
$ uv run pytest tests/integration/evidence_lifecycle -v
test_migration_created_tables_enforce_uniqueness_and_fks PASSED
test_real_upload_flow_against_live_postgres_and_minio PASSED
2 passed in 1.07s
```
Result: **pass — run live**, not self-skipped. Confirms the partial unique index on `(case_id, upload_idempotency_key)` and the FK from `evidence_records.uploaded_by` to `users.user_id` are both enforced by real PostgreSQL (the second one caught a real test-fixture bug — see below), and a real upload through `EvidenceLifecycleService` against real MinIO round-trips byte-identical content.

### Live end-to-end HTTP smoke test (beyond the required command list)

Ran a real `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000` process against the live containers, exercised over real HTTP with `curl`, plus direct Redis/MinIO inspection:

```bash
$ curl .../healthz     # {"status":"ok",...}                                                  200
$ curl .../readyz      # postgres:ok, neo4j:ok, redis:ok, minio:ok                             200
$ curl .../api/v1/meta/contracts                                                               200
$ curl -X POST .../auth/register ... ; curl -X POST .../auth/login ...                         201, 200
# seeded a real case + active membership directly via AccessControlRepository (no case-CRUD API exists)
$ curl -X POST .../cases/<case_id>/evidence -H "Idempotency-Key: smoke-2-key" -F file=@fir_fixture.txt \
    -F source_type=document -F classification=unclassified
{"evidence":{...,"sha256":"b54a...","processing_status":"queued",...},
 "job":{...,"processor_name":"fir_report_text_v1","status":"queued","dispatched_at":"2026-09-11T03:47:11.414906Z",...}}
                                                                                                201
$ curl -X POST ... (same Idempotency-Key, same file again)   # identical evidence_id/job_id     200
$ curl -X POST ... (same case, file declared as video/mp4 against source_type=document)
{"error":{"code":"validation_error","message":"content_type 'video/mp4' is not accepted for source_type 'document'",...}}
                                                                                                422
```
```bash
$ uv run python3 -c "... MinioObjectStorage(settings).read_bytes(<object_uri>) ..."
b'FIR No. 999/2026 filed at Sample Police Station. Section 420 IPC.\n'   # byte-identical to the uploaded fixture

$ docker compose exec -T redis redis-cli LLEN tracex:jobs:document
2
$ docker compose exec -T redis redis-cli LRANGE tracex:jobs:document 0 -1
# both pushed jobs' canonical JSON: schema_version, job_id, case_id, evidence_id, source_type,
# processor_name, processor_version, attempt, idempotency_key, input_object_uri, requested_at —
# no extra fields, no credentials. Exactly 2 entries for 2 distinct uploads (the idempotent
# replay above did NOT push a duplicate).
```
Result: **pass** — real evidence metadata, real private MinIO object, real durable+published job, idempotent replay, and rejection of an unsupported content type, all verified against live infrastructure. `docker compose ps` afterward: `minio`/`neo4j`/`postgres`/`redis` all `healthy`, left running (nothing destroyed); the manual host `uvicorn` verification process was stopped.

### Two real bugs caught and fixed during this build (before this record)

- **`UploadOutcome.job.dispatched_at` was always `null` on a fresh upload, even when the same-request Redis publish actually succeeded.** `service.upload_evidence` built the returned `UploadOutcome` from the in-memory `WorkerJobRecord` constructed *before* calling `_dispatch`, and `_dispatch` updated only the database row, never the caller's local variable. Caught live: the smoke-test upload response showed `"dispatched_at": null` immediately after a successful upload, while a subsequent `GET /jobs/{job_id}` (which re-reads from PostgreSQL) would have shown the correct timestamp — a real, if non-critical, response-accuracy bug that no unit test (which never checked the *returned* value's freshness, only the repository's stored value) had caught. Fixed by having `_dispatch` return the updated (or, on a deferred publish, unchanged) `WorkerJobRecord`, used to build the final `UploadOutcome`. Added a regression assertion (`outcome.job.dispatched_at is not None`) to `tests/unit/evidence_lifecycle/test_upload_service.py::test_valid_upload_creates_evidence_storage_write_and_job`.
- **A test-fixture bug (not an application bug), also only caught live.** `tests/integration/evidence_lifecycle/`'s two integration tests originally passed a bare `uuid4()` for `uploaded_by`, which violates `evidence_records.uploaded_by`'s real foreign key onto `users.user_id` (`FakeEvidenceLifecycleRepository`, used everywhere else, doesn't enforce foreign keys at all — exactly the class of bug live integration tests exist to catch, the same lesson `docs/decisions/ADR-003-...md` Decision 7 already recorded for `access_control`). Fixed by adding a `seeded_user` fixture (mirroring the existing `seeded_case`) to `tests/integration/evidence_lifecycle/conftest.py` and using it for `uploaded_by` in both tests.

```bash
$ git status --short -- app/modules/graph app/modules/structured_processing app/modules/communication_processing app/modules/media_processing app/contracts
(no output)
```
Result: **pass** — zero diff against every other contributor's owned module and the frozen contracts.

### Known limitations and intentionally deferred work

No worker consumer, no automatic job redrive, no case CRUD API, no document/OCR/ASR/video parsing, no entity resolution, no graph projection, no Merkle roots/signatures were introduced — all explicit non-goals for this phase. The containerized `api` image build was blocked by sandbox-level network flakiness, not a code defect — see above and `docs/qa/known-limitations.md` for the full list.

## 2026-09-10 — Gaurav — Video and Image Processing Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2, `ffmpeg`/`ffprobe` n9.0 and `nvidia-smi` present but reporting no driver/GPU (`NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver` — exercised directly as the expected "GPU absent" path, not worked around). Started on the `gaurav` branch at `e2f8827`; partway through the session `git log`/`git status` showed the branch had since picked up `53d1d4e Completed sarthak/phase-1 (#7)` (a concurrent, separately-authored build) via a manual git operation outside this task — confirmed via `git status --short` that this caused zero conflicts with this task's own files, and this task's own doc edits (`docs/qa/test-matrix.md`, `docs/qa/known-limitations.md`, `docs/qa/test-data.md`, `docs/runbooks/local-development.md`) were re-based on the current on-disk content before editing, additive on top of Sarthak's own additions to the same files. Additive edits only to shared files: `pyproject.toml`/`uv.lock` (three new dependencies only), the four docs above, plus this file and `docs/progress/mvp-progress.md`. No file under `app/modules/graph/`, `app/modules/structured_processing/`, `app/modules/access_control/`, `app/modules/communication_processing/`, or `app/contracts/` was touched (confirmed by `git status --short -- <owned paths>` below).

```bash
$ uv add opencv-python-headless Pillow numpy
Resolved 65 packages in 1.04s
Installed 4 packages: numpy, opencv-python-headless, pillow, tracex (rebuilt)
```
Result: **pass**. The only three additions permitted for this phase; `pyproject.toml`/`uv.lock` updated accordingly. All three ship `py.typed`/native type stubs (`cv2/py.typed`, `PIL/py.typed`, `numpy`'s own inline types), so no new `[[tool.mypy.overrides]]` entry was needed.

```bash
$ uv sync --all-groups
Resolved 65 packages in 5ms
Checked 64 packages in 0.88ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
230 files already formatted
```
Result: **pass**. (One pass through `ruff format .` without `--check` was needed first — this ruff version also formats fenced ` ```python ` code blocks inside `.md` files, and `docs/runbooks/media-development.md` needed that whitespace-only pass.)

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 106 source files
```
Result: **pass**. (85 files before this build, per Sarthak's entry below → 106 after adding the 21 files under `app/modules/media_processing/`.)

```bash
$ uv run pytest tests/unit/media_processing -q
194 passed in 0.49s
```
Result: **pass**. Every unit test is infrastructure-independent by design — `test_probe.py`/`test_frames.py` monkeypatch `subprocess`/`shutil.which` directly rather than depending on a real `ffmpeg`/`ffprobe` binary, and `test_media_worker.py`'s video-path tests monkeypatch `probe_video`/`extract_frames` on the `worker` module itself for the same reason (see `docs/qa/test-data.md`). Includes the static-AST module-isolation checks (`test_media_safety.py`) confirming no infra client, no `app.modules.graph`/`structured_processing`/`access_control` import, and no `EntityV1`/`EventV1` construction anywhere in the module.

```bash
$ uv run pytest -q
864 passed, 19 skipped in 18.65s
```
Result: **pass**, no regressions. 864 vs. the prior baseline of 664 (see Sarthak's entry below) is exactly the 200 new tests in `tests/unit/media_processing/` (194) + `tests/integration/media_processing/` (6); skip count unchanged (19) since this build touches no database/queue/storage code path and no infra was started for it.

```bash
$ docker compose config
```
Result: **pass** (exit 0). No `compose.yaml` changes made (no GPU Docker service was added, per the phase's explicit non-goal); ran only to confirm the file still parses.

```bash
$ uv run pytest tests/integration/media_processing -q
6 passed in 1.62s
```
Result: **pass — run live**, not self-skipped: `ffmpeg`/`ffprobe` were present in this sandbox, so all 6 tests exercised the real binaries against a tiny synthetic (`ffmpeg lavfi testsrc`-generated) MP4 — probe, deterministic sample-frame extraction (byte-for-byte identical pixels across two independent extraction runs), the fake detector+tracker analysis pipeline end to end, exact frame/time/bbox provenance, repeat-processing observation-ID stability, and confirmed temp-file cleanup (`tempfile.gettempdir()` directory listing unchanged before/after). Had `ffmpeg`/`ffprobe` been absent, `pytestmark = pytest.mark.skipif(not ffmpeg_available(), ...)` would have reported all 6 as skipped rather than fabricating a pass.

### GPU/CPU capability verification

```bash
$ nvidia-smi
NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. Make sure that the latest NVIDIA driver is installed and running.
```
This sandbox has an `nvidia-smi` binary present but no functioning driver/GPU behind it — the exact "GPU absent" case `capability.detect_capability()` must handle safely. Verified directly (not just via a unit test) that calling `detect_capability()` in this environment returns `cpu_only=True`, `gpu_visible=False`, `nvidia_smi_available=True`, `gpu_name=None`, `gpu_memory_mb=None`, `ffmpeg_available=True`, `ffprobe_available=True` — no exception, no hang, no fabricated GPU report.

```bash
$ git status --short -- app/modules/graph app/modules/structured_processing app/modules/access_control app/modules/communication_processing app/contracts migrations compose.yaml Dockerfile .env.example docs/architecture/graph-taxonomy-v1.md docs/architecture/access-control-v1.md docs/architecture/document-and-structured-processing-v1.md docs/decisions/ADR-001-graph-projection-and-case-isolation.md docs/decisions/ADR-002-deterministic-source-processing-and-provenance.md docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md
(no output)
```
Result: **pass** — zero diff against every other contributor's owned module/docs and every frozen-contract/forbidden path.

```bash
$ git status --short
 M docs/qa/known-limitations.md
 M docs/qa/test-data.md
 M docs/qa/test-matrix.md
 M docs/runbooks/local-development.md
 M pyproject.toml
 M uv.lock
?? app/modules/media_processing/
?? docs/architecture/media-observation-taxonomy-v1.md
?? docs/architecture/media-processing-v1.md
?? docs/decisions/ADR-004-media-provenance-and-anonymous-tracking.md
?? docs/runbooks/media-development.md
?? tests/fixtures/media_processing/
?? tests/integration/media_processing/
?? tests/unit/media_processing/
```
Result: nothing staged, nothing committed, no branch changed, per explicit instruction — Nipun will manually stage/commit/push/PR.

### Known limitations and intentionally deferred work

No real object-detection/tracking/OCR model, no face recognition/re-identification/biometric identification, no cross-camera identity correlation, no Neo4j graph writes, no actual file uploads/object-storage writes/hashing/Merkle roots/signatures, no video frontend, no ML training/evaluation, and no GPU Docker service were introduced — all explicit non-goals for this phase, confirmed by the zero-diff check above and the static-AST safety tests. Full list: `docs/qa/known-limitations.md`.

## 2026-09-10 — Sarthak — Audio, Social/Chat, Multilingual Alias, and Communication Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2. Branch `sarthak` was based on `main` after every build below was merged (`075e52b`, `a903d3a`, `85c55c4`, `67cd31c`, `dac3362`, `e2f8827`) — confirmed via `git log --oneline -8` and `git status --short` before starting (clean tree, correct branch). No dependency changes: `app/modules/communication_processing/` uses only the Python standard library (`wave`, `json`, `unicodedata`, `re`, `dataclasses`, `enum`, `itertools`, `datetime`) plus this repo's own `app.core.ids`/`app.contracts`, per the task's "prefer stdlib, add a dependency only if unavoidable" instruction — genuinely unavoidable here, so `pyproject.toml`/`uv.lock` are untouched (confirmed below).

```bash
$ uv sync --all-groups
Resolved 62 packages in 1ms
Checked 61 packages in 14ms
```
Result: **pass**. Zero new/changed packages.

```bash
$ uv run ruff format --check .
189 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 85 source files
```
Result: **pass**. (61 files before this build, per Aditya's entry below → 85 after adding the 24 files under `app/modules/communication_processing/`.)

```bash
$ uv run pytest tests/unit/communication_processing -q
215 passed in 0.31s
```
Result: **pass**.

```bash
$ uv run pytest tests/integration/communication_processing -q
1 passed in 0.03s
```
Result: **pass** — the full seven-processor pipeline test, which needs no external service and therefore never self-skips.

```bash
$ uv run pytest -q
664 passed, 19 skipped in 16.41s
```
Result: **pass**, no regressions. 664 vs. the prior baseline of 448 (see Aditya's entry below) is exactly the 216 new tests in `tests/unit/communication_processing/` (215) + `tests/integration/communication_processing/` (1); skip count unchanged (19) since this build touches no database/queue/storage code path and no infra was started for it.

```bash
$ docker compose config
compose config valid
```
Result: **pass** (exit 0; one warning about `AUTH_JWT_SECRET` defaulting to blank, expected since no `.env` exists in this session — this task never needed Docker services running, per its own instruction not to start them unless required, and this module requires none).

```bash
$ git status --short
 M docs/progress/mvp-progress.md
 M docs/qa/known-limitations.md
 M docs/qa/test-data.md
 M docs/qa/test-matrix.md
 M docs/runbooks/local-development.md
?? app/modules/communication_processing/
?? docs/architecture/audio-social-and-communication-processing-v1.md
?? docs/architecture/multilingual-alias-candidates-v1.md
?? docs/decisions/ADR-005-provenance-first-communication-processing.md
?? tests/fixtures/communication_processing/
?? tests/integration/communication_processing/
?? tests/unit/communication_processing/
```
Result: 5 docs additively modified, 6 new paths — nothing staged, nothing committed, no branch changed, per explicit instruction.

```bash
$ git diff --check
(no output)
$ git diff --stat
 docs/progress/mvp-progress.md      | 29 +++++++++++++++++++++++++----
 docs/qa/known-limitations.md       |  9 +++++++++
 docs/qa/test-data.md               |  6 ++++++
 docs/qa/test-matrix.md             | 13 +++++++++++++
 docs/runbooks/local-development.md | 18 ++++++++++++++++++
 5 files changed, 71 insertions(+), 4 deletions(-)
$ git diff --name-only
docs/progress/mvp-progress.md
docs/qa/known-limitations.md
docs/qa/test-data.md
docs/qa/test-matrix.md
docs/runbooks/local-development.md
```
Result: **pass** — no whitespace errors; the diff touches only the five QA/progress/runbook docs this task was scoped to update additively.

```bash
$ git status --short -- app/contracts app/core app/main.py app/api app/dependencies app/modules/graph app/modules/structured_processing app/modules/access_control app/modules/media_processing migrations compose.yaml Dockerfile pyproject.toml uv.lock .env.example
(no output)
```
Result: **pass** — zero diff against every frozen-contract and forbidden path, and against Gaurav's `app/modules/media_processing/` (which doesn't exist in this branch yet — confirmed not created by this task either).

### One real design decision revised during this build (before this record)

- **Worker input-payload shape.** The task brief's `process_job(job: WorkerJobV1, input_payload: ...) -> WorkerResultV1` signature (distinct from `structured_processing.worker.process_job(job, evidence, resolver)`) was interpreted as: `input_payload` is one of four already-typed, role-tagged dataclasses (`AudioMetadataInput`/`TranscriptImportInput`/`DiarizationImportInput`/`SocialExportInput`) rather than raw bytes needing a `SourceResolver`-style fetch — matching the task's "three safe input roles" framing for audio and the fact that transcript/diarization *import* inherently starts from already-produced structured data, not a file to resolve. Documented as Decision non-obvious enough to flag: see `docs/architecture/audio-social-and-communication-processing-v1.md`'s "Supported input boundaries" section.

## 2026-09-10 — Aditya — Integration Hardening 1: Central Middleware and Exception Handling

Environment: same sandbox as the build below, Python 3.12.13 (via `uv`), Docker 29.7.2, worked from the `gaurav` branch at the user's explicit direction (git operations — commit/push/branch changes — intentionally not performed; see git status confirmation below). Task: reproduce the reported "unhandled exception escapes the safe error envelope" symptom, determine the true root cause rather than trusting the originally-assumed diagnosis, and implement one central fix.

**Root cause investigation.** Reproduced the exact symptom with a synthetic app carrying *zero* custom middleware — it still occurred, which already falsified the assumed `BaseHTTPMiddleware` diagnosis. Read Starlette's `ServerErrorMiddleware` source directly: it sends a safe response via `send()`, then unconditionally re-raises the original exception afterward (an intentional, documented upstream behavior, so a server or test harness can also observe/log the error). Read httpx's `ASGITransport.__call__`: with its default `raise_app_exceptions=True`, that re-raise propagates to the test caller instead of the transport returning the response the app already sent. Confirmed empirically by running a real `uv run uvicorn app.main:app` process and hitting an endpoint designed to raise unexpectedly over real HTTP with `curl` — it returned the correct safe `500` envelope the whole time, proving real client/server traffic was never actually affected; only the test transport's default masked this.

A second, previously-unknown, real gap was found during the same investigation: Starlette dispatches the bare-`Exception` handler from its outermost `ServerErrorMiddleware` directly, bypassing every user-added middleware (including both `RequestIDMiddleware` and `SecurityHeadersMiddleware`) for that one response path — so an unhandled exception's `500` response was missing the correlation-ID header and security headers, regardless of `BaseHTTPMiddleware` vs. pure ASGI.

**Central fix.** `app/core/errors.py`'s three exception handlers (`http_exception_handler`, `request_validation_exception_handler`, `unhandled_exception_handler`) now build their own safe headers directly via a shared `_safe_error_headers()` helper (correlation ID + `Cache-Control: no-store` + baseline security headers), instead of relying on middleware to add them after the fact. Correlation ID survives exception unwinding via `request.state.request_id` (set on the ASGI `scope["state"]`, which persists through `ServerErrorMiddleware`'s re-raise/re-catch, unlike a `contextvars.ContextVar` alone) with a `_resolve_request_id()` fallback. `RequestIDMiddleware` and `SecurityHeadersMiddleware` were converted to pure ASGI middleware as an independent, well-justified simplification (both only ever wrapped `send`) — not itself the fix for the reported bug. The now-redundant per-endpoint/per-dependency `_internal_error` workaround in `app/modules/access_control/api.py` and `dependencies.py` was removed.

```bash
$ uv sync --all-groups
Resolved 62 packages in 1ms
Checked 61 packages in 0.74ms
```
Result: **pass**. No dependency changes — this task added no new packages (explicit non-goal).

```bash
$ uv run ruff format --check .
144 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 61 source files
```
Result: **pass**. Same file count as before this task (`app/core/errors.py`, `app/modules/access_control/api.py`, `app/modules/access_control/dependencies.py` edited in place; no files added under `app/`).

```bash
$ uv run pytest tests/unit -q
353 passed in 9.93s
```
Result: **pass**. Includes the new `tests/unit/test_error_handling.py` (18 tests: route/dependency `HTTPException`, request-validation failure, unexpected exception on both route and dependency paths, no `ExceptionGroup`/traceback/secret ever reaches the client, correlation ID present on success and on every error path including the `ServerErrorMiddleware`-dispatched one, non-HTTP `lifespan` scope pass-through, and per-request context cleanup) and `tests/unit/access_control` unchanged and passing with the local `_internal_error` workaround removed.

```bash
$ uv run pytest tests/security -q
36 passed in 0.57s
```
Result: **pass**. Includes `tests/security/access_control/test_auth_no_secret_leakage.py::test_unexpected_backend_failure_never_leaks_a_connection_secret`, the direct proof that removing `_internal_error` from `login()` still safely produces a generic `500` via the central handler with no secret leakage.

```bash
$ uv run pytest tests/integration -q
2 passed, 19 skipped in 6.45s
```
Result: **pass** — self-skipping as expected with no Postgres/Redis/Neo4j/MinIO running in this session (no infra was started for this task; it touches no database/queue/storage code path). The 2 that ran require no live infra.

```bash
$ uv run pytest -q
448 passed, 19 skipped in 16.65s
```
Result: **pass**, no regressions. 448 vs. the prior baseline of 430 (see the build below) is exactly the 18 new tests in `tests/unit/test_error_handling.py`; skip count unchanged (19) since no infra was started this session.

```bash
$ docker compose config
```
Result: **pass** (exit 0). No `compose.yaml` changes made (explicit non-goal); ran only to confirm the file still parses.

```bash
$ git status --short
 M app/core/errors.py
 M app/modules/access_control/api.py
 M app/modules/access_control/dependencies.py
 M docs/architecture/security-boundaries-v1.md
 M docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md
 M docs/progress/mvp-progress.md
 M docs/qa/known-limitations.md
 M docs/qa/test-matrix.md
 M tests/conftest.py
 M tests/security/access_control/test_auth_no_secret_leakage.py
 M tests/unit/access_control/test_api.py
?? tests/unit/test_error_handling.py
```
Result: 11 files modified, 1 new test file — nothing staged, nothing committed, no branch changed, per explicit instruction.

```bash
$ git diff --check
(no output)
$ git diff --stat
 app/core/errors.py                                 | 192 ++++++++++++++++++---
 app/modules/access_control/api.py                  |  93 ++++------
 app/modules/access_control/dependencies.py         |  34 +---
 docs/architecture/security-boundaries-v1.md        |  45 ++++-
 .../ADR-003-authentication-and-case-scoped-access-control.md |   4 +-
 docs/progress/mvp-progress.md                      |  22 ++-
 docs/qa/known-limitations.md                       |   3 +-
 docs/qa/test-matrix.md                             |   4 +
 tests/conftest.py                                  |  16 +-
 tests/security/access_control/test_auth_no_secret_leakage.py |  15 +-
 tests/unit/access_control/test_api.py              |   6 +-
 11 files changed, 308 insertions(+), 126 deletions(-)
```
Result: **pass** — no whitespace errors; confirmed the diff touches only the files this task was scoped to.

```bash
$ git status --short -- app/contracts app/modules/graph app/modules/structured_processing migrations compose.yaml Dockerfile pyproject.toml uv.lock .env.example
(no output)
```
Result: **pass** — zero diff against every frozen-contract and forbidden path; no new dependency was added.

### Known limitations and intentionally deferred work

- Starlette's `ServerErrorMiddleware`-re-raises-after-sending behavior is upstream and intentional, not something this project's code controls. Every test fixture in this repo that drives HTTP through the real app now explicitly sets `ASGITransport(..., raise_app_exceptions=False)`; a future test file that constructs its own `ASGITransport` without this setting will reproduce the original, misleading symptom if it exercises a genuinely-unhandled-exception path. Flagged in `docs/qa/known-limitations.md`.
- No new auth features, MFA/SSO/CORS/cookies/CSRF, business endpoints, DB/schema/migration changes, rate-limiting redesign, logging-platform integration, frontend, or graph/entity-resolution/document/media functionality were introduced — all explicit non-goals for this task, confirmed by the zero-diff check above.

## 2026-09-10 — Aditya — Authentication, Case Access Control, Security Middleware, and Operational Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2. Branch `aditya` was based on `main` after the Nipun/Shreshtha/Jasraj builds below were merged (`075e52b`, `a903d3a`, `85c55c4`, `67cd31c`) — confirmed via local `git log --oneline -5` before starting (`git fetch origin` itself failed with `fatal: could not read Username for 'https://github.com'` in this sandbox — no network credentials for GitHub specifically, unrelated to package registries, which worked fine for `uv add`). Additive edits only to shared files: `app/main.py`, `app/core/config.py`, `app/core/errors.py`, `tests/conftest.py`, `.env.example`, `compose.yaml` (new `AUTH_*` env passthrough for the `api` service only), plus `docs/qa/*`, `docs/progress/mvp-progress.md`, `docs/runbooks/local-development.md`. No file under `app/modules/graph/`, `app/modules/structured_processing/`, or `app/contracts/` was touched (confirmed by `git status` before finishing and by `tests/security/access_control/test_module_boundaries.py`).

```bash
$ uv add PyJWT "pwdlib[argon2]"
Resolved 62 packages in 7.34s
Installed 8 packages: et-xmlfile, lxml, openpyxl, pwdlib, pyjwt, pypdf, python-docx, (tracex rebuilt)
```
Result: **pass**. The only two additions allowed for this phase; `pyproject.toml`/`uv.lock` updated accordingly. Both ship `py.typed` markers, so no new `[[tool.mypy.overrides]]` entry was needed.

```bash
$ uv sync --all-groups
Resolved 62 packages in 0.73ms
Checked 61 packages in 0.38ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
143 files already formatted
```
Result: **pass**. (Note: this ruff version also formats fenced ` ```python ` code blocks inside `.md` files — two documentation files needed a pass through `ruff format .` for that reason, both whitespace-only.)

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 61 source files
```
Result: **pass**. (47 files before this build, per Jasraj's entry below → 61 after adding `app/modules/access_control/`'s 13 files plus `app/modules/__init__.py` already existing.)

```bash
$ uv run pytest tests/unit/access_control -q
105 passed in 2.92s
```
Result: **pass**.

```bash
$ uv run pytest tests/security/access_control -q
34 passed in 0.56s
```
Result: **pass**.

```bash
$ uv run pytest
443 passed, 6 skipped in 18.93s
```
Result: **pass**, run with PostgreSQL + Redis up (see below) — the 6 skips are `tests/integration/graph/` (4, Neo4j not started this session) and the `neo4j`/`minio` checks in `tests/integration/test_readiness_live.py` (2, same reason); `postgres`/`redis` in that same file, and this build's own 11 `tests/integration/access_control/` tests, all passed live. Without any infra running at all, the full suite is `430 passed, 19 skipped` (confirmed separately, after teardown below).

```bash
$ docker compose config
```
Result: **pass** (exit 0).

```bash
$ docker compose up -d --wait postgres redis
Container tracex-postgres-1 Healthy
Container tracex-redis-1 Healthy
```
Result: **pass**. Both required infra services reached `healthy`. (Neo4j/MinIO were not started this session — not needed for this phase's own verification — so `/readyz` correctly reports them `"unavailable"` alongside `postgres`/`redis` `"ok"` in the live smoke test below.)

```bash
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
```
Result: **pass** (exit 0; already at head from the integration suite's own autouse migration fixture — see below). Verified table creation directly: `users`, `cases`, `case_memberships`, `auth_sessions`, `security_audit_events` all present in `information_schema.tables` with the exact expected constraint/index set from the migration.

```bash
$ uv run pytest tests/integration/access_control -q
11 passed in 2.56s
```
Result: **pass** — all 11 live tests passed for real (not self-skipped): Alembic migration apply + idempotent re-apply, full register→login→refresh→logout round trip, wrong-password denial, refresh-token-hashed-not-raw, token-family revocation (including the still-active leaf session), live case-membership/clearance policy decisions (including a direct-SQL membership deactivation taking effect on the next read), and Redis-backed rate limiting (both the limit itself and independent keys). Confirmed via a direct row-count query afterward that `users`/`cases`/`case_memberships`/`auth_sessions` all returned to `0` (full cleanup); `security_audit_events` intentionally is not cleaned up per test by application design (see `docs/qa/test-data.md`) and was purged once, manually, as an end-of-session courtesy.

### Live end-to-end HTTP smoke test (beyond the required command list)

Ran a real `uv run uvicorn app.main:app` process against the live PostgreSQL/Redis containers and exercised the full API over real HTTP with `curl`:

```bash
$ curl .../healthz            # {"status":"ok",...}                                    200
$ curl .../readyz              # postgres:ok, redis:ok, neo4j/minio:unavailable         503 (expected, not started)
$ curl -X POST .../auth/register ...   # PublicUser body, no password/hash              201-equivalent
$ curl -X POST .../auth/login ...      # access_token + refresh_token; headers below     200
$ curl .../auth/me -H "Authorization: Bearer ..."   # correct identity + memberships     200
$ curl -X POST .../auth/refresh ...    # new token pair, refresh_token rotated           200
$ curl -X POST .../auth/refresh ... (old, now-rotated refresh_token replayed)            401
$ curl -X POST .../auth/login ... (wrong password)  # {"error":{"code":"unauthorized","message":"invalid email or password",...}}   401
```
Login response headers confirmed present: `x-content-type-options: nosniff`, `x-frame-options: DENY`, `referrer-policy: no-referrer`, `cache-control: no-store`. The smoke-test user was deleted afterward (`DELETE FROM users WHERE email_normalized = 'livesmoke@example.test'`, 1 row).

### One real bug caught and fixed during this build (before this record)

- **`repository.rotate_session` violated a foreign-key constraint against real PostgreSQL.** The original implementation ran `UPDATE auth_sessions SET replaced_by_session_id = <new_id> ...` *before* inserting the new session row. `auth_sessions.replaced_by_session_id` has a foreign key onto `auth_sessions.session_id`, checked immediately (no `DEFERRABLE` constraints in this schema) — so the `UPDATE` failed with `ForeignKeyViolationError: Key (replaced_by_session_id)=(...) is not present in table "auth_sessions"` the first time it ran against a real database. Every unit test passed regardless, because `FakeAccessControlRepository` (the in-memory test double) doesn't enforce foreign keys — this is exactly the class of bug the task's live-integration-test requirement exists to catch. Fixed by swapping the statement order (insert the new session first, then revoke-and-point the old one at it) in the same transaction. Caught by `tests/integration/access_control/test_auth_lifecycle_live.py::test_register_login_refresh_logout_round_trip_against_live_db` and `::test_token_family_revocation_works_in_live_persistence`. See `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, Decision 7.
- **A test-cleanup bug (not an application bug), also only caught live.** `tests/integration/access_control/conftest.py`'s `cleanup_user_ids`/`cleanup_case_ids` fixtures originally issued `DELETE FROM ... WHERE id = ANY(:ids)` via a raw `sa.text(...)` bind with a plain Python `list[UUID]` — which silently matched zero rows instead of raising (no type information to cast the array against), leaving real rows behind across test runs undetected until a manual row-count check after a full local run. Fixed by rebuilding the same deletes through the typed `users_table`/`cases_table` SQLAlchemy Core objects (`.where(col.in_(ids))`), which bind correctly through the column's own `UUID` type. Confirmed fixed via a before/after row-count check (`docs/qa/test-data.md`).
- **A genuinely pre-existing (not introduced by this build) Starlette/`BaseHTTPMiddleware` interaction**, reproduced and worked around locally rather than fixed at the shared-middleware level — see `docs/architecture/security-boundaries-v1.md` and `docs/decisions/ADR-003-...md`, Decision 10, and `docs/qa/known-limitations.md`. **Correction (Integration Hardening 1, see the entry above):** re-investigated from scratch; `BaseHTTPMiddleware` was not actually the cause of the observed symptom (that was httpx's `ASGITransport` test-only default). A different, real gap was found and fixed centrally instead; the local workaround referenced here was removed as redundant.

Stack was stopped cleanly afterward: `docker compose down` (containers/network removed, named volumes preserved). Re-ran `uv run pytest -q` once more after teardown to confirm the suite returns to self-skipping (`430 passed, 19 skipped` — the 19 are the pre-existing 8 (`test_readiness_live.py` ×4, `tests/integration/graph/` ×4) plus this build's 11 `tests/integration/access_control/` tests, all skipping cleanly rather than fabricating a pass with no infra up).

## 2026-09-10 — Jasraj — Document and Structured-Data Processing build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2. Branch `jasraj` was based on `main` after both the Nipun and Shreshtha builds below were merged (`075e52b`, `a903d3a`, `85c55c4`) — confirmed via `git log --oneline HEAD..origin/main` (empty) before starting, and re-confirmed no conflicts with `app/modules/graph/` or any shared doc after finishing (additive edits only to `docs/qa/*`, `docs/progress/mvp-progress.md`, `docs/runbooks/local-development.md`).

```bash
$ uv add pypdf python-docx openpyxl
Resolved 60 packages
Installed 6 packages: et-xmlfile, lxml, openpyxl, pypdf, python-docx, (tracex rebuilt)
```
Result: **pass**. The only three additions allowed for this phase; `pyproject.toml`/`uv.lock` updated accordingly, plus an `openpyxl.*` entry in `[[tool.mypy.overrides]]` (no type stubs published for `openpyxl`, same pattern as the existing `neo4j.*`/`minio.*`/`asyncpg.*` entries).

```bash
$ uv sync --all-groups
Resolved 60 packages in 1ms
Checked 59 packages in 0.82ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
107 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 47 source files
```
Result: **pass**. (39 files after adding `app/modules/structured_processing/` → 47 once `openpyxl` stub coverage was added and re-checked; 26 files before this build, per Shreshtha's entry below.)

```bash
$ uv run pytest tests/unit/structured_processing -q
150 passed in 0.44s
```
Result: **pass**.

```bash
$ uv run pytest tests/contract -q
56 passed in 0.03s
```
Result: **pass** (frozen contracts untouched — confirms this build didn't regress them).

```bash
$ uv run pytest -q
291 passed, 8 skipped in 12.80s
```
Result: **pass**. 140 pre-existing (per Shreshtha's entry: 140 passed/8 skipped without live infra) + 151 new (150 unit + 1 integration) = 291. The 8 skips are the pre-existing self-skipping live-infrastructure suites (4 `test_readiness_live.py`, 4 `tests/integration/graph/`) — unrelated to this build, which needs no live infrastructure at all (`tests/integration/structured_processing/test_local_file_pipeline.py` uses a real local-file `SourceResolver`, not a network service, so it runs unconditionally and is included in the 291 passed).

```bash
$ docker compose config
```
Result: **pass** (exit 0; also covered by `tests/integration/test_compose_config.py`, included in the 291 passed above). Full-stack `docker compose up` was not re-verified in this session — this build adds no new services/compose changes, so nothing about the previously-verified stack (Nipun's entry below) changes.

### One real design bug caught and fixed during this build (before this record)

- **Error-code priority was backwards for a fundamentally unsupported content type.** `worker.process_job` originally resolved the parser profile (`get_profile(job.processor_name)`) and checked its `accepted_content_types` *before* calling `classify()`. A job pointed at e.g. `application/zip` (not a Phase 1 format at all) was reported as `unsupported_parser_profile` — technically true but less specific than it should be, since the content type isn't even in the supported set regardless of which profile was chosen. Reordered so `classify()` runs first: a fundamentally unsupported format now reports `unsupported_content_type`; a supported format that just doesn't match the chosen profile still reports `unsupported_parser_profile`. Caught by `tests/unit/structured_processing/test_worker_dispatch.py::test_unsupported_content_type_fails`.
- Three other issues were test-fixture bugs, not code bugs (fixed in the test files, not the app): a CDR/finance CSV fixture with an unquoted comma-containing amount value that the CSV parser correctly split into extra columns; a zip-bomb test fixture written without compression, giving a 1:1 ratio that never tripped the compression-ratio limit; and a PDF fixture's second-page text one character short of the documented 20-character "meaningful text" threshold, correctly (if confusingly, for the test) routed to OCR.

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
