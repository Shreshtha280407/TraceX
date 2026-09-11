# Evidence Lifecycle (Phase 2 — Nipun)

`app/modules/evidence_lifecycle/` implements the real, case-scoped evidence ingestion foundation: authorized upload → streamed SHA-256 hashing → private object storage → immutable metadata persistence → a durable worker-job record → best-effort dispatch. As of Phase 2.1, this module also implements the job **claim** and **result-submission** half of the lifecycle — see `docs/architecture/worker-job-lifecycle.md` for that part in full; this document covers upload through durable job creation. It is the common foundation every later source-processing module (Jasraj's `structured_processing`, Sarthak's `communication_processing`, Gaurav's `media_processing`) will eventually consume jobs from, and that Shreshtha's `graph` module will eventually see reviewed observations flow past. This module does not implement any actual worker execution, entity resolution, correlation, review workflows, or Merkle/signature chains — see "Non-goals" below.

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

- **PostgreSQL** (`evidence_records`, `worker_jobs`, migration `f2086e1e89f6_evidence_lifecycle_foundation`) owns all *metadata*: who uploaded what, when, its hash, its case, its processing/job status. This is the durable source of truth for "does this evidence exist and what state is it in."
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
| `other` | *(none registered yet)* | *(no processor — upload rejected)* |

`source_type` is always declared explicitly by the client, never inferred from content — the same "never guess ambiguous input" rule applied everywhere else in this codebase (FIR extraction, CDR/financial normalization, audio routing). Selecting the *specific* profile within a source type (e.g. FIR-report vs. a fallback tabular profile for `document`) remains each processing module's own job once it actually reads the bytes; this registry only makes the coarse routing decision needed to construct a valid `WorkerJobV1`.

## Durable job dispatch (`jobs.py`)

`RedisJobProducer.publish` pushes the job's canonical JSON (`app.core.canonical.canonical_bytes`) onto a Redis list keyed `tracex:jobs:{source_type}` via `RPUSH` — one list per source type, so a future consumer's `BLPOP` naturally scopes itself to the modality it knows how to process. This is a **producer only**: no consumer loop, no `BLPOP`, no worker execution exists in this repository. A worker never receives PostgreSQL, Neo4j, or MinIO credentials — it is handed a `WorkerJobV1` (from the queue, or a future redrive off `worker_jobs`) and, separately, an internal `SourceResolver`-shaped object (`MinioSourceResolver`) that already holds the real credentials; the worker code itself never sees them.

## No raw-evidence-download API — and the one narrow exception (Phase 2.2)

No endpoint in this module returns a working URL to the underlying bytes, a presigned MinIO URL, an object key, a bucket name, a MinIO endpoint, or a storage credential. `EvidenceView`/`JobView` (`schemas.py`) are hand-picked safe projections — `object_uri` is deliberately excluded from both even though it carries no credential, simply because "an identifier a caller could try to resolve against MinIO directly" is exactly the shape of thing this rule exists to prevent. This boundary is unchanged by the section below — nothing in this module hands a raw MinIO URL or credential to any caller, ever.

### Worker evidence delivery (Phase 2.2)

`process_job`/`worker.py` implementations (Jasraj's `structured_processing`, and later Sarthak's/Gaurav's own workers) need the actual evidence bytes to do their job — `WorkerJobV1.input_object_uri` alone is an internal object key, not something a worker (which holds no MinIO credential) can resolve. Phase 2.1 left this as a documented gap (see `docs/architecture/structured-processing-worker.md`'s "Input-access boundary", written before this endpoint existed). Phase 2.2 closes it with exactly one narrow, authenticated, claim-token-bound, job-specific stream — not a general download API:

```
GET /api/v1/internal/worker-jobs/{job_id}/input
Authorization: Bearer <WORKER_SHARED_SECRET>
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

**Why this doesn't reopen the boundary above**: the API remains the sole MinIO credential holder and streams the exact object through itself — a worker never receives an object key, bucket name, MinIO endpoint, presigned URL, or credential, only the bytes it was already dispatched to process. Access requires both `require_worker_principal` (the same shared-secret boundary every internal endpoint requires) *and* the exact claim token returned when *this specific job* was claimed — verified via `EvidenceLifecycleService.get_claimed_evidence_input`, which rejects a wrong job's token, a never-claimed (`queued`) job, an already-terminal job, and an expired lease uniformly with the same generic `401` every other claim-token failure produces (never distinguishing which). The stream is valid only for the active lease on the claimed job — not a standing credential, not reusable after the job completes or the lease expires.

**Streaming, not buffering**: `ObjectStorage.open_stream` (`storage.py`) returns a bounded, chunked `ObjectStream` — `MinioObjectStorage`'s implementation bridges minio-py's synchronous chunk iterator to an async one via `asyncio.to_thread` per chunk (the same offload-to-thread convention every other method on that class already uses), so the object is never fully read into API process memory regardless of its size. A missing/unreadable backing object propagates as `StorageError` to the existing central safe-500 handler (`app/core/errors.py`) — never a bespoke error path, never a leaked object key or MinIO detail.

**Full design reasoning**: `docs/architecture/phase-2-decisions.md`'s "Phase 2.2 Decisions" section.

## Supported source/content types and limits (Phase 2)

- `MAX_EVIDENCE_BYTES` (default 200 MiB, `.env`-configurable) bounds a single upload; enforced incrementally during the streaming hash pass, not after a full read.
- The content-type table above is the complete Phase 2 allow-list — anything else (including `source_type=other`, and any content type not listed for a given source type) is rejected `422`.
- `original_filename` is required, trimmed, and capped at 255 characters; never validated for "looks like a real filename" beyond that (it's display metadata only).

## Intentional deferrals (this phase only; see `docs/qa/known-limitations.md` for the full list)

- No worker daemon/consumer loop — Phase 2.1 adds the claim/submit primitives a future worker would call, not the worker itself. See `docs/architecture/worker-job-lifecycle.md`.
- No document/OCR/ASR/video/CDR/financial extraction, no entity resolution, no graph projection, no correlation/scoring/hypothesis engine, no human-review workflow, no Merkle roots or signatures.
- No case CRUD API — cases/memberships are seeded via `app.modules.access_control.repository` directly (the existing minimal access-control anchor), exactly as every other integration test in this repository already does.
- `EvidenceRecordV1.processing_status` starts directly at `queued`, never `uploaded`, because evidence and its job are created in one atomic transaction — there is no separately observable intermediate state in this phase. It also does not yet reflect job completion (`processed`/`failed`) once a worker result comes in — see `docs/qa/known-limitations.md`.
