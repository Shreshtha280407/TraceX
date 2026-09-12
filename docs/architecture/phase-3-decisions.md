# Phase 3 Decisions — Nipun Canonical Observation Ingestion, Batch Persistence, Progress, and Transformation Provenance

Record of the concrete choices made while building the Phase 3 batch-submission foundation, and the reasoning behind each — so later contributors (starting with Jasraj's real document/OCR/CDR/finance workers) know what was deliberate versus what's still open.

## Scope, restated

This phase gives an authorized worker one stable, durable path to submit **partial** observation micro-batches while processing a large source, alongside the existing Phase 2.1 path for a single **terminal** `WorkerResultV1`. It does not implement any real extraction, entity resolution, graph mapping, or scoring — see "Non-goals" in the task brief and `docs/qa/known-limitations.md`.

## Reused, not duplicated

- **Worker identity, claim tokens, lease/expiry checks** (`require_worker_principal`, `X-Claim-Token`, `InvalidClaimTokenError`) are unchanged from Phase 2.1/2.2/Aditya's hardening — `submit_observation_batch` runs the exact same claim-token-hash → worker-identity → status/lease verification sequence as `submit_result`/`renew_claim`, in the same order, before anything is written.
- **`ObservationV1`** itself is unchanged — a batch's `observations` field is a plain `list[ObservationV1]`, validated by the same frozen contract every other producer already uses. No new observation shape, no new locator type.
- **`SourceLocator`** is reused verbatim for `TransformationProvenanceV1.input_locator` rather than inventing a parallel "transformation range" type — a transformation's input range is the same kind of fact an observation's own locator already models.
- **`DerivedArtifact`** (from `app/contracts/worker.py`) is reused verbatim for `TransformationProvenanceV1.derived_artifact_refs` — a transformation's byproduct is the same shape as a `WorkerResultV1`'s.
- **`canonical_sha256`** (Phase 1's canonical-serialization utility) is reused for batch-payload idempotency comparison, exactly like `WorkerResultRecord.payload_hash` already does for terminal results.
- **The `graph_projection_jobs` durable outbox** (Shreshtha's Phase 2.5 design, defined in `evidence_lifecycle/repository.py`) is reused unchanged: a batch-accepted observation enqueues exactly the same shape of row, in the same transaction, that a result-accepted observation already does. `app/modules/graph/` needed **zero code changes** — it already reads `worker_observations` by `observation_id` alone, with no idea whether the row came from a result or a batch.
- **`FOR UPDATE SKIP LOCKED` / unique-constraint-then-re-read** concurrency patterns are reused exactly as `create_evidence_with_job` (Idempotency-Key races) and `submit_result` (`worker_results.job_id` races) already established — no new concurrency primitive was invented.

## Genuinely new (this phase's own responsibility)

- `app/contracts/observation_batch.py`: `ObservationBatchSubmissionV1`, `ObservationBatchProgressV1`, `TransformationProvenanceV1`, `TransformationStatus`, `BatchAcceptanceStatus`, `ObservationBatchReceiptV1`.
- `observation_batches`, `observation_transformations`, `worker_progress_events` tables (migration `c1c9c1c9d9f1_observation_batch_ingestion`).
- `EvidenceLifecycleRepository.submit_observation_batch` (+ batch/transformation/progress lookup methods) and `EvidenceLifecycleService.submit_observation_batch` (+ `get_job_progress_summary`).
- `POST /api/v1/internal/worker-jobs/{job_id}/observations`.
- `JobView.latest_progress`, surfaced through the existing case-scoped `GET /api/v1/cases/{case_id}/jobs/{job_id}`.

## Why `worker_observations` grows two nullable columns instead of a second observations table

The task brief's own suggested schema names batch-linked observation storage "`observation_batch_items` **or an equivalent durable linkage**." A genuinely separate table would have forced every downstream reader of canonical observations — `count_observations_for_job`, and critically `app.modules.graph.outbox_repository.get_observation` (which looks a projection job's `observation_id` up in `worker_observations` alone) — to learn about a second source table, or forced this phase to duplicate rows into the old table. Instead:

- `worker_observations.result_id` becomes nullable.
- `worker_observations.observation_batch_id` (nullable, FK to `observation_batches`) is added.
- A `CHECK` constraint (`(result_id IS NULL) <> (observation_batch_id IS NULL)`) enforces that every row has **exactly one** parent — a terminal result or a partial batch, never both, never neither.

Both submission paths write into the same table. `app/modules/graph/` required no code change at all to start consuming batch-submitted observations — it was already correct by construction. This is the smallest, most coherent extension of the existing schema, not a new competing mechanism.

## Two independent opaque identifiers: `batch_id` and `idempotency_key`

The task brief's contract field list requires both. They serve different roles:

- **`batch_id`** names *this specific logical batch* within the job's ordered sequence. `(job_id, batch_id)` is unique — retrying the same `batch_id` with the same payload is a safe replay; with a different payload, a conflict.
- **`idempotency_key`** is a second, independently-unique-per-job opaque token — a defense-in-depth safety net against a worker accidentally reusing one `batch_id` for two logically different batches, or vice versa. `(job_id, idempotency_key)` is unique too, checked *after* the `batch_id` lookup finds nothing: if a different `batch_id` already claimed this exact `idempotency_key`, that is always a conflict, never a silent second acceptance.

A real worker in practice can set both to the same value (e.g. a monotonically increasing batch counter) — the two-key design does not require a worker to invent two unrelated schemes, only gives the server an extra collision check for free.

## Validation ordering in `submit_observation_batch` mirrors `submit_result` exactly

1. Claim-token hash match (unconditional).
2. Worker-identity match (unconditional, when supplied).
3. `submission.job_id`/`case_id`/`evidence_id` against the *claimed job's own* values (never the client's unverified word) — this is also how a path/body `job_id` mismatch is caught: the job was looked up by the **path** `job_id`, so `submission.job_id != job.job_id` structurally proves the two disagree.
4. **Existing-batch lookup by `(job_id, batch_id)` — before checking whether the job is currently `running`.** A worker legitimately may retry a batch submission whose acknowledgement was lost *after* the job has since gone terminal via a separate `/result` call; an idempotent replay must still succeed in that case, exactly like `submit_result`'s own "already terminal → replay/conflict, regardless of lease" branch.
5. Only for a **genuinely new** `batch_id`: the job must be `running` with an unexpired lease right now — a worker cannot open a brand-new batch against a job that has already completed or whose lease has lapsed.
6. A soft, best-effort progress non-regression check (see below).
7. Persist atomically; enqueue graph-projection jobs; return the receipt.

## `IntegrityError` disambiguation reuses the established re-read-then-decide pattern

`submit_observation_batch`'s single atomic transaction can fail on exactly three unique constraints, and the service disambiguates by re-querying afterward — the identical technique `create_evidence_with_job` (Idempotency-Key races) and `submit_result` (`worker_results.job_id` races) already use, not a new one:

1. `(job_id, batch_id)` collision (a genuine concurrent retry race) → re-read, compare `payload_hash`, replay or conflict.
2. `(job_id, idempotency_key)` collision under a *different* `batch_id` → always a conflict.
3. `worker_observations.observation_id` primary-key collision — an `observation_id` reused from a **completely different** batch or result, not this submission's own `batch_id` — always a conflict, never a second row, never a second graph-projection job. This is the documented behavior for "a previously accepted observation ID in a different non-identical batch": reject the whole new batch, leave history untouched.

Because the whole write happens inside one `engine.begin()` block, any of the three failures rolls back **everything** in that attempt — there is no scenario where a batch's observations partially land while its transformations or progress event don't, or vice versa.

## Progress events: append-only history, soft non-regression check

`worker_progress_events.ordinal` is a server-assigned, globally-monotonic `BIGSERIAL`-backed column — not a per-job counter — purely to give a stable, race-free "which came later" ordering; `docs/architecture/*` code elsewhere already establishes this convention isn't needed for correctness of the outbox pattern, only for the progress *history* being genuinely orderable.

**Regression rule**: a new progress event is rejected if, within the *same job `attempt`*, its `units_completed` or `observations_emitted` would be lower than the latest stored event's. A **new** `attempt` (a legitimate lease-expiry reclaim, `WorkerJobRecord.attempt` incremented by `claim_job`) is explicitly exempt — a freshly reclaimed worker restarting from scratch is expected to report smaller numbers again, and that is not a "stale/replayed event," it's a correct restart.

This check is deliberately a **read-then-decide, not a hard database constraint** — it reads the latest event, then validates, then writes, all within the same request but not inside a single `SELECT ... FOR UPDATE`-guarded critical section. This is an intentional, documented trade-off: real workers submit their own batches for one job strictly sequentially (a worker processing one document does not have two threads racing to submit progress for the same job concurrently), so linearizability isn't needed for what is fundamentally an *operational observability* signal, not a security or correctness boundary. If a future phase needs a hard guarantee here, add a `SELECT ... FOR UPDATE` on the job row before this check.

## Transformation provenance never stores raw content — an allow-then-deny content filter, not a mapping table

`TransformationProvenanceV1.safe_metadata` rejects (at the contract level, before any HTTP request even reaches the service):

- Keys matching a denylist of secret-shaped substrings (`password`, `secret`, `token`, `credential`, `apikey`/`api_key`, `stderr`, `stacktrace`/`traceback`, `object_uri`, `filepath`/`file_path`, `dsn`, `private_key`), case-insensitively, at any nesting depth.
- String values longer than 500 characters, anywhere in the (possibly nested) structure — a coarse but effective net against "just paste the whole extracted page text into metadata," which is exactly the kind of raw-content leak this phase must prevent.

This is enforced once, in the frozen contract's own validator — every future worker (Jasraj's document/OCR/CDR/finance workers) gets this protection automatically, with no per-module reimplementation needed, mirroring how `SourceLocator`'s "at least one field set" rule is enforced once for every observation producer.

