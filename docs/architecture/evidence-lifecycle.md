# Evidence Lifecycle (Phase 2 — Nipun)

`app/modules/evidence_lifecycle/` implements the real, case-scoped evidence ingestion foundation: authorized upload → streamed SHA-256 hashing → private object storage → immutable metadata persistence → a durable worker-job record → best-effort dispatch. As of Phase 2.1, this module also implements the job **claim** and **result-submission** half of the lifecycle — see `docs/architecture/worker-job-lifecycle.md` for that part in full; this document covers upload through durable job creation. It is the common foundation every later source-processing module (Jasraj's `structured_processing`, Sarthak's `communication_processing`, Gaurav's `media_processing`) will eventually consume jobs from, and that Shreshtha's `graph` module will eventually see reviewed observations flow past. This module does not implement any actual worker execution, entity resolution, correlation, review workflows, or Merkle/signature chains — see "Non-goals" below.

**As of Aditya's Phase 2 worker-identity hardening**, `worker_jobs` also carries `claimed_by_worker_id` (nullable, set on every successful claim/reclaim), and `/result`/`/input` additionally require the caller to be that verified identity, not just hold the job's claim token. Every claim/result/input request now authenticates against a real, revocable per-worker credential (`require_worker_principal`), not the temporary shared secret this document previously described. See `docs/architecture/worker-identity-and-security.md` for the full design; the upload → durable-job-creation flow this document covers is otherwise unchanged.

**As of Phase 3 (Nipun)**, this module also implements the **partial observation micro-batch** submission path — a worker may submit any number of provenance-rich partial batches while a job is still running, in addition to (not instead of) the terminal `/result` submission above. See "Observation-batch ingestion (Phase 3 — Nipun)" below.

## Lifecycle, end to end

```
authorized case member
  -> POST /api/v1/cases/{case_id}/evidence (multipart)
  -> stream bytes in bounded chunks, computing SHA-256 and enforcing MAX_EVIDENCE_BYTES
  -> write bytes to a private MinIO object at a deterministic key
  -> persist evidence_records + worker_jobs in one PostgreSQL transaction
  -> best-effort publish WorkerJobV1 onto a Redis list (durable row is the source of truth, not the publish)
  -> return safe evidence + job metadata (never object_uri, never a credential)
```

A later-phase worker's only contract is `WorkerJobV1` in, `WorkerResultV1` out — unchanged from Phase 1. This module produces `WorkerJobV1` and (Phase 2.1) validates and durably persists a worker-*submitted* `WorkerResultV1`/`ObservationV1` — it never fabricates either from raw evidence content itself, and it never produces `EntityV1` or `EventV1`.

## PostgreSQL vs. MinIO ownership

- **PostgreSQL** (`evidence_records`, `worker_jobs`, migration `f2086e1e89f6_evidence_lifecycle_foundation`, `source_type` widened for Phase 2.3 by `af5b05e61b08_structured_source_type_routing`) owns all *metadata*: who uploaded what, when, its hash, its case, its processing/job status. This is the durable source of truth for "does this evidence exist and what state is it in." Both `evidence_records.source_type`/`worker_jobs.source_type` are enforced by a `CHECK` constraint independent of (but kept in sync with) the Pydantic `SourceType` enum — a new `SourceType` value needs a migration widening that constraint, not just a contract change.
- **MinIO** owns the *bytes only*, addressed by an internal object key (`object_uri` in the persisted row). The private bucket (`Settings.minio_bucket`) is never made public, and no API response ever contains a working URL to the bytes — see "No raw-evidence-download API" below.
- Retrieving the bytes later (for a future worker) goes through `MinioSourceResolver`/`ObjectStorage.read_bytes`, never a direct MinIO credential handed to a caller or worker.

## Case-scoping rule

Every repository query in `app/modules/evidence_lifecycle/repository.py` filters on `case_id` in its `WHERE` clause — case isolation is structural, not just enforced by the route layer. Every API endpoint additionally requires an active case membership via `app.modules.access_control.dependencies.require_case_action` (`EVIDENCE_WRITE` for upload, `EVIDENCE_READ` for every read) before the repository is ever called. A case-scoped list/detail response can never contain another case's rows: `list_evidence`/`get_evidence`/`get_job` all take `case_id` as a mandatory filter, not an optional one.

## SHA-256 and immutable provenance

`service._hash_and_rewind` streams the upload in `UPLOAD_CHUNK_BYTES` (1 MiB) chunks through `hashlib.sha256`, enforcing `Settings.max_evidence_bytes` incrementally as bytes arrive — the full upload is never held in memory at once. Once persisted, `evidence_records` rows are never updated by this module (no `PATCH`/`PUT` endpoint exists) — the only mutation path is `worker_jobs.dispatched_at`, an internal dispatch-bookkeeping column, not evidence metadata itself. A record's `sha256` is therefore a permanent, load-bearing fact about exactly what bytes were received.

## Object-key / private-storage rule

`storage.object_key_for(case_id, evidence_id)` returns exactly `cases/{case_id}/evidence/{evidence_id}/original` — a pure function of two UUIDs the server itself generates. The caller-supplied filename is preserved only as display metadata (`original_filename`, returned in API responses, never used to build a path or object key). This makes path traversal, injection, or key-collision from a hostile filename structurally impossible: the object key doesn't have a code path that reads the filename at all.

## Idempotency behavior

Two distinct idempotency keys exist in this module, deliberately named differently to avoid confusion:

- **Upload idempotency** (`Idempotency-Key` HTTP header, optional, ≤200 chars): stored as `evidence_records.upload_idempotency_key`, unique per `(case_id, upload_idempotency_key)` via a partial unique index (only enforced when the client actually supplies a key). A retried request with the same key **and** the same `(source_type, content_type, sha256)` returns the original evidence + job (`200`, not `201`) without a second object write or a second `worker_jobs` row. The same key reused for genuinely different content is rejected `409`.
- **`WorkerJobV1.idempotency_key`** (frozen contract format `"{case_id}:{evidence_id}:{processor_name}:{processor_version}"`): unique globally via `worker_jobs.idempotency_key`. Since `evidence_id` is always freshly generated per logical upload, this constraint is a defense-in-depth safety net, not something the normal upload flow is expected to collide against.

A concurrent race (two identical requests racing past the pre-check simultaneously) is still safe: the loser's `INSERT` violates the partial unique index inside `create_evidence_with_job`'s single transaction, raising `sqlalchemy.exc.IntegrityError`; the service catches it, deletes the loser's own just-written object (never the winner's), re-reads the row that actually won, and either returns it as a replay (content matches) or reports `409` (content differs).

