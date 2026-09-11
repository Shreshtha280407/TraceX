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

- [ ] No real object-detection/tracking/OCR model exists — `analysis/interfaces.py`'s protocols are ready for a later phase's YOLO/ByteTrack/PaddleOCR adapter, but nothing in this repo calls a real model yet.
- [ ] No orchestration calls `media_processing.worker.process_job` yet — same "built but not yet wired up" situation `structured_processing`/`communication_processing`/`graph` are in.
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
- [ ] Denied-access-attempt auditing for case-scoped endpoints is not wired (a pre-existing gap in `require_case_action`, not introduced or fixed by this task).
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

- [ ] No real per-worker credential system — `require_worker_principal` is a narrow shared-secret boundary only; see "Worker identity" in `docs/architecture/worker-job-lifecycle.md` for the explicit Aditya handoff this implies.
- [ ] No max-attempt cutoff on reclaiming — a job with a perpetually-expiring lease is reclaimable forever.
- [ ] No lease renewal for a long-running worker.
- [ ] No automatic redrive of `deferred`/`cancelled` jobs.
- [ ] Denied worker actions (bad claim token, scope mismatch, conflict) are not audit-logged — mirrors the same pre-existing gap already noted for case-scoped user endpoints.
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

## Later phases (not started)

Owned by other contributors, building on the frozen Phase 1 contracts, the graph foundation, the document/structured-processing foundation, the access-control foundation, the audio/social/alias/communication foundation, and the video/image processing foundation above:

- Real OCR (Tesseract/cloud/model) consuming `document_requires_ocr` checkpoints; real ASR/diarization consuming `deferred_requires_asr`/`deferred_requires_diarization` checkpoints; real YOLO/ByteTrack/PaddleOCR model adapters behind `media_processing.analysis.interfaces`.
- Worker orchestration invoking `structured_processing.worker.process_job`, `communication_processing.worker.process_job`, `media_processing.worker.process_job`, and `graph.projection` from a real ingestion pipeline; a MinIO-backed `SourceResolver`.
- Entity resolution and merge review workflow; candidate identity links; a review workflow consuming `CommunicationLinkCandidate`s and alias/transliteration candidates.
- Cross-modal correlation, candidate scoring, hypothesis engine.
- Face recognition, person re-identification, biometric identification, and cross-camera `local_track_id` correlation — explicit non-goals for `media_processing` in every phase, not just this one (see `CLAUDE.md`).
- Graph analytics (centrality, community detection, motifs).
- Case CRUD API (evidence-lifecycle API itself is now delivered above, built against `access_control.dependencies.require_case_*`).
- A worker daemon/consumer loop that actually calls `/api/v1/internal/worker-jobs/claim`, runs `structured_processing`/`communication_processing`/`media_processing`'s `process_job`, and submits the result via `/api/v1/internal/worker-jobs/{job_id}/result` (the claim/submit primitives themselves are delivered — see Phase 2.1 above).
- A real per-worker credential-issuance system (Aditya-owned), replacing Phase 2.1's narrow shared-secret `require_worker_principal` stand-in.
- MFA, SSO, external identity provider, production secret management.
- Merkle checkpointing and signatures.
- Frontend (including any future cookie/CSRF/CORS decisions).
