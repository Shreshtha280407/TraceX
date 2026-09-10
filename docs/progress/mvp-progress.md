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

## Later phases (not started)

Owned by other contributors, building on the frozen Phase 1 contracts, the graph foundation, the document/structured-processing foundation, the access-control foundation, the audio/social/alias/communication foundation, and the video/image processing foundation above:

- Real OCR (Tesseract/cloud/model) consuming `document_requires_ocr` checkpoints; real ASR/diarization consuming `deferred_requires_asr`/`deferred_requires_diarization` checkpoints; real YOLO/ByteTrack/PaddleOCR model adapters behind `media_processing.analysis.interfaces`.
- Worker orchestration invoking `structured_processing.worker.process_job`, `communication_processing.worker.process_job`, `media_processing.worker.process_job`, and `graph.projection` from a real ingestion pipeline; a MinIO-backed `SourceResolver`.
- Entity resolution and merge review workflow; candidate identity links; a review workflow consuming `CommunicationLinkCandidate`s and alias/transliteration candidates.
- Cross-modal correlation, candidate scoring, hypothesis engine.
- Face recognition, person re-identification, biometric identification, and cross-camera `local_track_id` correlation — explicit non-goals for `media_processing` in every phase, not just this one (see `CLAUDE.md`).
- Graph analytics (centrality, community detection, motifs).
- Case CRUD and evidence-lifecycle API, built against `access_control.dependencies.require_case_*`.
- MFA, SSO, external identity provider, production secret management.
- Merkle checkpointing and signatures.
- Frontend (including any future cookie/CSRF/CORS decisions).