## Failure handling

- **Validation failures** (missing filename, unsupported `source_type`/`content_type`, empty upload, oversized upload) are rejected before any object-storage write — `422` (or `413` for oversized).
- **Storage write failure**: nothing is persisted to PostgreSQL and no job is published; the exception propagates to the safe generic-`500` handler in `app/core/errors.py`.
- **PostgreSQL persistence failure** (after a successful storage write): the just-written MinIO object is deleted (`_safe_delete`); if that cleanup itself fails, the failure is logged (object key + request ID only, never a MinIO credential or the underlying driver exception's text) and the object is left as a traceable, recoverable orphan rather than silently swallowed.
- **Job publish (Redis) failure**: does not fail the request. The `evidence_records`/`worker_jobs` rows are already durably committed; a `worker_jobs.dispatched_at IS NULL` row is a durable, queryable signal that a future redrive mechanism (not built in this phase) could pick up and republish. No consumer loop exists to do this automatically — see "Non-goals".

## Source/content-type routing (`routing.py`)

A small, explicit, independently-maintained registry — not derived from any processing module's own profile registry (that would create a cross-module import, which the module-boundary test forbids):

| `source_type` | Accepted `content_type`s | Routed processor |
|---|---|---|
| `document` | `application/pdf`, DOCX, `text/plain` | `fir_report_text_v1` / `1.0.0` |
| `cdr` | `text/csv`, XLSX, `application/json` | `cdr_generic_v1` / `1.0.0` |
| `financial` | `text/csv`, XLSX, `application/json` | `financial_transaction_generic_v1` / `1.0.0` |
| `audio` | `audio/wav`, `audio/x-wav`, `audio/wave` | `audio_metadata_v1` / `1.0.0` |
| `chat` | `application/json` | `generic_social_json_v1` / `1.0.0` |
| `image` | `image/jpeg`, `image/png` | `media_metadata_v1` / `1.0.0` |
| `video` | `video/mp4`, `video/quicktime`, `video/x-matroska` | `media_metadata_v1` / `1.0.0` |
| `structured_tabular` | `text/csv`, XLSX | `generic_tabular_v1` / `1.0.0` |
| `structured_json` | `application/json` | `generic_json_v1` / `1.0.0` |
| `audio_transcript` | `application/json` | `transcript_import_v1` / `1.0.0` |
| `audio_diarization` | `application/json` | `diarization_import_v1` / `1.0.0` |
| `whatsapp_chat` | `text/plain` | `whatsapp_export_v1` / `1.0.0` |
| `telegram_chat` | `application/json` | `telegram_export_v1` / `1.0.0` |
| `instagram_chat` | `application/json` | `instagram_export_v1` / `1.0.0` |
| `other` | *(none registered yet)* | *(no processor — upload rejected)* |

`source_type` is always declared explicitly by the client, never inferred from content — the same "never guess ambiguous input" rule applied everywhere else in this codebase (FIR extraction, CDR/financial normalization, audio routing). Selecting the *specific* profile within a source type (e.g. FIR-report vs. a fallback tabular profile for `document`) remains each processing module's own job once it actually reads the bytes; this registry only makes the coarse routing decision needed to construct a valid `WorkerJobV1`.

**`structured_tabular`/`structured_json` (Phase 2.3)**: general CSV/XLSX/JSON evidence that isn't specifically CDR- or financial-shaped now has its own explicit source type, reaching `structured_processing`'s existing `generic_tabular_v1`/`generic_json_v1` fallback profiles (previously constructible only via a direct/test job, never a real upload — see `docs/architecture/structured-processing-worker.md`). Each new source type's accepted content types are disjoint from the other's (CSV/XLSX only for `structured_tabular`, JSON only for `structured_json`) — a request can never be ambiguous about which of the two it means. `parser_profile` is server-controlled for *every* source type as of this phase (see "Server-controlled parser profile" below), not only these two.

**`audio_transcript`/`audio_diarization`/`whatsapp_chat`/`telegram_chat`/`instagram_chat` (Phase 2 routing fix)**: closes the gap Sarthak's Phase 2 `communication_processing` worker build reported but explicitly did not fix itself (routing is a shared contract, not that task's module to change unilaterally — see `docs/architecture/phase-2-decisions.md`). Five of `communication_processing`'s seven processor profiles were fully implemented and unit-tested since Phase 1 but had no `source_type` a real upload could ever reach; `audio`/`chat` remain unchanged (`audio_metadata_v1`/`generic_social_json_v1` respectively). `telegram_chat`/`instagram_chat`/`chat` all happen to accept `application/json` — since a single source type can only route to one processor, each gets its own explicit, disjoint source type rather than a client-supplied "which parser" hint (which would violate the same "client never chooses its own processor" rule `structured_tabular`/`structured_json` and "Server-controlled parser profile" below already establish).

