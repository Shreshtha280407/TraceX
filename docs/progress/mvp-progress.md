# MVP Progress

## Phase 4 - Nipun progressive media orchestration: In progress

- [x] Deterministic internal manifest/chunk records, partial publication,
  artifact lineage, and checkpoint/replay foundation.
- [x] Merged Phase 4 visual, audio/social, OCR, temporal graph, and
  worker-control implementations are present on `nipun`.
- [ ] Final release gate: Python, static, and migration-chain checks pass
  (`1693 passed, 61 conditionally skipped`), but the dedicated API Compose
  image cannot finish its locked install because Docker-build DNS cannot
  resolve PyPI. The live integration/Compose checks remain pending; see
  `docs/architecture/phase-4-integration-release-gate.md`.

## Phase 3 — In progress — blocked by fresh five-service Compose image rebuild

All six merged Phase 3 producer/lifecycle paths are test-accepted (`1656
passed, 1 skipped`), the video batch 422 is fixed, and real local-Tesseract
image/video lifecycle coverage passed. Do not mark deployment acceptance
complete until `docker compose up --build -d` succeeds on a host with Docker
Hub access; the local Docker Desktop registry connection was unreachable.
Details: `docs/qa/phase-3-acceptance.md`.

Team: Nipun (Phase 1 — core foundation), Shreshtha, Aditya, Jasraj, Gaurav, Sarthak (later-phase modules building on the Phase 1 contracts).

## Phase 3 — Shreshtha graph mapping

Deterministic, versioned document/CDR/finance mapping plans now project
case-scoped source claims and event-first CDR/financial temporal events
through the existing durable graph outbox. See
`docs/architecture/phase-3-graph-mapping.md`.

## Phase 1 — Nipun core foundation: Complete

All acceptance criteria below are verified as of 2026-09-10 — see `docs/qa/test-results.md` for actual command output, including the full-stack `docker compose up --build` run with all five services healthy and all three endpoints hit live.

### Delivered

