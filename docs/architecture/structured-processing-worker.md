# Structured-Processing Worker (Phase 2 — Jasraj)

This phase adds a **one-shot worker CLI** on top of `app/modules/structured_processing/`'s existing (Phase 1) `process_job` pure function: it claims a job through Nipun's internal worker API, resolves that job's evidence, calls `process_job` completely unchanged, and submits the result — establishing the worker pattern Gaurav's and Sarthak's later-phase workers will follow. No daemon, polling loop, Celery, or scheduler exists anywhere in this repository; running the worker means running it once, by hand or by an external scheduler this repo does not provide.

## The full flow

```
uv run python -m app.modules.structured_processing.worker --once
  -> POST /api/v1/internal/worker-jobs/claim (one compatible processor at a time)
  -> resolve evidence bytes + content_type/filename (input_resolver.py -- see "Input-access boundary" below)
  -> process_job(job, evidence, StaticBytesResolver(...))   (Phase 1, unchanged)
  -> POST /api/v1/internal/worker-jobs/{job_id}/result
  -> exit 0
```

A run that finds no eligible job for any supported processor, a run that successfully submits a `SUCCEEDED`/`FAILED`/`DEFERRED` result, and a run that submits a `DEFERRED` result because the live input-stream endpoint genuinely isn't reachable (see "Input-access boundary" below) are all *successful* CLI exits (`0`) — only a genuine auth/transport/API failure exits `1`. See `worker.main`'s docstring.

## Supported processors

`worker.SUPPORTED_PROCESSORS` lists every `(processor_name, processor_version)` this worker's `process_job` dispatch table handles, in the order `run_once` tries them:

| Processor | Source formats | Emits |
|---|---|---|
| `fir_report_text_v1` / `1.0.0` | native-text PDF, DOCX, TXT/Markdown | `document_text_mention`-family observations (FIR reference, police station, phone, email, vehicle plate, UPI/account/transaction reference, amount, legal section, date/time — only when explicitly labelled or structurally distinctive) |
| `cdr_generic_v1` / `1.0.0` | CSV, XLSX, JSON | `cdr_call_record` + identifier mentions |
| `financial_transaction_generic_v1` / `1.0.0` | CSV, XLSX, JSON | `financial_transaction_record` + identifier mentions |
| `generic_tabular_v1` / `1.0.0` | CSV, XLSX | `tabular_record` (one per row) |
| `generic_json_v1` / `1.0.0` | JSON | `json_scalar_value` (one per scalar leaf, with its exact `json_path`) |

These names/versions and their parsing logic are all **Phase 1, unchanged** (`app/modules/structured_processing/structured/profiles.py`, `document/fir_report.py`, `structured/{cdr,finance}.py`) — this phase adds the orchestration around them, not new parsing logic. `TXT`'s `ContentKind` was extended to also accept a `.md` extension (`document/classifier.py`) since Markdown is plain text and no separate parser exists or is needed for it.

**Nipun's live routing gap, stated plainly**: `evidence_lifecycle/routing.py` only ever creates `fir_report_text_v1`/`cdr_generic_v1`/`financial_transaction_generic_v1` jobs from a real evidence upload today (`SourceType.DOCUMENT`/`CDR`/`FINANCIAL`). `generic_tabular_v1`/`generic_json_v1` are supported by this worker for completeness and any future routing change, but are never claimable through the live upload path as it exists today — not a bug in this worker, a fact about the current routing table.

## Source-locator conventions (unchanged from Phase 1)

| Source format | Locator |
|---|---|
| PDF | `page` + `span_start`/`span_end` |
| TXT/Markdown | `span_start`/`span_end` |
| DOCX | character span (`span_start`/`span_end`) in the normalized extracted text |
| CSV | `row` (+ `column` when the mention is field-specific) |
| XLSX | `sheet` + `row` (+ `column` when field-specific) |
| JSON | `json_path` |
| CDR / Finance | `row`/`sheet`/`json_path` matching the source format, never a fabricated timestamp/location |

## Confidence semantics (unchanged from Phase 1)