**`image`/`video` (Gaurav's Phase 2 completion — worker-side gap, not a routing-table change)**: `image`/`video` have routed to `media_metadata_v1` since Phase 1 and this table's `video/x-matroska` entry was never wrong — but `media_processing/source.py`'s own `MediaKind` enum had no matching entry for it until this phase, so a real `.mkv` upload would pass this routing table's content-type check and then fail *inside the worker* with `unsupported_content_type`. This routing table required no change; `media_processing`'s own classification needed to catch up to what this table had already promised. See `docs/architecture/media-processing-worker.md`.

### Server-controlled parser profile (Phase 2.3)

`EvidenceRecordV1.parser_profile` is always the canonical routed processor's name (`route.processor_name`), computed server-side from `source_type` alone — never a client-supplied value, even though `POST .../evidence` still accepts an (now-ignored) `parser_profile` form field for backward request-shape compatibility. Before this phase, a client could set this field to an arbitrary string with no relationship to the evidence's real routing; `EvidenceLifecycleService.upload_evidence` now always overwrites it with `route.processor_name`, the same value already used to construct the job's `processor_name`. This closes a latent inconsistency between "what this evidence record claims was used to parse it" and "what actually will," for every source type, not only the two new ones.

## Durable job dispatch (`jobs.py`)

`RedisJobProducer.publish` pushes the job's canonical JSON (`app.core.canonical.canonical_bytes`) onto a Redis list keyed `tracex:jobs:{source_type}` via `RPUSH` — one list per source type, so a future consumer's `BLPOP` naturally scopes itself to the modality it knows how to process. This is a **producer only**: no consumer loop, no `BLPOP`, no worker execution exists in this repository. A worker never receives PostgreSQL, Neo4j, or MinIO credentials — it is handed a `WorkerJobV1` (from the queue, or a future redrive off `worker_jobs`) and, separately, an internal `SourceResolver`-shaped object (`MinioSourceResolver`) that already holds the real credentials; the worker code itself never sees them.

## No raw-evidence-download API — and the one narrow exception (Phase 2.2)

No endpoint in this module returns a working URL to the underlying bytes, a presigned MinIO URL, an object key, a bucket name, a MinIO endpoint, or a storage credential. `EvidenceView`/`JobView` (`schemas.py`) are hand-picked safe projections — `object_uri` is deliberately excluded from both even though it carries no credential, simply because "an identifier a caller could try to resolve against MinIO directly" is exactly the shape of thing this rule exists to prevent. This boundary is unchanged by the section below — nothing in this module hands a raw MinIO URL or credential to any caller, ever.

### Worker evidence delivery (Phase 2.2)

`process_job`/`worker.py` implementations (Jasraj's `structured_processing`, and later Sarthak's/Gaurav's own workers) need the actual evidence bytes to do their job — `WorkerJobV1.input_object_uri` alone is an internal object key, not something a worker (which holds no MinIO credential) can resolve. Phase 2.1 left this as a documented gap (see `docs/architecture/structured-processing-worker.md`'s "Input-access boundary", written before this endpoint existed). Phase 2.2 closes it with exactly one narrow, authenticated, claim-token-bound, job-specific stream — not a general download API:

```
GET /api/v1/internal/worker-jobs/{job_id}/input
Authorization: Bearer <WORKER_TOKEN>
X-Claim-Token: <claim_token from /claim>

200 OK
Content-Type: <evidence.content_type>
Content-Disposition: attachment; filename="..."; filename*=UTF-8''...
Cache-Control: no-store
Content-Length: <when known>
X-TraceX-Evidence-Id: <evidence_id>
X-TraceX-Evidence-SHA256: <evidence.sha256>
X-TraceX-Source-Type: <evidence.source_type>
X-TraceX-Parser-Profile: <evidence.parser_profile, when set>

<raw evidence bytes, streamed>
```

**Why this doesn't reopen the boundary above**: the API remains the sole MinIO credential holder and streams the exact object through itself — a worker never receives an object key, bucket name, MinIO endpoint, presigned URL, or credential, only the bytes it was already dispatched to process. Access requires `require_worker_principal` (a real, active, per-worker credential — see `docs/architecture/worker-identity-and-security.md`), the exact claim token returned when *this specific job* was claimed, *and* (as of Aditya's Phase 2 worker-identity hardening) that the caller is the worker identity currently bound to this job — verified via `EvidenceLifecycleService.get_claimed_evidence_input`, which rejects a wrong job's token, a different worker's valid token, a never-claimed (`queued`) job, an already-terminal job, and an expired lease uniformly with the same generic `401` every other claim-token failure produces (never distinguishing which). The stream is valid only for the active lease on the claimed job, held by the worker that claimed it — not a standing credential, not reusable after the job completes, the lease expires, or a different worker reclaims it.

