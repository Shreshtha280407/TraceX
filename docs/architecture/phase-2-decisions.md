# Phase 2 Decisions — Nipun Evidence Lifecycle Foundation

Record of the concrete choices made building the evidence-lifecycle foundation, and the reasoning behind each. Mirrors `docs/architecture/phase-1-decisions.md`'s format.

## Reused, not duplicated

Before writing any code, the existing Phase 1 foundation was inspected and reused directly rather than re-invented:

- **Case/membership model, authentication, and authorization**: `app.modules.access_control.dependencies.require_case_action`/`require_evidence_read` (already exported, already documented as "the integration points Nipun's later case/evidence endpoints ... depend on") gate every endpoint. No new case CRUD, no new auth flow, no new RBAC/ABAC policy.
- **Audit logging**: `app.modules.access_control.audit.record_audit_event` is called directly from `api.py` on a successful upload. Denied attempts are *not* separately audit-logged by this module, because `require_case_action`'s dependency itself (Aditya's code) doesn't call `record_audit_event` on denial — that's a pre-existing architectural gap common to every case-scoped endpoint, not something introduced or fixable here without editing another contributor's module. Flagged in `docs/qa/known-limitations.md` for team review.
- **Error envelope, correlation IDs, security headers**: every raised error is a plain `HTTPException`; `app/core/errors.py`'s existing handlers build the safe envelope. No custom error response shape was introduced.
- **SQLAlchemy Core / Alembic conventions**: `app/modules/evidence_lifecycle/repository.py` hand-writes `sa.Table` objects matching the migration exactly, the identical pattern `app/modules/access_control/repository.py` already established (`migrations/env.py`'s `target_metadata = None` is unchanged, deliberately).
- **MinIO client construction**: `MinioObjectStorage` constructs its `Minio(...)` client and offloads every sync call via `asyncio.to_thread`, identical to `app.dependencies.services.check_minio`.
- **`SourceResolver` protocol**: `MinioSourceResolver` implements `structured_processing.models.SourceResolver`'s shape (`read_bytes(object_uri) -> bytes`) structurally — no import of `structured_processing` (or any sibling module) was added, preserving the existing module-boundary rule.

## Genuinely new (this module's own responsibility)

- `app/modules/evidence_lifecycle/` — `models.py`, `errors.py`, `routing.py`, `schemas.py`, `storage.py`, `jobs.py`, `repository.py`, `service.py`, `dependencies.py`, `api.py`.
- `migrations/versions/f2086e1e89f6_evidence_lifecycle_foundation.py` — `evidence_records`, `worker_jobs`.
- `MAX_EVIDENCE_BYTES` setting.
- `python-multipart` (new dependency — required by FastAPI for `File`/`Form` multipart parsing; genuinely absent, confirmed via `uv run python -c "import multipart"` failing before this task).

## `source_type` is client-declared, never inferred from content

Every other extraction module in this codebase (`structured_processing`, `communication_processing`) already treats "never guess ambiguous input" as a hard rule for its own domain. Evidence lifecycle applies the same rule one layer up: a CSV file could legitimately be a CDR export, a financial export, or unrelated tabular data, and nothing about its bytes alone disambiguates that safely. The client (an authorized case member who knows what they're uploading) declares `source_type` explicitly as a required form field; the server only validates it against a small allow-list of accepted content types per source type (`routing.py`), never infers it.

## One canonical processor per `source_type`, not per-profile classification

`routing.ROUTING` maps each `source_type` to exactly one `(processor_name, processor_version)` — the same "default/generic" profile name each downstream module's own worker already recognizes (`fir_report_text_v1`, `cdr_generic_v1`, `financial_transaction_generic_v1`, `audio_metadata_v1`, `generic_social_json_v1`, `media_metadata_v1`). Choosing *between* multiple valid profiles for the same source type (e.g. a specific WhatsApp/Telegram/Instagram chat-export shape, or a FIR-report vs. a fallback tabular document) requires reading the actual bytes and classifying their shape — logic that belongs to, and already exists in, the owning processing module. Evidence lifecycle's job is coarse routing at ingestion time, not content classification.

## `source_type=other` has no registered processor and is rejected at upload

`SourceType.OTHER` is the frozen contract's deliberate escape hatch for an unanticipated modality (see `docs/architecture/phase-1-decisions.md`). No processing module in this repository handles it. Rather than inventing a fictitious processor assignment, uploads declaring `source_type=other` are rejected (`422`, `UnsupportedSourceTypeError`) until a later phase defines a real processor for it. This is a documented, honest limitation, not a silent gap.

## Evidence and its job are created in one PostgreSQL transaction

`EvidenceLifecycleRepository.create_evidence_with_job` inserts both rows inside a single `engine.begin()` block. An `evidence_records` row without a corresponding `worker_jobs` row (or vice versa) is therefore structurally impossible — a mid-transaction failure rolls back both, and the caller's cleanup logic only ever has to reason about "did the transaction commit," not "which of the two rows landed." A direct consequence: `EvidenceRecordV1.processing_status` starts at `queued`, never `uploaded` — there is no distinct intermediate state to observe when both rows are created together.

## Idempotency-Key conflict detection compares `(source_type, content_type, sha256)`, not the whole record

A replayed upload is recognized as "the same logical request" if these three values match the row already stored under that `(case_id, upload_idempotency_key)`. `sha256` alone would be nearly sufficient (content identity), but a client re-sending the same bytes with a different declared `source_type`/`content_type` is a meaningfully different request (it would route to a different processor) and must not be silently treated as a cache hit.

## Race-losing uploads are cleaned up by content comparison, not by transaction order

When two requests with the same `Idempotency-Key` race past the read-then-check pre-check simultaneously, the loser's `INSERT` fails with `IntegrityError` inside its own transaction (which also rolls back its `worker_jobs` insert). The service re-reads whichever row actually won, deletes only the *loser's* just-written MinIO object (identified by the loser's own locally-known object key, never guessed), and compares content to decide replay vs. conflict — the same logic the non-race path already uses.

## `WorkerJobV1` publication failure never fails the upload request

Redis is treated as a best-effort notification layer on top of the durable `worker_jobs` row, not a required dependency for upload success. If `RedisJobProducer.publish` raises, the service logs a structured `evidence.job.dispatch_deferred` warning and returns the successful upload result anyway — the row's `dispatched_at IS NULL` is itself the durable "not yet confirmed dispatched" signal a future redrive mechanism (out of scope for this phase) would query for. This mirrors the same fail-open-on-non-critical-path philosophy `check_minio`/`check_redis` already apply to `/readyz` (report unavailability; don't crash the request path that depends on it only loosely).

## `_dispatch` returns the (possibly updated) job record, not the one built before publishing

Caught during live-Compose verification: the first implementation returned the in-memory `WorkerJobRecord` built *before* calling `_dispatch`, so a same-request successful publish never showed up in the API response's `dispatched_at` field (it would only appear on a later `GET /jobs/{job_id}`, which re-reads from PostgreSQL). Fixed by having `_dispatch` return an updated copy (`model_copy(update={"dispatched_at": ...})`) on success, used to build the returned `UploadOutcome`. See `docs/qa/test-results.md` for how this was caught.

## `object_uri` is deliberately excluded from every API response

Even though it carries no credential (it's just `cases/{case_id}/evidence/{evidence_id}/original`, a MinIO object key that requires real credentials to resolve), it is the kind of identifier a caller could try to use directly against MinIO. Excluding it entirely from `EvidenceView` is the more conservative reading of "do NOT add unrestricted raw-evidence-download APIs" than "presigned URLs are the only thing to avoid."

## Open questions for team review

- Denied-access-attempt auditing for case-scoped endpoints is a pre-existing gap in `require_case_action` (Aditya's module), not something this task fixed — see `docs/qa/known-limitations.md`.
- No automatic redrive of `worker_jobs` rows with `dispatched_at IS NULL` exists yet; a later phase should decide whether that's a scheduled sweep, a manual admin action, or built into the eventual consumer's startup.
- `routing.ROUTING`'s one-processor-per-source-type mapping will need revisiting once a real orchestrator needs to choose between multiple valid profiles for the same source type (e.g. a specific chat-export platform) at ingestion time rather than at processing time.

---

# Phase 2.1 Decisions — Nipun Worker Claim and Result-Submission Integration

Full design reasoning lives in `docs/architecture/worker-job-lifecycle.md` (the dedicated document this phase's task brief required); this section records the handful of decisions worth cross-referencing from here.

## Reused, not duplicated (Phase 2.1)

- **Claim-token generation/hashing**: `secrets.token_urlsafe(32)` + SHA-256 hex, the exact primitives and reasoning `access_control.tokens.generate_refresh_token`/`hash_refresh_token` already established for "a high-entropy secret shown once, only its hash persisted." Not imported directly (would cross a module boundary for a two-line stdlib pattern) — duplicated as a documented pattern, the same precedent `_dump_for_insert` already set between `access_control.repository` and `evidence_lifecycle.repository`.
- **Race-safe duplicate handling**: `worker_results.job_id`'s unique constraint + catching `sqlalchemy.exc.IntegrityError` is the identical technique `create_evidence_with_job`'s `Idempotency-Key` race already uses — not a new pattern.
- **Canonical hashing for idempotency comparison**: `app.core.canonical.canonical_sha256`, Phase 1's own utility, used exactly as its docstring anticipated ("a utility for future integrity work").
- **Audit logging**: `record_audit_event`, called for an accepted (non-replay) result submission with `user_id=None` — a worker is not a human user, and the function's `user_id` parameter is already nullable for exactly this kind of caller.

## Genuinely new (Phase 2.1)

- `worker_results`, `worker_observations` tables + four additive columns on `worker_jobs` (`claimed_at`, `lease_expires_at`, `claimed_by`, `claim_token_hash`), migration `102857ca8d1d`.
- `EvidenceLifecycleRepository.claim_job`/`get_job_by_id`/`get_result_for_job`/`list_observations_for_result`/`count_observations_for_job`/`submit_result`.
- `EvidenceLifecycleService.claim_job`/`submit_result`.
- `require_worker_principal` + `Settings.worker_shared_secret`/`worker_lease_seconds`.
- `app/modules/evidence_lifecycle/internal_api.py` — `/api/v1/internal/worker-jobs/{claim,{job_id}/result}`.
- No new dependency: everything above is stdlib (`secrets`, `hashlib`, `hmac`) plus already-approved SQLAlchemy/FastAPI/Pydantic features (`FOR UPDATE SKIP LOCKED` is a SQLAlchemy Core method call, not a new library).

## Why `worker_results`/`worker_observations` are new tables, not columns bolted onto `worker_jobs`

A job can be claimed and its lease can expire more than once (each reclaim is a new attempt), but at most one attempt ever produces the accepted, canonical result. Modeling the result as its own table with a `job_id` unique constraint makes "exactly one accepted result per job, ever" a database-enforced fact rather than an application convention — and gives a durable, independent home for the full submitted payload (`canonical_payload`) that would be awkward to bolt onto the job row without duplicating most of its own columns.

## Why `get_job_by_id` is unscoped by `case_id`

Every case-scoped, human-facing query in this module takes `case_id` as a mandatory filter (see `docs/architecture/evidence-lifecycle.md`'s "Case-scoping rule"). The internal worker endpoints deliberately do not follow that rule for job lookups, because a worker's authorization model is different in kind: it authenticates by holding a valid claim token for one specific `job_id`, not by case membership. `get_job_by_id` is the one place this module intentionally looks up a job without a `case_id` filter — restricted to `internal_api.py`, which is itself gated by `require_worker_principal` and never reachable from the case-scoped router.

## Security test's forbidden-contract list updated for this module's new, legitimate scope

`tests/security/evidence_lifecycle/test_evidence_lifecycle_boundaries.py`'s `_FORBIDDEN_CONTRACT_IMPORTS` previously forbade importing `ObservationV1`/`WorkerResultV1` at all, on the Phase 2 assumption that this module only ever *produces* `WorkerJobV1`. Phase 2.1 legitimately needs to *validate and persist* a worker-submitted `WorkerResultV1` (and the `ObservationV1`s inside it) — the frozen contract's own Pydantic validators are the enforcement mechanism the task brief explicitly requires ("Validate the submitted `WorkerResultV1` using the frozen Pydantic contract before persistence"). The forbidden set was narrowed to `{EntityV1, EventV1, WorkerProgressV1}` — this module still never touches entity/event resolution or worker-progress reporting, and it never *fabricates* an `ObservationV1`/`WorkerResultV1` from raw evidence content itself (that remains extraction-module territory) — only re-validates and round-trips what a worker submitted.

## `worker_shared_secret` normalizes a blank value to `None` (real bug caught before this record)

Found while wiring `WORKER_SHARED_SECRET` into `compose.yaml`'s `api` service, before ever committing the passthrough: `docker compose`'s `${VAR}` substitution (no default) resolves an **unset** variable to an **empty string** inside the container, not an absent one — and pydantic-settings treats an environment variable's mere *presence* as "provided" regardless of its contents, so `Settings.worker_shared_secret` would have resolved to `SecretStr('')`, not `None`. `require_worker_principal`'s fail-closed check is `if configured_secret is None: raise 503` — an empty-but-not-`None` secret would skip that branch entirely and proceed to `hmac.compare_digest(token, "")`, which returns `True` for an empty submitted token (e.g. a request literally carrying `Authorization: Bearer ` with nothing after it) — a real authentication bypass on the internal worker endpoints in exactly the deployment configuration (compose, secret unset) this task's own "fails closed outside tests" requirement was meant to cover. Fixed with a `field_validator` on `worker_shared_secret` that normalizes any blank/whitespace-only value to `None` at the `Settings` level (so every caller gets the fix, not just this one passthrough), plus a redundant defense-in-depth emptiness check in `require_worker_principal` itself. Regression test: `tests/unit/test_config.py::test_blank_worker_shared_secret_normalizes_to_none`.

## Open questions for team review (Phase 2.1)

- No real per-worker credential system exists — `require_worker_principal` is a narrow shared-secret stand-in. See "Worker identity" in `docs/architecture/worker-job-lifecycle.md` for the exact Aditya handoff this implies.
- No max-attempt cutoff — a job with a perpetually-expiring lease is reclaimable forever. A later phase should decide the policy (likely a small addition to the claim query's eligibility condition).
- No automatic redrive of `deferred`/`cancelled` jobs.
- Denied worker actions (bad/expired/wrong claim token, scope mismatch, conflict) are not audit-logged, mirroring the same pre-existing gap already documented for case-scoped user endpoints.
