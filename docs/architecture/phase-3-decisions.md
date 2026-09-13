# Phase 3 Decisions — Nipun Canonical Observation Ingestion, Batch Persistence, Progress, and Transformation Provenance

Record of the concrete choices made while building the Phase 3 batch-submission foundation, and the reasoning behind each — so later contributors (starting with Jasraj's real document/OCR/CDR/finance workers) know what was deliberate versus what's still open.

## Shreshtha — deterministic graph mapping

Document mentions and documented relation claims map only to evidence-local
`SourceClaim` nodes. Canonical CDR and financial records map to temporal
events only when all mandatory source-backed endpoint and canonical-time
fields are present. Unsupported or incomplete records are retained in
PostgreSQL and their safe deterministic mapping result is recorded on the
projected canonical observation; they do not fail the graph outbox. No
identity resolution or cross-observation join is permitted. Full policy:
`docs/architecture/phase-3-graph-mapping.md`.

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

# Phase 3 Decisions — Jasraj Document, FIR, CDR, and Financial Evidence Processing Workers

Record of the concrete choices made building real document/OCR/NER/relation extraction and real CDR/finance vectorized batch processing on top of Nipun's batch-ingestion contract (above) and Aditya's worker-security boundary (above). This section's own scope: text-layer trust classification, real local OCR, layout normalization, regex/NER/relation extraction, CDR/finance chunked processing and canonical-value normalization, and the `worker.py` orchestration submitting all of it through Nipun's `/observations`/`/result`. Not: batch persistence/replay semantics (Nipun's, unchanged and reused verbatim), worker credential/claim/lease/retry policy (Aditya's, unchanged and reused verbatim), graph mapping (Shreshtha's, unchanged — this phase writes zero new code there).

## Reused, not duplicated

- **`document/fir_report.py`'s regex patterns, `structured/cdr.py`/`finance.py`'s existing normalization functions, and `structured/{csv,xlsx,json}_parser.py`'s single-shot parsers are all unchanged and directly reused** — this phase adds new callers/wrappers around them (a chunked reader, a timezone-aware timestamp resolver used by both CDR and finance), never a second competing implementation. `document/pdf.py`'s `extract_pdf` is extended (per-page trust classification replaces the old binary "has text or not" check) rather than replaced — `PdfExtractionResult`'s existing fields (`segments`, `ocr_required_pages`, `total_pages`) keep their exact pre-Phase-3 meaning; every pre-existing test in `test_pdf.py` passes unchanged.
- **`Extractor`/`observation_id`/`mention_to_observation` (`provenance.py`) and `RawMention`/`RawRecord`/`ParserProfile` (`models.py`) are the same shapes every extraction layer in this module already builds on** — OCR-derived mentions, NER mentions, and relation mentions all become `RawMention`s and flow through the identical `mention_to_observation` call every regex mention already used, so a downstream consumer (Shreshtha's graph mapping) sees one uniform shape regardless of which extraction layer produced it.
- **`Nipun's `ObservationBatchSubmissionV1`/`TransformationProvenanceV1`/`ObservationBatchProgressV1` contracts, `submit_observation_batch`'s idempotent-replay semantics, and the graph-projection outbox are all consumed exactly as documented in Nipun's own "Producer contract for Jasraj's Phase 3 ... workers" section above** — verified line by line against this phase's actual implementation (deterministic `batch_id`/`idempotency_key` reused correctly on retry, `case_id`/`evidence_id`/`job_id` copied verbatim, transformations reference only same-batch observations, monotonically non-decreasing progress, exactly one terminal result with `observations=[]`). Zero changes to `app/contracts/observation_batch.py` or `EvidenceLifecycleService`.
- **Aditya's claim-token/worker-identity/lease-renewal boundary is consumed, never re-implemented**: `client.submit_batch`/`.renew_lease` are thin wrappers over the exact same `X-Claim-Token`-bound HTTP endpoints `client.submit_result`/`.fetch_input` already used, with identical `_raise_for_auth_failure` handling. `run_structured_batches_job`'s periodic `renew_lease` call reuses `/renew` unmodified.
- **`media_processing.analysis.tesseract_ocr`'s real, local, no-cloud OCR *engine choice* is reused, not reinvented**: `document/ocr.py` calls the same `tesseract` binary via the same `pytesseract` library, with the identical "no bundled model, `pytesseract.get_tesseract_version()`/`get_languages()` checked at construction, fail with a safe error if missing" posture — the only new code is what that module didn't need: rendering a PDF page to an image in the first place (`pypdfium2`) and a page-appropriate PSM (3, "fully automatic page segmentation") instead of `media_processing`'s PSM 11 ("sparse text") choice for an arbitrary photo frame.
- **`media_processing.bootstrap_models`'s "explicit, operator-invoked, checksum-verified, never automatic" bootstrap posture is reused for `bootstrap_ner_model.py`** — same shape (pinned URL, pinned SHA-256, verify-before-install, idempotent unless `--force`), adapted only because the NER asset is a zip archive containing a directory rather than a single file.
- **`media_processing.worker._build_analysis_components`'s graceful-degradation philosophy (a missing model degrades quality, it never crashes a job) is reused for NER**: `worker._build_ner_adapter` tries the real `SpacyNerAdapter` first, falling back to `DeterministicNerAdapter` on any `ProcessingError` — the identical "try real, fall back to a documented deterministic stand-in, never fail" shape.

## Genuinely new (this phase's own responsibility)

- `document/page_trust.py` — the four-way `PageTrustLevel` classification (`TEXT_TRUSTED`/`SCANNED_NO_TEXT`/`UNTRUSTWORTHY_TEXT_LAYER`/`CORRUPT`), replacing the pre-Phase-3 binary `has_meaningful_text` check for PDF pages specifically (DOCX/TXT's own `has_meaningful_text` use is untouched).
- `document/ocr.py` (`DocumentPageOcrEngine`) + `document/pdf.py`'s new `render_pdf_pages` (`pypdfium2` rendering, a new dependency — self-contained, no `poppler-utils` system package needed, unlike `pdftoppm`).
- `document/normalization.py` — the exact per-character offset-map normalizer (NFC, dehyphenation, whitespace collapsing) with a `to_source(start, end)` API precise enough to survive a match spanning a whitespace-collapse or dehyphenation boundary.
- `document/ner.py`/`ner_fallback.py`/`ner_spacy.py` + `bootstrap_ner_model.py` — the `NerAdapter` protocol, the deterministic gazetteer/heuristic fallback, and the real `spacy`/`en_core_web_sm` adapter (new dependencies: `spacy`, `pypdfium2`, `polars`, `pyarrow`).
- `document/relations.py` — the four rule-based relation/event functions and their shared proximity-grouping helper.
- `structured/chunked_processing.py` — `assess_schema`, `normalize_chunk` (the row-level malformed-data tolerance), and the Polars-batched-CSV/PyArrow-batched-XLSX chunked readers.
- `cdr.py`'s `resolve_timezone`/`parse_record_timestamp` (shared by `finance.py`), E.164 phone normalization (`_normalize_phone`'s new output shape, with the original value now always kept alongside via new `*_raw` attributes), and `finance.py`'s new `direction`/`currency_is_known_iso4217` normalization plus optional (non-required) timestamp handling.
- `batching.py` — `build_batch_submission`/`build_transformation`/`build_progress`/`deterministic_batch_id`/`deterministic_idempotency_key`, the shared helpers every new orchestration function uses to build a valid `ObservationBatchSubmissionV1`.
- `client.py`'s new `submit_batch`/`renew_lease` methods (`ObservationBatchReceiptV1`/`RenewAck` mirrored response dataclasses, matching the existing `SubmitResultAck`/`ClaimResult` pattern).
- `worker.py`'s `run_document_job_with_batches`/`run_structured_batches_job` (+ their private per-page/per-chunk helpers) and `_dispatch_job`, which routes `fir_report_text_v1`/`cdr_generic_v1`/`financial_transaction_generic_v1` jobs to the new batch orchestration while leaving `generic_tabular_v1`/`generic_json_v1` on the unchanged `process_job` path.
- Five new `Settings` fields (`document_ocr_language`/`_dpi`/`_min_confidence`, `ner_model_path`/`_sha256`) and two new (`structured_batch_size`, `structured_default_timezone`) — see `.env.example`.

## Why `process_job` was kept unchanged instead of rewriting it to always batch

`process_job(job, evidence, resolver) -> WorkerResultV1` is a pure, synchronous, no-HTTP function — every existing unit test, the local-file-pipeline integration test, and `generic_tabular_v1`/`generic_json_v1` (Nipun's fallback profiles, outside this phase's ownership) all call it directly and expect exactly one returned result, never a side-effecting HTTP call mid-function. Rewriting it to submit batches would require threading a `WorkerApiClient`/claim token into a function whose entire value today is that it needs neither — and would force every existing caller (including tests this phase does not own) to either supply a fake client or break. Instead, `_dispatch_job` (new, in `run_once`'s call path only, where a real client and claim token already exist) decides per-profile: `fir_report_text_v1`/`cdr_generic_v1`/`financial_transaction_generic_v1` get the new batch orchestration; everything else still gets `process_job` + a single `/result` call, exactly as every phase before this one. `process_job`'s own PDF branch (`_process_pdf`, using the pre-existing `ocr_routing.route_pdf`/`DEFERRED`-on-any-OCR-needed behavior) is therefore still reachable — by direct callers, not by `run_once` for this profile — and deliberately left as-is rather than deleted, since removing a still-correct, still-tested code path job-scoped analysis showed no caller needed removed would only produce test churn with no behavioral benefit.

## Why OCR replaces the old `DEFERRED`-on-any-scanned-page behavior with a real attempt, but `DEFERRED` is still reachable

Before this phase, `_process_pdf` returned `WorkerStatus.DEFERRED` for *any* PDF with at least one OCR-required page — a deliberate, honest "OCR doesn't exist yet" placeholder (`docs/qa/known-limitations.md`'s "No real OCR engine" line, now resolved for documents specifically). Now that real OCR exists, the new `run_document_job_with_batches` actually runs it. `DEFERRED` is kept as the outcome **only** for the genuine "OCR runtime unavailable" case (the `tesseract` binary or its language pack missing) or a page whose content stream itself is `CORRUPT` — an honest "this specific page could not be processed in this environment" signal, not a "not yet built" placeholder. The `document_requires_ocr` error code is reused for this checkpoint (not a new code) since the *shape* of the outcome — some pages processed, others not, named explicitly — is identical; only the *reason* changed.

## Why NER excludes `DATE` even though spaCy's model emits it

`document/fir_report.py`'s `date_time_mention` regex already matches DD/MM/YYYY and ISO-format dates exactly and deterministically (confidence `0.95`). Mapping spaCy's `DATE` label into `SUPPORTED_NER_LABELS` would produce a second, lower-confidence (`0.75`), statistically-recognized observation for the same underlying fact whenever both layers fire — redundant signal, not new information, and a confusing pair of "date" observations at different confidence tiers for a reader trying to understand what a single sentence actually said. Excluding it is a one-line filter (`_SPACY_LABEL_MAP` simply has no `DATE` entry) rather than a design compromise.

## Why bbox precision for OCR-derived mentions is line-level, not word-level

`DocumentPageOcrEngine.recognize_page` groups tesseract's own per-word output into per-line `OcrRegion`s — the same choice `media_processing.analysis.tesseract_ocr.TesseractTextRecognizer.recognize_regions` already made, for the same reason: a line is the more useful atomic OCR observation for investigative evidence than an isolated word, and tesseract's own layout analysis already computes the grouping reliably. A consequence, observed directly in `test_ocr_field_match_precision.py`'s real measurement: a regex match against a *substring* of a line's joined text still resolves to that *whole line's* bounding box (`locate_bbox_for_span` returns the union of every overlapping region) — exact enough to locate the claim on the page, not pixel-exact to the individual matched word. Word-level bounding boxes were not implemented: tesseract's `image_to_data` does expose per-word boxes, but grouping to word level would multiply the number of `OcrRegion`s (and therefore the transformation/observation bookkeeping) for no benefit this phase's fixtures or the graph-mapping consumer need — flagged as a possible future refinement in `docs/qa/known-limitations.md`, not a defect.

## Why the OCR/regex/NER/relations pipeline runs against *normalized* text, remapped back through an offset map, rather than against raw embedded/OCR text directly

Running FIR regex/NER against raw text would miss a label split across a line-wrap hyphen or separated by inconsistent OCR spacing — exactly the robustness gap the task brief's "supports robust matching without destroying provenance" requirement calls out. The alternative (fixing up the *source* text in place) was rejected outright: it would either silently invent characters that were never in the source (forbidden — see `CLAUDE.md`) or lose the ability to point a `SourceLocator` back at the literal original bytes. `normalization.py`'s per-character offset-map design (rather than a coarser "run"-based map) was chosen after an initial coarse-run implementation was caught producing wildly incorrect remapped spans in early testing (a normalized span crossing a whitespace-collapse boundary resolved to the *entire* enclosing coarse run instead of the precise sub-range) — the per-character array design has no such failure mode, verified directly against hand-constructed test cases with a hyphenated word and adjacent whitespace runs.

## Why CDR/finance use Polars for CSV but PyArrow-over-`openpyxl` for XLSX, not a uniform single library

Polars has a genuine native lazy/batched CSV reader (`scan_csv(...).collect_batches(...)`) — a true streaming disk read, never materializing the whole file. Neither Polars nor PyArrow has an equivalent native *chunked* XLSX reader without an additional optional engine (e.g. `fastexcel`) this project does not depend on; `openpyxl`'s `read_only=True` row iterator (already the existing, tested Phase 1 reader) is itself already a lazy, streaming reader over the underlying ZIP/XML, so it remains the disk-level reader for XLSX, with each accumulated bounded group of rows assembled into a real `pyarrow.Table` before being handed back out — genuinely vectorized batch *handling*, honestly documented as not a vectorized *read*. This asymmetry is stated plainly in `document-structured-processing.md` rather than glossed over.

## Why phone numbers now normalize to E.164, changing two pre-existing test assertions

The task brief's canonical-value section explicitly asks for "E.164 when country context is known" — India is the only country context this codebase has ever established (the existing regex is Indian-mobile-only). The pre-Phase-3 behavior (`_normalize_phone` returning the bare 10-digit form) was a Phase 1 placeholder, not a deliberate different design choice recorded anywhere. Two existing `test_cdr.py` assertions (`caller_number == "9876543210"`) were updated to expect the new E.164 form (`"+919876543210"`) as a direct, intended consequence — never silently left failing. The original value is preserved unconditionally in a new `caller_number_raw`/`callee_number_raw` attribute so no information is lost by the normalization.

## Why the CDR/finance default timezone is `Asia/Kolkata`, and why an unrecognized explicit timezone falls back to it rather than rejecting the record

Every fixture, regex pattern, and phone-number rule already built into this module assumes an Indian context (Indian mobile format, `Rs.`/`₹` currency prefixes, DD/MM/YYYY date convention) — defaulting *time* interpretation to India's own zone (`Asia/Kolkata`, UTC+05:30, no DST) is the one choice consistent with everything else already decided. An explicit but unparseable `source_timezone` value (a typo, an unsupported abbreviation) falls back to the configured default rather than rejecting the whole record — a record's timestamp is still fully recoverable and useful even if its timezone annotation was malformed, and `timestamp_source_timezone`'s recorded value (the *resolved* zone, not the raw input) makes this fallback fully visible in provenance rather than a silent guess. Contrast this with the pre-existing "reject a timestamp matching no documented format" rule, which is about the timestamp *value* itself, not its zone annotation — that remains a hard rejection.

## Open questions for team review

- Whether Shreshtha's graph-mapping work wants `ner_entity_mention`/relation observation types (`person_contact_association`, `dated_communication_reference`, `transaction_claim`, `incident_event_mention`) surfaced any differently in the graph than a regex mention — this phase deliberately made no graph-side decision, since `app/modules/graph/` needed and received zero code changes to consume them (they are ordinary `ObservationV1`s).
- Whether `document/relations.py`'s fixed `PROXIMITY_MAX_CHARS = 200` threshold should eventually be corpus-tuned rather than a documented constant — flagged, not blocking, since it produces zero false "certainty" either way (every relation observation is explicitly a proximity inference, confidence `0.60`, never asserted as fact).
- Whether `structured_batch_size` (default 500 rows) and the "renew every 5 batches" lease-heartbeat cadence are reasonable for a real, large (hundreds-of-thousands-of-row) CDR export — not empirically tuned against a real file of that scale in this sandbox; `docs/qa/test-results.md` records the actual batch counts/timings measured against the synthetic fixtures used here.
- The real `SpacyNerAdapter`'s live behavior (loading the bootstrapped model, running inference against a real document) was verified directly in this sandbox (the bootstrap script and the adapter were both exercised for real — see `docs/qa/test-results.md`); what could **not** be verified in this specific sandboxed environment is a scenario requiring an actual `pip`/`uv pip install` step, since this environment's own tooling policy blocks that class of command even for a legitimate, documented, operator-invoked bootstrap use case. This did not block verification here because the chosen bootstrap design (extract a directory from a zip archive, never install a package) needs no such step at all — flagged for team awareness, not because anything was left unverified as a result.

# Phase 3 Decisions — Sarthak Audio, Social/Chat, and Multilingual Communication Evidence Pipelines

Record of the concrete choices made extending `app/modules/communication_processing/` (Phase 1/2, this same owner) with micro-batch submission, a chat-timezone default policy, mentioned-identifier extraction, sender transliteration wiring, and a typed ASR/diarization adapter boundary — on top of Nipun's batch-ingestion contract and Aditya's worker-security boundary above. This section's own scope: everything under `app/modules/communication_processing/`. Not: batch persistence/replay semantics (Nipun's, unchanged and reused verbatim), worker credential/claim/lease/retry policy (Aditya's, unchanged and reused verbatim), graph mapping (Shreshtha's, unchanged — zero new code there), document/CDR/finance processing (Jasraj's, unchanged).

## Scope, restated

Turn approved audio and social/chat evidence into provenance-rich canonical `ObservationV1` micro-batches through the existing secure worker lifecycle, following the exact pattern Jasraj's document/CDR/finance workers (above) already established for this same batch-ingestion contract. Hard boundaries carried over unchanged from every phase of this project: no automatic identity merge, no guilt/culpability scoring, no cross-case retrieval, no graph writes from this worker, no cloud ASR/diarization/translation/LLM calls, no silent model downloads.

## Reused, not duplicated

- **`_dispatch`/`_get_profile`/`_validate_source_type` (Phase 1/2, unchanged)**: unlike Jasraj's task (where document/CDR/finance genuinely needed different processing *paths* for the new batch orchestration), `communication_processing`'s existing `_dispatch` already produces the identical `list[RawMention]` shape regardless of which of the seven profiles is active — `run_communication_job_with_batches` reuses it entirely unchanged and only adds the *submission* strategy, never a second extraction implementation.
- **`aliases/transliteration.py` (Phase 1, unchanged)**: the full Devanagari/Gurmukhi consonant+matra+virama algorithm, phone/email/URL/handle exclusion, and `TransliterationCandidate` shape are reused verbatim — this phase's only change is wiring `generate_candidates_for_tokens` into `chat_message_to_mention`, which was previously unwired (the algorithm existed and was fully tested since Phase 1, but nothing ever called it against a real observation attribute).
- **`structured_processing.structured.cdr.resolve_timezone`'s exact policy shape** (explicit signal first, then a documented `Settings`-configured default, never silently UTC or host-local) is mirrored for chat timestamps (`social/common.py::resolve_naive_or_explicit_iso`/`utc_from_naive_local`) — same default value (`Asia/Kolkata`), same reasoning (India is this codebase's only established country/timezone context), not a second competing timezone design.
- **`structured_processing.document.fir_report`'s regex-only extraction shape** (a fixed `(observation_type, pattern, match_kind)` tuple list, one `RawMention` per match, no cross-pattern overlap resolution) is mirrored for `social/identifiers.py`'s phone/email/URL/handle extraction — a second, independent implementation of the same proven shape, not an import (this module's own independently-buildable-module convention).
- **`batching.py`/`client.py`'s `submit_batch`/`renew_lease`** (added in this same phase, directly modeled on Jasraj's identical `structured_processing` additions one phase earlier in this same session) — deterministic `batch_id`/`idempotency_key` as pure functions of `(job_id, batch_sequence)`, identical error-handling shape to `submit_result`/`fetch_input`.

## Genuinely new (this phase's own responsibility)

- `batching.py` — `build_batch_submission`/`build_progress`/`build_transformation`/`deterministic_batch_id`/`deterministic_idempotency_key`.
- `client.py`'s `submit_batch`/`renew_lease` methods (+ `RenewAck` dataclass).
- `worker.py`'s `run_communication_job_with_batches`, `_observations_for` (+ its collision-disambiguating discriminator logic), `_TRANSFORMATION_STEP_NAMES`; `run_once` now calls the new function instead of `process_job` + a single `/result` call.
- `Settings.communication_batch_size` (default 200), `Settings.communication_default_timezone` (default `Asia/Kolkata`).
- `social/common.py`'s timezone-resolution helpers (`default_timezone_name`, `utc_from_naive_local`, `resolve_naive_or_explicit_iso`) and `ChatMessageRecord.timestamp_source_timezone`; corresponding fallback wiring in `whatsapp.py`/`telegram.py`/`json_records.py` (Instagram unaffected — always unambiguous via `timestamp_ms`).
- `social/identifiers.py` — `extract_mentioned_identifiers`, wired into `worker._handle_social_export`.
- `social/common.py`'s `_sender_transliteration_candidates`/`_candidate_attribute`, wired into `chat_message_to_mention`.
- `audio/asr_adapter.py`, `audio/diarization_adapter.py` — the `AsrAdapter`/`DiarizationAdapter` `Protocol`s, `UnavailableAsrAdapter`/`UnavailableDiarizationAdapter`, `SEGMENT_SOURCE_METADATA_SUPPLIED`/`SEGMENT_SOURCE_MODEL_DERIVED`; `diarization_import.py`'s new `segment_source` attribute.
- `tests/fixtures/communication_processing/{asr,diarization}_fixture_adapter.py` — explicit, test-only adapter implementations, never imported by `app/`.
- Two new `ErrorCode`s: `ASR_ADAPTER_UNAVAILABLE`, `DIARIZATION_ADAPTER_UNAVAILABLE`.
- `limits.py`'s `MAX_IDENTIFIER_TEXT_LENGTH`, `MAX_IDENTIFIER_MATCHES_PER_MESSAGE`; `provenance.py`'s `CONFIDENCE_REGEX_EXACT_MATCH`.

## Why no real local ASR or diarization model was implemented

This module's own `tests/unit/communication_processing/test_module_safety.py` — written in an earlier phase by this same module's owner, not a boundary imposed by another contributor — statically bans `numpy`, `torch`, `transformers`, `whisper`, `faster_whisper`, `pyannote`, `speechbrain`, `librosa`, and `sklearn` anywhere under this module. That list covers every practical local ASR/diarization toolkit; there is no ML-free, pure-stdlib local ASR implementation to substitute (ASR is inherently a model-inference task, unlike, say, OCR via a system `tesseract` binary, which Jasraj's phase used precisely because it doesn't require an in-process ML import). Two options were considered: (a) reverse the import ban to add a real model, or (b) keep the ban and formalize the resulting absence as a typed interface. (a) was rejected — reversing a deliberate, pre-existing architectural boundary is a decision for the whole team, not a unilateral addition inside a single task, and no lightweight/CPU-only ASR toolkit avoids the banned list entirely in any case. (b) was implemented: `audio/asr_adapter.py`/`audio/diarization_adapter.py`'s `Protocol`s plus their `Unavailable*` production adapters give the existing (correct, tested since Phase 1) `AudioRoutingDecision.DEFERRED_REQUIRES_ASR`/`DEFERRED_REQUIRES_DIARIZATION` outcome a documented shape, so a future phase can add a real adapter behind the same interface without changing `worker.py`'s dispatch contract. **Flagged for team review**: whether reversing the ML-import ban for this module specifically (distinct from, say, `media_processing`'s or `structured_processing`'s own bans) is desired in a future phase, and if so, which toolkit.

## Why MP3/M4A/AAC/FLAC audio decoding was not added

The same reasoning as above applies from a different angle: real non-WAV decoding needs either `subprocess` (this module's own `test_module_safety.py` bans it too — `ffmpeg`/`ffprobe` via a subprocess call is exactly the Gaurav/`media_processing`-module pattern used for video) or a genuine expansion of `media_processing`'s own audio-decoding capability (out of scope — "do not change another contributor's in-progress module without being asked," `CLAUDE.md`). WAV remains the only real, fully-supported audio format, unchanged since Phase 1.

## Why `_dispatch`'s uniform `list[RawMention]` output meant no per-profile-family batch orchestration was needed

Contrast with Jasraj's task, where document/CDR/finance genuinely needed distinct batch-orchestration paths (`run_document_job_with_batches` vs. `run_structured_batches_job`) because the underlying extraction logic itself differed in shape (page-by-page OCR/NER vs. row-chunked Polars/PyArrow reads). `communication_processing`'s seven profiles, by contrast, already converged on one uniform `list[RawMention]` return type from `_dispatch` before this phase started — so `run_communication_job_with_batches` needed exactly one orchestration function, not one per profile family, chunking whatever `_dispatch` returns regardless of which profile produced it.

## Why mentioned-identifier mentions reuse their parent message's exact locator, and how the resulting ID collision is resolved

An extracted phone number/email/URL/handle's most accurate source location *is* the message it was found in — there is no more precise, independently-meaningful coordinate inside one message's text to anchor a locator to without either (a) claiming false precision (a byte offset relative to the message's own extracted text, which for WhatsApp specifically would use a *different* coordinate space than the parent message's own file-relative `span_start`/`span_end`, since the sender-prefix-stripped, continuation-joined `ChatMessageRecord.text` is not byte-identical to the raw file slice the parent's locator points at), or (b) inventing a message-level "coordinate system" duplicated across every platform's parser just for this one feature. Reusing the parent's exact, already-correct locator was judged simpler and more honest. This does mean two matches of the same observation type within one message (e.g. two phone numbers) share `(observation_type, locator)` — which `provenance.observation_id` treats as one identity. Rather than change `RawMention`'s shape (a wider-blast-radius change touching every profile) or `provenance.py`'s ID derivation (a shared, frozen-contract-adjacent module), the fix lives entirely in `worker._observations_for`: a per-call ordinal counter keyed by `(observation_type, locator)` supplies `mention_to_observation`'s existing (previously always-default) `discriminator` parameter only for the second and later occurrence of a collision — the first occurrence of any `(observation_type, locator)` pair, including every mention from every pre-existing profile that never collided before this phase, gets `discriminator=""`, producing byte-identical `observation_id`s to before this change.

## Why a multi-word sender name is split into per-word transliteration candidates, never joined into one synthesized "full name" candidate

`aliases/transliteration.py::generate_candidates`'s consonant+matra+virama algorithm operates on one token at a time by design (Phase 1) — a bare space character is not itself a mappable character, so running an unmodified multi-word name like "राहुल शर्मा" through it directly always returns `not_generated` for the *whole* string, discarding real transliteration information for both words. `generate_candidates_for_tokens` (the existing, already-tested batch wrapper, previously unused by anything in this module) is the correct granularity to call at. A second design was considered — joining each word's chosen candidate with a space to produce one convenient "full name" string — and rejected: that would be a new derived value this module's own tested contract doesn't cover, and section 7.2's own conservative framing ("candidates... never an interchangeable identity") argues against synthesizing a value nothing in the existing algorithm actually validated end-to-end. The per-word list is stored as-is, in original order.

## Why the default chat timezone changes previously-`None` `timestamp_utc` values for WhatsApp/naive-Telegram/naive-generic-JSON fixtures

Before this phase, a naive chat timestamp with no explicit signal left `timestamp_utc=None` permanently — for WhatsApp specifically, this meant *every* WhatsApp message ever processed had `timestamp_utc=None`, since WhatsApp's plain-text export format carries no timezone information at all by construction. The brief explicitly asks for a documented default (mirroring Jasraj's CDR/finance precedent, itself already established and shipped in this same repository), and leaving a genuinely common, real-world case permanently unresolved when a reasonable documented default exists was judged the wrong tradeoff — consistent with why Jasraj's CDR module made the identical choice for the identical reason (India is this codebase's only established timezone context). Existing tests asserting `timestamp_utc is None` for a naive WhatsApp/generic-JSON timestamp were updated to assert the new, default-zone-derived UTC value instead, mirroring exactly how Jasraj's CDR E.164-normalization test updates were handled one phase earlier in this same session.

## Open questions for team review

- Whether reversing the ML-import ban in `test_module_safety.py` for a future phase (to add a real local ASR/diarization model) is desired, and if so, which specific toolkit — see "Why no real local ASR or diarization model was implemented" above. Not treated as a blocker: the existing `DEFERRED_REQUIRES_ASR`/`DEFERRED_REQUIRES_DIARIZATION` outcome is honest and has been the correct, documented behavior since Phase 1.
- Whether `username_or_handle` extraction should eventually gain platform-specific validation (a bare `@name` token is currently accepted regardless of whether it's a plausible handle on any real platform) — flagged, not blocking, since every such extraction is already labeled an unresolved extracted claim, never a verified identity.
- Whether `MAX_IDENTIFIER_MATCHES_PER_MESSAGE` (100 per observation type per message) is a reasonable ceiling for a real large chat export — not empirically tuned against a real corpus of that scale in this sandbox.
- Whether `COMMUNICATION_BATCH_SIZE`'s default (200) and the "renew every 5 batches" lease-heartbeat cadence (identical to Jasraj's `structured_batch_size`/renewal cadence) are reasonable for a real, large chat export or long transcript — not empirically tuned against a real file of that scale in this sandbox.
- Whether a future phase should add attachment-reference support (a new `ChatMessageRecord` field, per-platform detection, a new observation type/attribute) — the task brief's approved observation-type list for social/chat evidence includes it, but no parser currently reads or models any attachment-related field. Judged out of scope this phase alongside the other deliberately-deferred additions (real ASR/diarization, non-WAV audio) rather than implemented as a new capability under time pressure — see `docs/qa/known-limitations.md`.

# Phase 3 Decisions — Gaurav Shared Raster-Image OCR Bounding-Box Adapter and Fixtures

Record of the choices made while implementing the OCR bounding-box adapter, precise raster-image/frame provenance, and integration with the secure worker + ObservationV1 micro-batch flow.

## Scope

This implements Gaurav's Phase 3 ownership: precisely locating text within standalone images and video frames, extracting it via Tesseract, normalizing the bounding boxes to the required `[0, 1]` scale, building correct `ObservationV1` and `TransformationProvenanceV1` records, and batching them through the existing worker infrastructure. 

## Reused, not duplicated

- **Worker pipeline**: the existing `worker.py::run_once` owns claim, claim-token-bound input fetch, SHA-256 verification, lease heartbeat, batch submission, and terminal status. There is no `run_once_with_ocr_batches` function.
- **Batching infrastructure**: `ocr_batching.py` reuses Nipun's existing `ObservationBatchSubmissionV1`, `TransformationProvenanceV1`, and `ObservationBatchProgressV1` models and limits.
- **Client**: `client.submit_batch` (built by Nipun/Jasraj) is reused verbatim to submit the `ObservationBatchSubmissionV1` batches.
- **OCR Engine**: Tesseract (via `pytesseract`) is reused, identical to Phase 2, but wrapped in a new `ImageOcrAdapter` that preserves bounding boxes.
- **Fixtures**: The synthetic image generator renders labelled high-contrast PNG/JPEG text. `FixtureOcrAdapter` is explicitly fixture-only; genuine adapter and live tests construct `ImageOcrAdapter` and self-skip with the precise local-runtime reason if Tesseract or a readable local font is unavailable.

## New implementations

- **`ocr_adapter.py`**: Introduced `ImageOcrAdapter` to run local Tesseract and emit ordered line-level bounding boxes with text, OCR-quality confidence, source dimensions, engine/version, language/configuration hash, and preprocessing version.
- **`FixtureOcrAdapter`**: A pure-Python fallback for testing that generates fake bounding boxes, ensuring CI and tests don't fail if Tesseract is missing.
- **EXIF Orientation Handling**: Bounding boxes are inverted/rotated back to the *original* unrotated coordinate space before being normalized, ensuring `ObservationV1` locators are always relative to the original bytes, not the arbitrarily rotated pixel buffer.
- **`ocr_batching.py`**: `iter_image_ocr_batches` and `build_video_frame_ocr_batch` create bounded OCR micro-batches and safe `TransformationProvenanceV1` records. `build_media_observation_batch` moves existing metadata/detection/tracking observations through the secure batch endpoint, leaving every successful terminal result with `observations=[]`.

## Bounding Box Normalization

`worker.py` explicitly drops bounding boxes that fall outside the `[0, 1]` range instead of clamping them, adhering to the strict "never fabricate or silently fix" rule. The coordinates are mapped back to the original image dimensions.

## Safe Metadata

`TransformationProvenanceV1.safe_metadata` is populated with `ocr_engine`, `ocr_engine_version`, `ocr_language`, `preprocessing_version`, `source_width`, and `source_height` (and `frame_time_start_ms` for video). This explicitly omits raw OCR text or sensitive keys, passing the contract's `_validate_safe_metadata` checks.

## Batch order and terminal behavior

Batch sequences are job-global and zero-based. Image OCR chunks occupy the first sequence values; the resulting metadata/detection/tracking observations are submitted in a final media-observation batch. Video frames are processed incrementally without buffering them: each non-empty frame OCR batch is submitted in order, then a final empty video-frame progress batch marks the last sampled frame after extraction completes. This preserves memory bounds and gives the final video OCR batch `is_final_batch=true`. A batch-submission transport/API failure is converted into one safe, retryable terminal failure (`media_batch_submission_failed`) if the terminal endpoint remains reachable; no raw HTTP body, URI, token, or OCR payload is recorded.

## Live verification now covers a real video frame, not only a real image

`test_video_frame_ocr_batch_submission_end_to_end_live` (plus its `_real_ocr_video_fixture`/`make_text_video_bytes` helpers) proves the same real-Tesseract-through-the-adapter path for a genuine sampled video frame, submitted through the batch endpoint, with a full graph-outbox/projector-idempotency/no-leaked-secrets check — mirroring `test_ocr_batch_submission_end_to_end_image_live`'s image-path coverage exactly, closing the gap where only the image path had a dedicated live OCR test. `make_text_video_bytes` builds the labelled clip by looping one `make_text_png_bytes` frame through `ffmpeg` (`-loop 1 -i frame.png`) rather than `ffmpeg`'s `drawtext` filter, which needs a fontconfig/freetype build not guaranteed present — this reuses the exact rendering already proven correct for the image-path tests instead of a second, independent text-rendering mechanism. `FakeObjectDetector` (not the real bootstrapped model) is used to enter the existing video-analysis sampling path deterministically in every environment; this test proves the OCR/batch/outbox behavior for a video frame, not detection accuracy, which the pre-existing pipeline test already covers separately.

The fixture and the adapter's video-frame path were independently confirmed correct (real `ffprobe`/frame-extraction, real Tesseract recognizing the labelled text on every sampled frame) — see `docs/qa/test-results.md`'s "Phase 3 closeout" entry. Whether this specific live test (and the pre-existing image one) actually passes end to end against a real running API/PostgreSQL/Neo4j/MinIO stack was not verified in the sandbox this was built in, because the local Docker daemon itself was unavailable there — this is reported honestly as "not run", never fabricated as a passing live result.