`extraction_confidence` measures extraction/statement quality only, **never** a probability of guilt or culpability — see `CLAUDE.md`. Deterministic, documented values only (never a continuous/learned score): `1.00` for a direct structured field or exact native parse (`CONFIDENCE_STRUCTURED_COMPLETE`), `0.95` for an exact deterministic labelled-pattern match (`CONFIDENCE_REGEX_EXACT_MATCH`), `0.90` for a conservative normalization (`CONFIDENCE_NORMALIZATION_CONSERVATIVE`), `0.80` for a weaker-context regex match (`CONFIDENCE_REGEX_WEAK_CONTEXT`). An out-of-range confidence is a `ValueError` from the frozen `ObservationV1` contract itself — never silently clamped.

## Deterministic observation IDs (unchanged from Phase 1)

`provenance.observation_id()` derives every `observation_id` from `(case_id, evidence_id, profile.name, profile.version, observation_type, locator)` via `app.core.ids.deterministic_uuid`. The same source content, profile, extractor version, and locator always produce the same ID — a safe retry (the worker resubmitting the identical `WorkerResultV1` for the same job) never creates duplicate observations, matching `EvidenceLifecycleService.submit_result`'s idempotency contract (`docs/architecture/worker-job-lifecycle.md`) exactly.

## Failure and defer policy

