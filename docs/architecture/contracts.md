# Shared Contracts Reference

This document is the canonical field-level reference for every Phase 1 shared contract in `app/contracts/`. If this document and the code ever disagree, the code + its tests in `tests/contract/` are authoritative — file a fix to this document.

## Versioning policy

- Every contract carries `schema_version`, currently pinned to the literal `"v1"` (`ContractVersion.V1`).
- `GET /api/v1/meta/contracts` reports the exact set of contract names this API build supports, sourced from `app.contracts.CONTRACT_VERSIONS` — the single source of truth, asserted directly in contract tests so the two can never drift.
- A breaking change to a `V1` contract (removing/renaming a required field, tightening a type, changing a validation rule so previously-valid payloads become invalid) requires introducing `V2`, not mutating `V1` in place. Additive, backward-compatible changes (a new optional field) may be made to `V1` with a contract-test update.
- Consumers should treat an unrecognized `schema_version` as a hard rejection, not a best-effort parse.

## Cross-cutting rules

- **IDs**: all `*_id` fields are UUIDs. Deterministic IDs (`app/core/ids.deterministic_uuid`) use `uuid5` over a fixed namespace (`TRACEX_NAMESPACE`) plus normalized string parts — same namespace + same normalized parts always yields the same UUID; changing any part changes the UUID.
- **Time**: every timestamp is an RFC 3339 UTC-aware datetime (Pydantic `AwareDatetime`). Naive datetimes are rejected.
- **Confidence**: `ObservationV1.extraction_confidence` and `EventV1.confidence` are both constrained to `[0.0, 1.0]` and mean *extraction/statement quality only* — never a probability of guilt or culpability. This applies everywhere these values are surfaced, including future UI.
- **Source locator**: `ObservationV1.source_locator` is mandatory, and at least one of its modality-relevant fields (`page`, `span_start`/`span_end`, `bbox_xyxy_normalized`, `sheet`/`row`/`column`, `json_path`, `frame_number`, `time_start_ms`/`time_end_ms`, `message_id`) must be set. An observation with no locator cannot be traced back to its evidence and is rejected.
  - If both `time_start_ms` and `time_end_ms` are set, `time_end_ms >= time_start_ms`.
  - If both `span_start` and `span_end` are set, `span_end >= span_start`.
  - `bbox_xyxy_normalized` uses normalized `[0, 1]` coordinates and must satisfy `x_min < x_max` and `y_min < y_max`.
- **Canonical serialization**: `app/core/canonical.py` provides `canonical_bytes`/`canonical_sha256` — key-sorted JSON bytes with deterministic UUID/datetime/enum/nested-model handling. Two logically-equal payloads built in different field/key order always hash identically. This is a utility for future integrity work (hashing, Merkle checkpoints); Phase 1 does not implement those chains yet.
- **`extra="forbid"`**: every contract model rejects unknown fields. A typo'd or unrecognized field is a loud validation error, not a silently-dropped value.

## Producer/consumer rule

- Any future source worker (document, CDR, financial, video, image, audio, chat) emits `ObservationV1` — and only `ObservationV1` — as its unit of output, wrapped in `WorkerResultV1`.
- Graph construction and cross-modal correlation consume canonical `ObservationV1`/`EntityV1`/`EventV1` records — never raw source bytes.
- Raw source data (the original PDF, audio file, CDR spreadsheet, etc.) is **not** written into Neo4j as the primary evidence payload. Neo4j holds the graph of entities/events/observation references; the bytes live in object storage (MinIO), addressed by `object_uri`.

## Contracts

### `EvidenceRecordV1` (`app/contracts/evidence.py`)