**Streaming, not buffering**: `ObjectStorage.open_stream` (`storage.py`) returns a bounded, chunked `ObjectStream` — `MinioObjectStorage`'s implementation bridges minio-py's synchronous chunk iterator to an async one via `asyncio.to_thread` per chunk (the same offload-to-thread convention every other method on that class already uses), so the object is never fully read into API process memory regardless of its size. A missing/unreadable backing object propagates as `StorageError` to the existing central safe-500 handler (`app/core/errors.py`) — never a bespoke error path, never a leaked object key or MinIO detail.

**Full design reasoning**: `docs/architecture/phase-2-decisions.md`'s "Phase 2.2 Decisions" section.

## Supported source/content types and limits (Phase 2)

- `MAX_EVIDENCE_BYTES` (default 200 MiB, `.env`-configurable) bounds a single upload; enforced incrementally during the streaming hash pass, not after a full read.
- The content-type table above is the complete Phase 2 allow-list — anything else (including `source_type=other`, and any content type not listed for a given source type) is rejected `422`.
- `original_filename` is required, trimmed, and capped at 255 characters; never validated for "looks like a real filename" beyond that (it's display metadata only).

## Observation-batch ingestion (Phase 3 — Nipun)

`app/modules/evidence_lifecycle/` also implements the **partial**-submission counterpart to Phase 2.1's terminal `WorkerResultV1` path: a worker processing a large document/CDR/finance source may call

```
POST /api/v1/internal/worker-jobs/{job_id}/observations
Authorization: Bearer <WORKER_TOKEN>
X-Claim-Token: <claim_token from /claim>
Body: ObservationBatchSubmissionV1
```

any number of times while the job is still `running`, then complete with exactly one terminal `POST /{job_id}/result` as before — `is_final_batch` on a submission is bookkeeping metadata only and never transitions the job's status itself. Authorization, claim-token verification, and worker-identity binding are byte-for-byte the same checks `/result`/`/renew` already use (see "Result validation, in order" in `docs/architecture/worker-job-lifecycle.md`) — this endpoint invents no new security boundary.

**Persistence**: one atomic transaction inserts an `observation_batches` receipt row, every submitted `ObservationV1` (into the same `worker_observations` table Phase 2.1 already uses — see below), every `TransformationProvenanceV1` (into `observation_transformations`), at most one `worker_progress_events` row, and one `graph_projection_jobs` row per newly accepted observation — all or nothing, exactly like `submit_result`'s own transaction.

**Equivalent durable linkage, not a second observations table**: `worker_observations.result_id` is now nullable, and a new nullable `worker_observations.observation_batch_id` was added (FK to `observation_batches`), with a `CHECK` constraint enforcing exactly one of the two is ever set. Both the terminal-result path and the partial-batch path write into the same table, so `count_observations_for_job` and `app.modules.graph.outbox_repository.get_observation` needed no code changes at all to see observations from either source. See `docs/architecture/phase-3-decisions.md` for the full reasoning.

**Replay and conflict**: `(job_id, batch_id)` and `(job_id, idempotency_key)` are each unique. An exact-payload resubmission of an already-accepted `batch_id` returns the original receipt (`status: "replayed"`), creating no new rows and enqueuing no additional graph-projection job, regardless of whether the job has since gone terminal via a separate `/result` call. A different payload under the same `batch_id`, or the same `idempotency_key` reused for a different `batch_id`, is a safe `409`. An `observation_id` reused from a genuinely different, already-accepted batch or result is also a safe `409` — never a second row, never a second projection job. A **brand-new** `batch_id` is only accepted while the job is currently `running` with an unexpired lease.

**Progress**: `ObservationBatchProgressV1` (optional per batch) is stored as an ordered, append-only `worker_progress_events` row; `GET /api/v1/cases/{case_id}/jobs/{job_id}` (`JobView.latest_progress`) surfaces the most recent one, case-scoped and authorized exactly like every other field on that existing response. A progress update that would move `units_completed`/`observations_emitted` backwards within the same job `attempt` is rejected — a fresh reclaim (a new `attempt`) is explicitly allowed to reset.

**Transformation provenance**: `TransformationProvenanceV1` records are immutable once accepted, retrievable via `EvidenceLifecycleRepository.list_transformations_for_batch` (no public read endpoint yet — not required by this phase). `safe_metadata` is contract-validated to reject secret-shaped keys and long string values, so raw document/media content, credentials, and stack traces can never enter it.

**Never calls Neo4j directly**: exactly like `submit_result`, this endpoint only ever writes to PostgreSQL — the existing graph projector remains the sole path that touches Neo4j, asynchronously, off the same durable outbox.

Full design reasoning: `docs/architecture/phase-3-decisions.md`. Producer contract for Jasraj's real document/OCR/CDR/finance workers: the same document's "Producer contract for Jasraj's Phase 3 document/OCR/CDR/finance workers" section.

## Securing the worker submission boundary (Phase 3 — Aditya)

Every worker-facing route (`claim`, `input`, `renew`, `observations`, `result`) shares one authorization shape end to end: an authenticated `WorkerPrincipal` (a real, active, non-revoked `WorkerCredentialRecord`) whose `allowed_processor_names` covers the job's processor (checked at claim time only — a claimed job is thereafter bound to a specific worker *identity*, not re-checked against processor scope), the exact claim token issued for the job's *current* attempt, and — for every mutating action — the caller being the worker identity currently bound to the job (`worker_jobs.claimed_by_worker_id`). Request-body `case_id`/`evidence_id`, where present (`/observations`, `/result`), are validated against the claimed job's own authoritative values and never trusted alone. None of this is new *shape* — Nipun's `/observations` route above already required it byte-for-byte; this phase's job was closing the gaps in what backs that shape:

- **Lease renewal, an absolute lease ceiling, and a bounded max-attempt cutoff** now exist and are database-enforced (`FOR UPDATE SKIP LOCKED` for every claim/reclaim, a unique constraint for every terminal result) — see `docs/architecture/worker-job-lifecycle.md`'s "Lease and retry policy". `/observations`/`/result` automatically inherit the new ceiling and cutoff, since both check the same `worker_jobs.lease_expires_at`/`attempt`/`max_attempts` columns `/claim`/`/renew` maintain — no code in the batch/result paths themselves needed to change.
- **A worker that has exhausted its retry budget is transitioned durably to `failed`** (`error.code="retry_exhausted"`) by reusing the existing `submit_result` write path with a synthetic terminal `WorkerResultV1` — not a second "mark terminal" mechanism, and not a silently dropped job.
- **Every worker-lifecycle state change is now audited**, not only denials — see `docs/architecture/worker-identity-and-security.md`'s "Audit event policy" for the complete, current event table (`worker_job_claimed`/`reclaimed`/`lease_renewed`/`completed`/`failed`/`retry_exhausted`, alongside the pre-existing denial events).

Full design reasoning: `docs/architecture/phase-3-decisions.md`'s "Aditya Phase 3" section.

## Real document/OCR/NER and CDR/finance batch producer (Phase 3 — Jasraj)

`app/modules/structured_processing/worker.py` is now a real, authorized producer against the `/observations` endpoint above — not just `/result`. For `fir_report_text_v1` (PDF/DOCX/TXT), `cdr_generic_v1`, and `financial_transaction_generic_v1` jobs specifically, `run_once` submits one or more real `ObservationBatchSubmissionV1`s (real local OCR for scanned/untrustworthy PDF pages, real regex/NER/rule-based-relation extraction, real vectorized CDR/finance record normalization) through `client.submit_batch`, then exactly one terminal `WorkerResultV1` with `observations=[]` — following the "Producer contract for Jasraj's Phase 3 ... workers" this module's own `/observations` section above already specified, verified against this real implementation. `generic_tabular_v1`/`generic_json_v1` (Nipun's fallback profiles) are unchanged: `process_job` still runs synchronously and submits its full result via `/result` directly, exactly as before this phase. No `evidence_lifecycle` code changed to support this — the batch-ingestion path above needed zero modification to serve a second, independent real producer. Full design: `docs/architecture/document-structured-processing.md` and `docs/architecture/phase-3-decisions.md`'s "Jasraj Phase 3" section.

