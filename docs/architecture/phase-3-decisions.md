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

# Phase 3 Decisions — Aditya Secure Worker Submission, Case-Scoped Claims, Lease/Heartbeat Control, Retry Limits, and Audit Events

Record of the concrete choices made securing the worker-facing lifecycle Nipun's batch-ingestion work above (and Phase 2's claim/result/renew/input routes) sits on. This section's own scope: worker credential/processor-scope enforcement, claim ownership and case/evidence scope, lease/renewal/reclaim semantics, bounded retry, and the audit trail around all of it — not observation-batch persistence semantics itself (Nipun's, unchanged), not extraction logic (Jasraj's), not graph mapping (unchanged).

## Reused, not duplicated

- **Every authorization primitive already existed and needed no redesign**: `require_worker_principal` (credential authentication), processor-scope enforcement at claim time, claim-token-hash verification, worker-identity binding, and case/evidence scope validation on request bodies were all already present and correct across `/claim`, `/input`, `/result`, and (Nipun's) `/observations` before this phase started — inspection confirmed the design was sound, so per the task's own instruction ("do not replace it with a new incompatible token system unless inspection proves the current design is unusable"), nothing was replaced. This phase's real work was closing the three genuine gaps inspection *did* find: no bounded retry, no absolute lease-renewal ceiling, and no audit trail for any worker action that *succeeds* (only denials were audited).
- **`FOR UPDATE SKIP LOCKED`** (`EvidenceLifecycleRepository.claim_job`'s existing concurrency pattern) is unchanged and untouched — the retry-limit filter is one additional `AND` clause in the same query, not a new locking strategy.
- **`worker_jobs.claim_token_hash`'s unique-per-attempt overwrite-on-reclaim behavior** (Phase 2.1, unchanged) is what already made "the old claim token becomes invalid" true before this phase; nothing needed to change there either — this phase only added tests proving it, and an audit event marking when it happens.
- **`submit_result`'s existing idempotent, race-safe terminal-write path** (`worker_results.job_id`'s unique constraint, `IntegrityError` → re-read → replay-or-conflict) is reused verbatim for retry exhaustion (see below) rather than inventing a second "mark terminal" mechanism.
- **`graph_projection_jobs.max_attempts`** (Shreshtha's Phase 2.5 design) is the exact precedent this phase's `worker_jobs.max_attempts` mirrors — same column shape, same "exclude `attempt >= max_attempts` from the claim query" pattern, same "sweep-and-transition-to-terminal" idea (`app.modules.graph.outbox_repository.claim_batch`'s own crash-recovery sweep) — not two different retry-limit designs for two conceptually identical problems.
- **`record_audit_event`/`record_audit_event_safely`** (`app.modules.access_control.audit`) are reused unchanged; no parallel audit store, no new event-persistence mechanism.

## Genuinely new (this phase's own responsibility)

- `worker_jobs.max_attempts` column (migration `d3f1a6c9b8e2_worker_job_retry_limits`), `Settings.worker_job_max_attempts` (default `5`).
- `Settings.worker_lease_max_seconds` (default `3600`), validated `>= worker_lease_seconds` at `Settings` construction time (a `model_validator`, since this is the first cross-field constraint this file has needed).
- `EvidenceLifecycleRepository.get_retry_exhausted_jobs` + `EvidenceLifecycleService._sweep_retry_exhausted_jobs` (called at the start of every `claim_job` attempt).
- `EvidenceLifecycleRepository.renew_lease`'s `LEAST(...)`-capped `UPDATE ... RETURNING`.
- `ClaimOutcome.was_reclaim`/`.retry_exhausted`, `RenewOutcome` (replacing a bare `datetime` return from `service.renew_claim`).
- Six new/extended audit call sites in `internal_api.py`: `worker_job_claimed`/`worker_job_reclaimed` (split from a single claim-success path), `worker_job_lease_renewed` (new — `/renew`'s success path was never audited before), `worker_job_retry_exhausted` (new), and `worker_job_completed`/`worker_job_failed` (split from the single `worker.result.accepted` event).
- New unit tests (`test_worker_claim.py`, `test_worker_lease_renewal.py`, `test_worker_identity_api.py`, `test_worker_internal_api.py`, `test_worker_result.py`, `test_observation_batch_submission.py`) and a new live-integration file, `tests/integration/evidence_lifecycle/test_worker_retry_and_lease_live.py`.

## Why a job's first-ever claim is exempt from the `max_attempts` check

`worker_jobs.attempt` starts at `1` at creation and is **not** incremented on a first claim (only a reclaim increments it — an intentional, already-documented divergence from `graph_projection_jobs.attempt`, which increments on every claim including the first; see `docs/architecture/worker-job-lifecycle.md`'s "Attempt semantics"). A naive `attempt < max_attempts` filter applied uniformly to *both* "queued, never claimed" and "running, lease-expired" rows would incorrectly block a job's very first claim whenever `max_attempts == 1` (`1 < 1` is false) — caught by this phase's own `test_retry_exhaustion_sweep_is_processor_agnostic` test during development, not shipped. The fix: the `attempt < max_attempts` predicate is nested *only* inside the `RUNNING`-with-expired-lease (reclaim) branch of the eligibility `OR`, never applied to the `QUEUED` branch — a first claim is never a "retry" at all, regardless of how small `max_attempts` is configured.

## Why retry exhaustion reuses `submit_result` instead of a dedicated "mark terminal" write

An earlier design sketch had the exhaustion sweep do its own `UPDATE worker_jobs SET status='failed', ...` directly (mirroring `graph.outbox_repository.claim_batch`'s plain status-only sweep). That would have broken an existing invariant every other code path in this module already assumes: `_replay_or_conflict_result`'s defensive comment states plainly that *every* terminal job has a corresponding `worker_results` row ("`pragma: no cover — defensive: terminal implies a result exists`"). A status-only sweep would make that comment false and reachable — a worker whose lease just got swept to `failed` calling `/result` afterward would hit `get_result_for_job() -> None` and raise on the "this should never happen" path instead of getting a normal, safe rejection. Instead, `_sweep_retry_exhausted_jobs` builds a genuine, ordinarily-shaped synthetic `WorkerResultV1` (`status=FAILED`, no observations, `error.code="retry_exhausted"`) and submits it through the *existing* `EvidenceLifecycleRepository.submit_result` — the exact same insert-result-and-transition-job transaction a real worker's own terminal call uses. This keeps "every terminal job has a result" true unconditionally, costs no new write path, and gets `submit_result`'s own concurrency safety for free: if two `claim_job` calls race to sweep the same exhausted job, the loser's `INSERT INTO worker_results` hits the pre-existing unique constraint and is simply swallowed (the winner already made it terminal) — no new locking was invented for this either.

## Why the sweep runs inside every `claim_job` call, unscoped by processor

Mirrors `graph.outbox_repository.claim_batch`'s identical choice: an exhausted job for processor X would never get swept if the sweep only ran while a processor-X worker happened to be claiming. Running the (cheap — a single indexed `SELECT`, no-op when nothing matches) sweep on *every* `claim_job` call regardless of which processor is being claimed means it gets exercised by whatever traffic already exists, opportunistically, system-wide — proven directly by `test_retry_exhaustion_sweep_is_processor_agnostic`. The known limitation this shares with the graph-projection precedent: if literally no worker for *any* processor ever calls `/claim` again, an exhausted job could in principle sit un-swept — accepted for the same reason `graph.outbox_repository`'s sweep accepts it (a real deployment always has *something* polling `/claim`).

## Why the absolute lease ceiling is computed with `LEAST(...)` inside the same atomic `UPDATE`, not a separate check-then-write

Reading `claimed_at`, computing the cap in Python, then writing it back would reopen exactly the race `renew_lease`'s existing atomic re-check (`WHERE lease_expires_at >= now`) was built to close: a concurrent reclaim could overwrite the row between the read and the write. Instead, `sa.func.least(candidate_expires_at, worker_jobs_table.c.claimed_at + timedelta(seconds=max_lease_seconds))` is evaluated by PostgreSQL itself, inside the same single-statement `UPDATE ... WHERE status='running' AND lease_expires_at >= now ... RETURNING lease_expires_at` `renew_lease` already used — one round-trip, one atomic decision, no new locking primitive.

**No separate rejection path exists for "you've renewed past the ceiling too many times."** Once real time passes `claimed_at + worker_lease_max_seconds`, the capped value stops advancing on every subsequent renewal call, so it inevitably falls behind `now` — at which point the *existing* `lease_expires_at >= now` re-check (both the defensive one in `service.renew_claim` and the atomic one in `repository.renew_lease`) starts rejecting renewal on its own, indistinguishably from "the worker simply stopped heartbeating." A stuck or runaway worker calling `/renew` as fast as it likes can never hold a job past the ceiling.

## Why `worker_job_claimed`/`reclaimed`/`lease_renewed`/`retry_exhausted` use `record_audit_event_safely` despite being `SUCCESS`-outcome events

`record_audit_event_safely` was documented (before this phase) as denial-path-only — a failed write must never turn a deny into an accidental grant. This phase reuses the *same function* for four `SUCCESS`-outcome events instead of the propagating `record_audit_event`, and updated its docstring to name the actual, broader principle rather than add a second near-identical helper: the deciding factor is not "was this a denial" but "would a lost audit write be worse than a spurious 500." A claim/reclaim/renewal's entire value to the caller is a one-time secret in the response body (`claim_token`, an extended `lease_expires_at`) — the state change is *already committed* by the time the audit call runs, so a 500 here would strand an already-claimed/renewed job with nobody holding a usable token, recoverable only by waiting out the lease. `worker_job_completed`/`worker_job_failed` deliberately keep the *propagating* `record_audit_event` (matching the pre-existing `worker.result.accepted` precedent) because a result is idempotent and safely retryable — a genuine audit failure there should be visible, not silently absorbed.

## Why `worker_job_access_denied` never carries `case_id`

Considered adding `case_id` to every audit event uniformly for easier per-case auditing. Rejected for the denial event specifically: resolving a `case_id` for an *unauthenticated or token-mismatched* request would require either (a) trusting a client-supplied case_id (exactly the kind of unverified input this phase exists to stop trusting), or (b) an extra lookup against the job/evidence tables *before* the caller has proven any right to that information — turning a simple auth failure into a probing oracle ("this case_id came back, so job X must belong to it"). `worker_job_access_denied` already carries the safe, load-bearing fields (`worker_id`, `job_id`, `reason`) an operator needs to investigate; `case_id` is available indirectly by cross-referencing `job_id` against `worker_jobs` directly, which is exactly what an operator investigating an incident would already be doing.

## Decision: no credential expiry

The task brief allowed "the existing model or the smallest necessary extension," and test requirement 5 ("expired credential is rejected safely *if expiry exists in the current design*") was explicitly conditional. Inspection confirmed `WorkerCredentialRecord` has never had an `expires_at`-style field — only `ACTIVE`/`REVOKED`, with revocation permanent by design (`docs/architecture/worker-identity-and-security.md`'s "Rotating the pepper..." section documents the same permanence philosophy for the pepper). Adding expiry would be a real, non-trivial model extension (a new column, a new check in `require_worker_principal`, a new CLI flag, new tests) for a capability nothing in the task's completion criteria actually requires — "the smallest necessary extension" argues against it. Revocation already covers "deny a credential's future use" completely; expiry would only add a *time-based* trigger for the same outcome, which an operator can already achieve by revoking on a schedule if needed. Flagged in `docs/qa/known-limitations.md`, not silently omitted.

## Open questions for team review

- Whether a future phase wants credential expiry as a genuinely distinct capability from revocation (e.g. for a worker credential provisioned for a fixed-duration engagement) — deferred per "smallest necessary extension" above, not because it's undesirable in principle.
- The retry-exhaustion sweep's "only swept when *someone* calls `/claim`" characteristic (shared with `graph.outbox_repository`'s identical precedent) — acceptable for a foundation phase; a scheduled sweep independent of claim traffic is a future option if a deployment ever has long idle gaps between claim attempts for every processor at once.
- `Settings.worker_lease_max_seconds`'s default (3600s / 1 hour) and `Settings.worker_job_max_attempts`'s default (5, matching `graph_projection_max_attempts`) are reasonable starting points, not empirically tuned against any real workload — revisit once real document/OCR/CDR/finance processing times (Jasraj's Phase 3 work) are actually measured.