`output_observation_ids` on a transformation must be a subset of the **same batch's own** `observations` — a transformation record only describes what it produced *in the batch it travels with*, never a dangling reference to some other batch's observations. This is validated at the contract level (`ObservationBatchSubmissionV1`'s own model validator), not deferred to a database foreign-key check.

## Why the route lives at `/api/v1/internal/worker-jobs/{job_id}/observations`, not `/internal/jobs/{job_id}/observations`

The task brief's illustrative path was `POST /internal/jobs/{job_id}/observations`. The brief's own rules explicitly prioritize coherence over the literal example: "Do not create a second, competing result-submission mechanism. Extend the existing worker/evidence lifecycle coherently" and "Use the existing worker identity, claim-token, job ownership, case, and evidence checks." The existing internal worker router is `app/modules/evidence_lifecycle/internal_api.py`, mounted at `/api/v1/internal/worker-jobs`, already hosting `/claim`, `/{job_id}/result`, `/{job_id}/renew`, and `/{job_id}/input`. Adding a fifth sibling route on the *same* router, with the *same* dependency wiring, is the coherent extension; standing up a second router at a different prefix for one new endpoint would be the "second, competing mechanism" the brief explicitly forbids. **Flagged for team review** in case a differently-named public path was expected elsewhere (e.g. by a frontend or API-gateway convention not visible in this repository).

## `is_final_batch` is metadata only — it never transitions a job's status

The existing Phase 2.1 lifecycle (`queued → running → {succeeded, failed, deferred, cancelled}`, driven exclusively by a submitted terminal `WorkerResultV1`) is unchanged. `ObservationBatchSubmissionV1.is_final_batch` is stored on the `observation_batches` row for a worker's own bookkeeping/observability (e.g. "was this the last page I processed before I plan to submit `/result`") but `submit_observation_batch` never reads it to change `worker_jobs.status`, and no code path lets a batch alone mark a job `succeeded`. A worker must still call `POST /{job_id}/result` to complete a job — proven directly by the live integration test `test_partial_batches_then_final_result_preserves_existing_lifecycle`.

## `GET /api/v1/cases/{case_id}/jobs/{job_id}` gained `latest_progress` instead of a new endpoint

The brief prefers "extending an existing job representation over proliferating endpoints." `JobView.latest_progress` (an `ObservationBatchProgressV1 | None`) is populated from `EvidenceLifecycleService.get_job_progress_summary`, itself a thin read of the latest `worker_progress_events` row for the job. Case-scoping and authorization are entirely unchanged (`require_evidence_read`, `require_case_action`) — this is a pure additive field on an existing, already-authorized response, not a new authorization surface.

## Producer contract for Jasraj's Phase 3 document/OCR/CDR/finance workers

A worker processing one claimed job should, while still `running`:

1. Emit zero or more `POST /{job_id}/observations` calls, each with:
   - a fresh `batch_id`/`idempotency_key` per logical micro-batch (a retried HTTP call must reuse the *same* `batch_id`/`idempotency_key` as the attempt it's retrying, not mint new ones — otherwise it will be treated as a brand-new batch, not a replay);
   - `case_id`/`evidence_id`/`job_id` copied verbatim from the claimed `WorkerJobV1`;
   - `observations`: only canonical `ObservationV1`s belonging to this case/evidence;
   - `transformations`: optional but recommended provenance for each real processing step, referencing only this same batch's own `observations`;
   - `progress`: optional, but when present must use a monotonically-non-decreasing `units_completed`/`observations_emitted` for the current attempt.
2. Finish with exactly one terminal `POST /{job_id}/result` (`WorkerResultV1`), as it already did before this phase — typically with an empty `observations` list, since everything was already delivered via batches, though a worker that doesn't batch at all may continue submitting all its observations in the terminal result exactly as before (both paths remain fully valid and independent).

**Explicitly deferred to Jasraj's own Phase 3 work**: the actual PDF/OCR/CDR/finance parsing logic that produces these observations/transformations. This phase only builds the durable, tested, secure path they will submit through.

## Open questions for team review

- The illustrative route path in the master plan (`/internal/jobs/{job_id}/observations`) versus this implementation's actual path (`/api/v1/internal/worker-jobs/{job_id}/observations`) — see above; flagged for confirmation, not treated as a blocker.
- The progress non-regression check's soft (read-then-decide, not `SELECT ... FOR UPDATE`) consistency guarantee — acceptable today given real workers submit batches for one job sequentially; revisit if a future concurrent-batch-submission use case emerges.
- No public read endpoint exists yet for transformation-provenance records beyond repository-level queries (`list_transformations_for_batch`) — the brief explicitly allows this ("even if no public read endpoint is yet required"); a future phase may want a case-scoped provenance API for analyst review tooling.