Contract-only in Phase 1. As of Phase 2, `app/modules/evidence_lifecycle/` is a real producer of this contract — see `docs/architecture/evidence-lifecycle.md`. The field set and validation rules below are unchanged; `EvidenceRecord` (that module's internal persistence record) adds lifecycle-only bookkeeping columns (`upload_idempotency_key`, `created_at`/`updated_at`) that never appear in the contract itself.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `"v1"` | |
| `evidence_id` | UUID | |
| `case_id` | UUID | |
| `source_type` | enum | `document`, `cdr`, `financial`, `video`, `image`, `audio`, `chat`, `other` |
| `original_filename` | str | |
| `content_type` | str | MIME type |
| `object_uri` | str | Where the bytes live in object storage |
| `sha256` | str | 64 lowercase hex chars |
| `classification` | enum | `unclassified`, `restricted`, `confidential`, `secret` (label only — RBAC/ABAC enforcement is later-phase) |
| `uploaded_by` | str | |
| `uploaded_at` | datetime | Fulfills the common "created_at" rule for this record |
| `parser_profile` | str \| null | |
| `processing_status` | enum | `uploaded`, `queued`, `processing`, `processed`, `failed` |

### `ObservationV1` (`app/contracts/observation.py`)

The canonical output of every source extractor/worker.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `"v1"` | |
| `observation_id` | UUID | |
| `case_id` | UUID | |
| `evidence_id` | UUID | |
| `observation_type` | str | Free-form; modality taxonomy is later-phase |
| `extracted_entities` | list[`ExtractedEntityMention`] | Raw, **unresolved** mentions (`text`, `entity_type_hint`, `attributes`) — not `EntityV1` references |
| `event_time` | datetime \| null | |
| `time_window` | `TimeWindow` \| null | |
| `location` | `Location` \| null | |
| `attributes` | dict[str, JSON] | |
| `extraction_confidence` | float `[0,1]` | Extraction quality only |
| `source_locator` | `SourceLocator` | Mandatory, see cross-cutting rules |
| `extractor` | `Extractor` | `name`, `version`, `config_hash`, `model_version` (all required; use `"n/a"` for `model_version` on non-ML extractors) |
| `created_at` | datetime | |

`EntityV1` is created **from** observations (`created_from_observation_ids`), not the reverse — at observation time, no entity has been resolved yet.

### `EntityV1` (`app/contracts/entity.py`)

Envelope only — no entity resolution/merging logic in Phase 1. `entity_type` is a plain string (not an enum) because the detailed entity/relationship taxonomy is owned by a later phase.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `"v1"` | |
| `entity_id` | UUID | |
| `case_id` | UUID | |
| `entity_type` | str | Extension point — taxonomy frozen later |
| `canonical_label` | str | |
| `aliases` | list[str] | |
| `stable_identifiers` | dict[str, JSON] | e.g. phone/PAN/account numbers |
| `attributes` | dict[str, JSON] | |
| `created_from_observation_ids` | list[UUID], min 1 | Evidence-first: an entity must trace back to at least one observation |
| `review_status` | enum | `unreviewed` (default), `confirmed`, `disputed`, `rejected` |
| `created_at` | datetime | |

### `EventV1` (`app/contracts/event.py`)

The only way two entities connect in TraceX. Timeless direct entity-to-entity edges are deliberately not modeled — every connection must be time-bounded.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `"v1"` | |
| `event_id` | UUID | |
| `case_id` | UUID | |
| `event_type` | str | e.g. `call`, `transaction`, `sighting`, `message`, `meeting` |
| `participant_entity_ids` | list[UUID], min 1 | |
| `event_time` | datetime \| null | Required unless `time_window` is set |
| `time_window` | `TimeWindow` \| null | Required unless `event_time` is set |
| `location` | `Location` \| null | |
| `attributes` | dict[str, JSON] | |
| `evidence_refs` | list[UUID], min 1 | Evidence-first: every event must cite evidence |
| `confidence` | float `[0,1]` | Statement quality only, never guilt probability |
| `review_status` | enum | Same enum as `EntityV1` |
| `created_at` | datetime | |

### Worker contracts (`app/contracts/worker.py`)

| Type | Purpose |
|---|---|
| `WorkerJobV1` | Dispatched to a worker: `job_id`, `case_id`, `evidence_id`, `source_type`, `processor_name`, `processor_version`, `attempt`, `idempotency_key`, `input_object_uri`, `requested_at` |
| `WorkerResultV1` | Returned by a worker: `job_id`, `case_id`, `evidence_id`, `status`, `observations` (list of `ObservationV1`), `derived_artifacts`, `checkpoint`, `error`, `completed_at` |
| `WorkerProgressV1` | Optional intermediate progress: `job_id`, `case_id`, `status`, `progress_pct`, `message`, `reported_at` |

`WorkerStatus`: `queued`, `running`, `succeeded`, `failed`, `deferred`, `cancelled`. A `failed` result must include a `WorkerError` (`code`, `message`, `retryable`); a `succeeded` result must not.

**Idempotency key format**: `"{case_id}:{evidence_id}:{processor_name}:{processor_version}"` — colon-separated, each segment matching `[A-Za-z0-9_.-]+` (dots allowed for semantic-version processor versions like `1.0.0`). The key deliberately excludes `attempt`, so retries of the same logical job reuse the same key and a consumer can safely deduplicate re-delivered results.

A worker never needs direct PostgreSQL, Neo4j, or MinIO credentials: it receives `input_object_uri` pre-scoped for reading, and returns `ObservationV1`s for the API/orchestrator to persist.

As of Phase 2, `app/modules/evidence_lifecycle/` is a real, durable producer of `WorkerJobV1` (persisted in `worker_jobs`, published to Redis) — see `docs/architecture/evidence-lifecycle.md`. As of Phase 2.1, the same module also validates and durably persists a worker-*submitted* `WorkerResultV1` (and the `ObservationV1`s inside it) via `POST /api/v1/internal/worker-jobs/{job_id}/result` — see `docs/architecture/worker-job-lifecycle.md`. No actual worker process, consumer loop, or daemon exists anywhere in this repository; `WorkerProgressV1` remains contract-only.
