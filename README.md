# TraceX

TraceX is an evidence-first criminal-network intelligence backend built for SIH 2026 (Problem Statement 26189). It ingests heterogeneous investigative material — documents, CDRs, financial records, surveillance video, images, audio, and social/chat exports — turns it into provenance-rich observations, and assembles a temporal, auditable graph of entities and events that investigators can review without losing sight of the underlying evidence.

## Scope

Phase 1 (**Foundation and Frozen Contracts**) delivered the backend skeleton:

- A minimal FastAPI service with health, readiness, and contract-metadata endpoints.
- Independently startable local infrastructure (PostgreSQL, Neo4j, Redis, MinIO) via Docker Compose.
- Versioned, frozen shared contracts (`EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, worker job/result envelopes) that every later module builds against.
- Deterministic ID and canonical serialization helpers.
- Authentication and case-scoped RBAC/ABAC (`app/modules/access_control/`).
- Document/structured-data, audio/social/chat, and video/image processing foundations (`structured_processing`/`communication_processing`/`media_processing`) — deterministic extraction logic; each is now wired to the real internal worker API via its own one-shot `--once` CLI (below), not a continuous ingestion pipeline.
- Graph projection and case-scoped query foundation (`app/modules/graph/`) — built; wired to a real, durable pipeline as of Phase 2.5 (below) for `Evidence`/`Observation`/`EntityMention` specifically.

Phase 2 (`app/modules/evidence_lifecycle/`) adds the real, case-scoped **evidence lifecycle**: authenticated upload → streamed SHA-256 hashing → private MinIO storage → immutable PostgreSQL metadata → a durable `WorkerJobV1` job record → best-effort Redis dispatch. See `docs/architecture/evidence-lifecycle.md`.

Phase 2.1 (in progress, same module) adds the **worker claim and result-submission** half of the lifecycle: a fail-closed internal API for a future worker to claim exactly one queued job (via `FOR UPDATE SKIP LOCKED` + a one-time claim token) and durably submit its `WorkerResultV1`/`ObservationV1`s, atomically transitioning the job to a terminal state. See `docs/architecture/worker-job-lifecycle.md`.

Jasraj's Phase 2 structured-processing worker (`app/modules/structured_processing/worker.py`) is the first real consumer of that internal API: a one-shot CLI (`uv run python -m app.modules.structured_processing.worker --once`) that claims a compatible job, streams its evidence through Phase 2.2's claim-token-bound worker-input endpoint, parses it (FIR/document text, CDR, financial, generic tabular/JSON), and submits a canonical `WorkerResultV1` — still not a daemon. See `docs/architecture/structured-processing-worker.md`.

Phase 2.2 (Nipun, same module) adds the **worker evidence-delivery** endpoint that closes the gap the paragraph above used to defer on: `GET /api/v1/internal/worker-jobs/{job_id}/input` streams a claimed job's evidence bytes through the API itself — never an object key, bucket, MinIO endpoint, or credential — scoped to the exact job and claim token a worker holds. See `docs/architecture/evidence-lifecycle.md`'s "Worker evidence-delivery" section.

Phase 2.3 (Nipun, same module) adds two explicit evidence source types, `structured_tabular` (CSV/XLSX) and `structured_json` (JSON), for general structured data that isn't specifically a CDR or financial export — routed deterministically to Jasraj's existing `generic_tabular_v1`/`generic_json_v1` fallback profiles, which a real upload could not reach before this. An additive, backward-compatible `SourceType` extension — every existing source type is unchanged. See `docs/architecture/evidence-lifecycle.md`'s routing table.

Aditya's Phase 2 worker-identity hardening (`app/modules/access_control/worker_credentials.py`) replaces the temporary shared secret every earlier worker-integration phase above used with revocable, per-worker service identities: a trusted-operator-only CLI provisions/rotates/revokes a `WorkerCredentialRecord` per worker (digest-only storage, plaintext token shown once), `/claim` now enforces per-worker processor scoping, and `/result`/`/input` now require the caller to *be* the worker identity currently bound to that job, not just hold its claim token. Closes the case-access-denial audit gap flagged since Phase 2 along the way. See `docs/architecture/worker-identity-and-security.md`.

Sarthak's Phase 2 communication-processing worker (`app/modules/communication_processing/worker.py`) is a second real consumer of the internal worker API, following Jasraj's structured-processing worker's exact pattern: a one-shot CLI (`uv run python -m app.modules.communication_processing.worker --once`) that claims a compatible job, streams its evidence through the same claim-token-bound, SHA-256-verified worker-input endpoint, parses it (WAV technical metadata, externally-produced transcript/diarization interchange, WhatsApp/Telegram/Instagram/generic-JSON social exports), and submits a canonical `WorkerResultV1` — still not a daemon, and still authenticated via Aditya's per-worker-credential system with no new authentication path. All seven of its processor profiles are now reachable through a real evidence upload (a follow-up routing fix — see below); originally only two were. See `docs/architecture/communication-processing-worker.md`.

Phase 2.5 (Shreshtha, `app/modules/graph/`) closes the "graph projection exists but nothing calls it" gap for `Evidence`/`Observation` specifically: every canonical `ObservationV1` a worker result accepts now durably enqueues a PostgreSQL-backed projection job (in the same transaction as the result itself), a bounded `uv run python -m app.modules.graph.worker --once` CLI claims and projects those jobs into a case-scoped Neo4j graph with safe retry/lease semantics, and a new evidence-local `EntityMention` node (never a resolved entity) captures each observation's raw extracted-entity mentions. A new case-scoped, paginated read endpoint (`GET /api/v1/cases/{case_id}/graph/observations`) exposes the result safely. A Neo4j outage never discards an accepted worker result — PostgreSQL remains authoritative and Neo4j fully rebuildable. See `docs/architecture/graph-projection.md`.

A follow-up routing fix (Shreshtha, `evidence_lifecycle/routing.py`) closed the gap Sarthak's build above reported but explicitly did not fix itself (shared routing was outside that task's scope to change unilaterally): five new, additive `SourceType` values (`audio_transcript`, `audio_diarization`, `whatsapp_chat`, `telegram_chat`, `instagram_chat`) route real uploads to `transcript_import_v1`/`diarization_import_v1`/`whatsapp_export_v1`/`telegram_export_v1`/`instagram_export_v1`, verified live end to end (upload → the real `communication_processing` worker claim/input/result → a persisted `ObservationV1` → graph projection) for all five. See `docs/architecture/phase-2-decisions.md`.

Gaurav's Phase 2 completion (`app/modules/media_processing/worker.py`) is a third real consumer of the internal worker API, following the exact same pattern: a one-shot CLI that claims a compatible job, streams its evidence through the same claim-token-bound, SHA-256-verified worker-input endpoint, decodes/probes it (image metadata via Pillow; video metadata/deterministic frame sampling via `ffprobe`/`ffmpeg`), and submits a canonical `WorkerResultV1`. Also fixed a second real gap this phase's live wiring found: `media_processing` had no classification entry for `video/x-matroska`, even though routing had accepted it since Phase 1.

Nipun's Phase 2 closeout replaces that phase's remaining prototype gap: `process_job` now runs a **real, local, checksum-verified/runtime-checked** object detector (YOLOX-s via `onnxruntime`, CPU by default with an auto-detected optional CUDA execution provider), OCR engine (the local `tesseract` binary via `pytesseract`), and deterministic class-aware IoU tracker — no cloud AI API, no `ultralytics`/`torch`, ever; see `docs/architecture/media-processing-worker.md`'s "Model asset bootstrap" for the explicit, operator-invoked, never-automatic download step. `evidence_lifecycle/routing.py` now routes every real image/video upload to `media_detection_v1` (previously the metadata-only `media_metadata_v1`) — the metadata observation is still always emitted first, so this is strictly additive. Both this worker and the graph projector also gained a continuous `uv run python -m app.modules.<module>.worker --loop` mode alongside their existing `--once` CLI — configurable idle-poll interval, bounded exponential backoff, graceful SIGINT/SIGTERM shutdown that always finishes any in-flight job/batch first, and a lease-renewal heartbeat (a new `POST /api/v1/internal/worker-jobs/{job_id}/renew` endpoint, same claim-token/worker-identity authorization as `/result`/`/input`) for real processing that may outlast its original lease window. Optional `media-worker`/`graph-projector` Compose services (opt-in `workers` profile) run `--loop` continuously. Still no face recognition, person re-identification, cross-camera/cross-case identity matching, or guilt/risk scoring — every detection/track/OCR mention remains anonymous, evidence-local, and unresolved. See `docs/architecture/media-processing-worker.md` and `docs/architecture/graph-projection.md`.

Phase 3 (Nipun, same module) adds the **canonical observation batch-ingestion** path: a worker processing a large document/CDR/finance source may submit any number of partial, provenance-rich `ObservationBatchSubmissionV1` micro-batches (`POST /api/v1/internal/worker-jobs/{job_id}/observations`) while its job is still `running`, each atomically persisting its observations, `TransformationProvenanceV1` records, and a durable progress event, alongside exactly-once durable graph-projection handoff — then completes with one terminal `WorkerResultV1` exactly as before. Batch-sourced observations reach `app/modules/graph/`'s existing outbox/projector unchanged — that module needed zero code changes. Idempotent replay, safe conflict detection, and case/evidence scope enforcement all reuse the identical claim-token/worker-identity security boundary Phase 2.1/2.4 already established. This phase implements no real document/OCR/CDR/finance parsing itself — it is the durable, tested contract Jasraj's own Phase 3 workers build against. See `docs/architecture/phase-3-decisions.md`.

Aditya's Phase 3 work (`app/modules/evidence_lifecycle/`, `app/modules/access_control/audit.py`) closes the three remaining gaps in the worker-submission security boundary that Phases 2.1/2.2/2.4 had left open: a typed, configurable retry-attempt ceiling (`worker_jobs.max_attempts`, `WORKER_JOB_MAX_ATTEMPTS`) that transitions a job whose lease keeps expiring to a durable terminal `failed` state instead of leaving it reclaimable forever; an absolute maximum lease lifetime (`WORKER_LEASE_MAX_SECONDS`) that no number of `POST .../renew` calls can extend a claim past, computed atomically inside the renewal query itself; and a full audit trail for every worker action that *succeeds* (`worker_job_claimed`, `worker_job_reclaimed`, `worker_job_lease_renewed`, `worker_job_completed`, `worker_job_failed`, `worker_job_retry_exhausted`), joining the denial events Phase 2.4 already recorded. No claim-token/case/evidence authorization boundary, contract, or graph-projection behavior changed. See `docs/architecture/phase-3-decisions.md`'s Aditya section and `docs/architecture/worker-job-lifecycle.md`.

Jasraj's Phase 3 work (`app/modules/structured_processing/`) builds the real producer that submits through Nipun's/Aditya's batch-ingestion path above: per-page PDF trust classification (a page's embedded text is only trusted after passing printable-ratio/token-quality checks — never blindly because `len(text) > 0`), real local Tesseract OCR for scanned/untrustworthy pages (via `pypdfium2` rendering, no cloud API), deterministic layout normalization with an exact offset map back to source, regex + local NER mention extraction (a real, operator-bootstrapped spaCy model when available, a deterministic no-ML gazetteer fallback otherwise — never a crash either way), and four rule-based relation/event extraction rules — all submitted as one or more real `ObservationBatchSubmissionV1` micro-batches per document page/segment. CDR and financial-transaction records are now normalized in bounded, vectorized micro-batches (Polars-batched CSV, PyArrow-batched XLSX) with a documented row-level malformed-data policy, E.164 phone normalization, and a shared CDR/finance timezone-resolution policy (an explicit per-record zone, else a configured default — never silently UTC). No routing, contract, or graph-projection code changed — every format processed was already reachable, and batch-sourced observations reach the existing, unmodified graph outbox exactly like a terminal result's own observations always have. See `docs/architecture/document-structured-processing.md` and `docs/architecture/phase-3-decisions.md`'s Jasraj section.

Sarthak's Phase 3 work (`app/modules/communication_processing/`) submits through the same batch-ingestion path: every one of the module's seven processor profiles now delivers observations via `/observations` micro-batches instead of one bundled terminal result, reusing its existing extraction logic (`_dispatch`) completely unchanged. A naive chat timestamp with no explicit offset/`Z`/epoch signal now resolves to UTC via a documented, configured default timezone (mirroring Jasraj's identical CDR/finance policy) instead of staying permanently unresolved. Chat message text is now also scanned for `phone_number`/`email_address`/`url`/`username_or_handle` via fixed, deterministic regexes — separate observations from the parent message, never a graph edge. A message's sender name gets per-word, review-only transliteration candidates attached (via the pre-existing, unmodified Phase 1 transliteration algorithm) — never auto-attached to any entity's aliases. **Still no real local ASR or diarization**: this module's own pre-existing `test_module_safety.py` bans every practical local ASR/diarization toolkit, so this phase instead gives that absence a typed `Protocol` boundary (`AsrAdapter`/`DiarizationAdapter`) with an explicit fixture-only test adapter, flagged for team review rather than reversing that boundary unilaterally. See `docs/architecture/communication-processing.md` and `docs/architecture/phase-3-decisions.md`'s Sarthak section.

Gaurav's Phase 3 work (`app/modules/media_processing/`) implements a precise shared raster-image OCR bounding-box adapter. `run_once` submits OCR plus existing media observations through Nipun's/Aditya's `/observations` micro-batch pipeline, then sends one terminal result with `observations=[]`. `ImageOcrAdapter` extracts ordered line-level boxes with real local Tesseract and maps them safely to original unrotated `[0, 1]` geometry, including EXIF orientation inversion. `ocr_batching.py` produces bounded batches and safe transformation provenance without raw OCR text, URIs, or credentials. `FixtureOcrAdapter` remains explicitly fixture-only; live OCR tests use the real adapter when available and otherwise skip precisely. See `docs/architecture/image-ocr-provenance.md`.

Explicitly **not** implemented anywhere in this repository yet: a continuous worker loop for `structured_processing`/`communication_processing` (`media_processing`/`graph` gained one in the Phase 2 closeout, `--once` remains available for all four), real ASR for audio transcription (`media_processing`'s and `structured_processing`'s own real, local Tesseract OCR are unrelated — neither reads spoken audio), automatic entity resolution or identity merging, face recognition or person re-identification, cross-modal correlation, a human-review workflow, Merkle checkpointing or signatures, worker-credential expiry, case CRUD, worker login/logout or OAuth/OIDC/SAML/mTLS, MFA/SSO, or any frontend. See `CLAUDE.md`, `docs/architecture/phase-1-decisions.md`, `docs/architecture/phase-2-decisions.md`, `docs/architecture/phase-3-decisions.md`, `docs/architecture/worker-job-lifecycle.md`, `docs/architecture/worker-identity-and-security.md`, `docs/architecture/graph-projection.md`, `docs/architecture/media-processing-worker.md`, `docs/architecture/document-structured-processing.md`, and `docs/qa/known-limitations.md` for the full non-goal list.

**No sensitive or real production evidence is used anywhere in this repository.** All fixtures and test data are synthetic (see `docs/qa/test-data.md`).

## Prerequisites

- Git
- Docker and Docker Compose (Compose v2, i.e. `docker compose`, not `docker-compose`)
- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

This project uses `uv` exclusively for dependency management — never `pip` directly.

## Setup

```bash
git clone <repo-url>
cd TraceX
cp .env.example .env      # edit if you need non-default local values
uv sync --all-groups
```

## Running the API

Full stack (API + all infrastructure, in Docker):

```bash
docker compose up --build
```

Infrastructure only (run the API on your host for faster iteration):

```bash
docker compose up -d postgres neo4j redis minio
uv run uvicorn app.main:app --reload
```

Once running, the API is available at:

- `GET http://localhost:8000/healthz` — liveness
- `GET http://localhost:8000/readyz` — readiness (checks PostgreSQL, Neo4j, Redis, MinIO)
- `GET http://localhost:8000/api/v1/meta/contracts` — supported contract versions
- `POST/GET http://localhost:8000/api/v1/cases/{case_id}/evidence`, `GET .../evidence/{evidence_id}`, `GET .../jobs/{job_id}` — case-scoped evidence upload/status (see `docs/architecture/evidence-lifecycle.md`)
- `POST http://localhost:8000/api/v1/internal/worker-jobs/claim`, `GET .../worker-jobs/{job_id}/input`, `POST .../worker-jobs/{job_id}/observations`, `POST .../worker-jobs/{job_id}/result` — internal worker claim/input-stream/partial-batch/result endpoints, gated by a per-worker `WORKER_TOKEN` credential + a per-job claim token (see `docs/architecture/worker-identity-and-security.md`, `docs/architecture/worker-job-lifecycle.md`, `docs/architecture/evidence-lifecycle.md`, `docs/architecture/phase-3-decisions.md`)
- `GET http://localhost:8000/api/v1/cases/{case_id}/graph/observations` — safe, paginated, case-scoped projected observation graph (see `docs/architecture/graph-projection.md`)
- `GET http://localhost:8000/docs` — interactive OpenAPI docs

Exposed development ports: API `8000`, PostgreSQL `5432`, Neo4j `7474` (HTTP browser) / `7687` (Bolt), Redis `6379`, MinIO `9000` (API) / `9001` (console).

## Development commands

```bash
uv sync --all-groups          # install runtime + dev dependencies
uv run ruff format --check .  # formatting check
uv run ruff check .           # lint
uv run mypy app                # type-check the app package
uv run pytest                 # run the test suite
docker compose config         # validate compose syntax
```

## Repository layout

- `app/` — FastAPI application: API routes, core infra (config, errors, IDs, canonical serialization), versioned contracts, and `app/modules/` (access control, evidence lifecycle, graph, structured/communication/media processing).
- `tests/` — unit, contract, integration, security, and e2e tests, plus shared fixtures.
- `docs/` — architecture decisions, contract reference, QA tracking, progress log, and runbooks.
- `migrations/` — Alembic baseline plus additive domain migrations (`access_control`, `evidence_lifecycle`, worker job claim/result, observation-batch ingestion).
- `compose.yaml`, `Dockerfile` — local reproducible infrastructure.

## Contributing

See `CLAUDE.md` for the rules this repository is built and maintained under (frozen-contract policy, module boundaries, required tests/docs per change).