- [x] `pyproject.toml` / `uv.lock` — Python 3.12, `uv`-managed, Ruff + mypy configured.
- [x] FastAPI app (`app/main.py`) with `/healthz`, `/readyz`, `/api/v1/meta/contracts`.
- [x] Frozen Phase 1 contracts: `EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, `WorkerJobV1`, `WorkerResultV1`, `WorkerProgressV1` (`app/contracts/`).
- [x] Deterministic ID helper (`app/core/ids.py`) and canonical serialization (`app/core/canonical.py`).
- [x] Typed configuration (`app/core/config.py`) — fails fast on missing/invalid required settings.
- [x] Safe error envelope + request-correlation-ID middleware (`app/core/errors.py`).
- [x] `compose.yaml` + `Dockerfile` — api/postgres/neo4j/redis/minio, named volumes, health checks.
- [x] Alembic baseline (`migrations/`), no domain tables.
- [x] Contract, unit, security, e2e, and self-skipping integration tests (`tests/`).
- [x] `.github/workflows/ci.yml` running the required verification commands.
- [x] Documentation set: README, CLAUDE.md, `docs/architecture/*`, `docs/qa/*`, this file, `docs/runbooks/local-development.md`.

- [x] Full-stack `docker compose up --build` verified: all five services reached `healthy`/`running`, and `/healthz`, `/readyz`, `/api/v1/meta/contracts` all responded correctly against the live stack. `/readyz` reported all four real dependencies `"ok"`.

### Outstanding for team review

- [ ] Open questions in `docs/architecture/phase-1-decisions.md` (taxonomy ownership, classification/RBAC levels, `DerivedArtifact` shape) — not blockers for building against the frozen contracts, but worth a team pass.

## Shreshtha Phase 1 — Graph Foundation and Taxonomy: Complete

Verification is complete as of 2026-09-10 (see `docs/qa/test-results.md` for full command output, including live projection/query/schema/isolation tests run against the Compose Neo4j instance). Marked **in progress**, not complete, pending the team-review items below.

### Delivered

- [x] `docs/architecture/graph-taxonomy-v1.md` — proposed node/relationship taxonomy, recommended entity/event type values, identity-safety rules. Does not modify any frozen `V1` contract.
- [x] `app/modules/graph/` — `errors.py`, `models.py`, `schema.py`, `repository.py`, `projection.py`, `queries.py`. Idempotent, case-scoped, allow-listed-property projection of `EvidenceRecordV1`/`ObservationV1`/`EntityV1`/`EventV1`; safe, bounded, case-scoped read queries; explicit (never automatic) Neo4j schema management via `uv run python -m app.modules.graph.schema apply|verify`.
- [x] `docs/architecture/neo4j-graph-foundation.md`, `docs/decisions/ADR-001-graph-projection-and-case-isolation.md` — module structure, property-storage policy, and the design decisions behind case isolation, dependency ordering/deferral, and the deferred `SUPPORTS` relationship.
- [x] `tests/unit/graph/`, `tests/integration/graph/` (self-skipping without live Neo4j, same pattern as `tests/integration/test_readiness_live.py`), `tests/fixtures/graph/` — all 12 required unit-test scenarios and all 6 required integration-test scenarios from the task brief, verified passing both self-skipped and live against the Compose Neo4j container.
- [x] QA entries `GRAPH-SCHEMA-001`, `GRAPH-PROJECTION-001`, `GRAPH-PROJECTION-002`, `GRAPH-ISOLATION-001`, `GRAPH-PROVENANCE-001`, `GRAPH-QUERY-001`, `GRAPH-INTEGRATION-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/runbooks/local-development.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md` updated.

### Outstanding for team review

- [ ] `SUPPORTS` (`Observation` -> `Event`) is schema-defined but not populated — neither `ObservationV1` nor `EventV1` carries the observation-to-event link needed (ADR-001, Decision 4). Needs a decision once a later phase builds events from observations.
- [ ] The "entities must be projected before the events that reference them" ordering constraint (ADR-001, Decision 3) needs confirming against whatever later-phase orchestrator actually calls this projection code.
- [ ] No API endpoint, worker, or orchestrator calls `app/modules/graph/projection.py` yet — there is no ingestion pipeline to call it from in this phase.

## Jasraj Phase 1 — Document and Structured-Data Processing: In progress

Verification is complete as of 2026-09-10 (see `docs/qa/test-results.md` for full command output). Marked **in progress**, not complete, pending the team-review items below and final sign-off.

### Delivered

- [x] `app/modules/structured_processing/` — `models.py`, `errors.py`, `limits.py`, `provenance.py`, `worker.py`; `document/` (`classifier`, `text_extractors`, `pdf`, `docx`, `txt`, `fir_report`, `ocr_routing`); `structured/` (`csv_parser`, `xlsx_parser`, `json_parser`, `profiles`, `cdr`, `finance`). Typed, deterministic, no ML/LLM/embeddings, no direct PostgreSQL/Neo4j/Redis/MinIO access (statically verified).
- [x] Five explicit versioned parser profiles (`fir_report_text_v1`, `cdr_generic_v1`, `financial_transaction_generic_v1`, `generic_tabular_v1`, `generic_json_v1`) documented in `docs/architecture/parser-profiles-v1.md`.
- [x] `docs/architecture/document-and-structured-processing-v1.md`, `docs/decisions/ADR-002-deterministic-source-processing-and-provenance.md` — design, provenance policy, and the decisions behind deterministic IDs, conservative normalization, Decimal-safe money, and OCR-deferral semantics.
- [x] `pypdf`, `python-docx`, `openpyxl` added via `uv add` (the only three allowed additions), plus the `openpyxl.*` mypy override.
- [x] `tests/unit/structured_processing/` (151 tests across classification, PDF/DOCX/TXT extraction, FIR regex extraction, CSV/XLSX/JSON parsing + limits, CDR/financial normalization, provenance/determinism, worker dispatch, and static safety checks), `tests/integration/structured_processing/` (full pipeline via a real local-file `SourceResolver`, no external service needed), `tests/fixtures/structured_processing/` (hand-built PDF bytes, `python-docx`/`openpyxl`-built DOCX/XLSX, evidence/job factories) — all 20 required scenarios from the task brief covered.
- [x] QA entries `DOC-PROCESS-001`, `DOC-PROVENANCE-001`, `DOC-OCR-ROUTING-001`, `CDR-NORMALISE-001`, `FINANCE-NORMALISE-001`, `STRUCTURED-SAFETY-001`, `WORKER-RESULT-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/runbooks/local-development.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md` updated.

### Outstanding for team review

- [ ] No worker orchestration calls `worker.process_job` yet — there is no API endpoint, queue consumer, or scheduler in this repo to invoke it from, and no real MinIO-backed `SourceResolver` (later-phase evidence-lifecycle integration).
- [ ] `document/fir_report.py`'s patterns (Indian mobile only, Indian vehicle-plate format only, a curated UPI handle list, no DD/MM date resolution) are intentionally narrow — see `docs/qa/known-limitations.md` and ADR-002's open questions.
- [ ] DOCX extraction order (paragraphs, then tables) is deterministic but not visually interleaved — flagged in ADR-002 in case a later phase needs positional fidelity.
- [ ] No real OCR engine — `document/ocr_routing.py` only makes the routing decision; a later phase must consume its `document_requires_ocr` checkpoint.

## Aditya Phase 1 — Authentication, Case Access Control, Security Middleware, and Operational Foundation: Complete

Verification is complete as of 2026-09-10, including live PostgreSQL/Redis integration tests (see `docs/qa/test-results.md` for full command output). Marked **in progress**, not complete, pending the team-review items below.

### Delivered

- [x] `app/modules/access_control/` — `models.py`, `errors.py`, `password.py`, `tokens.py`, `sessions.py`, `repository.py`, `service.py`, `policy.py`, `dependencies.py`, `rate_limit.py`, `audit.py`, `retry.py`, `api.py`. Argon2id password hashing (`pwdlib[argon2]`); short-lived signed JWT access tokens + opaque rotating refresh tokens (`PyJWT`), hashed at rest, never raw; refresh rotation with token-family reuse detection; case-scoped RBAC/ABAC with default deny; fixed-window login/refresh rate limiting (Redis-backed, in-memory for tests, fail-closed on outage); safe security-audit-event recording; a bounded retry helper that refuses to retry non-idempotent security actions.
- [x] `POST /api/v1/auth/{register,login,refresh,logout}` + `GET /api/v1/auth/me`, registered in `app/main.py` alongside a new `SecurityHeadersMiddleware` (safe static headers + `Cache-Control: no-store` on every auth response, applied at the middleware layer so it also covers error responses).
- [x] `migrations/versions/7e8499f34f29_access_control_foundation.py` — `users`/`cases`/`case_memberships`/`auth_sessions`/`security_audit_events`, hand-written (not autogenerated; `migrations/env.py`'s `target_metadata = None` is unchanged).
- [x] New approved dependencies `PyJWT`, `pwdlib[argon2]` added via `uv add`; `AUTH_JWT_*`/`AUTH_*_TOKEN_TTL_SECONDS`/`AUTH_*_RATE_LIMIT` settings added to `app/core/config.py` (fails fast on a missing/weak `AUTH_JWT_SECRET`, matching every other required setting) and `.env.example`.
- [x] Two small, additive fixes to Nipun's shared `app/core/errors.py`: a `RequestValidationError` handler that never echoes a submitted request-body value (closing a real leak risk once any endpoint accepts a password), and an extended `ErrorCode`/`http_exception_handler` status-code mapping that also now preserves exception headers like `WWW-Authenticate`.
- [x] `docs/architecture/access-control-v1.md`, `docs/architecture/security-boundaries-v1.md`, `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, `docs/runbooks/lan-development.md` — design, security-boundary picture, and the reasoning behind every non-obvious decision (including a real Starlette/`BaseHTTPMiddleware` exception-handling interaction found and worked around locally).
- [x] `tests/unit/access_control/`, `tests/security/access_control/` (no live infra needed — an in-memory fake repository/rate-limiter), `tests/integration/access_control/` (self-skipping without live PostgreSQL/Redis, same pattern as `tests/integration/test_readiness_live.py`), `tests/fixtures/access_control/` — all 22 required unit/security scenarios and all 6 required integration scenarios from the task brief, verified passing both self-skipped and live.
- [x] QA entries `AUTH-REGISTER-001`, `AUTH-LOGIN-001`, `AUTH-REFRESH-001`, `AUTH-LOGOUT-001`, `AUTH-RATE-LIMIT-001`, `RBAC-CASE-001`, `ABAC-CLEARANCE-001`, `SECURITY-MIDDLEWARE-001`, `SECURITY-AUDIT-001`, `RETRY-SAFETY-001`, `LAN-RUNBOOK-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/runbooks/local-development.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md` updated.

### Outstanding for team review

- [ ] `require_authenticated_user`'s per-request live session/user database read (so logout/deactivation take effect immediately) is untested at real load — see ADR-003, Decision 1.
- [x] ~~The Starlette/`BaseHTTPMiddleware` exception-propagation interaction... worked around locally~~ — **resolved centrally**, see "Integration Hardening 1" below.
- [ ] No case-management API exists yet for `require_case_*` dependencies to actually protect — they're ready for Nipun's later case/evidence endpoints to build against, but unexercised by any real endpoint in this repo.
- [ ] CORS and browser cookie/CSRF policy are explicitly deferred until a browser frontend exists (see `docs/architecture/security-boundaries-v1.md`).

## Integration Hardening 1 — Central Middleware and Exception Handling: Complete

Owner: Aditya. Verification complete as of 2026-09-10 (see `docs/qa/test-results.md`). Re-investigated the "`BaseHTTPMiddleware` interaction" reported in the Aditya Phase 1 entry above from scratch, rather than trusting the original diagnosis — found it incorrect, found a different real gap in the same investigation, and replaced the local per-endpoint workaround with one central fix. Full writeup: `docs/architecture/security-boundaries-v1.md`'s "Integration Hardening 1" section, `docs/decisions/ADR-003-...md` Decision 10 (revised).

### Delivered

- [x] Corrected root-cause finding: the originally-reported symptom (an unhandled exception "escaping" the safe error envelope) was caused by httpx's `ASGITransport` test-only default (`raise_app_exceptions=True`), not by `BaseHTTPMiddleware` — verified by reproducing the identical symptom with zero custom middleware, and by confirming a real `uvicorn` process (with `BaseHTTPMiddleware`-based middleware in the stack) returns the correct safe response over real HTTP the whole time.
- [x] A separate, previously-unknown real gap found during the same investigation and fixed: Starlette dispatches the bare-`Exception` handler from its outermost `ServerErrorMiddleware`, bypassing every user-added middleware's header injection for that one response path (no correlation ID, no security headers, regardless of `BaseHTTPMiddleware` vs. pure ASGI). Fixed by having all three `app/core/errors.py` exception handlers set their own safe headers directly (`_safe_error_headers`).
- [x] `RequestIDMiddleware` (`app/core/errors.py`) and `SecurityHeadersMiddleware` (`app/modules/access_control/api.py`) rewritten as pure ASGI middleware — a well-justified simplification (both only ever needed to wrap `send`), not the fix for the reported bug.
- [x] `app/modules/access_control/api.py`/`dependencies.py`'s `_internal_error` per-endpoint/per-dependency workaround removed outright; the module-local `structlog` warning it emitted is now emitted centrally from `unhandled_exception_handler` instead.
- [x] `tests/conftest.py`'s shared `client` fixture and every access-control test file's own override now set `ASGITransport(..., raise_app_exceptions=False)`, matching real client/server behavior instead of the test-transport-only re-raise.
- [x] `tests/unit/test_error_handling.py` (new, 18 tests) — a synthetic test app proving every required scenario: route/dependency `HTTPException`, request-validation failure, unexpected exception (route and dependency), no `ExceptionGroup`/traceback/secret ever reaches the client, correlation ID present on success and every error path (including the specific deep `ServerErrorMiddleware` path the original design missed), non-HTTP (`lifespan`) scope pass-through, and context cleanup with no cross-request leakage.
- [x] `tests/unit/access_control/` + `tests/security/access_control/` (139 tests, including the one test specifically designed for this exact path) confirmed passing unchanged with the local workaround removed.
- [x] QA entries `CORE-MIDDLEWARE-001`, `CORE-ERROR-ENVELOPE-001`, `CORE-REQUEST-CONTEXT-001`, `SECURITY-ERROR-SAFETY-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/security-boundaries-v1.md`, `docs/decisions/ADR-003-...md`, `docs/qa/known-limitations.md` updated additively.

### Outstanding for team review

- [ ] Starlette's `ServerErrorMiddleware`-re-raises-after-sending behavior (documented, upstream, intentional) means any *new* test exercising a genuinely-unhandled-exception path must remember `ASGITransport(..., raise_app_exceptions=False)` — flagged at every fixture definition site, but not enforceable by tooling.

## Sarthak Phase 1 — Audio, Social/Chat, Multilingual Alias, and Communication Foundation: Complete

Verification is complete as of 2026-09-10 (see `docs/qa/test-results.md` for full command output). Marked **in progress**, not complete, pending the team-review items below and final sign-off.

### Delivered

- [x] `app/modules/communication_processing/` — `models.py`, `errors.py`, `limits.py`, `provenance.py`, `worker.py`; `audio/` (`metadata`, `transcript_import`, `diarization_import`, `routing`); `social/` (`common`, `whatsapp`, `telegram`, `instagram`, `json_records`); `aliases/` (`normalize`, `scripts`, `transliteration`); `linking/` (`models`, `deterministic`). Typed, deterministic, no ML/ASR/diarization model, no direct PostgreSQL/Neo4j/Redis/MinIO/queue/HTTP/subprocess access (statically verified).
- [x] Seven explicit processor profiles (`audio_metadata_v1`, `transcript_import_v1`, `diarization_import_v1`, `whatsapp_export_v1`, `telegram_export_v1`, `instagram_export_v1`, `generic_social_json_v1`) in `worker.py`.
- [x] `docs/architecture/audio-social-and-communication-processing-v1.md`, `docs/architecture/multilingual-alias-candidates-v1.md`, `docs/decisions/ADR-005-provenance-first-communication-processing.md` — design, provenance policy, and the decisions behind ASR/diarization deferral, structural identity-merge prevention, deterministic candidate IDs, and the never-guess timezone/format/transliteration policy.
- [x] No new dependencies — stdlib `wave`/`json`/`unicodedata`/`re` only, per the task's "prefer stdlib" instruction; zero changes to `pyproject.toml`/`uv.lock`.
- [x] `tests/unit/communication_processing/` (215 tests across audio metadata/routing, transcript/diarization import, all four chat parsers + safety limits, alias normalization/script-detection/transliteration, communication-link candidates, worker dispatch, and static safety checks), `tests/integration/communication_processing/` (all seven processors end to end, no external service needed), `tests/fixtures/communication_processing/` (real in-memory WAV bytes via stdlib `wave`, synthetic WhatsApp/Telegram/Instagram/generic-JSON exports) — all 31 required scenarios from the task brief covered.
- [x] QA entries `AUDIO-METADATA-001`, `AUDIO-PROVENANCE-001`, `AUDIO-DEFER-001`, `DIARIZATION-BOUNDARY-001`, `CHAT-PARSER-001`, `CHAT-PROVENANCE-001`, `CHAT-SAFETY-001`, `ALIAS-NORMALIZATION-001`, `ALIAS-TRANSLITERATION-001`, `COMM-LINK-001`, `COMM-ISOLATION-001`, `COMM-WORKER-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/runbooks/local-development.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md` updated additively.

### Outstanding for team review

- [ ] No orchestration calls `communication_processing.worker.process_job` or `linking.deterministic`'s candidate functions yet — same "built but not yet wired up" situation `structured_processing`/`graph` are in.
- [ ] Format/shape coverage is intentionally narrow: WAV only for audio; one documented WhatsApp text shape, Telegram's plain-string-text form only, Instagram's basic export shape only (see `docs/qa/known-limitations.md`).
- [ ] `aliases/transliteration.py`'s Devanagari/Gurmukhi character tables cover common consonants/vowels/vowel-signs, not either script exhaustively — a later phase may need to extend them (always through the same fully-tested-table discipline, never a fuzzy fallback).
- [ ] `find_same_conversation_candidates`'s different-evidence-source restriction (ADR-005, Decision 5) is a deliberate design call worth the team validating against real case data shapes.

## Gaurav Phase 1 — Video and Image Processing Foundation: Complete

Verification is complete as of 2026-09-10 (see `docs/qa/test-results.md` for full command output).

### Delivered

- [x] `app/modules/media_processing/` — `models.py`, `errors.py`, `limits.py`, `provenance.py`, `worker.py`, `capability.py`, `performance.py`, `source.py`; `video/` (`probe`, `sampling`, `frames`); `image/` (`decoder`, `geometry`); `analysis/` (`interfaces`, `fake_detector`, `fake_tracker`, `fake_ocr`). Typed, deterministic, no real object-detection/tracking/OCR model, no direct PostgreSQL/Neo4j/Redis/MinIO/queue access (statically verified).
- [x] Two explicit processors in `worker.py`: `media_metadata_v1` (probe/decode only, no analysis component required) and `media_detection_v1` (requires an explicit `detector`; `tracker`/`ocr` optional) — a fake analysis component is never selected as a silent default.
- [x] `docs/architecture/media-processing-v1.md`, `docs/architecture/media-observation-taxonomy-v1.md`, `docs/decisions/ADR-004-media-provenance-and-anonymous-tracking.md`, `docs/runbooks/media-development.md` — design, provenance policy, and the decisions behind frame-number trustworthiness, never-clamped bounding boxes, per-item (not per-job) malformed-output handling, the `media_metadata` locator convention, and no-silent-fake-selection.
- [x] Three new dependencies (`opencv-python-headless`, `Pillow`, `numpy`) via `uv add`, the only ones permitted for this phase; `pyproject.toml`/`uv.lock` updated.
- [x] `tests/unit/media_processing/` (194 tests: classification, limits, `ffprobe` parsing, sampling determinism, frame extraction/trustworthy frame numbers, image decode/decompression-bomb rejection, bbox normalization, deterministic observation IDs, fake detector/tracker/OCR determinism, worker orchestration for both video and image, GPU-absence safety, performance-metric bounds, and static module-isolation/no-path-leakage checks), `tests/integration/media_processing/` (6 tests against real `ffmpeg`/`ffprobe` and a synthetic `lavfi testsrc` MP4: probe, deterministic frame extraction, fake-analysis pipeline end to end, exact provenance, repeat-processing ID stability, temp-file cleanup), `tests/fixtures/media_processing/` (synthetic PNG/frame/MP4 builders — no real footage or imagery) — all required scenarios from the task brief covered.
- [x] QA entries `MEDIA-CLASSIFY-001`, `VIDEO-PROBE-001`, `VIDEO-SAMPLING-001`, `FRAME-PROVENANCE-001`, `IMAGE-DECODE-001`, `BBOX-NORMALISE-001`, `MEDIA-WORKER-001`, `MEDIA-ISOLATION-001`, `GPU-CAPABILITY-001`, `MEDIA-PERFORMANCE-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/runbooks/local-development.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md` updated additively.

### Outstanding for team review

- [ ] No real object-detection/tracking/OCR model exists — `analysis/interfaces.py`'s protocols are ready for a later phase's YOLO/ByteTrack/PaddleOCR adapter, but nothing in this repo calls a real model yet. (Orchestration itself is now wired — see "Phase 2 — Gaurav Media-Processing Worker Foundation" below — this bullet is specifically about the missing real model.)
- [ ] `video/frames.py` extracts each sampled timestamp via its own `ffmpeg` subprocess invocation (correct and simple, bounded by `max_sampled_frames`, but not the fastest possible approach for a very large sample plan) — see ADR-004's open questions.
- [ ] GPU visibility detection is `nvidia-smi`-only, no AMD/Apple-Silicon-equivalent check — acceptable until a real GPU-backed model adapter exists to make the distinction matter.

## Phase 2 — Nipun evidence lifecycle foundation: Complete

Verification ran 2026-09-11 (see `docs/qa/test-results.md` for full command output). The containerized `api` image initially failed to rebuild in this sandbox due to transient DNS flakiness reaching PyPI from inside the Docker build network (reproduced against three different pre-existing dependencies, unrelated to this task's own code) — the full flow was first verified live via infra-in-Docker/API-on-host, then the full `docker compose up --build` was retried and completed cleanly, with all five containers (`api`, `postgres`, `neo4j`, `redis`, `minio`) healthy/running and `/healthz`/`/readyz`/`/api/v1/meta/contracts` all correct against the fully containerized stack.

### Delivered

- [x] `app/modules/evidence_lifecycle/` — `models.py`, `errors.py`, `routing.py`, `schemas.py`, `storage.py`, `jobs.py`, `repository.py`, `service.py`, `dependencies.py`, `api.py`. Real, case-scoped evidence upload: streamed SHA-256 hashing (bounded 1 MiB chunks, never fully buffered), private MinIO object storage, atomic PostgreSQL persistence of evidence + a durable `WorkerJobV1` job in one transaction, best-effort Redis publish (`RPUSH` per source type), safe metadata/status retrieval. No document/OCR/ASR/video parsing, no entity resolution, no graph projection, no worker consumer, no Merkle/signatures — a producer-only foundation.
- [x] `POST/GET /api/v1/cases/{case_id}/evidence`, `GET /api/v1/cases/{case_id}/evidence/{evidence_id}`, `GET /api/v1/cases/{case_id}/jobs/{job_id}`, registered in `app/main.py` alongside a new best-effort MinIO-bucket-init `lifespan` hook (never fails startup; `/readyz` remains the readiness signal).
- [x] `migrations/versions/f2086e1e89f6_evidence_lifecycle_foundation.py` — `evidence_records`/`worker_jobs`, hand-written (not autogenerated), FKs to `cases`/`users`, a partial unique index on `(case_id, upload_idempotency_key)`, a unique index on `worker_jobs.idempotency_key`, check constraints matching the frozen `SourceType`/`EvidenceClassification`/`EvidenceProcessingStatus`/`WorkerStatus` enums exactly.
- [x] `MAX_EVIDENCE_BYTES` setting (`app/core/config.py`, `.env.example`, `compose.yaml`); `python-multipart` added via `uv add` (the only new dependency — required by FastAPI for `File`/`Form` parsing, confirmed genuinely absent first).
- [x] Reused, not duplicated: `app.modules.access_control.dependencies.require_case_action`/`require_evidence_read` for authorization, `record_audit_event` for successful-upload audit logging, the existing SQLAlchemy-Core/Alembic hand-written-table convention, the existing MinIO-client-construction pattern (`asyncio.to_thread`), and the `SourceResolver` protocol shape (via `MinioSourceResolver`, no cross-module import).
- [x] `tests/unit/evidence_lifecycle/` (29 tests: upload/hash/store/persist/publish flow, idempotency replay/conflict/race, storage/DB/dispatch failure recovery, HTTP wiring, case-scoping/authorization, safe-response-shape), `tests/security/evidence_lifecycle/` (34 tests, module-boundary static-AST checks), `tests/integration/evidence_lifecycle/` (2 tests, migration + FK/uniqueness enforcement + a real upload flow against live PostgreSQL/MinIO, run live in this session), `tests/fixtures/evidence_lifecycle/` — all required scenarios from the task brief covered.
- [x] QA entries `EVIDENCE-UPLOAD-001`, `EVIDENCE-KEY-SAFETY-001`, `EVIDENCE-VALIDATION-001`, `EVIDENCE-IDEMPOTENCY-001`, `EVIDENCE-FAILURE-RECOVERY-001`, `EVIDENCE-AUTHZ-001`, `EVIDENCE-SAFE-RESPONSE-001`, `EVIDENCE-CONTRACT-001`, `EVIDENCE-ISOLATION-001`, `EVIDENCE-MIGRATION-001`, `EVIDENCE-LIVE-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/evidence-lifecycle.md`, `docs/architecture/phase-2-decisions.md` (new); `docs/architecture/contracts.md`, `docs/runbooks/local-development.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md`, README.md updated additively.
- [x] Verified live end to end against a real Compose stack (postgres/neo4j/redis/minio up, API run on host per the documented "Option B" workflow): a real multipart upload produced the expected PostgreSQL rows, a real private MinIO object with byte-identical content, and a real Redis-queued `WorkerJobV1`; idempotent replay (`200`) and a conflicting-content rejection (`409`) both confirmed live; `/healthz`/`/readyz`/`/api/v1/meta/contracts` all remained correct with the new router and startup hook wired in.
- [x] Full-stack `docker compose up --build -d` verified: all five services (`api`, `postgres`, `neo4j`, `redis`, `minio`) reached `healthy`/`running`, and `/healthz`, `/readyz`, `/api/v1/meta/contracts` all responded correctly against the fully containerized stack.

### Outstanding for team review

- [x] ~~No worker consumer... `worker_jobs.status` is always `queued`~~ — **claim/result submission delivered in Phase 2.1 below**; a real worker daemon still does not exist.
- [x] ~~Denied-access-attempt auditing for case-scoped endpoints is not wired~~ — **resolved in Phase 2.4 below**: `require_case_action` now records `case_access_denied`.
- [ ] `source_type=other` has no registered processor and is rejected outright — a later phase must decide how (or whether) to support it.
- [ ] `Idempotency-Key` conflict detection does not consider `classification`/`parser_profile` — see `docs/qa/known-limitations.md`.

## Phase 2.1 — Nipun job claim/result integration: Complete

Verification ran 2026-09-11 (see `docs/qa/test-results.md` for full command output), including a full `docker compose up --build` rebuild and a real claim→submit-result smoke test against the fully containerized stack with real auth/case setup, producing durable `worker_jobs`/`worker_results`/`worker_observations` rows confirmed both via the API and by querying PostgreSQL directly. Two real bugs were found and fixed during that live testing (a claim-token-check ordering gap and a blank-secret normalization gap — see `docs/qa/test-results.md` for both), each closed with a regression test before this record.

### Delivered

- [x] `EvidenceLifecycleRepository.claim_job` — atomic, concurrency-safe job claiming via `FOR UPDATE SKIP LOCKED`, matching on `processor_name`/`processor_version`, recovering expired leases (incrementing `attempt` only on a genuine reclaim), never double-claimable by two concurrent callers. Verified both against `FakeEvidenceLifecycleRepository` (unit) and real PostgreSQL (integration).
- [x] One-time, high-entropy claim tokens (`secrets.token_urlsafe(32)`), hashed at rest (SHA-256), never part of `WorkerJobV1` or any frozen contract — the same primitive/reasoning as `access_control.tokens`'s refresh-token handling.
- [x] `worker_results`/`worker_observations` tables (migration `102857ca8d1d`, additive on top of `f2086e1e89f6`) plus four additive columns on `worker_jobs` (`claimed_at`, `lease_expires_at`, `claimed_by`, `claim_token_hash`) — durable, canonical persistence of one `WorkerResultV1` and every `ObservationV1` per job attempt, atomic with the job's terminal status transition.
- [x] `EvidenceLifecycleService.submit_result` — validates claim-token match/expiry, `job_id`/`case_id`/`evidence_id`/observation scope, and terminal-status-only submission, before persisting; idempotent exact-payload replay; safe `409` conflict for a different payload against an already-terminal job; a failed persistence transaction leaves the job non-terminal with zero result/observation rows.
- [x] `POST /api/v1/internal/worker-jobs/claim`, `POST /api/v1/internal/worker-jobs/{job_id}/result` — gated by a new `require_worker_principal` dependency (fails closed `503` when unconfigured, `401` on a wrong/missing shared secret) — a narrow, documented stand-in for a real per-worker credential system, per this task's explicit instruction.
- [x] User-facing `GET /api/v1/cases/{case_id}/jobs/{job_id}` extended with safe `claimed_at`/`completed_at`/`observation_count` fields; still never exposes a claim token, `claim_token_hash`, or `object_uri`.
- [x] Reused, not duplicated: `access_control.tokens`'s token-generation/hashing pattern, `create_evidence_with_job`'s `IntegrityError`-based race handling, `app.core.canonical.canonical_sha256` for idempotency comparison, `record_audit_event` for accepted-result auditing.
- [x] `tests/unit/evidence_lifecycle/{test_worker_claim,test_worker_result,test_worker_internal_api}.py` (21 new unit/HTTP tests), `tests/integration/evidence_lifecycle/test_worker_lifecycle_live.py` (2 tests, run live against real PostgreSQL/MinIO), plus one new case-scoped HTTP test and `FakeEvidenceLifecycleRepository`/factory extensions — all 18 required scenarios from the task brief covered.
- [x] `tests/security/evidence_lifecycle/test_evidence_lifecycle_boundaries.py`'s forbidden-contract-import list narrowed (`ObservationV1`/`WorkerResultV1` removed) to reflect this module's new, legitimate validate-and-persist-a-submitted-result responsibility — documented in `docs/architecture/phase-2-decisions.md`.
- [x] QA entries `WORKER-CLAIM-001`, `WORKER-RESULT-SUBMIT-001`, `WORKER-RESULT-IDEMPOTENCY-001`, `WORKER-INTERNAL-API-001`, `WORKER-JOB-STATUS-001`, `WORKER-LIVE-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/worker-job-lifecycle.md` (new); `docs/architecture/evidence-lifecycle.md`, `docs/architecture/phase-2-decisions.md`, `docs/architecture/contracts.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md`, `docs/runbooks/local-development.md`, README.md updated additively.

### Outstanding for team review

- [x] ~~No real per-worker credential system — `require_worker_principal` is a narrow shared-secret boundary only~~ — **resolved in Phase 2.4 below**: per-worker `WorkerCredentialRecord`s replace the shared secret entirely.
- [ ] No max-attempt cutoff on reclaiming — a job with a perpetually-expiring lease is reclaimable forever.
- [ ] No lease renewal for a long-running worker.
- [ ] No automatic redrive of `deferred`/`cancelled` jobs.
- [x] ~~Denied worker actions (bad claim token, scope mismatch, conflict) are not audit-logged~~ — **resolved in Phase 2.4 below**: `worker_authentication_denied`/`worker_processor_scope_denied`/`worker_job_access_denied` are now recorded.
- [ ] `evidence_records.processing_status` still doesn't reflect job completion once a result is submitted — deliberately out of this task's scope (see `docs/qa/known-limitations.md`).

## Jasraj Phase 2 — Structured-Processing Worker: Complete

Initial verification ran 2026-09-11 (`uv sync`/`ruff format --check`/`ruff check`/`mypy app`/`pytest -q`, `docker compose config` all passed) with Docker itself unavailable in that session's sandbox, so the full live claim/parse/submit path couldn't be run then. Docker became available later the same day (Phase 2.2 below); the full live path was verified for real in that session (`docs/qa/test-results.md`'s "Live smoke test" entry) using this worker's unmodified code — `client.py`/`input_resolver.py`/`worker.py` required no changes to consume the real endpoint once it existed, exactly as this build's Protocol-based design anticipated. Marked **Complete** now that both blockers below are resolved.

### Delivered

- [x] `app/modules/structured_processing/{client.py,input_resolver.py}` (new) and an extended `worker.py` — a one-shot worker CLI (`uv run python -m app.modules.structured_processing.worker --once`) that authenticates via `WORKER_SHARED_SECRET`, claims one compatible job through Nipun's internal worker API (`/api/v1/internal/worker-jobs/claim`), resolves its evidence via an injected `WorkerInputResolver`, calls the existing (Phase 1, unchanged) `process_job`, and submits the result (`/{job_id}/result`). No daemon, polling loop, Celery, or scheduler — `--once` is the only supported mode.
- [x] `httpx` promoted from a dev-only to a runtime dependency (`uv add httpx`); `Settings.worker_api_base_url` (`WORKER_API_BASE_URL`, default `http://localhost:8000`) added to `app/core/config.py`/`.env.example`/`compose.yaml`.
- [x] `document/classifier.py`'s `ContentKind.TXT` extended to also accept a `.md` extension (Markdown is plain text; no new `ContentKind` or parser needed) — the only change to existing Phase 1 parsing logic this task made.
- [x] Five supported processors reused unchanged from Phase 1 (`SUPPORTED_PROCESSORS`): `fir_report_text_v1`, `cdr_generic_v1`, `financial_transaction_generic_v1`, `generic_tabular_v1`, `generic_json_v1` — exact names/versions/routing untouched.
- [x] `docs/architecture/structured-processing-worker.md` (new) — the full design, including the documented **input-access boundary**: no endpoint currently exists for a claimed worker to retrieve its job's evidence bytes/`content_type`/`original_filename`; a `WorkerInputResolver` Protocol (`StaticInputResolver` for tests, `LiveInputResolver` for the real API) isolates the worker from this gap, and a claimed job whose input can't be resolved submits a real `DEFERRED` result (checkpoint `input_resolution_unavailable`) rather than a fabricated success. A precise, proposed `GET /api/v1/internal/worker-jobs/{job_id}/input` endpoint shape is documented for team/Nipun review — not implemented unilaterally in another contributor's module.
- [x] `tests/unit/structured_processing/{test_worker_client,test_worker_orchestration}.py` (20 new tests: wire-format claim/submit/fetch_input behavior via `httpx.MockTransport`, `run_once`/`main` orchestration via a fake client, secret/claim-token non-leakage, multi-processor claim loop, no-job clean exit, deferred-on-missing-input, submit-failure-propagates-safely), plus one new Markdown-classification test — all 19 required scenarios from the task brief covered by these plus Phase 1's existing `structured_processing` test suite (parsing/provenance/safety scenarios were already covered and are unchanged).
- [x] `tests/integration/structured_processing/test_worker_live.py` (new) — proves the real `WorkerApiClient` against a real running server when one is reachable (a genuine "no work" claim, a genuine `InputResolutionUnavailableError` from `fetch_input`), self-skipping (never fabricating success) without a `.env`, a reachable server, or a configured `WORKER_SHARED_SECRET`.
- [x] QA entries `SP-WORKER-CLIENT-001`, `SP-WORKER-ORCHESTRATION-001`, `SP-WORKER-LIVE-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/contracts.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md`, `docs/runbooks/local-development.md`, README.md updated additively.

### Outstanding for team review

- [x] ~~The input-access boundary is unresolved~~ — **closed in Phase 2.2 below**: `GET /api/v1/internal/worker-jobs/{job_id}/input` now exists, and `client.py`/`input_resolver.py` consume it for real (including worker-side SHA-256 verification before parsing) with no change to their originally-designed shape.
- [ ] `generic_tabular_v1`/`generic_json_v1` remain unreachable through the live upload path (Nipun's `routing.py` doesn't route to them) — investigated in Phase 2.2 and deliberately left as a team decision rather than worked around; see that section below.

## Phase 2.2 — Nipun secure worker evidence delivery: Complete

Verification ran 2026-09-11 (see `docs/qa/test-results.md` for full command output), including a full `docker compose up --build -d` (succeeded on the fifth attempt after transient sandbox DNS flakiness on the first four — see that entry) and a genuine live smoke test: a real case/user/evidence upload followed by a real, unmodified `structured_processing` worker `--once` run completed the full `claim -> GET .../input (200 OK, real streamed bytes) -> parse -> submit` path end to end, producing a real `SUCCEEDED` result with `observation_count: 2` — not the `DEFERRED` fallback every prior live attempt in this repository produced. That same live pipeline was then turned into a permanent, self-skipping automated test (`tests/integration/structured_processing/test_worker_live.py::test_full_claim_stream_parse_submit_live_pipeline`), and the **entire** repository test suite ran against fully-live infrastructure with zero skips: `uv run pytest -q` → 1022 passed. Marked **Complete**.

### Delivered

- [x] `ObjectStorage.open_stream`/`ObjectStream` (`storage.py`) — a bounded, chunked read handle; `MinioObjectStorage`'s implementation bridges minio-py's synchronous `.stream()` generator to an async one via `asyncio.to_thread` per chunk (never fully buffering the object in API memory), with a matching `FakeObjectStorage.open_stream` test double.
- [x] `EvidenceLifecycleService.get_claimed_evidence_input`/`ClaimedEvidenceInput` (`service.py`) — validates a claim token against a currently-`running`, unexpired-lease job (deliberately narrower than `submit_result`'s branching: no terminal-job replay case for input delivery), reusing the existing `_hash_claim_token`/`get_evidence` helpers unchanged.
- [x] `GET /api/v1/internal/worker-jobs/{job_id}/input` (`internal_api.py`) — streams a claimed job's evidence bytes via `StreamingResponse`; safe headers only (`Cache-Control: no-store`, a sanitized RFC 6266 `Content-Disposition`, `X-TraceX-Evidence-Id`/`-SHA256`/`-Source-Type`/`-Parser-Profile`) — never an object key, bucket, MinIO endpoint, or credential. Reuses the existing `X-Claim-Token` header rather than introducing a second one.
- [x] `evidence-lifecycle.md`'s "No raw-evidence-download API" boundary revised in place (not violated): the API remains the sole MinIO credential holder and streams the object itself; a worker still never receives an object key/bucket/endpoint/credential, only the bytes of the job it actively holds a live claim token for.
- [x] `structured_processing/client.py`'s `fetch_input` updated to consume the real response (`Content-Disposition` filename parsing, `X-TraceX-Evidence-SHA256`, a worker-side `MAX_INPUT_BYTES` bound applied before the bytes go anywhere else); `worker.run_once` verifies the resolved bytes' SHA-256 before ever calling `process_job`, submitting `FAILED`/`evidence_integrity_mismatch` on a mismatch. `input_resolver.py`'s `WorkerInputResolver` Protocol required **zero changes** — the seam Jasraj's Phase 2 build left was sufficient as designed.
- [x] `generic_tabular_v1`/`generic_json_v1` routing reachability investigated and deliberately left unresolved (not worked around): making them reachable would require either a new `source_type` (a frozen-contract change) or upload-time content-shape-sniffing inside `evidence_lifecycle` (contradicting its own documented "coarse routing only" boundary) — see `docs/architecture/phase-2-decisions.md`'s "Phase 2.2 Decisions".
- [x] `tests/unit/evidence_lifecycle/test_worker_input_api.py` (14 tests), `tests/unit/evidence_lifecycle/test_object_storage.py` (4 new tests: lazy-chunk-pull proof, round-trip, missing-object safety), `tests/unit/structured_processing/test_worker_orchestration.py` (1 new SHA-256-mismatch test), `tests/unit/structured_processing/test_worker_client.py` (`fetch_input` tests updated for the real header contract, plus an oversized-response test) — all scenarios from the task brief covered.
- [x] QA entries `WORKER-INPUT-STREAM-001`, `WORKER-INPUT-STREAM-002` added to `docs/qa/test-matrix.md`; `SP-WORKER-CLIENT-001`/`SP-WORKER-ORCHESTRATION-001`/`SP-WORKER-LIVE-001` updated to reflect the real (not proposed) endpoint.
- [x] `docs/architecture/evidence-lifecycle.md`, `docs/architecture/worker-job-lifecycle.md`, `docs/architecture/structured-processing-worker.md`, `docs/architecture/phase-2-decisions.md`, `docs/qa/known-limitations.md`, `docs/runbooks/local-development.md`, README.md updated additively.

### Outstanding for team review

- [x] ~~`generic_tabular_v1`/`generic_json_v1` routing reachability needs a team decision~~ — **resolved in Phase 2.3 below.**
- [ ] A storage failure mid-stream (after response headers are already sent) cannot be converted into a clean error response — an inherent HTTP-streaming limitation, not something this endpoint's code can work around.
- [ ] No lease-renewal exists yet (unchanged from Phase 2.1) — a very large evidence stream close to its lease boundary could have the lease expire before the subsequent `/result` submission, which would then be rejected as `lease_expired`.

## Phase 2.3 — Nipun explicit structured-data upload routing: Complete

Verification ran 2026-09-11, including a full live Docker pass (see `docs/qa/test-results.md`). The first live attempt found a real bug: the app-level `SourceType` contract change alone wasn't sufficient — PostgreSQL's own `CHECK` constraints on `evidence_records.source_type`/`worker_jobs.source_type` independently enumerated the original eight values and rejected the two new ones with a `500`. Fixed with an additive migration (`af5b05e61b08_structured_source_type_routing`) widening both constraints; re-verified live afterward with a genuine `SUCCEEDED` result (real observations) for all three formats (CSV, XLSX, JSON) via `uv run pytest -q` (1037 passed, zero skips, fully live) and a standalone XLSX smoke check.

### Delivered

- [x] `SourceType.STRUCTURED_TABULAR`/`STRUCTURED_JSON` (`app/contracts/evidence.py`) — additive, backward-compatible enum values; every pre-existing `SourceType` value remains valid and unchanged.
- [x] `routing.py`: `structured_tabular` → `text/csv`/XLSX → `generic_tabular_v1`/`1.0.0`; `structured_json` → `application/json` → `generic_json_v1`/`1.0.0`. Fully disjoint content-type sets between the two, preserving "one source type, one processor" with zero branching logic.
- [x] `EvidenceLifecycleService.upload_evidence`'s persisted `parser_profile` is now always server-computed (`route.processor_name`), for every source type — closing a latent, previously-untested inconsistency where a client-supplied value was stored verbatim. `upload_evidence`'s signature and every existing call site are unchanged.
- [x] `app/modules/structured_processing/structured/profiles.py` inspected and confirmed to already match the required routing exactly — no parser code changed.
- [x] `migrations/versions/af5b05e61b08_structured_source_type_routing.py` (new) — widens the two `CHECK` constraints PostgreSQL independently enforces on `source_type`; found necessary only by attempting the live upload, not by static review.
- [x] `tests/unit/evidence_lifecycle/test_structured_routing.py` (11 tests: CSV/XLSX/JSON valid routing, cross-MIME rejection before storage write, unsupported-MIME rejection, processor/parser-profile override prevention, idempotent replay ×2, cross-case isolation), `tests/contract/test_evidence.py` (+1 parametrized round-trip test), `tests/integration/structured_processing/test_worker_live.py`'s full pipeline test parametrized to also cover both new processors — all 12 required scenarios from the task brief covered (scenario 7 "`other` remains rejected" and scenario 9 "existing routing unchanged" both already covered by the pre-existing, untouched `test_source_type_other_has_no_registered_processor` plus a full-suite regression pass).
- [x] QA entries `STRUCTURED-ROUTING-001`, `STRUCTURED-ROUTING-002`, `STRUCTURED-ROUTING-003` added to `docs/qa/test-matrix.md`; `WORKER-INPUT-STREAM-LIVE-001` updated to reflect the parametrized live pipeline.
- [x] `docs/architecture/{contracts,evidence-lifecycle,phase-2-decisions,structured-processing-worker}.md`, `docs/qa/known-limitations.md`, `docs/runbooks/local-development.md`, README.md updated additively.

### Outstanding for team review

- None. This phase closes the one open question Phase 2.2 raised and introduces no new unresolved boundary.

## Phase 2.4 — Aditya worker identity, authorization binding, and security audit completion: Complete

Verification ran 2026-09-11, including a full `docker compose up --build -d` (all five services healthy on the first attempt), a real `alembic upgrade head` applying this phase's migration on top of the existing live chain, and the **entire** repository test suite run against fully-live infrastructure with zero skips: `uv run pytest -q` → 1084 passed. Two real bugs were found and fixed during that live testing — both in this phase's own test helpers, not in the application code being tested (the security-critical paths — digest verification, processor scoping, identity binding, audit recording — were all confirmed correct on the first live attempt via direct `curl`/CLI checks against the running container):

1. **Test-environment pepper pollution.** `tests/conftest.py` sets a fixed test-only `WORKER_CREDENTIAL_PEPPER` in `os.environ` for the rest of the suite's sake. Both new live test files' `_live_settings()` helper built its `Settings` from `.env` values only for keys `.env` actually defined, so a `.env` that (correctly) leaves `WORKER_CREDENTIAL_PEPPER` unset let pydantic-settings silently fall back to conftest's fake value instead — diverging from what the live API container itself resolves (a real blank env var, normalized to `None`). This produced a genuine credential-digest mismatch between the test process and the live server (`401` on every claim). Fixed by explicitly forcing `worker_credential_pepper=None` into both helpers' kwargs so an explicit `.env`-derived value always wins over OS-env leakage.
2. **`_ensure_worker_credential` bound the wrong token.** The helper called `worker_credentials.create_worker_credential()` to seed a live-test credential, but that function *always* mints its own fresh random token (correct behavior for the trusted-operator CLI, wrong for a helper trying to bind a credential to the developer's own already-configured `WORKER_TOKEN`) — so the stored digest could never match the token the test client actually presented. Fixed by constructing the `WorkerCredentialRecord` directly with `credential_digest = hash_worker_credential(token, pepper)` for the known token, exactly what the CLI does internally minus the random generation step.

Both fixes are confirmed against the real running stack: `tests/integration/evidence_lifecycle/test_worker_identity_lifecycle_live.py` (the full provision → claim → stream → submit → cross-worker-denial → revoke → future-denial sequence) and all four tests in `tests/integration/structured_processing/test_worker_live.py` (including the complete claim→stream→parse→submit pipeline for all three processor types) now pass for real. A manual CLI smoke test against the live stack additionally confirmed: `create`/`rotate`/`revoke`/`list` all work against real PostgreSQL; an in-scope claim returns `200`, an out-of-scope one returns `403` with a real `worker_processor_scope_denied` row; a rotated-away token is immediately rejected `401`; and `worker_credential_rotated`/`worker_credential_revoked`/`worker_processor_scope_denied`/`worker_authentication_denied`/`worker_job_access_denied` all appear as real rows in `security_audit_events` — verified by querying PostgreSQL directly, not just trusting the HTTP response.

### Delivered

- [x] `WorkerCredentialRecord` (`app/modules/access_control/models.py`) — `worker_id`, `display_name`, `status` (`active`/`revoked`), `allowed_processor_names`, `credential_digest`, `created_at`/`rotated_at`/`revoked_at`. `migrations/versions/48e9e76153ca_worker_credentials_and_job_ownership.py` — the `worker_credentials` table plus a nullable `worker_jobs.claimed_by_worker_id` (FK `ON DELETE SET NULL`, indexed), applied cleanly on top of the existing live migration chain.
- [x] `app/modules/access_control/worker_credentials.py` — the trusted-operator-only CLI (`create`/`rotate`/`revoke`/`list`), never a public HTTP API. `secrets.token_urlsafe(32)` tokens; `HMAC-SHA256(pepper, token)` digest with an unkeyed-SHA-256 fallback when no pepper is configured (`resolve_worker_pepper` fails closed `503` only in production when the pepper is missing); plaintext shown exactly once, to the local terminal only; `list` never prints a token or digest. `rotate_worker_credential`/`revoke_worker_credential` each record a `worker_credential_rotated`/`worker_credential_revoked` audit event via the plain (propagating, not denial-swallowing) `record_audit_event` — an accepted operator action, not a denial.
- [x] `Settings.worker_shared_secret` removed outright (no fallback path); `Settings.worker_token` (client-side, what a worker process presents) and `Settings.worker_credential_pepper` (server-side) added, both blank-string-normalized to `None` by a shared validator (closing the exact `docker compose` `${VAR:-}`-resolves-to-empty-string class of bug Phase 2.1 already documented once for the old shared secret).
- [x] `evidence_lifecycle.dependencies.require_worker_principal` rewritten to authenticate against a real `WorkerCredentialRecord` looked up by digest — `503` only for a missing production pepper, `401` (identical generic detail) for missing/malformed/unknown/revoked credentials. `WorkerPrincipal` now carries `worker_id`/`display_name`/`allowed_processor_names` from the real row, never self-declared.
- [x] `POST /claim` enforces per-worker processor scoping (`403`, `worker_processor_scope_denied` audited) before the claim query ever runs. A successful claim persists `claimed_by_worker_id` for the authenticated worker. `POST /result` and `GET /input` both verify the authenticated worker **is** the identity bound to the job — checked immediately after claim-token-hash match and **before** the terminal-result replay/idempotency branch — before any lease/status check; a wrong worker with a right-shaped claim token is rejected `401` (`worker_job_access_denied` audited) whether the job is running or already terminal. A legitimate lease-expiry reclaim transfers ownership to the new claiming worker. `FOR UPDATE SKIP LOCKED`, attempt counting, claim-token hashing, and result idempotency are all unchanged from Phase 2.1.
- [x] `access_control.dependencies.require_case_action` now records a `case_access_denied` audit event via `record_audit_event_safely` before raising `403` — closing the exact gap flagged since Phase 2.
- [x] `record_audit_event_safely` (`access_control/audit.py`) — a denial-only wrapper that swallows a write failure (logs a warning) so a broken audit sink never turns a deny into a grant; every new denial call site in this phase uses it, while the two accepted-action events (`worker_credential_rotated`/`_revoked`) use the plain, propagating `record_audit_event`.
- [x] `structured_processing.worker`/`.client`/`test_worker_live.py` updated to use `WORKER_TOKEN` (no shared-secret fallback anywhere); `.env.example`/`compose.yaml` updated to match.
- [x] `tests/unit/access_control/test_worker_credentials.py` (19 tests), `tests/unit/access_control/test_case_access_audit.py` (4 tests), `tests/unit/evidence_lifecycle/test_worker_identity_service.py` (6 tests), `tests/unit/evidence_lifecycle/test_worker_identity_api.py` (14 tests) — all 19 required scenarios from the task brief covered, plus every pre-existing `evidence_lifecycle`/`structured_processing` test updated for the new signatures and passing unchanged in behavior.
- [x] `tests/integration/evidence_lifecycle/test_worker_identity_lifecycle_live.py` (new, 1 test) — proves the complete lifecycle for real; verified passing live, not just self-skipping.
- [x] QA entries `AUTH-WORKER-IDENTITY-001`, `WORKER-PROCESSOR-SCOPE-001`, `WORKER-JOB-OWNERSHIP-001`, `WORKER-DENIAL-AUDIT-001` added to `docs/qa/test-matrix.md`; `WORKER-INTERNAL-API-001`'s auth description and the two stale `WORKER_SHARED_SECRET` mentions in Jasraj's `SP-WORKER-ORCHESTRATION-001`/`SP-WORKER-LIVE-001` rows corrected.
- [x] `docs/architecture/worker-identity-and-security.md` (new); `README.md`, `docs/architecture/evidence-lifecycle.md`, `docs/architecture/worker-job-lifecycle.md`, `docs/architecture/phase-2-decisions.md`, `docs/qa/test-data.md`, `docs/qa/known-limitations.md`, `docs/runbooks/local-development.md` updated additively.

### Outstanding for team review

- [ ] Rotating or removing `WORKER_CREDENTIAL_PEPPER` invalidates every existing worker credential's digest at once — no per-credential pepper version exists; documented, not automated.
- [ ] No maximum-attempt cutoff or fleet-size limit on worker credentials — an unbounded number may be provisioned, and no credential is auto-revoked after repeated authentication failures.
- [ ] No lease renewal, no automatic redrive of `deferred`/`cancelled` jobs, no max-attempt cutoff on reclaiming — all pre-existing Phase 2.1 limitations, unchanged by this phase.

## Phase 2 — Sarthak Communication Processing Worker Foundation: Complete

Verification ran 2026-09-11, including a full `docker compose up --build -d` (all five services healthy), `uv run alembic upgrade head` confirming the DB already at the existing head (no new migration — no schema change this phase), and the **entire** repository test suite run against fully-live infrastructure with zero failures: `uv run pytest -q` → 1137 passed. A real `--once` CLI subprocess run (not just the pytest live test) additionally proved the complete path against the real stack: a real evidence upload → real claim → real claim-token-bound `GET .../input` stream → real parse → real `POST .../result`, producing a genuine `SUCCEEDED` result with a stored, case-scoped, provenance-complete `ObservationV1` (confirmed via direct PostgreSQL query, not just the HTTP response) — see `docs/qa/test-results.md`'s full entry.

One real cross-suite test-environment bug was found and fixed during live testing, not present in the application code under test (see `docs/qa/test-results.md` and `docs/architecture/phase-2-decisions.md` for the full diagnosis): this worker's own live test and `structured_processing`'s live test collided over a shared local-dev `WORKER_TOKEN` digest when run together in one full-suite pass, and cleaning up the resulting stale credential rows surfaced a second, latent robustness gap in both live tests' `_ensure_worker_credential` helper (a revoked-digest row crashed the fallthrough `INSERT` with a raw `IntegrityError` instead of a clean, actionable failure). Both are fixed; the security-critical paths themselves (digest verification, processor scoping, claim-token binding, SHA-256 integrity check) were correct throughout.

### Delivered

- [x] `app/modules/communication_processing/{client.py,input_resolver.py}` (new) and an extended `worker.py` (`run_once`, `main`, `RunOnceOutcome`, `_build_input_payload`) — a one-shot worker CLI (`uv run python -m app.modules.communication_processing.worker --once`) following `structured_processing`'s established pattern exactly: authenticates via `WORKER_TOKEN` (Aditya's Phase 2.4 per-worker-credential system, unchanged), claims one compatible job, resolves evidence via the claim-token-bound `GET .../input` stream, verifies SHA-256 before parsing, calls the existing (Phase 1, unchanged) `process_job`, and submits the result. No daemon, polling loop, or scheduler.
- [x] `audio/transcript_import.py::parse_transcript_import_payload`, `audio/diarization_import.py::parse_diarization_import_payload` (new) — JSON-interchange deserializers (`{"segments": [...]}`) for the two profiles that previously only accepted pre-built dataclasses; malformed-shape failures (`malformed_json_payload`) checked before timing/bounds validation.
- [x] Seven processors reused unchanged from Phase 1 (`SUPPORTED_PROCESSORS`): `audio_metadata_v1`, `transcript_import_v1`, `diarization_import_v1`, `whatsapp_export_v1`, `telegram_export_v1`, `instagram_export_v1`, `generic_social_json_v1` — exact names/versions/parsing logic untouched. ~~Only two... are reachable through the live upload path today~~ — **resolved**, see "Communication-processing routing fix" below: all seven are now reachable.
- [x] `docs/architecture/communication-processing-worker.md` (new) — the full design, including the "Routing boundary" gap report and the two proposed (not-yet-approved) additive resolutions.
- [x] `tests/unit/communication_processing/{test_communication_worker_client,test_communication_worker_orchestration}.py` (new, 27 tests), `test_transcript_import.py`/`test_diarization_import.py` (+15 tests for the new JSON-interchange deserializers), `test_module_safety.py` updated (`httpx` un-forbidden, matching `structured_processing`'s already-reviewed precedent) — all 15+ required scenarios from the task brief covered, plus every pre-existing `communication_processing` test passing unchanged.
- [x] `tests/integration/communication_processing/test_communication_worker_live.py` (new) — proves the real `WorkerApiClient` against a real running server, and the complete claim→stream→parse→submit pipeline for both live-reachable profiles; self-skips (never fabricates success) without a `.env`, a reachable server, or a configured `WORKER_TOKEN`; provisions its own dedicated credential token to avoid colliding with `structured_processing`'s live test in a shared full-suite run.
- [x] QA entries `COMM-INTERCHANGE-001`, `COMM-WORKER-CLIENT-001`, `COMM-WORKER-ORCHESTRATION-001`, `COMM-WORKER-LIVE-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/{contracts,phase-2-decisions}.md`, `docs/qa/{test-results,test-data,known-limitations}.md`, `docs/runbooks/local-development.md`, `README.md` updated additively.

### Outstanding for team review

- [x] ~~The routing gap: `transcript_import_v1`/.../`instagram_export_v1` are... unreachable through a real evidence upload~~ — **resolved**, see "Communication-processing routing fix" below.
- [ ] No lease renewal, no automatic redrive of `deferred`/`cancelled` jobs — pre-existing Phase 2.1 limitations, unchanged by this phase.
- [ ] `linking.deterministic`'s `CommunicationLinkCandidate` generation still has no caller anywhere in this repository (unchanged from Phase 1) — a later phase's review-workflow integration point, not this task's scope.

## Phase 2.5 — Shreshtha canonical observation-to-Neo4j graph projection: Complete

Verification ran 2026-09-11, including a full `docker compose up --build -d` and the entire repository test suite run against fully-live infrastructure with zero skips: `uv run pytest -q` → 1173 passed (see `docs/qa/test-results.md`). Stable across three consecutive full-suite runs.

### Delivered

- [x] `app/modules/graph/{outbox_repository,projector,worker,dependencies,schemas,api}.py` (new) — a durable PostgreSQL-backed projection queue (`graph_projection_jobs`, defined in `evidence_lifecycle/repository.py`, the transaction owner; read/claimed by `graph/outbox_repository.py`, a sanctioned one-directional dependency), a `FOR UPDATE SKIP LOCKED` claim/lease/bounded-retry orchestration layer (`projector.py`), a one-shot `uv run python -m app.modules.graph.worker --once` CLI, and a case-scoped, paginated read endpoint (`GET /api/v1/cases/{case_id}/graph/observations`, gated by Aditya's pre-provisioned `CaseAction.GRAPH_READ`).
- [x] `EntityMention`/`MENTIONS` (`app/modules/graph/{models,projection,schema,queries}.py`) — a new, evidence-local node/relationship kind capturing `ObservationV1.extracted_entities`, deterministically IDed from `(case_id, observation_id, ordinal, normalized text)`, never merged across observations, never promoted to a resolved `EntityV1`.
- [x] `EvidenceLifecycleRepository.submit_result` durably enqueues one `graph_projection_jobs` row per accepted observation, atomically with `worker_results`/`worker_observations` — a Neo4j outage never discards an accepted worker result, only leaves the projection retryable.
- [x] `migrations/versions/a204a94ccd49_graph_projection_jobs.py` — the new table, applied cleanly on top of the existing live migration chain.
- [x] `tests/unit/graph/{test_projection,test_projector,test_graph_api}.py` (extended/new), `tests/security/graph/test_graph_module_boundaries.py` (new), `tests/integration/graph/{test_outbox_repository_live,test_full_pipeline_live}.py` (new) — all 15 required unit/security scenarios plus the full live pipeline (upload → claim → stream → result → durable job → projector run → real Neo4j query and real HTTP endpoint), verified passing both self-skipped-safe and live. Real `FOR UPDATE SKIP LOCKED` concurrency safety, lease-expiry reclaim, retry exhaustion, and a crash-recovery sweep all proven against live PostgreSQL, not just faked.
- [x] QA entries `GRAPH-ENTITY-MENTION-001`, `GRAPH-PROJECTION-OUTBOX-001`, `GRAPH-PROJECTION-OUTBOX-LIVE-001`, `GRAPH-READ-API-001`, `GRAPH-PROJECTION-LIVE-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/graph-projection.md` (new); `docs/architecture/{graph-taxonomy-v1,neo4j-graph-foundation,phase-2-decisions,contracts}.md`, `docs/qa/{test-data,known-limitations}.md`, README.md updated additively.
- [x] Full-stack `docker compose up --build -d` verified: all five services healthy, `/healthz`/`/readyz`/`/api/v1/meta/contracts` correct, and the real graph-projection pipeline (upload → claim → stream → result → durable job → `worker --once` → real Neo4j query + real HTTP endpoint) verified live end to end.

### Outstanding for team review

- [ ] Whether `EvidenceNode.object_uri` (a Phase 1 decision, unchanged here) should eventually be removed from Neo4j storage entirely, now that a real case-scoped read API has to be deliberately curated to exclude it.
- [ ] No re-projection/rebuild-from-scratch maintenance CLI — recovering from a full graph loss requires a manual SQL reset of every `graph_projection_jobs.status` back to `queued` today.
- [ ] No continuous projector daemon (same non-goal every other worker CLI in this repository documents) and no lease renewal.
- [ ] `project_entity`/`project_event` remain unwired — nothing in this repository constructs a real `EntityV1`/`EventV1` yet.

## Communication-processing routing fix (Shreshtha, evidence_lifecycle): Complete

A team-requested fix: `evidence_lifecycle/routing.py` only ever routed `SourceType.AUDIO`/`SourceType.CHAT` to `audio_metadata_v1`/`generic_social_json_v1`, leaving five of `communication_processing`'s seven Phase-1-built, unit-tested processor profiles (`transcript_import_v1`, `diarization_import_v1`, `whatsapp_export_v1`, `telegram_export_v1`, `instagram_export_v1`) unreachable through a real evidence upload — a gap Sarthak's Phase 2 worker build (above) reported but explicitly did not fix itself, since routing is a shared contract outside that task's scope to change unilaterally. `origin/main` (containing Sarthak's Phase 2 worker) was fast-forward-merged into this branch to make full end-to-end verification possible — see "Origin/main integration" below.

### Delivered

- [x] Five new, additive `SourceType` values (`audio_transcript`, `audio_diarization`, `whatsapp_chat`, `telegram_chat`, `instagram_chat` — `app/contracts/evidence.py`) and matching `evidence_lifecycle/routing.py` entries, following Phase 2.3's exact precedent (a new source type per processor, never a client-supplied "which parser" hint — consistent with the already-tested `STRUCTURED-ROUTING-002` invariant that a client never chooses its own processor).
- [x] `migrations/versions/ed593db47d8c_communication_source_type_routing.py` — widens the same `evidence_records`/`worker_jobs` `source_type` `CHECK` constraints Phase 2.3's migration already widened once.
- [x] `tests/unit/evidence_lifecycle/test_communication_routing.py` (11 tests: valid routing for all 5 new types, cross-MIME rejection, idempotent replay, client-cannot-override, cross-case isolation), `tests/contract/test_evidence.py` (+5 parametrized round-trip tests).
- [x] QA entry `COMM-ROUTING-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/{evidence-lifecycle,phase-2-decisions,contracts}.md`, `docs/qa/{test-data,known-limitations}.md` updated additively.
- [x] Second, deeper gap found and fixed only by real end-to-end verification: `communication_processing/worker.py`'s own `_validate_source_type` independently re-checked source type by the old group-based scheme (`AUDIO` for all audio profiles, `CHAT` for all chat profiles) and rejected all five new, disjoint source types with `unsupported_source_type`, even after the routing-table fix above was correct. Fixed with an exact per-profile `_PROFILE_REQUIRED_SOURCE_TYPES` mapping; 11 pre-existing tests in that module that encoded the old assumption were corrected. Full write-up in `docs/architecture/phase-2-decisions.md`'s "Second gap found only by real end-to-end verification" section.
- [x] End-to-end verification (upload → server-selected processor → real `communication_processing` worker `--once` claim/input/result → persisted `ObservationV1` → real graph projection via `graph.worker --once`) run live against the full Docker Compose stack on 2026-09-11 for all five new source types (`audio_transcript`, `audio_diarization`, `whatsapp_chat`, `telegram_chat`, `instagram_chat`), each confirmed projected via both a direct Neo4j query and the `GET /api/v1/cases/{case_id}/graph/observations` endpoint. All test data (case, user, worker credential, evidence, jobs, observations, results, Neo4j nodes) cleaned up afterward. Full test-suite re-run after the fix: 1226 passed; `ruff format --check`, `ruff check`, and `mypy app` all clean. See `docs/qa/test-results.md` for the dated entry.

### Outstanding for team review

- None — routing and worker-level source-type validation are both now real-upload-reachable and verified live end to end for all seven `communication_processing` processor profiles.

## Origin/main integration (this branch)

`shreshtha`'s local branch tip was fast-forwarded to `origin/main` (`0bf0ff2`, "Completed sarthak/phase-2") mid-task, at the requester's explicit direction, so the routing fix above could be verified end to end against the real `communication_processing` worker rather than routing-only. `shreshtha`'s prior tip (`05897b0`) was itself an ancestor of `origin/main`, so this was a clean fast-forward at the commit level (`git merge origin/main`, no new merge commit created — the branch pointer simply advanced to the existing `0bf0ff2` commit). Local uncommitted work was `git stash`ed first, the fast-forward applied to a clean tree, then the stash was popped back on top; four documentation files (`README.md`, `phase-2-decisions.md`, `mvp-progress.md`, `known-limitations.md`) had overlapping edits and required manual conflict resolution (content from both sides preserved, stale cross-references updated) — no application code required manual merging. No new commit was authored, nothing was pushed, and no branch switch occurred — this only moved `shreshtha`'s own local tip forward to an already-existing commit.

## Phase 2 — Gaurav Media-Processing Worker Foundation: Complete

Verification complete as of 2026-09-12 (see `docs/qa/test-results.md` for full command output and live results). Builds a one-shot worker CLI on top of Gaurav's Phase 1 `process_job` pure function, following the exact orchestration pattern Jasraj's/Sarthak's Phase 2 workers established — see `docs/architecture/media-processing-worker.md`.

### Delivered

- [x] `app/modules/media_processing/{client,input_resolver}.py` (new) — `WorkerApiClient`, `ResolvedMediaInput`, `StaticInputResolver`/`LiveInputResolver`, mirroring `communication_processing`'s identical modules exactly.
- [x] `run_once`/`main`/`RunOnceOutcome`/`SUPPORTED_PROCESSORS`/`_shim_evidence_record` added to `worker.py` — real claim → claim-token-bound input stream → SHA-256 verification (before any decode) → `process_job` (unchanged) → result submission. `uv run python -m app.modules.media_processing.worker --once`.
- [x] `errors.py` extended with `WorkerOrchestrationError`/`WorkerAuthenticationError`/`InputResolutionUnavailableError`/`WorkerApiError`, mirroring `communication_processing.errors` exactly.
- [x] `_shim_evidence_record` reconstructs the minimal `EvidenceRecordV1` `process_job` needs from what a live run genuinely knows, with clearly-commented unused placeholders for the rest — the exact same pattern `structured_processing.worker._shim_evidence_record` already established, deliberately reused rather than inventing a new metadata type (an earlier draft of this work did introduce one, then was reverted for consistency — see `docs/architecture/phase-2-decisions.md`).
- [x] Second real gap found only by live wiring, fixed: `MediaKind.VIDEO_MATROSKA` added to `source.py` — `evidence_lifecycle/routing.py` had accepted `video/x-matroska` since Phase 1, but `media_processing` itself had no matching classification, so a real `.mkv` upload would have failed inside the worker despite passing routing.
- [x] `SUPPORTED_PROCESSORS` deliberately claims only `media_metadata_v1` live — `media_detection_v1` needs a real detector this phase does not add (none approved/available), and no real upload is ever routed to it anyway; documented, not silently omitted.
- [x] `tests/unit/evidence_lifecycle/test_media_routing.py` (new, 12 tests) — image/video routing including matroska, cross-MIME rejection, client-cannot-override-processor, idempotent replay, cross-case isolation.
- [x] `tests/unit/media_processing/{test_media_worker_client,test_media_worker_orchestration}.py` (new, 22 tests) — wire-format proof against a fake transport, `run_once` sequencing, SHA-mismatch-before-decode, input-resolution-gap deferral, no-secret-leakage.
- [x] `tests/integration/media_processing/test_media_worker_live.py` (new) — self-skipping live pipeline test proving the complete real path (upload → claim → SHA-verified stream → `process_job` → result → durable graph-projection job → real Neo4j projection, twice, confirming no duplication → real HTTP graph-read confirmation → no leaked `object_uri`/credential) for both image and video.
- [x] QA entries `MEDIA-ROUTING-001`, `MEDIA-WORKER-CLIENT-001`, `MEDIA-WORKER-ORCHESTRATION-001`, `MEDIA-GRAPH-LIVE-001` added to `docs/qa/test-matrix.md`; `MEDIA-CLASSIFY-001` updated for matroska.
- [x] `docs/architecture/media-processing-worker.md` (new); `docs/architecture/{media-processing-v1,contracts,phase-2-decisions,graph-projection,evidence-lifecycle}.md`, `docs/qa/{test-data,known-limitations}.md`, `docs/runbooks/local-development.md`, `README.md` updated additively.
- [x] Full repository regression: `uv run pytest -q` → 1272 passed (0 failed, 0 skipped-unexpectedly); `ruff format --check`/`ruff check`/`mypy app` all clean; `docker compose config` valid.
- [x] Real Docker-backed end-to-end verification: `docker compose up --build -d`, `/healthz`/`/readyz`/`/api/v1/meta/contracts` all healthy, a real image upload and a real synthetic-video upload each processed by a genuine `subprocess`-invoked `uv run python -m app.modules.media_processing.worker --once` (not only a mocked or in-process test wrapper), projected into Neo4j by a genuine `subprocess`-invoked `uv run python -m app.modules.graph.worker --once` (run twice, confirming no duplicate node/relationship), confirmed via both a direct Neo4j query and the real HTTP graph-read endpoint. All test data cleaned up afterward.

### Outstanding for team review

- [x] ~~No real object-detection/tracking/OCR model exists~~ — **resolved in the Phase 2 closeout below.**
- [x] ~~`media_detection_v1` is fully implemented and unit-tested but never claimed live~~ — **resolved in the Phase 2 closeout below.**
- [ ] No object-storage-backed `SourceResolver` for a human-facing evidence-download path (same situation `structured_processing`/`communication_processing` are in) — the worker fetches bytes through the existing claim-token-bound input endpoint, never MinIO directly, by design.

## Phase 2 Closeout — Nipun Real Local Media Inference and Continuous Worker/Projector Operation: Complete

Verification complete as of 2026-09-12 (see `docs/qa/test-results.md` for full command output and live results). Closes the two remaining Phase 2 prototype gaps: real local image/video detection/OCR/tracking (replacing the fake-only analysis components), and continuous, graceful `--loop` operation with lease renewal for both the media worker and the graph projector. See `docs/architecture/media-processing-worker.md`, `docs/architecture/graph-projection.md`, and `docs/architecture/phase-2-decisions.md`'s "Real Local Media Inference and Continuous Worker/Projector Operation" section.

### Delivered

- [x] `analysis/onnx_detector.py` — real YOLOX-s object detector via `onnxruntime`; checksum-verified before load; CPU by default, CUDA auto-detected when genuinely available (`onnxruntime-gpu`); deterministic; emits real COCO class labels, never a face/identity.
- [x] `analysis/tesseract_ocr.py` — real local OCR via the `tesseract` binary (`pytesseract`); runtime/language-pack availability checked at construction; a new `recognize_regions` entry point OCRs the whole frame directly, independent of the general object detector's own (COCO-only, no `text_region` class) output.
- [x] `analysis/iou_tracker.py` — the same deterministic greedy class-aware IoU algorithm `fake_tracker.py` always used, promoted to a named, versioned, configurable production component (`fake_tracker.py` itself is unchanged, kept for existing CI tests).
- [x] `bootstrap_models.py` — explicit, operator-invoked, checksum-verified model-asset download; never run automatically; never bundled in Git.
- [x] `benchmark.py` — reproducible local image/video processing benchmark; reports only measured results, never a fabricated/extrapolated throughput claim.
- [x] `worker.py`: `_build_analysis_components` (graceful degradation to metadata-only on a missing model/OCR runtime, never a crash; `--require-analysis` for a hard-fail alternative), `_effective_processors`, whole-frame OCR wiring, `_lease_heartbeat`, `run_loop`/`RunLoopSummary`, `--loop` CLI mode.
- [x] `evidence_lifecycle/routing.py`: `SourceType.IMAGE`/`SourceType.VIDEO` re-routed from `media_metadata_v1` to `media_detection_v1` — inspection proved the required real-detection pipeline was otherwise unreachable via any real upload; strictly additive (the metadata observation is still always emitted first).
- [x] New internal endpoint `POST /api/v1/internal/worker-jobs/{job_id}/renew` (`evidence_lifecycle` `repository.renew_lease`/`service.renew_claim`/`schemas.RenewLeaseResponse`) — same claim-token + worker-identity authorization as `/result`/`/input`; a lease can never be renewed past its own expiry.
- [x] `graph/outbox_repository.renew_lease`; `graph/projector.run_batch`'s `renew_interval_seconds`/injectable `monotonic`; `graph/worker.py`'s `run_loop`/`RunLoopSummary`/`--loop` (asyncio-native SIGINT/SIGTERM handling).
- [x] Dockerfile: `ffmpeg`/`tesseract-ocr` system packages. `compose.yaml`: `media-worker`/`graph-projector`/`media-model-bootstrap` services under an opt-in `workers` profile, a shared `x-tracex-app-env` YAML anchor, a new `api` healthcheck, a `media-models-data` named volume. `.gitignore`: `models/`.
- [x] New dependencies: `onnxruntime` (CPU; `onnxruntime-gpu` documented as the CUDA drop-in), `pytesseract` — no `ultralytics`/`torch`/cloud AI API.
- [x] 15 new/extended test files (`test_onnx_detector.py`, `test_tesseract_ocr.py`, `test_iou_tracker.py`, `test_media_worker_loop.py`, `test_worker_lease_renewal.py`, `test_worker_loop.py` (graph), extended `test_media_worker.py`/`test_media_worker_orchestration.py`/`test_media_worker_client.py`/`test_projector.py`/`test_media_routing.py`/`test_media_worker_live.py`) — all required scenarios from the task brief covered; failure-mode/mechanics tests always run, real-model/real-OCR-quality scenarios self-skip without a locally bootstrapped model/suitable font (never downloaded by the test suite itself).
- [x] QA entries `MEDIA-DETECTOR-001`, `MEDIA-OCR-001`, `MEDIA-TRACKER-001`, `MEDIA-WHOLE-FRAME-OCR-001`, `MEDIA-WORKER-LOOP-001`, `MEDIA-LEASE-RENEWAL-001`, `GRAPH-WORKER-LOOP-001`, `GRAPH-LEASE-RENEWAL-001` added; `MEDIA-ROUTING-001`/`MEDIA-WORKER-ORCHESTRATION-001`/`MEDIA-GRAPH-LIVE-001` updated for the `media_detection_v1` re-route.
- [x] `docs/architecture/{media-processing-worker,contracts,phase-2-decisions,graph-projection}.md`, `docs/qa/{test-matrix,test-data,known-limitations}.md`, `docs/runbooks/{local-development,media-development}.md`, `README.md`, `.env.example` updated additively.
- [x] Full repository regression and live Docker verification — see "Commands run and results" in the session's final report; real bootstrap → real detector run on a real photographic test image (correct "person" detections) → real OCR run on rendered text → real `--once` CLI subprocess claim→stream→parse→submit against the live stack, confirmed via direct PostgreSQL query.

### Outstanding for team review

- [ ] The real detector is a general 80-class COCO model, not fine-tuned for investigative-evidence-specific classes (weapons, specific vehicle types, license plates as a dedicated class) — see `docs/architecture/phase-2-decisions.md`'s open questions.
- [ ] `onnxruntime-gpu`/real CUDA execution has not been verified against actual GPU hardware in this development environment (CPU-only sandbox) — documented (`pyproject.toml`'s `gpu` extra), not hardware-tested.
- [ ] `structured_processing.worker`/`communication_processing.worker` still have `--once` only — no `--loop` mode was added to either in this phase (out of scope for this task; the underlying atomic-claim/durable-dispatch mechanisms they already build on are unchanged and would support the identical pattern).
- [ ] `media-worker`/`graph-projector`'s Compose services are opt-in (`workers` profile) rather than default-on — a deliberate choice so local development's default `docker compose up` stays exactly as fast and side-effect-free as before this phase; revisit once a real deployment target is chosen.

## Phase 3 — Nipun Canonical Observation Ingestion, Batch Persistence, Progress, and Transformation Provenance: Complete

Verification complete as of 2026-09-12 (see `docs/qa/test-results.md` for full command output and live results). Gives Jasraj's real document/OCR/CDR/finance workers one stable, tested, secure path to submit partial `ObservationV1` micro-batches while processing a large source, alongside the pre-existing Phase 2.1 terminal `WorkerResultV1` path. See `docs/architecture/phase-3-decisions.md`, `docs/architecture/evidence-lifecycle.md`'s "Observation-batch ingestion" section, and `docs/architecture/graph-projection.md`.

### Delivered

- [x] `app/contracts/observation_batch.py` — `ObservationBatchSubmissionV1`, `TransformationProvenanceV1` (+ `TransformationStatus`), `ObservationBatchProgressV1`, `ObservationBatchReceiptV1` (+ `BatchAcceptanceStatus`); registered in `CONTRACT_VERSIONS`.
- [x] Migration `c1c9c1c9d9f1_observation_batch_ingestion` — new `observation_batches`/`observation_transformations`/`worker_progress_events` tables; `worker_observations.result_id` made nullable, new nullable `observation_batch_id` FK, and a `CHECK` constraint enforcing exactly one of the two is ever set (the "equivalent durable linkage" chosen over a second observations table).
- [x] `EvidenceLifecycleRepository.submit_observation_batch` (+ batch/transformation/progress lookup methods) — one atomic transaction: batch receipt, observations, transformations, at most one progress event, and one `graph_projection_jobs` row per newly accepted observation.
- [x] `EvidenceLifecycleService.submit_observation_batch` (+ `get_job_progress_summary`) — claim-token/worker-identity verification identical to `submit_result`'s; existing-batch replay/conflict lookup before the running/lease check; progress non-regression validation; `IntegrityError` disambiguation into replay/conflict, mirroring `submit_result`'s established pattern.
- [x] `POST /api/v1/internal/worker-jobs/{job_id}/observations` — same worker-identity/claim-token/case/evidence authorization boundary as `/result`/`/renew`; safe structured receipt; never leaks an object URI/credential/raw content/SQL/stack trace.
- [x] `JobView.latest_progress` on the existing `GET /api/v1/cases/{case_id}/jobs/{job_id}` — no new read endpoint.
- [x] `app/modules/graph/` required **zero code changes** — batch-sourced observations reach the exact same outbox/projector path unmodified.
- [x] 28 required test scenarios covered across 5 new/extended test files (`tests/contract/test_observation_batch.py`, `tests/unit/evidence_lifecycle/test_observation_batch_submission.py`, `tests/unit/evidence_lifecycle/test_observation_batch_api.py`, extended `tests/unit/evidence_lifecycle/test_evidence_api.py`, `tests/integration/evidence_lifecycle/test_observation_batch_live.py`) plus extended test fixtures (`fake_repository.py`, `factories.py`).
- [x] QA entries `OBSBATCH-CONTRACT-001`, `OBSBATCH-SERVICE-001`, `OBSBATCH-API-001`, `OBSBATCH-PROGRESS-001`, `OBSBATCH-LIVE-001`, `OBSBATCH-MIGRATION-001` added.
- [x] `docs/architecture/{contracts,evidence-lifecycle,graph-projection,phase-3-decisions}.md`, `docs/qa/{test-matrix,test-results,test-data,known-limitations}.md`, `docs/runbooks/local-development.md`, `README.md` updated additively.
- [x] Full repository regression and live Docker/PostgreSQL/Neo4j verification — see "Commands run and results" in the session's final report: real migration apply, real document-style two-batch submission through the live API, exactly-once durable graph-outbox handoff, real projector consumption confirmed via direct Neo4j query and the HTTP graph API, idempotent replay, and a partial-batch-then-final-result lifecycle.

### Outstanding for team review

- [ ] The batch-submission route's actual path (`/api/v1/internal/worker-jobs/{job_id}/observations`) differs from the master plan's illustrative example (`/internal/jobs/{job_id}/observations`) — a deliberate coherence choice, see `docs/architecture/phase-3-decisions.md`.
- [ ] The progress non-regression check is a soft, read-then-decide guard, not a hard database constraint (acceptable given real workers submit batches for one job sequentially) — see `docs/architecture/phase-3-decisions.md`.
- [ ] No public read endpoint exists yet for transformation-provenance records beyond repository-level queries — explicitly allowed by the task brief, flagged for a future analyst-review API.

## Phase 3 — Aditya Secure Worker Submission, Case-Scoped Claims, Lease/Heartbeat Control, Retry Limits, and Audit Events: Complete

Verification complete as of 2026-09-12 (see `docs/qa/test-results.md` for full command output and live results). Started on a stale `aditya` branch (6 commits behind cached `origin/main`, missing Nipun's Phase 3 entirely) — reported per this task's own instruction, resolved by the requester manually updating the branch before any code was written. Closes the three genuine gaps inspection found in an otherwise already-mature worker-security boundary (most of the required security model — credential auth, processor scoping, claim-token/case/evidence binding, lease expiry, denial auditing — was already correct from Phases 2.1/2.2/2.4): no retry-attempt ceiling, no absolute lease-renewal ceiling, and no audit trail for any worker action that *succeeds*. See `docs/architecture/phase-3-decisions.md`'s Aditya section.

### Delivered

- [x] `worker_jobs.max_attempts` (migration `d3f1a6c9b8e2_worker_job_retry_limits`, default `5`, configurable via `WORKER_JOB_MAX_ATTEMPTS`) — mirrors `graph_projection_jobs.max_attempts`'s existing, already-reviewed pattern exactly, per this task's own "no duplicate retry systems" instruction. `claim_job`'s eligibility query now bounds reclaiming; a job whose lease keeps expiring is swept to a durable terminal `failed` state (reusing `submit_result`, a safe `retry_exhausted` error code) once its budget is exhausted, rather than remaining reclaimable forever.
- [x] Absolute lease-renewal ceiling (`WORKER_LEASE_MAX_SECONDS`, default `3600`) — `renew_lease` computes `LEAST(candidate_expiry, claimed_at + max_lease_seconds)` inside one atomic `UPDATE ... RETURNING`, so no race window exists between checking and capping. Self-enforcing: once the ceiling is reached, the capped lease simply falls behind `now` and the pre-existing lease-expiry check does the rejecting — no separate rejection branch needed.
- [x] Success-path audit events: `worker_job_claimed`, `worker_job_reclaimed`, `worker_job_lease_renewed`, `worker_job_completed`, `worker_job_failed`, `worker_job_retry_exhausted` now recorded alongside the pre-existing denial events (`worker_authentication_denied`, `worker_processor_scope_denied`, `worker_job_access_denied`) — every field safe (never a claim/bearer token, object URI, or raw exception). `record_audit_event_safely`'s documented scope broadened from "denial paths only" to also cover an already-committed state change whose value to the caller is a one-time secret (a claim token, an extended lease) rather than an idempotently-retryable result; `worker_job_completed`/`worker_job_failed` use the plain, propagating `record_audit_event` instead, since a terminal result is safely retryable by the caller if the audit write itself fails.
- [x] `EvidenceLifecycleService`: `ClaimOutcome` gained `was_reclaim`/`retry_exhausted`; new `RenewOutcome` dataclass (`lease_expires_at`/`case_id`/`evidence_id`/`attempt`) replaces a bare `datetime` return from `renew_claim`, giving the route enough safe context to audit without a second lookup; new `_sweep_retry_exhausted_jobs` called at the start of every `claim_job`.
- [x] A real application bug caught during development (never shipped): the first `max_attempts` eligibility filter applied uniformly across both branches of `claim_job`'s `OR`, incorrectly blocking a job's very first claim whenever `max_attempts == 1` (`attempt` starts at `1`, never `0`). Fixed by nesting the check inside the reclaim branch only — see `docs/architecture/phase-3-decisions.md`.
- [x] A real deployment gap caught during Docker verification (not a code bug): `WORKER_LEASE_MAX_SECONDS`/`WORKER_JOB_MAX_ATTEMPTS` were never passed through to the `api` container's environment in `compose.yaml` — silently unconfigurable in a real Dockerized deployment. Fixed additively.
- [x] All 33 required test scenarios covered: `tests/unit/evidence_lifecycle/{test_worker_claim,test_worker_lease_renewal,test_worker_identity_api,test_worker_internal_api,test_worker_result,test_observation_batch_submission}.py` extended; `tests/integration/evidence_lifecycle/test_worker_retry_and_lease_live.py` (new, 4 tests against real PostgreSQL: absolute lease ceiling, reclaim invalidates the old claim token, real retry exhaustion, a genuinely concurrent `asyncio.gather` terminal-result race proving exactly-one-winner semantics).
- [x] QA entries `WORKER-RETRY-LIMIT-001`, `WORKER-LEASE-CEILING-001`, `WORKER-SUCCESS-AUDIT-001`, `WORKER-RETRY-LEASE-LIVE-001` added to `docs/qa/test-matrix.md`; `WORKER-CLAIM-001`/`MEDIA-LEASE-RENEWAL-001` extended.
- [x] `docs/architecture/{worker-job-lifecycle,worker-identity-and-security,evidence-lifecycle,contracts,phase-3-decisions}.md`, `docs/qa/{test-matrix,test-results,test-data,known-limitations}.md`, `docs/runbooks/local-development.md`, `README.md`, `.env.example` updated additively.
- [x] Full repository regression (`uv run pytest -q` → 1432 passed) and a full Docker rebuild + a 25-point manual live-HTTP verification round (temporary short lease/attempt values, restored afterward) covering every item in the task's required-verification checklist — see `docs/qa/test-results.md`'s dated entry for the complete checklist output.

### Outstanding for team review

- [ ] No worker-credential expiry — deliberate; the task brief's own wording made this conditional on expiry already existing in the design, and it does not. See `docs/architecture/phase-3-decisions.md`'s open questions.
- [ ] No cryptographic tamper-evidence (Merkle chain/signature) over the audit trail yet — explicitly Phase 6 scope per `CLAUDE.md`; this phase's audit events remain ordinary, unsigned PostgreSQL rows.
- [ ] No maximum-attempt cutoff or fleet-size limit on worker credentials themselves (distinct from the new per-job `max_attempts`) — unchanged from Phase 2.4.

## Phase 3 — Jasraj Document, FIR, CDR, and Financial Evidence Processing Workers: Complete

Verification complete as of 2026-09-12 (see `docs/qa/test-results.md` for full command output and live results). Builds real, local document OCR/NER/relation extraction and real, vectorized CDR/finance batch processing on top of Nipun's observation-batch-ingestion contract and Aditya's worker-security boundary (both above, unchanged). No routing change was needed — every format this phase processes (PDF/DOCX/TXT for documents; CSV/XLSX/JSON for CDR/finance) was already reachable through a real upload since Phase 1/2.

### Delivered

- [x] `document/page_trust.py` — four-way per-page PDF trust classification (`TEXT_TRUSTED`/`SCANNED_NO_TEXT`/`UNTRUSTWORTHY_TEXT_LAYER`/`CORRUPT`), replacing the prior binary "has embedded text or not" check; a `CORRUPT` page is reported safely without aborting the rest of the document (a real, previously-untested gap closed via a monkeypatched-`pypdf` test).
- [x] `document/ocr.py` + `pdf.py`'s new `render_pdf_pages` — real local OCR fallback for scanned/untrustworthy pages: `pypdfium2` rendering (a self-contained renderer, no `poppler-utils` system package needed) at a configurable DPI, the same real `tesseract`/`pytesseract` engine `media_processing` already established, normalized `[0,1]` line-level bounding boxes. Verified with real, rendered-text scanned-PDF fixtures (`tests/fixtures/structured_processing/builders.py`'s new `build_scanned_pdf_page`/`build_mixed_pdf`) — real Tesseract genuinely recovers the rendered text.
- [x] `document/normalization.py` — deterministic per-character offset-map text normalization (NFC, line-wrap dehyphenation, whitespace collapsing) so regex/NER matching survives incidental formatting noise while every mention's span still resolves back to the exact original source location (and, for OCR'd text, the exact bounding box).
- [x] `document/ner.py`/`ner_fallback.py`/`ner_spacy.py` + `bootstrap_ner_model.py` — a `NerAdapter` protocol; a deterministic, no-ML gazetteer/heuristic fallback (always available, what every unit test runs against); a real local spaCy (`en_core_web_sm` 3.8.0) adapter loaded only from an operator-bootstrapped model directory, never downloaded automatically at worker runtime. `worker.py` degrades gracefully (real model → deterministic fallback) exactly like `media_processing`'s own missing-model posture, never crashing a job.
- [x] `document/relations.py` — four named, versioned, deterministic rule-based relation/event functions (`person_contact_proximity_v1`, `dated_communication_reference_v1`, `transaction_claim_v1`, `incident_event_v1`), each citing an exact combined source span, confidence `0.60` (an inference from proximity, never an asserted fact).
- [x] `structured/chunked_processing.py` — schema assessment before any row is read (`ambiguous_schema` on a header that can't resolve required fields); vectorized, bounded-memory chunked reading (Polars-batched CSV, PyArrow-batched-over-`openpyxl` XLSX, bounded-then-chunked JSON); row-level malformed-data tolerance (a bad row is reported safely, never aborts its chunk).
- [x] `structured/cdr.py`/`finance.py` extended: phone numbers normalized to E.164 (India-only context, original value always kept alongside via new `*_raw` attributes — two pre-existing test assertions updated as a deliberate, documented consequence); a shared, documented CDR/finance timezone policy (explicit per-record `source_timezone` else `Settings.structured_default_timezone`, default `Asia/Kolkata`; canonical `timestamp` always UTC, source zone/offset recorded in provenance); finance gained `direction` (debit/credit) normalization and an informational (never rejecting) `currency_is_known_iso4217` flag.
- [x] `batching.py` — deterministic `batch_id`/`idempotency_key` (pure functions of `job_id`/`batch_sequence`, so a genuine retry always reuses the same tokens) and shared builders for every `ObservationBatchSubmissionV1`/`TransformationProvenanceV1`/`ObservationBatchProgressV1` this phase submits.
- [x] `client.py`'s new `submit_batch`/`renew_lease` methods — thin wrappers over Nipun's `/observations` and Aditya's `/renew` endpoints, identical wire-level conventions (`X-Claim-Token`, safe error handling, no token ever logged) to the pre-existing `submit_result`/`fetch_input`.
- [x] `worker.py`'s new `run_document_job_with_batches`/`run_structured_batches_job` + `_dispatch_job` — `fir_report_text_v1`/`cdr_generic_v1`/`financial_transaction_generic_v1` jobs now submit real observations via one or more `/observations` micro-batches (one per PDF page/DOCX-TXT segment; one per CDR/finance chunk) with real transformation provenance and progress, then exactly one terminal result with `observations=[]`. `process_job` itself (and `generic_tabular_v1`/`generic_json_v1`, Nipun's fallback profiles) is completely unchanged and still used directly by every existing test and caller.
- [x] Five new dependencies (`spacy`, `pypdfium2`, `polars`, `pyarrow`) — no cloud OCR/NER API, no LLM extraction.
- [x] 34 required test scenarios covered across 9 new/extended test files (`test_normalization`, `test_page_trust`-equivalent coverage in `test_pdf`, `test_ner`, `test_relations`, `test_chunked_processing`, `test_ocr_field_match_precision`, `test_worker_document_batches`, `test_worker_structured_batches`, extended `test_worker_client`/`test_worker_orchestration`/`test_cdr`/`test_finance`), plus a live-pipeline extension (`test_worker_live.py`'s `_PIPELINE_CASES` gained `cdr`/`financial` entries).
- [x] QA entries `DOC-PAGE-TRUST-001`, `DOC-OCR-001`, `DOC-OCR-PRECISION-001`, `DOC-NORMALIZATION-001`, `DOC-NER-001`, `DOC-RELATIONS-001`, `CDR-CHUNKED-001`, `FINANCE-CHUNKED-001`, `WORKER-DOC-BATCH-001`, `WORKER-STRUCTURED-BATCH-001`, `WORKER-CLIENT-BATCH-001`, `WORKER-LIVE-BATCH-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/document-structured-processing.md` (new, the authoritative Phase 3 reference); `docs/architecture/{structured-processing-worker,evidence-lifecycle,contracts,graph-projection,phase-3-decisions}.md`, `docs/qa/{test-matrix,test-results,test-data,known-limitations}.md`, `docs/runbooks/local-development.md`, `README.md`, `.env.example` updated additively.
- [x] Full repository regression (`uv run pytest -q` → see "Commands run and results" in the session's final report) and real, live Docker-backed verification — see `docs/qa/test-results.md` for the actual measured OCR/NER/field-match results and the full live-pipeline verification.

### Outstanding for team review

- [ ] `fir_report.py`'s `police_station_mention` regex can over-match in free-flowing prose without line breaks — a pre-existing Phase 1 pattern, now more visible through real OCR/prose fixtures; not fixed in this phase (outside this phase's ownership of that specific regex).
- [ ] The deterministic NER fallback's gazetteer and the real `en_core_web_sm` model are both narrow/general-purpose respectively — neither is fine-tuned for Indian names/places/investigative vocabulary. See `docs/qa/known-limitations.md`.
- [ ] `SourceType.DOCUMENT` still has no route to a structured-JSON report export shape — no such shape is defined anywhere in this codebase; not added since no routing change was actually needed for anything this phase processes.
- [ ] `document/relations.py`'s proximity threshold and `structured_batch_size`'s default are documented constants, not empirically tuned against a real large-scale corpus/export.

## Phase 3 — Sarthak Audio, Social/Chat, and Multilingual Communication Evidence Pipelines: Complete

Builds micro-batch submission, a chat-timezone default policy, mentioned-identifier extraction, sender-transliteration wiring, and a typed ASR/diarization adapter boundary on top of Nipun's observation-batch-ingestion contract, Aditya's worker-security boundary, and this module's own Phase 1/2 foundation (all above, unchanged). No routing change was needed — all seven processor profiles were already reachable through a real upload since the Phase 2 routing fix.

### Delivered

- [x] `batching.py` — deterministic `batch_id`/`idempotency_key` (pure functions of `job_id`/`batch_sequence`) and shared builders for every `ObservationBatchSubmissionV1`/`TransformationProvenanceV1`/`ObservationBatchProgressV1` this phase submits, mirroring Jasraj's `structured_processing.batching` exactly.
- [x] `client.py`'s new `submit_batch`/`renew_lease` methods — thin wrappers over Nipun's `/observations` and Aditya's `/renew` endpoints, identical wire-level conventions to the pre-existing `submit_result`/`fetch_input`.
- [x] `worker.py`'s new `run_communication_job_with_batches` + `_observations_for` — every one of the seven profiles now submits observations via one or more `/observations` micro-batches (bounded by `Settings.communication_batch_size`, default 200) with real transformation provenance and progress, then exactly one terminal result with `observations=[]`. `_dispatch` (all extraction logic) is reused completely unchanged — this module's seven profiles already converged on one uniform `list[RawMention]` shape before this phase, so no per-profile-family orchestration split was needed (unlike Jasraj's document/CDR/finance split). `process_job` itself is completely unchanged and still directly callable.
- [x] Chat timezone default policy (`Settings.communication_default_timezone`, default `Asia/Kolkata`, mirroring `structured_default_timezone`'s identical precedent): a naive chat timestamp with no explicit offset/`Z`/epoch signal (WhatsApp always; Telegram when `date_unixtime` is absent; generic JSON's naive ISO strings) now resolves to UTC via the configured default instead of staying unresolved forever, with `timestamp_source_timezone` recording which zone was actually used.
- [x] `social/identifiers.py` (new) — deterministic regex-based extraction of `phone_number`/`email_address`/`url`/`username_or_handle` from chat message text, mirroring `structured_processing.document.fir_report`'s regex-only approach; each extracted mention reuses its parent message's exact locator, with `worker._observations_for`'s new positional discriminator disambiguating any resulting `(observation_type, locator)` collision into distinct, stable `observation_id`s.
- [x] `social/common.py`'s `chat_message_to_mention` now attaches `sender_transliteration_candidates` (one candidate per word in the sender name, via the pre-existing, unmodified `aliases/transliteration.py`) — wiring an algorithm that has existed and been fully tested since Phase 1 but was never previously called against a real observation attribute.
- [x] `audio/asr_adapter.py`/`audio/diarization_adapter.py` (new) — `AsrAdapter`/`DiarizationAdapter` `Protocol`s with exactly one production implementation each (`UnavailableAsrAdapter`/`UnavailableDiarizationAdapter`, always defers, never fabricates), plus explicit, clearly-labeled fixture-only adapters under `tests/fixtures/`. No real local ASR/diarization model was implemented — this module's own `test_module_safety.py` (an earlier-phase, deliberate boundary set by this same owner) statically bans every practical local ASR/diarization toolkit; flagged for team review in `docs/architecture/phase-3-decisions.md`. `diarization_import.py`'s `diarization_speaker_turn` observations now carry a `segment_source` attribute (`"metadata_supplied"` always today).
- [x] 63 new tests (`tests/unit/communication_processing/` grew from 269 to 332 passing) across `test_communication_worker_orchestration.py`, `test_communication_worker_batches.py` (new file — multi-batch/lease-renewal/provenance-scoping/replay coverage), `test_communication_provenance.py` (new file), `test_whatsapp.py`/`test_telegram.py`/`test_json_records.py`/`test_instagram.py` (timezone-default and malformed-entry coverage), `test_social_identifiers.py` (new file), `test_social_common.py` (new file), `test_asr_adapter.py`/`test_diarization_adapter.py` (new files), `test_diarization_import.py` (`segment_source` coverage), `test_aliases_transliteration.py` (mixed-script coverage) — full repository suite remains green throughout. A dedicated background audit against all 34 required scenarios drove this final coverage pass; see `docs/architecture/phase-3-decisions.md` for what the audit found and how each gap was closed or explicitly deferred.
- [x] QA entries `COMM-BATCH-001`, `COMM-IDENTIFIER-001`, `COMM-TRANSLIT-WIRING-001`, `COMM-TIMEZONE-DEFAULT-001`, `COMM-ASR-ADAPTER-001` added to `docs/qa/test-matrix.md`.
- [x] `docs/architecture/communication-processing.md` (new, the authoritative Phase 3 reference); `docs/architecture/phase-3-decisions.md`, `docs/qa/{test-matrix,test-data,known-limitations}.md`, `docs/runbooks/local-development.md`, `.env.example` updated additively. A pre-existing stale line in `docs/runbooks/local-development.md` (claiming only 2 of 7 processors were upload-reachable, contradicted by the already-completed Phase 2 routing fix) was corrected while in the file for an unrelated reason.

### Outstanding for team review

- [ ] Whether reversing `test_module_safety.py`'s ML-import ban for this module specifically, to add a real local ASR/diarization model in a future phase, is desired — and if so, which toolkit. See `docs/architecture/phase-3-decisions.md`.
- [ ] `username_or_handle` extraction has no platform-specific validation (a bare `@name` token is accepted regardless of platform plausibility) — every such extraction is already an unresolved extracted claim, so this is a precision limitation, not a safety gap.
- [ ] `MAX_IDENTIFIER_MATCHES_PER_MESSAGE` (100) and `COMMUNICATION_BATCH_SIZE`'s default (200) are documented constants, not empirically tuned against a real large-scale chat export or long transcript.

## Phase 3 — Gaurav: Shared Raster-Image OCR Bounding-Box Adapter and Fixtures: Complete

- [x] Shared `ImageOcrAdapter` implementing real local raster text extraction using Tesseract, preserving original-coordinate line boxes and returning typed `OcrBoxResult` values.
- [x] Correct EXIF orientation inversion logic handling EXIF rotation codes precisely, mapping the bounding box to the unrotated coordinate space before normalizing into `[0, 1]`.
- [x] Explicit fixture-only adapter for deterministic unit/orchestration tests, plus genuine labelled PNG/JPEG adapter tests using local Tesseract when available.
- [x] `ObservationBatchSubmissionV1` integrated inside `worker.py::run_once`; successful OCR, metadata, detection, and tracking observations are micro-batched and the terminal result is always `observations=[]`.
- [x] `TransformationProvenanceV1` includes exactly the permitted metadata fields (`ocr_engine`, `preprocessing_version`, `frame_time_start_ms`) without exposing forbidden keys.
- [x] Focused real-OCR, geometry, secure-batch, replay, video-frame, lease, and graph-idempotency coverage; live checks self-skip with an explicit unavailable reason rather than using fixture OCR.
- [x] Live video-frame OCR test (`test_video_frame_ocr_batch_submission_end_to_end_live`) added alongside the pre-existing image-path live test, closing the gap where only a real image had a dedicated end-to-end live OCR check.

### Outstanding for team review

- [ ] Neither the image-path nor the new video-path live OCR test (nor any other `tests/integration/` test) was actually run against a live API/PostgreSQL/Neo4j/MinIO stack in the sandbox this was finished in — the local Docker daemon itself was unavailable there (environment issue, not application code). The fixture/adapter logic the video test depends on was independently confirmed correct outside pytest; the live HTTP/DB/graph round trip itself is unverified. See `docs/qa/test-results.md`'s "Phase 3 closeout" entry.

## Later phases (not started)

Owned by other contributors, building on the frozen Phase 1 contracts, the graph foundation, the document/structured-processing foundation, the access-control foundation, the audio/social/alias/communication foundation, and the video/image processing foundation above:

- Real ASR/diarization consuming `deferred_requires_asr`/`deferred_requires_diarization` checkpoints (unrelated to `media_processing`'s or `structured_processing`'s own now-real Tesseract OCR — neither reads spoken audio). A real local object detector/tracker/OCR engine now exists for `media_processing` (Phase 2 closeout, above), and a real local page-OCR/NER/relation-extraction pipeline now exists for `structured_processing` (Phase 3 — Jasraj, above) — a fine-tuned/specialized detector or NER model variant, or real GPU-hardware verification, remain open (see each section's own "Outstanding for team review").
- A MinIO-backed `SourceResolver` for a human-facing authorized evidence-download path (`structured_processing.worker.process_job`/`communication_processing.worker.process_job`/`media_processing.worker.process_job` are all now callable via their own one-shot `--once` CLIs, each fetching evidence bytes through the existing claim-token-bound worker-input endpoint, not a direct MinIO read; `graph.projection`'s `Evidence`/`Observation`/`EntityMention` path is now wired via its own `--once` CLI too — see Phase 2.5 below; `project_entity`/`project_event` remain unwired).
- Entity resolution and merge review workflow; candidate identity links; a review workflow consuming `CommunicationLinkCandidate`s and alias/transliteration candidates.
- Cross-modal correlation, candidate scoring, hypothesis engine.
- Face recognition, person re-identification, biometric identification, and cross-camera `local_track_id` correlation — explicit non-goals for `media_processing` in every phase, not just this one (see `CLAUDE.md`).
- Graph analytics (centrality, community detection, motifs).
- Case CRUD API (evidence-lifecycle API itself is now delivered above, built against `access_control.dependencies.require_case_*`).
- A worker daemon/consumer loop that actually calls `/api/v1/internal/worker-jobs/claim`, runs `structured_processing`/`communication_processing`/`media_processing`'s `process_job`, and submits the result via `/api/v1/internal/worker-jobs/{job_id}/result` (the claim/submit primitives themselves are delivered — see Phase 2.1 above).
- MFA, SSO, external identity provider, production secret management.
- Merkle checkpointing and signatures.
- Frontend (including any future cookie/CSRF/CORS decisions).
# Phase 5 - Nipun graph/correlation integration: Complete

- [x] Additive case-scoped correlation/candidate/feature-snapshot persistence
  plus transactional typed graph-update outbox.
- [x] Typed replay handler seam, deterministic projection key, safe
  case-scoped integration read endpoints, and focused fixture tests.
- [x] Shreshtha semantic correlation projection handler and intelligence
  producers -- registered into a reachable process in the Phase 5A
  reconciliation below (`app/modules/graph/intelligence_worker.py`).
- [ ] Aditya write/review authorization policy (explicit Phase 5 non-goal;
  unrelated to this integration closeout).

## Phase 5 final integration and release gate — Nipun

Closed every open Phase 5A/5B integration item after all contributors
merged: fixed the refresh-rate-limit regression (P5-REGRESSION-AUTH-001);
closed both persisted media-manifest/chunk gaps
(P5-INTEG-VISUAL-001/P5-INTEG-COMMUNICATION-001); enforced producer
validation gates at Phase 5 sourcing for the structured family (the
visual/communication gates already existed); completed the two-party
CDR caller/callee and finance sender/receiver descriptor extension;
measured and froze the transparent rules baseline; and built one
end-to-end acceptance test proving the whole chain against real
HTTP/PostgreSQL/Neo4j boundaries. Also fixed one genuine pre-existing
gap found while completing the release gate: `Dockerfile` never copied
`README.md`/`alembic.ini`/`migrations/` into the runtime image, so
`uv run alembic upgrade head` could never run inside *any* phase's
container -- now fixed with a minimal three-file `COPY` addition.

**All completion gates passed**: static checks (`ruff format --check`,
`ruff check`, `mypy app`), a single coherent Alembic head, the full
1885-test repository suite against real live PostgreSQL (with the
Phase 5A-required `pgvector` extension)/Neo4j/Redis/MinIO, and a
dedicated Docker Compose release-gate run (`tracex-phase5-gate`, fully
port-isolated from the main stack) covering healthy-service readiness,
a real dependency-outage/recovery cycle, a clean in-container migration,
and the full worker-claim -> manifest/chunk -> chunk-scoped publication
-> correlation -> idempotent replay -> authorized/denied case-scoped
read acceptance path. Full record in
`docs/architecture/phase-5-integration.md`'s "Phase 5 final integration"
section; exact commands and results in `docs/qa/test-results.md`'s
dated entry.

## Phase 6 Part 1 — Nipun integrity foundation: Complete

- [x] New additive `app/modules/integrity/` module: `IntegrityEventKind`
  (`evidence_registered`/`observation_published`/`correlation_completed`
  now wired; `review_decision`/`hypothesis_action` forward-compatible
  enum values only), a deterministic domain-separated SHA-256 Merkle
  checkpoint service (frozen leaf/parent hash rules, per-case gap-free
  sequencing, duplicate-last odd-leaf rule), and local Ed25519 signing/
  verification with public key material stored alongside each signature.
- [x] Focused Alembic migration (`8f68fb441037`) adding
  `integrity_events`/`merkle_checkpoints`/`checkpoint_signatures`/
  `integrity_sequence_counters`, with a database-level GiST exclusion
  constraint preventing overlapping checkpoint ranges within a case —
  single coherent head, preserved.
- [x] Producer seams wired additively (optional, default-`None`
  parameters) into evidence registration, observation-batch acceptance,
  and correlation completion — verified backward compatible against the
  full pre-existing 386-test `tests/unit/evidence_lifecycle`/
  `tests/unit/graph` suite, and against a deliberately-unreachable
  integrity store to confirm the safe, non-blocking contract.
- [x] Operator CLI (`app.modules.integrity.cli`) for key generation,
  checkpoint building, verification, and safe export — no new HTTP
  endpoint in this phase; protected API exposure is explicitly left to
  Aditya. See `docs/architecture/phase-6-integrity.md`'s "Verification
  and safe export" section.
- [x] All 18 required proof points covered with synthetic fixtures only;
  see `docs/qa/test-matrix.md`'s "Phase 6 Part 1" section for the full
  ID-to-test mapping.
- [x] `docs/decisions/ADR-013-phase-6-integrity-checkpoints.md` records
  why this is explicitly not a public-blockchain-anchoring claim.
- [ ] Live-infrastructure (real PostgreSQL) verification of this
  module's DB-backed tests was not run in this environment — Docker
  Desktop's daemon was unavailable throughout. Static checks, the full
  migration-graph check, and every pure-logic test ran and passed; see
  `docs/qa/test-results.md`'s dated Phase 6 Part 1 entry for the exact
  commands and reason.
- [ ] Review-decision/hypothesis-action recording (Shreshtha),
  authorization-protected API exposure (Aditya), and modality-specific
  provenance leaf wiring (Jasraj/Gaurav/Sarthak) remain later Phase 6
  work — see `docs/architecture/phase-6-integrity.md`'s "Handoff to
  later branches" section.

# Phase 5 - Shreshtha graph intelligence and rules baseline: Complete

- [x] Deterministic case-scoped exact, alias/transliteration, local-vector, and hot-window retrieval.
- [x] Preliminary transparent scoring, correlation submission adapter, semantic typed-outbox handler, analytics, and motif.
- [x] **Phase 5A reconciliation**: closed the gap between "algorithm implemented" and "reachable against
  real data," found by an explicit audit before any new code was written. Added `intelligence/sourcing.py`
  (real `ObservationV1` -> retrieval/analytics/motif input, documented bounded mapping scope across
  Jasraj's/Sarthak's Phase 3/4 observation types), `intelligence/pipeline.py` (retrieval -> scoring ->
  correlation -> Nipun's `submit()`, exactly once per case), and `app/modules/graph/intelligence_worker.py`
  (the minimal `--replay-once`/`--replay-loop` registration wiring the semantic handler was missing --
  it existed and was tested, but no process ever invoked it outside a test). Fixed one genuine,
  pre-existing bug found along the way: `retrieval.py`'s transliteration-vs-alias check was
  order-dependent (only checked one direction), now symmetric with a permanent regression test. Added
  live pgvector coverage (previously zero) and a full live pipeline test proving exactly-once submission,
  replay idempotency, and provenance-gated projection against real PostgreSQL + Neo4j. 63 new tests; full
  repository suite green (1759+ passed). See `docs/architecture/phase-5-graph-intelligence.md`'s
  dependency-map section and `docs/qa/known-limitations.md` for the corrected record and remaining,
  deliberately-scoped Phase 5A limitations.
- [x] **Phase 5 final integration (Nipun)**: measured and froze the rules baseline
  against a versioned synthetic benchmark (Precision@K = 1.0, Recall@K = 1.0,
  0 false links, correct same-event suppression/contradiction handling/case
  isolation/determinism; one explicit alternative weight configuration
  compared and found not to improve on the baseline). See
  `docs/decisions/ADR-006-phase-5-rules-baseline.md`'s "Measurement and
  freeze" addendum and `tests/unit/graph/test_phase5_rules_benchmark.py`.
- [ ] Operation Nightfall evaluation, real-dataset, and LAN validation remain separate, later release-validation activities.

# Phase 5B - Aditya secure graph/correlation reads: Complete

- [x] Reused the existing case-membership authorization seam with
  `CaseAction.GRAPH_READ`; successful decisions now carry their required action
  and are safely audited alongside non-enumerating denials.
- [x] Protected all currently exposed Phase 5 graph/correlation/candidate/
  hypothesis reads before Neo4j or PostgreSQL access. Durable correlation and
  vector read queries retain their `case_id` predicate as defense in depth;
  internal outbox event lookup is case-scoped too.
- [x] Added focused unauthenticated, cross-case, object-ID probing,
  safe-audit, PostgreSQL/authorization-outage, parameterized Neo4j, and
  pgvector case-predicate coverage.
- [x] **Docker-backed secure-read verification (Nipun, Phase 5 final
  integration)**: `test_phase5_final_acceptance_live.py` proves an
  authorized case-scoped `graph/candidates` read and a denied
  cross-membership read against a real dedicated Docker Compose stack.
  See `docs/qa/test-results.md`'s dated entry.
- [ ] Phase 5 write/review policy, public feature snapshots/analytics/motifs/vector
  routes, and any frontend flow are intentionally not implemented here.

# Phase 5B - Jasraj document, CDR, and financial source-signal validation: Complete

- [x] Correlation-ready CDR and financial records now require both explicit,
  source-local participant roles plus a valid source-backed time; CDR uses
  caller/callee and finance sender/receiver without swapping, inferring, or
  resolving either party.
- [x] Added deterministic role/amount/currency/time/duration/reference checks,
  source-safe validation metadata, canonical event-time/provenance retention,
  and structural OCR page/span/bounding-box/confidence validation.
- [x] Added synthetic CSV/XLSX/JSON and Phase 4-to-Phase 5 motif-boundary tests;
  no candidate generation, scoring, direct graph write, entity merge, or model
  change was introduced.
- [x] **Compose-backed verification (Nipun, Phase 5 final integration)**:
  the full repository suite (1885 tests) and a dedicated Docker Compose
  release-gate run both passed against real live infra, including real
  structured/CDR processing paths. See `docs/qa/test-results.md`'s dated
  entry.
- [ ] Real-local-OCR accuracy and wider real-dataset/LAN validation
  remain separate, later release-validation activities.

# Phase 5B - Gaurav visual-source reliability: Complete

- [x] Added deterministic producer-side visual locator, timeline, normalized
  geometry, and optional chunk-boundary validation with accepted/rejected/
  incomplete quality outcomes.
- [x] Preserved evidence-local track lifecycle conditions and bounded OCR
  provenance without creating identity, candidate, graph-write, or scoring paths.
- [x] **Manifest-aware worker publication (Nipun, Phase 5 final integration)**:
  `media_processing.worker.run_once` now registers a coordinator-persisted
  manifest and publishes every sampled frame's OCR output chunk-scoped
  (`POST .../media-chunks/publish`), replacing the previous plain/unscoped
  submission (P5-INTEG-VISUAL-001). See
  `docs/architecture/phase-5-integration.md`.
- [ ] Real-media/model accuracy and variable-frame-rate validation remain merge-wave work.

# Phase 5B - Sarthak audio/social source-signal validation: Complete

- [x] Added deterministic audio/chat/identifier validation outcomes and safe
  correlation-readiness metadata; raw transcript/message content remains out
  of graph-facing attributes.
- [x] Added supplied audio-chunk bounds checks, evidence-local speaker-label
  semantics, platform-scoped handle normalization, and actual worker gating so
  incomplete chat records emit no retrieval-eligible identifiers.
- [x] **Persisted coordinator manifest/chunk wiring (Nipun, Phase 5 final
  integration)**: `run_communication_job_with_batches` now registers the
  manifest with the coordinator and publishes ASR/diarization mentions
  chunk-scoped, replacing the previous worker-local-only plan
  (P5-INTEG-COMMUNICATION-001). See
  `docs/architecture/phase-5-integration.md`.
- [ ] Real model quality and Compose/full merge-wave validation remain team work.

# Phase 4 - Aditya LAN worker security and reliability: In progress

- [x] Existing worker credential, claim-token, and lease path extended with
  TLS-ready transport gating, bounded metadata controls, and heartbeat renewal.
- [ ] Real multi-machine TLS/LAN, GPU, and dataset validation at merge wave.

# Phase 4 - Shreshtha media graph mapping and temporal semantics: In progress

- [x] Deterministic case-scoped mappings for staged visual, speech, message,
  and explicit meeting-candidate canonical observations through the existing
  durable graph outbox.
- [x] Source-relative/absolute temporal precision policy, bounded provenance,
  and safe manifest/chunk/artifact identifier lineage.
- [ ] Merge-wave real-media, GPU, LAN, Docker/Compose, and dataset validation.

# Phase 4 - Jasraj shared extracted-text utilities: In progress

- [x] Pure typed OCR-fragment cleanup and deterministic unresolved identifier
  candidates with span/locator/lineage provenance.
- [ ] Real OCR, GPU/media, LAN, and dataset validation at merge wave.

# Phase 4 - Gaurav video and image evidence worker: In progress

- [x] Deterministic rapid/deep profile, chunk-plan, adaptive sampling, and
  authenticated staged-media publication seam added over existing local worker.
- [ ] GPU/model-bundle/LAN/real-media validation at merge wave.

# Phase 4 - Sarthak audio and social evidence worker: In progress

- [x] Deterministic rapid/deep profile, local PCM/VAD execution, offline
  command ASR/diarization adapters, and claimed-worker canonical batch
  publication over existing social parsers.
- [ ] Real model-bundle, LAN, Docker, dataset, and merge-wave validation.

# Phase 6 Part 2 - Integrity security boundary: Complete (static/unit gate)

- [x] Case-scoped checkpoint metadata, verification, and safe export routes
  with distinct integrity permissions and safe audit telemetry.
- [x] PostgreSQL append-only triggers for durable integrity events,
  checkpoints, and signatures.
- [x] Bounded, idempotent evidence-event reconciliation command.
- [ ] Live Docker/infrastructure release validation (deferred to Shreshtha Part 5).

# Phase 6 Part 3 - Structured provenance integrity: Complete (static/unit gate)

- [x] Added immutable, case-scoped safe provenance commitments for accepted
  document/legal, OCR, CDR, and finance observations.
- [x] Added exactly one idempotent `structured_observation_provenance` leaf per
  eligible observation without changing generic lifecycle events or graph behavior.
- [x] Added exact-projection reconciliation and append-only migration coverage.
- [ ] Live PostgreSQL/Docker release validation is deferred to Shreshtha Part 5.