- **Unsupported format, malformed input, encrypted PDF, invalid time range**: `process_job` catches `ProcessingError` and returns `WorkerStatus.FAILED` with a safe, non-secret `WorkerError` (code + message only — never a raw file path, object URI, or the offending value). See `tests/unit/structured_processing/test_safety.py` and the module's existing error codes.
- **Scanned/image-only PDF**: `WorkerStatus.DEFERRED` with checkpoint `document_requires_ocr` — OCR is explicitly out of scope for this phase (`CLAUDE.md`); this worker never invents text from a scanned page. Unchanged Phase 1 behavior (`document/ocr_routing.py`).
- **Input resolution unavailable** (the claimed job's input-stream request genuinely fails, e.g. the endpoint is unreachable against an older API build — see "Input-access boundary" below): `WorkerStatus.DEFERRED` with checkpoint `input_resolution_unavailable` (`worker.CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE`). An honest "not yet possible" outcome, never a fabricated `SUCCEEDED`.
- **Resolved bytes fail SHA-256 verification** (the stream succeeded, but the received bytes don't hash to the value the API reported for this evidence — see "Input-access boundary"): `WorkerStatus.FAILED` with `WorkerError(code="evidence_integrity_mismatch", retryable=True)`. A genuine data-integrity signal, not a "not yet possible" one — `FAILED`, not `DEFERRED`.
- `queued`/`running` are never submitted as a final result — `process_job` only ever returns a terminal status, and both gap-handling paths above always submit a terminal outcome too.

## What this worker emits — and what it never does

Every emitted item is a canonical `ObservationV1` — a raw, unresolved statement about the source, carrying its own provenance. This worker **never** creates an `EntityV1`, `EventV1`, a graph node/relationship, an entity-resolution decision, or a guilt/suspicion score — see `CLAUDE.md`'s "No automatic identity merge, guilt conclusion" rule. A parsed FIR complainant/accused name, for example, is emitted as `extracted_entities` (`ExtractedEntityMention`, unresolved text + a type hint) on a `document_text_mention` observation, never as a resolved `EntityV1`.

## Input-access boundary — closed in Phase 2.2, history kept for context

`process_job(job, evidence, resolver)` needs two things a `WorkerJobV1` (the only thing `/claim` returns) does not carry: the raw evidence **bytes**, and `EvidenceRecordV1.content_type`/`.original_filename` (used by `document.classifier.classify` to pick the right parsing path — `WorkerJobV1.source_type` is too coarse for this, e.g. `document` alone doesn't distinguish PDF from DOCX from TXT).

**As originally written (this phase's first pass)**: `app/modules/evidence_lifecycle/` (Nipun's Phase 2/2.1) exposed no authenticated way for a claimed worker to retrieve either — `internal_api.py` had exactly two routes, `/claim` and `/{job_id}/result`. Per this task's explicit instruction not to bypass the boundary (no direct MinIO reads, no storage credentials on the worker, no unilateral endpoint added to another contributor's module), this worker was built against a typed `WorkerInputResolver` Protocol and a *proposed* (not-yet-implemented) endpoint shape, so it was fully testable end to end via an injected in-memory resolver even without the live capability.

**As of Phase 2.2 (Nipun)**: that proposed endpoint is now implemented — `GET /api/v1/internal/worker-jobs/{job_id}/input`, documented in full in `docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery" section and `docs/architecture/phase-2-decisions.md`'s "Phase 2.2 Decisions". The architecture this worker was built with anticipated exactly this shape, so closing the gap required **zero changes to `input_resolver.py`'s Protocol** and only small, additive changes to this module:

- `client.WorkerApiClient.fetch_input` now parses the real response: filename from a standard `Content-Disposition` header (RFC 6266, not the originally-proposed bespoke `X-Original-Filename`), and the evidence's recorded SHA-256 from `X-TraceX-Evidence-SHA256` into `ResolvedInput.expected_sha256`. A worker-side size bound (`MAX_INPUT_BYTES`, 50 MiB) is enforced on the response before it's handed anywhere else, independent of `process_job`'s own check.
- `run_once` verifies `expected_sha256` against the actually-received bytes (`hashlib.sha256(resolved.data).hexdigest()`) **before** calling `process_job` — a mismatch submits `FAILED`/`evidence_integrity_mismatch` (see "Failure and defer policy" above), never silently parsing bytes that don't match what the API reported.
- `InputResolutionUnavailableError`/`CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE` are retained (not removed) as defensive fallback behavior — e.g. against an older API build without this route, or a genuine transient `404` — so a real claimed job still degrades to an honest `DEFERRED` rather than crashing if the endpoint is ever unreachable for any reason.
- `StaticInputResolver` (unit tests) and `LiveInputResolver` (the real client) are both unchanged in shape — this is exactly what "testable with an injected in-memory input resolver even if the live input-stream capability is not currently available" was designed to make possible, and it held up without modification once the capability arrived.

**The endpoint itself**, for reference (see the evidence-lifecycle doc for the authoritative version):

```
GET /api/v1/internal/worker-jobs/{job_id}/input
Authorization: Bearer <WORKER_SHARED_SECRET>
X-Claim-Token: <claim_token from /claim>

200 OK
Content-Type: <evidence.content_type>
Content-Disposition: attachment; filename="..."; filename*=UTF-8''...
X-TraceX-Evidence-SHA256: <evidence.sha256>
<raw evidence bytes, streamed>
```

Authenticated identically to `/result` (shared secret + the same claim-token header, reused rather than a second header name), scoped to exactly the job the caller holds a valid, currently-`running`, unexpired-lease claim token for — never an object key, bucket, endpoint, or credential.

## Shimming `EvidenceRecordV1` for `process_job`

`process_job` only ever reads two fields off its `evidence: EvidenceRecordV1` parameter — `.content_type` and `.original_filename` (both routed through `document.classifier.classify`). `worker._shim_evidence_record` reconstructs the minimal object `process_job` needs: real values where this worker actually has them (`case_id`/`evidence_id` from the job, `object_uri=job.input_object_uri`, `sha256` computed from the bytes actually resolved, `uploaded_at=job.requested_at`, `content_type`/`original_filename` from the resolved input), and clearly-commented unused placeholders for the rest (`classification`, `uploaded_by`, `processing_status`, `parser_profile` — fields this worker has no authenticated way to obtain and `process_job` never reads). This avoids changing `process_job`'s signature or duplicating its logic, and is called out explicitly in code so a future reader never mistakes a placeholder for real evidence metadata.

## Structured logging

`_configure_logging` mirrors `app.main`'s structlog JSON configuration (deliberately duplicated, not imported, so a one-shot CLI script doesn't have to construct the full FastAPI app just to log one line). `run_once` binds a `run_id` (and a `job_id` once claimed) via `structlog.contextvars` for the duration of one run — the same correlation-ID pattern `app/core/errors.py` uses for HTTP requests, applied here to a non-HTTP CLI context. The shared secret and claim token are never logged, by construction: `client.py` only ever logs `job_id`s, processor names, and status codes, never a header value or response body.

## Non-goals (this phase)

Everything `CLAUDE.md` and Phase 1 already ruled out remains ruled out: OCR, ASR, NER, LLM/embedding extraction, entity resolution, `EntityV1`/`EventV1` creation, Neo4j writes, guilt/risk scoring, case CRUD, evidence upload/storage changes, direct MinIO/PostgreSQL/Neo4j/Redis access from a worker, a worker daemon/polling loop/scheduler, real per-worker credential issuance, audio/image/video/chat/social processing, Merkle/signature work, frontend work. This worker adds exactly one new capability: turning a claimed `WorkerJobV1` into a submitted `WorkerResultV1` through the internal API, for the five processors above.
