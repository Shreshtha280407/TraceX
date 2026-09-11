# MVP Progress

Team: Nipun (Phase 1 — core foundation), Shreshtha, Aditya, Jasraj, Gaurav, Sarthak (later-phase modules building on the Phase 1 contracts).

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

- [ ] No real object-detection/tracking/OCR model exists — same limitation Phase 1 already documented, unchanged by this phase's orchestration work.
- [ ] `media_detection_v1` is fully implemented and unit-tested but never claimed live — a future phase wiring in a real, local detector needs only a change to how `main()` constructs and injects one, not to `process_job`.
- [ ] No object-storage-backed `SourceResolver` for a human-facing evidence-download path (same situation `structured_processing`/`communication_processing` are in) — the worker fetches bytes through the existing claim-token-bound input endpoint, never MinIO directly, by design.

## Later phases (not started)

Owned by other contributors, building on the frozen Phase 1 contracts, the graph foundation, the document/structured-processing foundation, the access-control foundation, the audio/social/alias/communication foundation, and the video/image processing foundation above:

- Real OCR (Tesseract/cloud/model) consuming `document_requires_ocr` checkpoints; real ASR/diarization consuming `deferred_requires_asr`/`deferred_requires_diarization` checkpoints; real YOLO/ByteTrack/PaddleOCR model adapters behind `media_processing.analysis.interfaces` (the adapter boundary and deterministic test doubles are ready; no real model is invoked anywhere in this repository).
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
