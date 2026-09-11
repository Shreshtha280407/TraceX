# Worker Job Claim and Result Submission (Phase 2.1 — Nipun)

Phase 2.1 extends `app/modules/evidence_lifecycle/` with the second half of the durable job foundation Phase 2 started: a safe, durable path for a future modality worker to **claim** one queued job and **submit** its result, without ever holding PostgreSQL, Neo4j, or MinIO credentials. This is still a foundation, not a worker: no worker daemon, consumer loop, or actual extraction logic exists anywhere in this repository.

## The full flow

```
evidence upload
  -> durable WorkerJobV1 (Phase 2)
  -> worker claim + temporary claim token (Phase 2.1)
  -> worker processing (outside this service, not implemented here)
  -> WorkerResultV1 validation (Phase 2.1)
  -> atomic result + ObservationV1 persistence (Phase 2.1)
  -> later: reviewed observation -> graph projection (a later phase, not implemented here)
```

## PostgreSQL is the durable queue; Redis is only a hint

`worker_jobs` (Phase 2) plus the two new tables this phase adds -- `worker_results` and `worker_observations` -- are the complete source of truth for "does this job exist, what state is it in, and what did it produce." A future worker orchestrator (not built in this phase) could poll `worker_jobs` directly and never touch Redis at all, and the system would behave identically. The Redis `RPUSH` Phase 2 already does is, and remains, a wake-up hint only -- a worker that never sees the Redis message can still discover and claim the same job by calling `/claim` and querying `worker_jobs` (which is exactly the eligibility query `claim_job` already runs). Losing the Redis message never loses the job.

## Claiming: `FOR UPDATE SKIP LOCKED`

`EvidenceLifecycleRepository.claim_job` runs, in one transaction:

```sql
SELECT * FROM worker_jobs
WHERE processor_name = :name AND processor_version = :version
  AND (status = 'queued' OR (status = 'running' AND lease_expires_at < :now))
ORDER BY requested_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED
```

`FOR UPDATE SKIP LOCKED` is what makes this concurrency-safe without an application-level lock: two workers claiming at the same instant each run this query in their own transaction; whichever commits first holds the row lock, and the second transaction's `SELECT` simply skips that locked row and finds a different eligible job (or none) instead of blocking or double-claiming. Verified against a real PostgreSQL instance in `tests/integration/evidence_lifecycle/test_worker_lifecycle_live.py`, not just asserted.

The eligibility condition is the whole lease/retry policy: a `queued` job has never been claimed; a `running` job whose `lease_expires_at` has passed was claimed once but never resolved (crash, network partition, a worker that never called `/result`) and is safely reclaimable. A `running` job whose lease has **not** expired, and every terminal job (`succeeded`/`failed`/`deferred`/`cancelled`), is never eligible -- so a cancelled or otherwise-terminal job is never automatically retried, and two workers can never simultaneously believe they own the same job.

## Attempt semantics