`app/modules/media_processing/worker.py::run_once` is also a real authorized producer for image/video OCR. It keeps media decoding/detection/tracking local, submits bounded original-coordinate OCR and other media observations only through `/observations`, and then submits one terminal result with `observations=[]`. Claim-token-bound input, SHA verification, lease renewal, idempotent replay, and graph outbox handling are unchanged shared lifecycle behavior. See `docs/architecture/image-ocr-provenance.md`.

## Intentional deferrals (this phase only; see `docs/qa/known-limitations.md` for the full list)

- No worker daemon/consumer loop — Phase 2.1 adds the claim/submit primitives a future worker would call, not the worker itself. See `docs/architecture/worker-job-lifecycle.md`.
- No document/OCR/ASR/video/CDR/financial extraction, no entity resolution, no graph projection, no correlation/scoring/hypothesis engine, no human-review workflow, no Merkle roots or signatures.
- No case CRUD API — cases/memberships are seeded via `app.modules.access_control.repository` directly (the existing minimal access-control anchor), exactly as every other integration test in this repository already does.
- `EvidenceRecordV1.processing_status` starts directly at `queued`, never `uploaded`, because evidence and its job are created in one atomic transaction — there is no separately observable intermediate state in this phase. It also does not yet reflect job completion (`processed`/`failed`) once a worker result comes in — see `docs/qa/known-limitations.md`.
- No worker-credential expiry (active/revoked only, permanent revocation), no maximum-concurrent-credential cap, and no cryptographic tamper-evidence over the audit trail (Merkle roots, hash chains, signatures — Phase 6's responsibility). See `docs/architecture/worker-identity-and-security.md`'s "Explicit non-goals".