`WorkerJobV1.attempt` (frozen, `ge=1`) starts at `1` when the job is created (Phase 2, unchanged). The **first** claim of a job does not increment it -- that first claim *is* attempt 1. Only a **reclaim** (an expired lease recovered by the eligibility query's second branch) increments `attempt`. This is why a job's attempt count is a true count of "how many times has a worker actually held this job," not "how many times has anyone tried to read it."

## Claim tokens: one-time, hashed at rest, never in the frozen contract

Every successful claim generates a fresh 256-bit token (`secrets.token_urlsafe(32)`, the exact same primitive and reasoning as `access_control.tokens.generate_refresh_token`) and returns it to the caller **once**, in the claim response body. Only its SHA-256 hex digest (`worker_jobs.claim_token_hash`) is ever persisted -- the same "hash a high-entropy secret with a fast hash, not a slow KDF" reasoning `access_control.tokens.hash_refresh_token` already documents (there is no dictionary-attack surface against a 256-bit random value, so Argon2id-style slow hashing buys nothing here). `claim_token` is **not** part of `WorkerJobV1` or any other frozen contract; it is pure transport metadata, carried in the internal API's response body (on claim) and an `X-Claim-Token` request header (on submission), exactly like the existing `Idempotency-Key` header convention.

Presenting the correct claim token for a given `job_id` is itself the proof of "I am the party that legitimately claimed this specific job" -- there is no separate per-worker identity check on submission beyond this (see "Worker identity" below for why). A token that doesn't hash-match the job's stored `claim_token_hash` (wrong job, or a job whose lease has since been reclaimed by someone else, overwriting the hash) is rejected exactly like an expired one -- both collapse to the same generic `InvalidClaimTokenError` / `401`, the same default-deny philosophy `access_control.errors.AuthenticationError` already applies to login. A lease that has expired is rejected on submission even if nobody has reclaimed it yet -- once your lease window has passed, you do not get a late pass; a future phase could add lease renewal if this proves too strict in practice.

## Worker identity: a narrow, temporary boundary, not a real credential system

There is no per-worker credential-issuance system anywhere in this codebase, and building one is explicitly out of scope for this task (it belongs to Aditya's access-control module). `require_worker_principal` (`app/modules/evidence_lifecycle/dependencies.py`) is a small, fail-closed stand-in:

- `Settings.worker_shared_secret` (`WORKER_SHARED_SECRET`, optional, no default): if unset, **every** call to `/api/v1/internal/worker-jobs/*` is rejected with `503` -- there is no "unauthenticated is fine" fallback, ever. This is the "fails closed outside tests" requirement made concrete.
- If set, a caller must present `Authorization: Bearer <secret>`, compared with `hmac.compare_digest` (constant-time), or is rejected `401`.
- The resulting `WorkerPrincipal` carries **no per-worker identity** -- it only proves "this caller passed the shared boundary," not "this caller is worker instance X, authorized for processor Y." The `processor_name` a worker declares in its `/claim` request body is self-declared, non-secret, and stored as `worker_jobs.claimed_by` purely as a non-secret identifier for observability (the "record a non-secret worker identifier ... where existing security architecture supports it" requirement) -- it is not cryptographically verified against any per-worker credential, because no such credential exists yet.

**This is the Aditya handoff**: a real system would issue each worker a distinct credential scoped to the specific `processor_name`(s) it is authorized to run, verified independently of the shared secret above. Until that exists, the shared secret is the entire trust boundary between "an internal worker" and "an unauthenticated caller" -- adequate to prevent the internal endpoints from being wide open, not adequate as a multi-tenant worker-fleet authorization system. Flagged in `docs/qa/known-limitations.md`.

## Result validation, in order

`EvidenceLifecycleService.submit_result` validates, before ever writing anything:

1. **Job exists** (`get_job_by_id`, unscoped by case -- see below) -- else `InvalidClaimTokenError`.
2. **Already terminal?** -- short-circuits to the idempotency/conflict comparison (next section) *before* touching claim-token/lease checks at all, since a completed job's lease is no longer meaningful.
3. **Claimed and lease still valid**: `status == running`, `claim_token_hash` set, the submitted token's hash matches, and `lease_expires_at >= now` -- else `InvalidClaimTokenError` (uniformly, regardless of which specific check failed).
4. **Scope match**: `result.job_id`/`case_id`/`evidence_id` all equal the claimed job's own values, and every `result.observations[i].case_id`/`evidence_id` matches too -- else `ResultValidationError`.
5. **Terminal status**: `result.status` must be one of `succeeded`/`failed`/`deferred`/`cancelled` -- `queued`/`running` are never accepted as a final submission -- else `ResultValidationError`.

Only after all five pass does anything get written.

## Why a `deferred` or `cancelled` result is also terminal in this phase

The required status diagram is `queued -> running -> {succeeded, failed, deferred, cancelled}` -- all four are modeled as equally terminal outcomes a worker can submit, not just `succeeded`/`failed`. There is no automatic redrive of a `deferred` job in this phase (matching how `structured_processing`'s own `DEFERRED` results, e.g. "this PDF needs OCR," are never automatically retried by anything in this repository today) -- a `deferred` or `cancelled` job simply stops being claimable, exactly like a completed one, until a later phase adds an explicit redrive/requeue mechanism. This keeps the scope to "safe claim/result primitives," not a full retry-policy engine.

## Idempotent, safe result submission

`worker_results.job_id` is unique -- at most one result row ever exists per job. This single constraint is what makes duplicate submission safe:

- **Exact same canonical payload, already-terminal job**: `payload_hash` (SHA-256 of `app.core.canonical.canonical_bytes(result)`, the same canonical-serialization helper Phase 1 built for this exact purpose) matches the stored row's hash -> return the original accepted outcome (`200`), no new write, no duplicate observations, ever.
- **Different payload, already-terminal job**: hash differs -> `ResultConflictError` / `409`.
- **True concurrent race** (two submissions for the same job land inside their own transactions at nearly the same instant): the loser's `INSERT INTO worker_results` violates the unique constraint and raises `sqlalchemy.exc.IntegrityError` -- caught, then handled by re-reading the row that actually won and running the same hash-comparison logic above. This is the identical pattern `create_evidence_with_job`'s `Idempotency-Key` race already established in Phase 2; `submit_result` reuses it rather than inventing a second technique.

Result + every observation + the job's terminal status update happen in **one transaction** (`EvidenceLifecycleRepository.submit_result`). A failed transaction (simulated in `tests/unit/evidence_lifecycle/test_worker_result.py::test_failed_persistence_leaves_job_non_terminal`) leaves the job `running`, leaves zero result/observation rows, and re-raises to the safe generic-`500` handler -- there is no path that marks a job `succeeded` without its observations actually having committed.

## Why the internal endpoints look up jobs by `job_id` alone, not `case_id` + `job_id`

The case-scoped user-facing endpoints (`/api/v1/cases/{case_id}/...`) authorize by case membership; the internal worker endpoints authorize by claim-token possession instead. A worker legitimately does not know (and has no need to know) a human case-membership structure -- it knows a `job_id` and a `claim_token`, both handed to it by `/claim`. `EvidenceLifecycleRepository.get_job_by_id` is a deliberate, narrow addition for exactly this: it is never reachable from the case-scoped router, only from `internal_api.py`, which is itself gated by `require_worker_principal`.

## Safe responses

- **Claim** (`ClaimResponse`): the full `WorkerJobV1` (a worker needs `input_object_uri` -- the whole point of the job), plus `claim_token`/`lease_expires_at` as transport-only sibling fields. No work available is `{"job": null, "claim_token": null, "lease_expires_at": null}` with `200`, never an error.
- **Submit** (`ResultAcknowledgement`): `job_id`, final `status`, `result_id`, `observation_count`, `observation_ids` -- never an object URI, a credential, or the claim token.
- **User-facing job status** (`JobView`, extended this phase): now also carries `claimed_at`, `completed_at` (from the associated result, once one exists), and `observation_count` -- still never `claim_token`/`claim_token_hash`/`object_uri`. Verified directly in `tests/unit/evidence_lifecycle/test_evidence_api.py::test_completed_job_status_exposes_safe_fields_only`.

## Audit logging

A successfully **accepted** result submission (first acceptance only, not an idempotent replay) is recorded via the existing `app.modules.access_control.audit.record_audit_event` -- the same facility Phase 2's upload endpoint already uses, with `user_id=None` (there is no human user behind a worker call) and `case_id` from the submitted result. **Denied** worker actions (bad claim token, scope mismatch, conflict) are **not** audit-logged in this phase, mirroring the exact same pre-existing gap Phase 2 already documented for case-scoped user endpoints (`require_case_action` doesn't audit denials either) -- not a new gap this task introduces, and not fixed here since it belongs to Aditya's module. See `docs/qa/known-limitations.md`.

## Lease and retry policy, stated plainly

- **Lease duration**: `Settings.worker_lease_seconds` (`WORKER_LEASE_SECONDS`, default 300s / 5 minutes), set once at claim time (`lease_expires_at = now + lease_seconds`), not renewable in this phase.
- **Retry eligibility**: a `running` job becomes reclaimable the instant its lease expires; there is no cooldown, no backoff, no jitter.
- **Max-attempt behavior**: **none implemented**. `attempt` increments without bound on every reclaim; nothing in this phase transitions a job to `failed` automatically after N attempts. A later phase should decide that policy (and would naturally do so in the same eligibility query, e.g. adding `AND attempt < :max_attempts`).
- **What remains deferred**: an actual worker daemon/consumer loop; automatic redrive of `deferred`/`cancelled` jobs; lease renewal (a long-running worker extending its own lease mid-processing); a real per-worker credential system (see "Worker identity" above); max-attempt cutoff.

## Worker evidence delivery (Phase 2.2 addendum)

A third internal endpoint, `GET /api/v1/internal/worker-jobs/{job_id}/input`, was added after this document was first written — see `docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery" section for its full shape and `docs/architecture/phase-2-decisions.md`'s "Phase 2.2 Decisions" for the reasoning. It authenticates identically to `/result` (`require_worker_principal` + the same per-job `X-Claim-Token`), and only ever succeeds while the job is `running` with an unexpired lease — no new claim-token semantics were introduced, this endpoint reuses the ones documented above exactly.

## Non-goals (unchanged from Phase 2, still true)

No document/OCR/CDR/finance/video/image/audio/social-chat extraction, no entity resolution, no graph projection, no correlation/scoring/hypotheses, no human review workflow, no Merkle roots/signatures, no new queue platform, no frontend. This phase adds exactly two capabilities to the existing evidence-lifecycle foundation: claim, and submit-result. Nothing calls either from a real worker process anywhere in this repository.
